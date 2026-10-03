"""一键升级：下载远端 fpk → 校验 → 交给飞牛应用中心安装。

设计取舍（都是踩过坑才定下来的）：

1. **必须校验 sha256，且只认官方发行页登记的值。**
   安装 fpk 等于以 root 跑任意安装脚本，拿到一个错误的包就是任意代码执行。
   所以校验值不从包自身读取、不从第三方取，只认 release 上并排发布的
   `<name>.sha256`；取不到就拒绝安装，宁可让用户手动装。

2. **只允许升级到更高版本，且只允许官方仓库的资产。**
   防止把包指针指到任意 URL（等于任意下载），也防止"降级"覆盖掉更新的安装。

3. **用 appcenter-cli install-fpk，不自己解包。**
   fnOS 的安装流程（停服务、备份、迁移数据、装依赖、重启）由应用中心负责，
   自己解包替换等于绕过系统管理，装坏了系统状态就乱了。

4. **安装是长任务，接口立即返回 + 轮询状态。**
   405MB 离线包下载+安装要好几分钟，HTTP 请求挂在那里必然超时。
   进程还会被安装动作重启掉，所以状态要落盘，重启后仍能查到真实结果。

5. **下载体积大，边下边报进度。**
   离线包 400MB+，不给进度用户会以为卡死。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger("webui.upgrade")

# 官方仓库（与 app.py 的 RELEASE_SOURCES 必须一致，双向核对过）
REPO_OWNER = "haonanren118"
REPO_NAME = "fnos_music_ext"
GITEE_OWNER = "yygitee118"

APP_NAME = "fnmusic-ext"
APPCENTER_CLI = "/usr/local/bin/appcenter-cli"

WORK_DIR = Path("/tmp/fnmusic-upgrade")
STATE_FILE = WORK_DIR / "state.json"
FPK_FILE = WORK_DIR / "package.fpk"

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
    # 下载/安装中途进程被杀会留下 running 状态，10 分钟后视为中断
    if data.get("stage") in ("downloading", "verifying", "installing"):
        if _now() - float(data.get("updated_at") or 0) > 600:
            data["stage"] = "interrupted"
            data["message"] = "上次升级被中断（服务重启或网络断开），可重新发起升级"
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

    timeout = httpx.Timeout(connect=15.0, read=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"取不到校验文件 HTTP {r.status_code}")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
    if not m:
        raise RuntimeError("校验文件格式异常")
    return m.group(1).lower()


def _run_appcenter(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    """调应用中心 CLI。需要 root；WebUI 以 root 运行时可直接执行。"""
    import subprocess

    if os.geteuid() != 0:
        raise RuntimeError("一键升级需要 root 权限运行 WebUI；当前权限不足，请改用手动安装")
    if not Path(APPCENTER_CLI).exists():
        raise RuntimeError("未找到 appcenter-cli，无法自动安装")
    proc = subprocess.run(  # noqa: S603
        [APPCENTER_CLI, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


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

        _write_state(stage="installing", target=target, message="正在安装，请勿关闭设备电源")
        code, out = await asyncio.get_running_loop().run_in_executor(
            None, _run_appcenter, ["install-fpk", str(FPK_FILE)],
        )
        if code == 0:
            _write_state(stage="done", target=target,
                         message=f"已升级到 v{target}，管理台可能会自动刷新")
            FPK_FILE.unlink(missing_ok=True)
        else:
            tail = out[-600:] or f"退出码 {code}"
            _write_state(stage="failed", target=target,
                         error=f"应用中心安装失败：{tail}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("upgrade failed")
        _write_state(stage="failed", target=target, error=str(exc))


async def start_upgrade(assets: list, target: str, current: str) -> dict:
    """发起升级。校验通过后丢后台任务，立即返回。"""
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
        raise ValueError("远端未提供离线完整安装包，无法自动升级；请用「下载安装包」手动安装")
    if not _host_allowed(url):
        raise ValueError("安装包来源不在允许的官方仓库列表内，已拒绝")

    sha_url = url + ".sha256"
    if not _host_allowed(sha_url):
        raise ValueError("校验文件来源不合法，已拒绝")
    try:
        expect = await _fetch_expected_sha(sha_url)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"取不到官方校验值（{exc}），为安全起见不自动安装") from exc

    async with _lock:
        cur = read_state()
        if cur.get("stage") in ("downloading", "verifying", "installing"):
            raise ValueError("已有升级任务在进行中")
        task = asyncio.create_task(_do_upgrade(url, expect, target))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    _write_state(stage="starting", target=target, percent=0, error="", message="")
    return {"ok": True, "stage": "starting", "target": target, "file": name}


_TASKS: set = set()


def cleanup() -> None:
    """服务启动时清掉上次残留的临时包（可能是 400MB+）。"""
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
