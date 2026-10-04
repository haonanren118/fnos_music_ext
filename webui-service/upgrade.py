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
FPK_FILE = WORK_DIR / "package.fpk"

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
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK):
                    got += len(chunk)
                    if got > MAX_FPK_BYTES:
                        raise RuntimeError("包体积超限，已中止")
                    f.write(chunk)
                    now = _now()
                    if now - last > 1.0:
                        pct = int(got * 100 / total) if total else 0
                        _write_state(stage="downloading", received=got, total=total, percent=pct)
                        last = now


async def _fetch_expected_sha(url: str) -> str:
    """取 release 上并排发布的 .sha256。

    只接受「64 位十六进制」，不接受包内自述的校验值。
    """
    import httpx

    # 四个参数必须齐全：httpx 0.28 起httpx.Timeout() 不再接受部分参数，
    # 只给 connect/read 会抛 ValueError，导致取校验值必失败（表现为「下载失败」）。
    timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"取不到校验文件 HTTP {r.status_code}")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
    if not m:
        raise RuntimeError("校验文件格式异常")
    return m.group(1).lower()


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
        "fpk": str(FPK_FILE),
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
    """给用户看的安装包目录（宿主真实路径）。

    容器里的 /repo 只是挂载点，用户在「文件」App 里看到的是宿主路径。
    优先用宿主网关告知的真实路径；拿不到就退回按 appname 推算的常见位置；
    再不行只说相对位置 —— 宁可含糊也别给一个用户点不存在的 /repo/... 。
    """
    env = (os.environ.get("UPGRADE_HOST_DIR") or "").strip()
    if env:
        return env
    # 容器里 /repo 只是挂载点，宿主真实路径推不出来（Path.exists 必然 False），
    # 所以兜底不查存在性，只按 fnOS 的固定布局拼一个 —— 与 Dockerfile 的
    # UPGRADE_HOST_DIR、宿主网关的 UPGRADE_FPK_DIRS 三处保持一致。
    name = (os.environ.get("FNMUSIC_APP_NAME") or "").strip() or "fnmusic-ext"
    return f"/vol1/@appcenter/{name}/repo/sources-data/{UPGRADE_SUBDIR}"


def _ready_message(target: str) -> str:
    """包下载校验完后的引导文案。

    刻意写清楚三件事：包已完整可用、在哪、怎么装 —— 不让用户猜。
    """
    where = _host_visible_dir()
    return (
        f"安装包 v{target} 已下载完成，并通过官方 sha256 校验。\n"
        f"飞牛系统没有可用的自动安装接口（应用中心会跳过对已安装应用的安装），"
        f"最后一步需要你手动点一下：\n"
        f"① 打开「应用中心」，在侧边栏底部开启「手动安装」\n"
        f"② 点「手动安装」，选择 {where} 下的 {FPK_FILE.name}\n"
        f"③ 确认后即完成升级；当前版本在升级前一直可用"
    )


async def _do_upgrade(asset_url: str, expect_sha: str, target: str) -> None:
    """后台任务：下载 → 校验 → 安装。全程写状态，供前端轮询。"""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        FPK_FILE.unlink(missing_ok=True)

        _write_state(stage="downloading", percent=0, received=0, total=0,
                     target=target, error="")
        await _download(asset_url, FPK_FILE)

        _write_state(stage="verifying", percent=100, target=target)
        real = await asyncio.get_running_loop().run_in_executor(None, _sha256_file, FPK_FILE)
        if real.lower() != (expect_sha or "").lower():
            FPK_FILE.unlink(missing_ok=True)
            _write_state(stage="failed", target=target,
                         error="安装包校验不通过，已中止安装（文件可能已损坏或被篡改）")
            return

        # 校验通过 —— 包已就位。fnOS 无可用自动升级通道（见 docstring 第 3 条），
        # 这里**不**去调 appcenter-cli装：它对已安装应用是空操作，会返回 0
        # 却什么都没做，把它当成功就是骗用户。改为把包留在宿主目录并给出安装指引。
        _write_state(
            stage="ready", target=target, percent=100,
            message=_ready_message(target),
            file_path=str(FPK_FILE),
            file_size=FPK_FILE.stat().st_size,
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
        if FPK_FILE.exists():
            FPK_FILE.unlink()
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
