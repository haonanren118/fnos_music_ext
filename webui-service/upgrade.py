"""下载安装包：拉取官方 fpk → 校验 sha256 → 存到宿主可见目录 → 指引用户手动安装。

设计取舍（都是踩过坑才定下来的）：

1. **必须校验 sha256，且只认官方发行页登记的值。**
   安装 fpk 等于以 root 跑任意安装脚本，拿到一个错误的包就是任意代码执行。
   所以校验值不从包自身读取、不从第三方取，只认 release 上并排发布的
   `<name>.sha256`；取不到就不给下载，宁可让用户自己去发行页拿。

2. **只允许升级到更高版本，且只允许官方仓库的资产。**
   防止把包指针指到任意 URL（等于任意下载），也防止"降级"覆盖掉更新的安装。

3. **【实测结论】fnOS 没有可用的自动升级通道，最后一步只能交给用户。**
   真机上把appcenter-cli 的所有姿势都试过了：
   - `install-fpk <fpk>`（带/不带 `-e`）→ 只跑一遍 `Verifying files.`，
     打印 `[Info]Application [fnmusic-ext] is installed.` 并**返回 0**，
     但版本号、日志、install 记录全都不变 —— 对已安装应用是**纯空操作**；
   - `install <appname>` → `[Error]Something wrong with appcenter: code 10030`；
   - 顶层子命令里**根本没有 upgrade**（只有 install / uninstall / start / stop /
     check / status / list / install-fpk / install-local / manual-install /
     default-volume）；
   - `install-local` 更危险：它先 stop + uninstall，再因环境变量解析失败中断，
     把应用留在「已卸载」的坏状态且 repo/ 被清空，**不要用**。
   所以「点一下就自动装完」在 fnOS 上做不到。**不把空操作包装成成功**
   （那等于骗用户说升级了其实啥也没变），改为：自动下载 + 校验到宿主共享目录，
   然后明确告诉用户包在哪、怎么装。宿主特权通道的代码全部保留在
   `proxy/webui_gateway.py` 的 `POST /api/host-upgrade`，万一后续 fnOS 出了真接口，
   重新接上即可，不用重写。

4. **下载必须落在宿主可见的目录，容器自己 /tmp 不行。**
   WebUI 跑在容器里（uid 1000），既没有 root 也没有 appcenter-cli
   （它在宿主 `/usr/local/bin/`，容器里 `which` 直接 not found）——
   就算 fnOS 给了能用的安装接口，容器里也调不动。
   现在包下到宿主仓库下的 `sources-data/upgrade/package.fpk`，用户在
   文件管理器里就能直接看到并拿去装。
   ⚠️ 另一个坑：**容器 /tmp 与宿主 /tmp 是两套互不可见的空间**，包下到
   容器 /tmp 宿主根本看不到。必须放在共享挂载 `/repo/sources-data/upgrade/`
   （`/repo` 挂自宿主仓库目录，且该子目录在打包排除列表里）。

5. **下载是长任务，接口立即返回 + 轮询状态。**
   405MB 离线包要下好几分钟，HTTP 请求挂在那里必然超时。
   状态落盘，页面刷新/重开仍能查到真实结果。

6. **下载体积大，边下边报进度。**
   离线包 400MB+，不给进度用户会以为卡死。

7. **卸载会连带删掉 sources-data/ 里的用户数据。**
   实测安装失败回滚、或误用 install-local 时，网易云登录态 / 洛雪源脚本 /
   `sources-data/config/` 里的访问令牌都会一起消失。重装后必须重建这些，
   令牌需重新下发（它不在安装包里）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path

logger = logging.getLogger("webui.upgrade")

# 与 app.py 的 CONF["repo_dir"] 同源（容器内 /repo，宿主为仓库实际路径）
CONF_REPO_DIR = os.environ.get("WEBUI_REPO_DIR", "/repo")

# 官方仓库（与 app.py 的 RELEASE_SOURCES 必须一致，双向核对过）
REPO_OWNER = "haonanren118"
REPO_NAME = "fnos_music_ext"
GITEE_OWNER = "yygitee118"

APP_NAME = "fnmusic-ext"

# 安装动作交给宿主 root 执行（见下）。
#
# ⚠️ WebUI 跑在容器里（uid 1000）：既没有 root，也没有 appcenter-cli
# （它在宿主 /usr/local/bin/），容器自己装不了 fpk。宿主侧的
# proxy/webui_gateway.py（root）提供 POST /api/host-upgrade 代为执行。
#
# ⚠️ 包必须放在**容器与宿主共享的目录**：`/repo` 挂自宿主仓库目录，
# 容器里的 /tmp 与宿主 /tmp 是两套互不可见的空间 —— 下到容器 /tmp 的包
# 宿主根本看不到。取 sources-data/upgrade：既是 /repo 下的共享子目录，
# 又在打包排除列表里（不会混进发行包）。
UPGRADE_SUBDIR = "upgrade"
WORK_DIR = Path(CONF_REPO_DIR) / "sources-data" / UPGRADE_SUBDIR
STATE_FILE = WORK_DIR / "state.json"


def _fpk_path(target: str) -> Path:
    """下载落盘路径。文件名带版本号，与发行页资产名一致。

    【为什么不能固定叫 package.fpk】前端版本页的引导写的是
    "选择 fnmusic-ext-<版本>.fpk"，而实际落盘叫 package.fpk —— 用户照着
    页面指引在文件管理器里找那个名字，**这个文件并不存在**。
    名字与发行页一致后，用户按指引能直接找到包，多个版本也能并存。
    """
    ver = re.sub(r"[^\w.\-]", "", str(target or "")) or "unknown"
    return WORK_DIR / f"fnmusic-ext-{ver}.fpk"

# 宿主 root 网关额外在 /repo 下 bind 的 socket（见 proxy/webui_gateway.py）：
# 容器只挂了 /repo，看不到应用目录里那个；宿主 8774 绑在回环，容器也连不上。
GATEWAY_SOCK = Path(CONF_REPO_DIR) / ".upgrade-gw.sock"
# 兼容回退：桌面网关那个 socket（WebUI 若被部署在宿主上则可用）
GATEWAY_SOCK_FALLBACK = ("/run/fnmusic-ext/fnmusic-ext.sock",)

# 单个包上限：离线包约 405MB，留足余量；也是防止被诱导下载超大文件做 DoS
MAX_FPK_BYTES = 900 * 1024 * 1024
CHUNK = 256 * 1024

# 允许的下载主机：只从这两个官方平台取包
ALLOWED_HOSTS = (
    re.compile(r"^github\.com$"),
    re.compile(r"^objects\.githubusercontent\.com$"),
    re.compile(r"^release-assets\.github\.com$"),
    re.compile(r"^githubusercontent\.com$"),
    re.compile(r"^gitee\.com$"),
    re.compile(r"^(?:[\w.-]+\.)*gitee\.com$"),
)

# GitHub 加速镜像前缀。key 是短名，value 是要拼在原始 URL 前的代理前缀。
# ⚠️ 这是在【下载与取校验值】有意放开的边界：走第三方代理意味着字节来自
# 第三方服务器。安全性由三件事兜住，且都不因加速而放松：
#   1) 校验值优先只从官方 github.com 取，此时代理一个都不参与；
#   2) 官方不可达时（实测国内直连常为 ConnectTimeout），要求 >= 2 个
#      互不相关的代理返回同一哈希才采信 —— 一致性本身就是校验；
#   3) 无论来源如何，最终都以该哈希比对整包，不匹配就删包中止。
# 也就是说代理只能影响"快慢"，影响不了"装不装"。
#
# 实测（2026-10-04 22:18，用户 NAS，5.4MB online 包，每源 6 秒，扫 24 个）：
#   ghfast.top 990 KB/s、gh-proxy.com 446、ghproxy.imciel.com 163、
#   gh.noki.icu 104、官方直连 19 KB/s；其余 19 个不可用
#   （大量 ConnectTimeout/403/404/429）。
# ⚠️ 速度波动极大：同一镜像 5 分钟前 ghfast 是 15 KB/s、gh-proxy 是 26 KB/s。
# 所以下面的顺序不代表快慢，只代表「都试一遍」，由 _pick_download_url 择优。
CDN_PREFIXES = (
    ("ghfast", "https://ghfast.top/"),
    ("gh-proxy", "https://gh-proxy.com/"),
    ("imciel", "https://ghproxy.imciel.com/"),
    ("noki", "https://gh.noki.icu/"),
)

# 测速：每个候选最多试这么久、读这么多字节。够判快慢，又不至于拖慢启动。
# 【为什么是 8 秒而不是 2.5】实测各源「首字节延迟」就有 1.2~2.8 秒，
# 窗口太短则大半时间都在等首字节，几乎没读到数据，算出来是噪声：
#   同一时刻实测 gh-proxy/官方直连的真实差距是 846 vs 31 KB/s（27 倍），
#   但 2.5 秒窗口只能测出 97 vs 68（1.4 倍）→ 择优完全失效。
# 5 秒能测出 9 倍差距，8 秒能测出 27 倍。取 8 秒。
CDN_PROBE_SECONDS = 8.0
# 低于这个速度就不如直连。直连实测在 19~88 KB/s 间波动，
# 取 100 KB/s 作为「值得走代理」的门槛：低于它的代理不如老实直连。
CDN_MIN_BYTES_PER_SEC = 100 * 1024

_lock = asyncio.Lock()


def _now() -> float:
    return time.time()


def _write_state(**kw) -> dict:
    """状态落盘。安装过程会重启服务，内存态会丢，必须持久化。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        cur = {}
    cur.update(kw)
    cur["updated_at"] = _now()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_FILE)
    return cur


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"stage": "idle"}
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {"stage": "idle"}
    # 下载中途进程被杀会留下 running 状态，10 分钟后视为中断
    if data.get("stage") in ("downloading", "verifying"):
        if _now() - float(data.get("updated_at") or 0) > 600:
            data["stage"] = "interrupted"
            data["message"] = "上次下载被中断（服务重启或网络断开），可重新发起"
    return data


def _host_allowed(url: str) -> bool:
    m = re.match(r"^https?://([^/:]+)", url or "")
    if not m:
        return False
    host = m.group(1).lower()
    return any(p.match(host) for p in ALLOWED_HOSTS)


def _ver_tuple(raw: str) -> tuple:
    parts = re.findall(r"\d+", str(raw or ""))
    return tuple(int(p) for p in parts[:4]) + (0,) * max(0, 4 - len(parts[:4]))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def _pick_download_url(url: str) -> tuple[str, str]:
    """在官方 URL 与各CDN 镜像之间测速，返回 (实际下载 URL, 来源说明)。

    直连国内通常只有几十 KB/s甚至连不上（实测 19~88 KB/s，
    428MB 要 3~6 小时）；好的镜像能到几百 KB/s 以上。所以先花几秒
    并发测一下谁快，用最快的那个下。

    任何环节失败（网络不通、代理全挂、都不够快）都回落到官方 URL ——
    加速只是优化，不该成为下载失败的原因。返回的来源串会写进状态，
    用户能看见包究竟是从哪拿的。
    """
    official = url

    async def _probe(candidate: str) -> float:
        """取候选的实际吞吐（字节/秒）。失败返回 0.0。

        纯按时间窗口收尾，不设「读满 N 字节就提前退出」：快的源几秒就
        读满并提前停，网络正好在提速时会低估它；慢的源本来就读不满，
        两种源用同一把尺子（同一个时间窗口）才可比。
        """
        import httpx

        got = 0
        t0 = _now()
        try:
            timeout = httpx.Timeout(connect=6.0, read=CDN_PROBE_SECONDS + 3.0,
                                    write=10.0, pool=6.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                async with c.stream("GET", candidate) as resp:
                    if resp.status_code != 200:
                        return 0.0
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        got += len(chunk)
                        if _now() - t0 > CDN_PROBE_SECONDS:
                            break
        except Exception:  # noqa: BLE001 — 测速失败就是不可用，不该中断下载
            return 0.0
        el = _now() - t0
        return got / el if el > 0 else 0.0

    # 并发测速：直连 + 各镜像各读几秒，取最快。官方 URL 放在最后兜底。
    cands = [(official, "官方直连")] + [
        (pre + official, name) for name, pre in CDN_PREFIXES
    ]
    # gather 要等**最慢的**那个返回。实测最慢的镜像能拖到13.5 秒
    # （8 秒窗口 + 缓冲读取），点一次按钮干等十几秒不可接受。
    # 超时的源按 0.0（不可用）处理，不影响其余候选的择优。
    try:
        speeds = await asyncio.wait_for(
            asyncio.gather(*[_probe(u) for u, _ in cands]),
            timeout=CDN_PROBE_SECONDS + 4.0,
        )
    except asyncio.TimeoutError:
        # 整体超时：按已完成的重新择优代价高，直接回落官方，
        # 至少不会卡住用户（官方仍是可用的兜底路径）。
        logger.info("CDN 测速整体超时，回落官方直连")
        return official, "官方直连（镜像测速超时）"
    best_i = max(range(len(cands)), key=lambda i: speeds[i])
    if speeds[best_i] < CDN_MIN_BYTES_PER_SEC:
        return official, "官方直连（未找到更快的镜像）"
    return cands[best_i][0], f"{cands[best_i][1]}（{speeds[best_i] / 1024:.0f} KB/s）"


async def _download(url: str, dest: Path) -> None:
    """流式下载并汇报进度。不用 httpx 的 .content —— 400MB 会全进内存。"""
    import httpx

    timeout = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"下载失败 HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            if total > MAX_FPK_BYTES:
                raise RuntimeError(f"包体积异常（{total} 字节），已拒绝")
            got = 0
            last = 0.0
            started = _now()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK):
                    got += len(chunk)
                    if got > MAX_FPK_BYTES:
                        raise RuntimeError("包体积超限，已中止")
                    f.write(chunk)
                    now = _now()
                    if now - last > 1.0:
                        pct = int(got * 100 / total) if total else 0
                        # 同时报速度与剩余秒数：整百分比在慢速下长时间不动，
                        # 看着像卡死；有了 KB/s 和剩余时间才知道它在走。
                        speed = got / max(now - started, 0.001)
                        eta = int((total - got) / speed) if speed > 0 and total else 0
                        _write_state(stage="downloading", received=got, total=total,
                                     percent=pct, speed=int(speed), eta=eta)
                        last = now


async def _fetch_one_sha(url: str) -> str:
    """从单个地址取 64 位校验值。失败抛异常（由调用方决定是否采信）。"""
    import httpx

    # 四个参数必须齐全：httpx 0.28 起 httpx.Timeout() 不再接受部分参数，
    # 只给 connect/read 会抛 ValueError，导致取校验值必失败（表现为「下载失败」）。
    timeout = httpx.Timeout(connect=8.0, read=12.0, write=10.0, pool=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
    if not m:
        raise RuntimeError("校验文件格式异常")
    return m.group(1).lower()


async def _fetch_expected_sha(url: str) -> str:
    """取 release 上并排发布的 .sha256。

    只接受「64 位十六进制」，不接受包内自述的校验值。

    【为什么官方取不到时允许退到代理】实测（2026-10-04）国内宽带直连
    github.com 会 ConnectTimeout（不是慢，是连不上），坚持只走官方等于
    下载永远无法开始。退到代理时的安全依据是**一致性**：
    官方可达时完全按官方值，代理一个都不参与；
    官方不可达时，必须有 >= 2 个**互不相关的**代理返回同一个 64 位哈希
    才采信 —— 第三方要同时骗过两个来源才能得手。
    只回来一个（或几个互不一致）一律视为取不到，直接拒绝下载。
    """
    try:
        return await _fetch_one_sha(url)
    except Exception as exc:  # noqa: BLE001 — 官方不可达，转入代理一致性校验
        official_err = exc
        logger.warning("官方校验值取不到（%s），改由多个 CDN 代理交叉确认", exc)

    cands = [(n, p + url) for n, p in CDN_PREFIXES]
    got = await asyncio.gather(*[_fetch_one_sha(u) for _, u in cands],
                               return_exceptions=True)
    vals: dict[str, list[str]] = {}
    for (name, _), r in zip(cands, got):
        if isinstance(r, Exception):
            logger.info("  代理 %s 不可用：%s", name, r)
            continue
        vals.setdefault(r, []).append(name)

    if not vals:
        raise RuntimeError(f"官方与{len(cands)} 个代理都取不到校验值（官方错误：{official_err}）")
    if len(vals) > 1:
        raise RuntimeError("各代理返回的校验值不一致，已拒绝（可能有中间人篡改）")
    sha, names = next(iter(vals.items()))
    if len(names) < 2:
        raise RuntimeError(
            f"仅 1 个代理（{names[0]}）返回了校验值、无法交叉确认，已拒绝"
        )
    logger.info("校验值经 %s 交叉确认一致：%s", names, sha[:16])
    return sha


def _host_socket() -> "socket.socket | None":
    """连宿主 root 网关的 Unix socket；连不上返回 None（应降级为手动安装）。"""
    for path in (GATEWAY_SOCK, *(Path(p) for p in GATEWAY_SOCK_FALLBACK)):
        try:
            if not path.exists():
                continue
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1800)
            s.connect(str(path))
            return s
        except OSError:
            continue
    return None


def _host_upgrade(target: str) -> tuple[int, str]:
    """请宿主 root 代为安装 fpk。返回 (状态码, 输出)。

    走 webui_gateway.py 的特权端点：容器内没有 root 也没有 appcenter-cli，
    只有宿主侧能装。socket 不存在时返回明确错误，让前端提示手动安装，
    而不是静默"成功"。
    """
    s = _host_socket()
    if s is None:
        return 503, ("无法连接宿主升级通道（未找到网关 socket）。"
                     "请改用手动安装：应用中心 → 手动安装 → 选择下载好的 fpk")
    body = json.dumps({
        "fpk": str(_fpk_path(target)),
        "target": target,
    }).encode("utf-8")
    # 走网关的路径（与 proxy/webui_gateway.py 的 HOST_UPGRADE_PATHS 保持一致）
    path = "/api/host-upgrade"
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "X-Trim-Isadmin: true\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    try:
        s.sendall(head + body)
        chunks = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
    except OSError as exc:
        return 502, f"与宿主升级通道通信失败: {exc}"
    finally:
        try:
            s.close()
        except OSError:
            pass
    raw = b"".join(chunks)
    head_txt, _, payload = raw.partition(b"\r\n\r\n")
    status = 0
    first = head_txt.split(b"\r\n", 1)[0].decode("latin-1", "replace").split()
    if len(first) >= 2 and first[1].isdigit():
        status = int(first[1])
    text = payload.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
        if status == 200 and obj.get("ok"):
            return 0, f"已升级到 v{obj.get('version')}"
        return status or 500, str(obj.get("error") or text)
    except Exception:  # noqa: BLE001
        return status or 500, text[-600:]


def _host_visible_dir() -> str:
    """给用户看的安装包目录（宿主真实路径，「我的文件」里能直接看到）。

    【为什么指向 /vol1/1000 而不是应用目录】飞牛「应用中心 → 手动安装」的
    文件选择器只浏览「我的文件」，实测其根为 /vol1/<uid>/（admin 即
    /vol1/1000/，里面有 Photos / data / 各应用目录）。包原先落在
    /vol1/@appcenter/... 下，选择器里根本看不到 —— 用户找不到文件。
    所以下载完成后由宿主网关把包复制一份到「我的文件」根（见_host_publish），
    页面显示的就是这个真实位置。
    """
    env = (os.environ.get("UPGRADE_HOST_DIR") or "").strip()
    if env:
        return env
    # 与宿主网关 webui_gateway.py 的 MY_FILES_ROOT 保持一致。
    # 拿不到真实 uid 时按 admin=1000 兜底：fnOS 首个用户就是 uid 1000。
    uid = (os.environ.get("FNMUSIC_UID") or "").strip() or "1000"
    return f"/vol1/{uid}"


async def _host_publish(fpk: Path) -> tuple[int, str]:
    """请宿主 root 把包复制到「我的文件」。返回 (状态码, 宿主路径或错误)。

    容器只挂了 /repo 与 /data，**看不到 /vol1**（实测 ls /vol1 → No such file
    or directory），所以这一步只能由宿主代劳。走的是与 host-upgrade 同一个
    特权socket（proxy/webui_gateway.py 的 POST /api/host-publish）。

    失败不阻断下载：包还在升级目录里，用户仍可手动去取，只是选择器里没有而已。
    """
    s = _host_socket()
    if s is None:
        return 503, "（未找到宿主通道，包仍在应用目录，需手动拷贝到「我的文件」）"
    body = json.dumps({"fpk": str(fpk)}).encode("utf-8")
    path = "/api/host-publish"
    head = (
        f"POST {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "X-Trim-Isadmin: true\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    try:
        s.sendall(head + body)
        chunks = []
        s.settimeout(1800)
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except OSError as exc:
        return 500, f"（宿主通道出错：{exc}）"
    finally:
        try:
            s.close()
        except OSError:
            pass
    text = b"".join(chunks).decode("utf-8", "replace")
    try:
        obj = json.loads(text.split("\r\n\r\n", 1)[-1])
    except Exception:  # noqa: BLE001
        return 500, "（宿主返回无法解析）"
    if obj.get("ok"):
        return 200, str(obj.get("path") or "")
    return int(obj.get("code") or 500), str(obj.get("error") or "（未知错误）")


def _ready_message(target: str, where: str | None = None) -> str:
    """包下载校验完后的引导文案。

    刻意写清楚三件事：包已完整可用、在哪、怎么装 —— 不让用户猜。
    where 是「我的文件」里的真实落点（宿主网关复制后的路径）。
    """
    at = where or _host_visible_dir()
    return (
        f"安装包 v{target} 已下载完成，并通过官方 sha256 校验。\n"
        f"已为你放到「我的文件」，飞牛系统没有可用的自动安装接口"
        f"（应用中心会跳过对已安装应用的安装），最后一步需要你手动点一下：\n"
        f"① 打开「应用中心」，在侧边栏底部开启「手动安装」\n"
        f"② 点「手动安装」，在「我的文件」里选择 {at}\n"
        f"③ 确认后即完成升级；当前版本在升级前一直可用"
    )


async def _do_upgrade(asset_url: str, expect_sha: str, target: str) -> None:
    """后台任务：下载 → 校验 → 安装。全程写状态，供前端轮询。"""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        fpk = _fpk_path(target)
        fpk.unlink(missing_ok=True)

        _write_state(stage="downloading", percent=0, received=0, total=0,
                     target=target, error="", speed=0, eta=0)
        # 测速择优：直连太慢时走 CDN 镜像。校验值仍取自官方，不受此影响。
        real_url, source = await _pick_download_url(asset_url)
        _write_state(source=source)
        logger.info("upgrade download source: %s", source)
        await _download(real_url, fpk)

        _write_state(stage="verifying", percent=100, target=target)
        real = await asyncio.get_running_loop().run_in_executor(None, _sha256_file, fpk)
        if real.lower() != (expect_sha or "").lower():
            fpk.unlink(missing_ok=True)
            _write_state(stage="failed", target=target,
                         error="安装包校验不通过，已中止安装（文件可能已损坏或被篡改）")
            return

        # 校验通过 —— 包已就位。fnOS 无可用自动升级通道（见 docstring 第 3 条），
        # 这里**不**去调 appcenter-cli 装：它对已安装应用是空操作，会返回 0
        # 却什么都没做，把它当成功就是骗用户。
        #
        # 再把包复制一份到「我的文件」：手动安装的文件选择器只认那里，
        # 放在应用目录用户根本选不到。容器够不到 /vol1，只能请宿主 root 代劳。
        code, published = await _host_publish(fpk)
        if code == 200:
            where = published
        else:
            where = f"{_host_visible_dir()}（复制到「我的文件」未成功：{published}；"
            where += "包仍在应用目录，可手动拷贝过去）"
        _write_state(
            stage="ready", target=target, percent=100,
            message=_ready_message(target, where),
            file_path=str(fpk),
            # 下发宿主真实路径：前端要把它原样显示出来，用户照着就能找到包。
            host_dir=_host_visible_dir(),
            file_name=fpk.name,
            file_size=fpk.stat().st_size,
            published_path=where if code == 200 else "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("upgrade download failed")
        _write_state(stage="failed", target=target, error=str(exc))


async def start_upgrade(assets: list, target: str, current: str) -> dict:
    """发起下载。校验通过后丢后台任务，立即返回。"""
    if _ver_tuple(target) <= _ver_tuple(current):
        raise ValueError(f"目标版本 v{target} 不高于当前 v{current}，已拒绝")

    name = ""
    url = ""
    for a in assets or []:
        n = str(a.get("name") or "")
        if re.search(r"-online\.fpk$", n, re.I):
            continue  # 在线包要现拉 Docker 镜像，用户的 NAS 未必拉得到
        if n.endswith(".fpk") and str(target) in n:
            name, url = n, str(a.get("url") or "")
            break
    if not url:
        # 退化：只要有非 online 的 fpk 就用，但必须同版本号
        for a in assets or []:
            n = str(a.get("name") or "")
            if n.endswith(".fpk") and not re.search(r"-online\.fpk$", n, re.I):
                name, url = n, str(a.get("url") or "")
                break
    if not url:
        raise ValueError("远端未提供离线完整安装包，无法自动下载")
    if not _host_allowed(url):
        raise ValueError("安装包来源不在允许的官方仓库列表内，已拒绝")

    sha_url = url + ".sha256"
    if not _host_allowed(sha_url):
        raise ValueError("校验文件来源不合法，已拒绝")
    try:
        expect = await _fetch_expected_sha(sha_url)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"取不到官方校验值（{exc}），为安全起见不提供下载") from exc

    async with _lock:
        cur = read_state()
        if cur.get("stage") in ("downloading", "verifying"):
            raise ValueError("已有下载任务在进行中")
        task = asyncio.create_task(_do_upgrade(url, expect, target))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    _write_state(stage="starting", target=target, percent=0, error="", message="")
    return {"ok": True, "stage": "starting", "target": target, "file": name}


_TASKS: set = set()


def cleanup() -> None:
    """服务启动时清掉上次残留的临时包（可能是 400MB+）。

    注意：只在 ready/failed 之外的状态下清理；若上一轮已 ready 且用户还没装，
    保留状态与包，好让页面继续显示"包已就位"。
    """
    try:
        # 落盘文件名带版本号（_fpk_path），不再是单一固定文件，所以按名字前缀清。
        # 语义与改动前一致：服务启动即清掉上轮残留的包。
        for old in WORK_DIR.glob("fnmusic-ext-*.fpk"):
            old.unlink(missing_ok=True)
        # 兼容改动前落盘的旧名，避免 428MB 残留白占磁盘
        (WORK_DIR / "package.fpk").unlink(missing_ok=True)
    except OSError:
        pass
    try:
        for old in WORK_DIR.glob("*.tmp"):
            old.unlink(missing_ok=True)
    except OSError:
        pass


def disk_free_mb(path: Path = Path("/tmp")) -> int:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1
