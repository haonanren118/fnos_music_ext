#!/usr/bin/env python3
"""把飞牛桌面网关的 Unix socket 原样转到本机 WebUI。

应用中心按 ui/config 的 gatewaySocket 连接
<应用目录>/fnmusic-ext.sock，把 https://<主机>:<桌面端口>/app/fnmusic-ext/
转到这个 socket。WebUI 自己监听 127.0.0.1:8774，并认识 /app/fnmusic-ext 前缀。

本模块还处理两个特权端点（都在宿主侧本进程里跑，root）：

1. POST /app/fnmusic-ext/api/host-file —— 浏览器把 NAS 上选中的 .js 源脚本
   路径发来，由宿主代读文件内容。
2. POST /app/fnmusic-ext/api/host-upgrade —— 一键升级的**安装动作**。
   WebUI 跑在容器里（uid 1000，既没有 root 也没有 appcenter-cli），自己装不了
   fpk；这里由宿主 root 代为执行 `appcenter-cli install-fpk`。
3. POST /app/fnmusic-ext/api/host-publish —— 把已校验的 fpk **复制到「我的文件」**。
   飞牛「手动安装」的文件选择器只浏览「我的文件」（实测根为 /vol1/<uid>/），
   而容器只挂 /repo 与 /data、**看不到 /vol1**，自己放不进去，故由宿主代劳。

这三个端点要求网关注入的 X-Trim-Isadmin: true。其余请求一律原样转发，不动字节。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

UPSTREAM = ("127.0.0.1", 8774)
SOCK_NAME = "fnmusic-ext.sock"
PID_FILE = Path("/run/fnmusic-ext/webui-gateway.pid")
# create_connection 的 timeout 会留在套接字上，之后的 recv 也受它限制。
# 洛雪源校验要几十秒才有响应；沿用 5 秒会让桌面网关中途拆连接，nginx 回 502。
CONNECT_TIMEOUT = 5
RELAY_TIMEOUT = 180

HOST_FILE_PATHS = ("/app/fnmusic-ext/api/host-file", "/api/host-file")
HOST_FILE_MAX_BODY = 1 << 20          # 请求体上限 1MB（只装一个路径字符串）
HOST_FILE_MAX_BYTES = 9_000_000       # 与 lxmusic SCRIPT_MAX_BYTES 对齐

HOST_UPGRADE_PATHS = ("/app/fnmusic-ext/api/host-upgrade", "/api/host-upgrade")
HOST_PUBLISH_PATHS = ("/app/fnmusic-ext/api/host-publish", "/api/host-publish")
HOST_UPGRADE_MAX_BODY = 1 << 16
APPCENTER_CLI = "/usr/local/bin/appcenter-cli"
# 只允许应用仓库的数据目录下那个升级子目录，挡住"任意路径喂给 install-fpk"。
# 不能用 /tmp：容器 /tmp 与宿主 /tmp 是两套互不可见的空间，包下到容器 /tmp
# 宿主根本读不到；WebUI 侧把包放在 /repo/sources-data/upgrade/（/repo 挂自
# 宿主仓库目录），这里对应到宿主真实路径。
UPGRADE_FPK_DIRS = (
    "/vol1/@appcenter/fnmusic-ext/repo/sources-data/upgrade",
)
UPGRADE_MAX_FPK_BYTES = 900 * 1024 * 1024
APP_NAME = "fnmusic-ext"

# 「手动安装」的文件选择器只浏览「我的文件」。实测其根为 /vol1/<uid>/：
# admin(uid=1000) 即 /vol1/1000/，里面是 Photos / data / 各应用目录。
# 目标目录固定写死，不接受调用方指定 —— 这是特权端点，不能让人指到任意路径。
MY_FILES_ROOT = "/vol1/1000"

# 容器把仓库挂在 /repo，本进程在宿主 —— 收到容器发来的路径要按前缀换算。
CONTAINER_REPO_PREFIX = "/repo"
HOST_REPO_ROOT = Path("/vol1/@appcenter/fnmusic-ext/repo")
# 容器内 WebUI 用它连宿主 root 网关（同在 /repo 下，容器可见）
UPGRADE_SOCK_NAME = ".upgrade-gw.sock"

# ── host-hotfix：代码级热更新（改宿主 repo 里的代码，不重建镜像） ──────────────
#
# 【为什么需要】S1 之后容器内 /srv/* 已是指向 /repo/* 的软链，改宿主代码分三种生效方式：
#   - static/*            → 刷新浏览器即生效（StaticFiles 每次请求读盘）
#   - webui-service/*.py  → docker exec supervisorctl restart webui（1~2s）
#   - proxy/*.py          → systemctl restart fnmusic-ext（本进程也会被重启）
# 这三件事容器里做不了（无 root、无 docker、无 systemctl），必须由宿主 root 代劳。
#
# 【版本基线从哪来】热更新改的是代码，不改应用中心登记的 fpk 版本，所以不能只看
# `_installed_version()`。这里用三处取最大：
#   1) 应用中心登记版本（_installed_version，本机实测可能为空）
#   2) 仓库根 VERSION 文件
#   3) .hotfix-version（本端点自己维护，每次成功写入后更新）
# 每次成功写入后，把 to_version 落到 .hotfix-version，后续热更新以此为基线递增。
HOST_HOTFIX_PATHS = ("/app/fnmusic-ext/api/host-hotfix", "/api/host-hotfix")
HOST_HOTFIX_ROLLBACK_PATHS = (
    "/app/fnmusic-ext/api/host-hotfix-rollback", "/api/host-hotfix-rollback")
HOST_HOTFIX_MAX_BODY = 8 << 20        # 8MB：hotfix 含多文件代码，比 host-file 的 1MB 宽
HOTFIX_BACKUP_ROOT = HOST_REPO_ROOT / ".hotfix-backup"
HOTFIX_VERSION_FILE = HOST_REPO_ROOT / ".hotfix-version"

# 白名单：只允许改这些前缀下的文件（用**容器路径**写法，与 CONTAINER_REPO_PREFIX 一致，
# 因为调用方在容器里，它只知道 /repo/...）。换算成宿主路径后再做逃逸检查。
HOTFIX_ALLOW_PREFIXES = (
    "/repo/proxy/",
    "/repo/webui-service/",
    "/repo/musicdl-service/",
    "/repo/musicbox-service/",
    "/repo/lxmusic-service/",
)
# 明确禁止：即便前缀匹配也拦掉。
#   .env           —— 站点配置，改了要重装
#   sources-data/  —— 用户数据（曲库缓存、升级包），绝不碰
#   ui/            —— 桌面入口定义，碰了图标会出问题
#   container/     —— 镜像构建物，热更新改它没意义（要 rebuild 才生效）
HOTFIX_DENY_PARTS = (".env", "sources-data", "ui", "container", ".hotfix-backup")
HOTFIX_ALLOW_SUFFIXES = (".py", ".js", ".html", ".css", ".json", ".sh")
# 允许重启的服务名 → 实际命令。只接受这几个名字，绝不接受任意命令/参数。
HOTFIX_RESTART_TARGETS = {
    "host": ("systemctl", "restart", "fnmusic-ext"),
    "webui": ("docker", "exec", "fnmusic-sources", "supervisorctl",
              "-c", "/etc/supervisor/supervisord.conf", "restart", "webui"),
    "musicdl": ("docker", "exec", "fnmusic-sources", "supervisorctl",
                "-c", "/etc/supervisor/supervisord.conf", "restart", "musicdl"),
    "musicbox": ("docker", "exec", "fnmusic-sources", "supervisorctl",
                 "-c", "/etc/supervisor/supervisord.conf", "restart", "musicbox"),
    "lxmusic": ("docker", "exec", "fnmusic-sources", "supervisorctl",
                "-c", "/etc/supervisor/supervisord.conf", "restart", "lxmusic"),
}
HOTFIX_MAX_FILES = 200


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ver_tuple(raw: str) -> tuple:
    """把版本串解析成可比较的 4 元组。

    与 webui-service/upgrade.py 的同名函数保持一致的行为（只取数字，
    不足 4 段补 0），这样两边的"谁更新"判断不会打架。
    """
    parts = re.findall(r"\d+", str(raw or ""))
    return tuple(int(p) for p in parts[:4]) + (0,) * max(0, 4 - len(parts[:4]))


def _current_hotfix_base() -> str:
    """取当前版本基线 = max(应用中心登记, VERSION 文件, .hotfix-version)。"""
    candidates = [_installed_version()]
    try:
        candidates.append((HOST_REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    try:
        candidates.append(HOTFIX_VERSION_FILE.read_text(encoding="utf-8").strip())
    except OSError:
        pass
    valid = [c for c in candidates if re.search(r"\d", str(c or ""))]
    if not valid:
        return ""
    return max(valid, key=_ver_tuple)


def _container_to_host(container_path: str) -> "Path | None":
    """容器路径 → 宿主真实路径；不是 /repo 开头或含 .. 返回 None。

    路径按 POSIX 语义解析（容器与 NAS 都是 Linux），避免在 Windows 上测试时
    被 os.path.normpath 换成反斜杠而行为不一致。
    """
    if not container_path.startswith(CONTAINER_REPO_PREFIX):
        return None
    rel = container_path[len(CONTAINER_REPO_PREFIX):].lstrip("/")
    if not rel:
        return None
    parts = Path(rel).parts
    if ".." in parts:
        return None
    return HOST_REPO_ROOT.joinpath(*parts)


def _hotfix_path_ok(container_path: str, host_path: Path) -> "str | None":
    """返回拒绝原因；通过则返回 None。三重校验：前缀 + 后缀 + 禁名单 + 逃逸。"""
    if not any(container_path.startswith(p) for p in HOTFIX_ALLOW_PREFIXES):
        return f"路径不在白名单目录内：{container_path}"
    if not container_path.endswith(HOTFIX_ALLOW_SUFFIXES):
        return f"文件后缀不允许（仅 {'/'.join(HOTFIX_ALLOW_SUFFIXES)}）：{container_path}"
    parts = Path(container_path).parts
    for denied in HOTFIX_DENY_PARTS:
        if denied in parts:
            return f"路径命中禁止项 {denied}：{container_path}"
    try:
        resolved = host_path.resolve()
        root = HOST_REPO_ROOT.resolve()
    except OSError as exc:
        return f"路径解析失败：{exc}"
    if resolved != root and root not in resolved.parents:
        return f"路径逃逸出仓库目录：{container_path}"
    return None


def socket_path_for(base: Path) -> Path:
    """fpk 布局是 <应用目录>/repo。socket 必须放在应用目录下，桌面网关才找得到。"""
    parent = base.resolve().parent
    if (parent / "ui").is_dir() and base.name == "repo":
        return parent / SOCK_NAME
    run = Path("/run/fnmusic-ext")
    run.mkdir(parents=True, exist_ok=True)
    return run / SOCK_NAME


def _relay(src: socket.socket, dst: socket.socket, prefix: bytes = b"") -> None:
    try:
        if prefix:
            dst.sendall(prefix)
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _read_request_head(client: socket.socket) -> "tuple[bytes, dict[str, str]] | None":
    """读到 HTTP 头部结束；返回 (头部原始字节, 解析出的头)。超时/格式异常返回 None。"""
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            if len(buf) > 65536:
                return None
            chunk = client.recv(8192)
            if not chunk:
                return None
            buf += chunk
    except OSError:
        return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    headers: dict[str, str] = {}
    lines = head.decode("latin-1", "replace").split("\r\n")
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    if rest:
        headers["__rest__"] = rest.decode("latin-1", "replace")
    headers["__raw__"] = head.decode("latin-1", "replace")
    return head + b"\r\n\r\n" + rest, headers


def _read_full_body(client: "socket.socket | None", headers: dict[str, str], head_raw: bytes,
                    max_body: int = HOST_FILE_MAX_BODY) -> "bytes | None":
    """按 Content-Length 收齐 POST 体。

    head_raw 里已含头部之后读到的部分体，先扣出来；不够再 recv（client 为 None
    时表示调用方已把全部体读进 head_raw，无需再收）。max_body 可覆盖上限：
    host-file 用默认 1MB，host-hotfix 传 8MB。
    """
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length < 0 or length > max_body:
        return None
    head_end = head_raw.find(b"\r\n\r\n") + 4
    body = head_raw[head_end:]
    while len(body) < length:
        if client is None:
            return None
        chunk = client.recv(65536)
        if not chunk:
            return None
        body += chunk
    return body[:length]


def _http_response(status: int, reason: str, payload: bytes, content_type: str) -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    return head + payload


def _json_response(status: int, obj: dict) -> bytes:
    return _http_response(status, "OK" if status == 200 else "Error",
                          json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")


def handle_host_file(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/host-file：校验 admin + .js 路径，读文件内容回 JSON {script}。"""
    try:
        if headers.get("x-trim-isadmin", "").lower() != "true":
            client.sendall(_json_response(403, {"ok": False, "error": "仅管理员可读取主机文件"}))
            return
        body = _read_full_body(client, headers, head_raw)
        if body is None:
            client.sendall(_json_response(400, {"ok": False, "error": "请求体无效"}))
            return
        try:
            req = json.loads(body.decode("utf-8"))
            path = str(req.get("path") or "")
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(400, {"ok": False, "error": "请求体必须是 JSON"}))
            return
        target = Path(path)
        if not path.startswith("/") or ".." in target.parts:
            client.sendall(_json_response(400, {"ok": False, "error": "路径必须是绝对路径且不含 .."}))
            return
        if not path.lower().endswith(".js"):
            client.sendall(_json_response(400, {"ok": False, "error": "只支持读取 .js 后缀文件"}))
            return
        try:
            st = target.stat()
        except OSError:
            client.sendall(_json_response(404, {"ok": False, "error": f"文件不存在: {path}"}))
            return
        if not os.path.isfile(str(target)) or st.st_size > HOST_FILE_MAX_BYTES:
            client.sendall(_json_response(400, {"ok": False, "error": "不是常规文件或超过 9MB 上限"}))
            return
        try:
            script = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            client.sendall(_json_response(400, {"ok": False, "error": f"读取失败: {exc}"}))
            return
        client.sendall(_json_response(200, {"ok": True, "script": script}))
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _accept_loop(server: socket.socket, upstream: tuple[str, int]) -> None:
    while True:
        client, _addr = server.accept()
        threading.Thread(target=_handle, args=(client, upstream), daemon=True).start()


def serve(sock_path: Path, upstream: tuple[str, int] = UPSTREAM) -> None:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chmod(sock_path, 0o666)
    server.listen(64)
    threading.Thread(target=_accept_loop, args=(server, upstream), daemon=True).start()

    # 额外在 /repo 下再 bind 一个同样的 socket：容器只挂了 /repo，看不到应用
    # 目录里的那个（容器里 8774 绑在宿主回环，容器也连不上）。
    # 有了这个，容器内 WebUI 就能通过 /repo/.upgrade-gw.sock 找到本进程，
    # 从而把「装 fpk」这件必须 root 的事交上来。
    # 同一个进程、同一个端口复用表，行为与桌面网关完全一致。
    try:
        alt = HOST_REPO_ROOT / UPGRADE_SOCK_NAME
        alt.parent.mkdir(parents=True, exist_ok=True)
        try:
            alt.unlink()
        except FileNotFoundError:
            pass
        alt_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        alt_srv.bind(str(alt))
        os.chmod(alt, 0o666)
        alt_srv.listen(64)
        _accept_loop(alt_srv, upstream)          # 阻塞即主循环
    except OSError as exc:
        # 只影响容器内直连通道，桌面网关照常工作
        print(f"[warn] 容器升级通道 socket 绑定失败: {exc}", file=sys.stderr)


def _is_host_file_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_FILE_PATHS)


def _is_post_to(head_raw: bytes, paths: tuple[str, ...]) -> bool:
    try:
        request_line = head_raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    except Exception:  # noqa: BLE001
        return False
    parts = request_line.split()
    if len(parts) < 2 or parts[0].upper() != "POST":
        return False
    return parts[1] in paths


def _installed_version() -> str:
    """读应用中心登记的版本号；读不到返回空串。

    只用于「装完到底换没换」的判定：install-fpk 对已安装应用是**空操作** ——
    它只做文件校验，打印 `[Info]Application [x] is installed.` 并**返回 0**，
    实际既不升级也不报错。只看退出码会把"什么都没发生"当成"升级成功"。
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [APPCENTER_CLI, "list"], capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return ""
    for line in (proc.stdout or "").splitlines():
        if APP_NAME not in line:
            continue
        for cell in (c.strip() for c in line.split("│")):
            if re.fullmatch(r"v?\d+\.\d+\.\d+\S*", cell):
                return cell.lstrip("v")
    return ""


def handle_host_upgrade(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/host-upgrade：由宿主 root 代容器执行 fpk 安装。

    请求体：{"fpk": "/tmp/fnmusic-upgrade/package.fpk", "target": "2.6.18"}
    响应：  {"ok": true, "version": "2.6.18", "output": "..."}

    安全约束（装 fpk = 以 root 执行安装脚本，这里是唯一的特权入口）：
      - 必须是管理员（网关注入的 X-Trim-Isadmin: true）；
      - 包路径必须在 sources-data/upgrade/ 下（容器 /repo/... 会自动映射为宿主路径）；
      - 后缀必须是 .fpk，体积不超过 900MB；
      - 目标版本必须高于当前登记版本（拒绝降级）；
      - **成功判据是"装完读回来的版本 == 目标版本"**，不是退出码。
    """
    try:
        if headers.get("x-trim-isadmin", "").lower() != "true":
            client.sendall(_json_response(403, {"ok": False, "error": "仅管理员可执行升级"}))
            return
        body = _read_full_body(client, headers, head_raw)
        if body is None:
            client.sendall(_json_response(400, {"ok": False, "error": "请求体无效"}))
            return
        try:
            req = json.loads(body.decode("utf-8"))
            fpk = str(req.get("fpk") or "")
            target = str(req.get("target") or "")
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(400, {"ok": False, "error": "请求体必须是 JSON"}))
            return

        # WebUI 在容器里，发来的路径是容器视角（/repo/...），本进程在宿主，
        # 必须映射成宿主真实路径，否则 install-fpk 会找不到文件。
        fpk_host = fpk
        if fpk.startswith(CONTAINER_REPO_PREFIX):
            fpk_host = str(HOST_REPO_ROOT / fpk[len(CONTAINER_REPO_PREFIX):].lstrip("/"))

        target_path = Path(fpk_host)
        if not fpk_host.startswith("/") or ".." in target_path.parts:
            client.sendall(_json_response(400, {"ok": False, "error": "路径必须是绝对路径且不含 .."}))
            return
        if str(target_path.parent) not in UPGRADE_FPK_DIRS:
            client.sendall(_json_response(403, {"ok": False,
                                                 "error": f"安装包必须位于 {UPGRADE_FPK_DIRS[0]}"}))
            return
        if target_path.suffix.lower() != ".fpk":
            client.sendall(_json_response(400, {"ok": False, "error": "只接受 .fpk 安装包"}))
            return
        try:
            size = target_path.stat().st_size
        except OSError:
            client.sendall(_json_response(404, {"ok": False, "error": f"安装包不存在: {fpk_host}"}))
            return
        if size <= 0 or size > UPGRADE_MAX_FPK_BYTES:
            client.sendall(_json_response(400, {"ok": False,
                                                 "error": f"安装包大小异常: {size} 字节"}))
            return

        current = _installed_version()
        if current and target and current == target:
            client.sendall(_json_response(409, {
                "ok": False, "version": current,
                "error": f"当前已是 v{current}，无需升级",
            }))
            return

        try:
            proc = subprocess.run(  # noqa: S603
                [APPCENTER_CLI, "install-fpk", fpk_host],
                capture_output=True, text=True, timeout=1800,
            )
        except subprocess.TimeoutExpired:
            client.sendall(_json_response(504, {"ok": False, "error": "应用中心安装超时"}))
            return
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        after = _installed_version()
        if proc.returncode == 0 and after == target:
            client.sendall(_json_response(200, {"ok": True, "version": after,
                                                 "output": out[-800:]}))
        elif proc.returncode != 0:
            client.sendall(_json_response(500, {
                "ok": False, "version": after,
                "error": f"应用中心安装失败（退出码 {proc.returncode}）",
                "output": out[-800:],
            }))
        else:
            # 退出码 0 但版本没变 —— install-fpk 的空操作，必须如实报失败
            client.sendall(_json_response(409, {
                "ok": False, "version": after or current,
                "error": (f"应用中心未执行升级（当前 v{after or current or '未知'}，"
                          f"目标 v{target}）。请改用手动安装。"),
                "output": out[-800:],
            }))
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _is_host_upgrade_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_UPGRADE_PATHS)


def handle_host_publish(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/host-publish：把已校验的 fpk 复制到「我的文件」目录。

    【为什么需要这一步】飞牛「应用中心 → 手动安装」的文件选择器只浏览
    「我的文件」（实测其根为 /vol1/<uid>/，admin 即 /vol1/1000/），而容器
    只挂了 /repo 与 /data，**根本没有 /vol1**（实测 ls: No such file or
    directory），无法自己把包放进那里。所以必须由宿主 root 代劳。

    请求体：{"fpk": "/repo/sources-data/upgrade/fnmusic-ext-2.6.19.fpk"}
    响应：  {"ok": true, "path": "/vol1/1000/fnmusic-ext-2.6.19.fpk"}

    安全约束（这是特权入口，规矩与 host-upgrade 同级）：
      - 必须是管理员；
      - 源路径必须落在 UPGRADE_FPK_DIRS 之内（挡住任意路径读取）；
      - 目标目录固定为该uid 的「我的文件」根，不接受调用方指定；
      - 目标后缀必须 .fpk，体积不超过 900MB；
      - 用 copy 而不是 move/rename —— 升级目录里的包要留着，
        用户若选错版本还能重新选；只读源文件，不删。
    """
    try:
        if headers.get("x-trim-isadmin", "").lower() != "true":
            client.sendall(_json_response(403, {"ok": False, "error": "仅管理员可执行"}))
            return
        body = _read_full_body(client, headers, head_raw)
        if body is None:
            client.sendall(_json_response(400, {"ok": False, "error": "请求体无效"}))
            return
        try:
            req = json.loads(body.decode("utf-8"))
            fpk = str(req.get("fpk") or "")
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(400, {"ok": False, "error": "请求体必须是 JSON"}))
            return

        # 容器发来的是容器视角（/repo/...），本进程在宿主，必须换算
        fpk_host = fpk
        if fpk.startswith(CONTAINER_REPO_PREFIX):
            fpk_host = str(HOST_REPO_ROOT / fpk[len(CONTAINER_REPO_PREFIX):].lstrip("/"))

        target_path = Path(fpk_host)
        if not fpk_host.startswith("/") or ".." in target_path.parts:
            client.sendall(_json_response(400, {"ok": False, "error": "路径必须是绝对路径且不含 .."}))
            return
        if str(target_path.parent) not in UPGRADE_FPK_DIRS:
            client.sendall(_json_response(403, {
                "ok": False, "error": f"源包必须位于 {UPGRADE_FPK_DIRS[0]}"}))
            return
        if target_path.suffix.lower() != ".fpk":
            client.sendall(_json_response(400, {"ok": False, "error": "只接受 .fpk 安装包"}))
            return
        try:
            size = target_path.stat().st_size
        except OSError:
            client.sendall(_json_response(404, {"ok": False, "error": f"安装包不存在: {fpk_host}"}))
            return
        if size <= 0 or size > UPGRADE_MAX_FPK_BYTES:
            client.sendall(_json_response(400, {"ok": False, "error": f"安装包大小异常: {size} 字节"}))
            return

        # 目标固定为「我的文件」根下，不接受调用方指定 —— 避免被指到任意路径
        dest_dir = Path(MY_FILES_ROOT)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            client.sendall(_json_response(500, {"ok": False, "error": f"目标目录不可用: {exc}"}))
            return
        dest = dest_dir / target_path.name
        try:
            # 已是最新且大小一致则跳过，避免每次都重拷 428MB
            if not (dest.exists() and dest.stat().st_size == size
                    and dest.stat().st_mtime >= target_path.stat().st_mtime):
                shutil.copy2(str(target_path), str(dest))
                # copy2 会连权限位一起搬过来，而源包是 0600 root（升级目录
                # 本来就是 root 私有）。落到「我的文件」后属主若仍是 root，
                # 文件管理器里会显示成 root 的文件，且与同目录里admin 拥有
                # 的其他文件不一致。按目标目录的属主纠正 —— 只是让文件在
                # 「我的文件」里看起来正常，不放宽任何权限位。
                owner = dest_dir.stat()
                try:
                    os.chown(str(dest), owner.st_uid, owner.st_gid)
                except (OSError, AttributeError):
                    pass
        except OSError as exc:
            client.sendall(_json_response(500, {"ok": False, "error": f"复制失败: {exc}"}))
            return
        client.sendall(_json_response(200, {
            "ok": True, "path": str(dest), "size": size, "skipped": False}))
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _is_host_publish_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_PUBLISH_PATHS)


def _run_restart(svc: str) -> bool:
    """按白名单执行重启；未知名字返回 False。绝不接受任意命令。"""
    cmd = HOTFIX_RESTART_TARGETS.get(svc)
    if cmd is None:
        return False
    try:
        subprocess.run(list(cmd), capture_output=True, text=True, timeout=120)  # noqa: S603
    except (subprocess.TimeoutExpired, OSError):
        # 重启超时不等于没重启（systemctl restart 会先停后起），记录为已尝试
        return True
    return True


def _restart_after_response(client: socket.socket, svc_list: list[str], payload: bytes) -> None:
    """先把响应发给调用方、关连接，**再**执行重启。

    【为什么必须这样】restart=["host"] 等价于 `systemctl restart fnmusic-ext`，
    而本进程（gateway）正是该 service 的 ExecStartPost 子进程 —— 同步执行会
    在 `client.sendall()` 之前就把自己杀掉，调用方收到 0 字节，无法区分
    "成功但没回包" 与 "失败"。所以顺序必须是：回包 → 关连接 → 重启。

    【★ 顺序约束（2026-10-09 修）】`host` 必须**放到最后**执行。
    原因同上：`systemctl restart fnmusic-ext` 会杀掉本进程，而重启动作跑在本
    函数的线程里 —— 线程随进程一起消失，排在 host 之后的服务**永远不会被重启**。
    实测事故：restart=["host","lxmusic"] 时 lxmusic 的 pid 不变、代码实际未生效，
    但端点仍回报 `restarted:["host","lxmusic"]`（静默成功，正是纪律七要防的形态）。
    """
    try:
        client.sendall(payload)
    except OSError:
        pass
    try:
        client.close()
    except OSError:
        pass
    # host 排到最后：它一旦执行，本进程即终止，后续步骤不可能再运行。
    ordered = [s for s in svc_list if s != "host"] + [s for s in svc_list if s == "host"]
    for svc in ordered:
        _run_restart(svc)


def _parse_hotfix_request(client: socket.socket, headers: dict[str, str], head_raw: bytes):
    """公共前段：管理员校验 + 读体 + JSON 解析。

    返回 (req_dict, None) 或 (None, response_bytes)。
    """
    if headers.get("x-trim-isadmin", "").lower() != "true":
        return None, _json_response(403, {"ok": False, "error": "仅管理员可执行热更新"})
    body = _read_full_body(client, headers, head_raw, max_body=HOST_HOTFIX_MAX_BODY)
    if body is None:
        return None, _json_response(413, {"ok": False, "error": "请求体无效或超过 8MB 上限"})
    try:
        req = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, _json_response(400, {"ok": False, "error": "请求体必须是 JSON"})
    if not isinstance(req, dict):
        return None, _json_response(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
    return req, None


def handle_host_hotfix(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/host-hotfix：把一批代码文件写入宿主 repo，并按需重启服务。

    请求体：
      {"version": "2.6.21",
       "files": [{"path": "/repo/webui-service/app.py",
                  "sha256": "<hex>", "content_b64": "<base64>"}],
       "restart": ["webui"]}

    响应：
      {"ok": true, "applied": 1, "backup": "...", "restarted": ["webui"],
       "need_reload_browser": false, "version_file_written": true}

    安全约束（这是特权入口，规矩与 host-upgrade 同级）：
      - 必须是管理员（网关注入的 X-Trim-Isadmin: true）；
      - 请求体 ≤ 8MB；文件数 ≤ HOTFIX_MAX_FILES；
      - 路径三重白名单（允许前缀 + 允许后缀 + 禁止项）且 resolve 后不得逃逸出 repo；
      - 每个文件的 sha256 必须与 base64 内容自洽（防传输损坏/被改）；
      - 版本必须**严格递增**（基线 = max(应用中心版本, VERSION, .hotfix-version)）；
      - **先全量预检、后写入** —— 避免"改一半失败"；
      - 写入前整批备份到 .hotfix-backup/<ver>/ 并写 MANIFEST.json，可原样回滚；
      - **成功判据是"写入后逐个复查 sha256 与请求一致"**，不是"没抛异常"；
      - restart 只接受白名单服务名（host/webui/musicdl/musicbox/lxmusic），
        未知名字忽略，subprocess 不带 shell；若含 host，则**先回包再重启**
        （本进程就是被重启的对象，同步执行会导致调用方收不到响应）。
    """
    handed_off = False
    try:
        req, err = _parse_hotfix_request(client, headers, head_raw)
        if err is not None:
            client.sendall(err)
            return
        handed_off = _run_hotfix(client, req)
    except OSError:
        pass
    finally:
        if not handed_off:
            try:
                client.close()
            except OSError:
                pass


def _run_hotfix(client: socket.socket, req: dict) -> bool:
    """执行热更新的写入/备份/重启核心（被 host-hotfix 与 host-auto-update 共用）。

    返回 True 表示响应已移交给后台重启线程（调用方**不得**再关闭 client）；
    返回 False 表示已同步回包或出错，调用方负责关闭 client。
    """
    handed_off = False
    try:
        ver = str(req.get("version") or "").strip()
        files = req.get("files")
        if not isinstance(files, list) or not files:
            client.sendall(_json_response(400, {"ok": False, "error": "files 必须是非空数组"}))
            return
        if len(files) > HOTFIX_MAX_FILES:
            client.sendall(_json_response(400, {"ok": False,
                                                "error": f"文件数超过上限 {HOTFIX_MAX_FILES}"}))
            return

        # 版本严格递增
        base = _current_hotfix_base()
        if not ver or not re.search(r"\d", ver):
            client.sendall(_json_response(400, {"ok": False, "error": "version 必须是有效版本号"}))
            return
        if base and _ver_tuple(ver) <= _ver_tuple(base):
            client.sendall(_json_response(409, {
                "ok": False, "current": base,
                "error": f"目标版本 v{ver} 必须大于当前 v{base}"}))
            return

        # 全量预检
        plan: list[tuple[str, Path, bytes]] = []
        seen: set[str] = set()
        for i, item in enumerate(files):
            if not isinstance(item, dict):
                client.sendall(_json_response(400, {"ok": False, "error": f"files[{i}] 不是对象"}))
                return
            cp = str(item.get("path") or "")
            sha = str(item.get("sha256") or "").lower()
            b64 = str(item.get("content_b64") or "")
            if not cp:
                client.sendall(_json_response(400, {"ok": False, "error": f"files[{i}].path 为空"}))
                return
            if cp in seen:
                client.sendall(_json_response(400, {"ok": False, "error": f"路径重复：{cp}"}))
                return
            seen.add(cp)
            hp = _container_to_host(cp)
            if hp is None:
                client.sendall(_json_response(400, {
                    "ok": False, "error": f"路径必须以 {CONTAINER_REPO_PREFIX}/ 开头且不含 ..：{cp}"}))
                return
            why = _hotfix_path_ok(cp, hp)
            if why:
                client.sendall(_json_response(403, {"ok": False, "error": why}))
                return
            try:
                raw = base64.b64decode(b64, validate=True)
            except Exception:  # noqa: BLE001
                client.sendall(_json_response(400, {"ok": False,
                                                    "error": f"content_b64 不是合法 base64：{cp}"}))
                return
            if _sha256_of(raw) != sha:
                client.sendall(_json_response(400, {"ok": False,
                                                    "error": f"sha256 与内容不符：{cp}"}))
                return
            plan.append((cp, hp, raw))

        # 备份（整批一次性，便于回滚）
        bdir = HOTFIX_BACKUP_ROOT / ver
        try:
            bdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            client.sendall(_json_response(500, {"ok": False, "error": f"备份目录不可用: {exc}"}))
            return
        backed: list[str] = []
        for cp, hp, _raw in plan:
            if hp.is_file():
                try:
                    rel = hp.relative_to(HOST_REPO_ROOT)
                    dst = bdir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(hp), str(dst))
                    backed.append(str(rel))
                except OSError as exc:
                    client.sendall(_json_response(500, {
                        "ok": False, "error": f"备份失败 {cp}: {exc}"}))
                    return
        restart_list = [s for s in (req.get("restart") or [])
                        if isinstance(s, str) and s in HOTFIX_RESTART_TARGETS]
        try:
            (bdir / "MANIFEST.json").write_text(json.dumps({
                "from_version": base,
                "to_version": ver,
                "files": [str(hp.relative_to(HOST_REPO_ROOT)) for _cp, hp, _r in plan],
                "backed_up": backed,
                "restart": restart_list,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            client.sendall(_json_response(500, {"ok": False, "error": f"写 MANIFEST 失败: {exc}"}))
            return

        # 写入 + 复查（成功判据：读回来的 sha256 与请求一致）
        for cp, hp, raw in plan:
            try:
                hp.parent.mkdir(parents=True, exist_ok=True)
                hp.write_bytes(raw)
            except OSError as exc:
                client.sendall(_json_response(500, {"ok": False, "error": f"写入失败 {cp}: {exc}"}))
                return
        for cp, hp, raw in plan:
            try:
                got = _sha256_of(hp.read_bytes())
            except OSError as exc:
                client.sendall(_json_response(500, {"ok": False,
                                                    "error": f"回读失败 {cp}: {exc}"}))
                return
            if got != _sha256_of(raw):
                client.sendall(_json_response(500, {
                    "ok": False, "error": f"回写校验失败（内容与请求不符）：{cp}"}))
                return

        # 更新热更新版本基线
        version_file_ok = False
        try:
            HOTFIX_VERSION_FILE.write_text(ver + "\n", encoding="utf-8")
            # 同步写 /repo/VERSION：UI 版本号（webui-service 的 /api/version）按请求
            # 实时读取该文件，若不写，热更新装完后界面仍显示旧版本号。
            if re.fullmatch(r"\d+\.\d+\.\d+", ver):
                (HOST_REPO_ROOT / "VERSION").write_text(ver + "\n", encoding="utf-8")
                version_file_ok = True
        except OSError:
            pass  # 不影响本次生效，只影响下次递增判断 / UI 版本显示

        need_reload = any(str(hp).endswith((".js", ".css", ".html")) for _c, hp, _r in plan)
        payload = _json_response(200, {
            "ok": True, "applied": len(plan), "version": ver,
            "backup": str(bdir), "restarted": restart_list,
            "need_reload_browser": need_reload,
            "version_file_written": version_file_ok,
        })
        # 若需要重启 host，则必须"先回包再重启" —— 见 _restart_after_response 的说明。
        # 交给后台线程，且把 client 的关闭责任一并移交，避免 finally 提前关掉它。
        if restart_list:
            handed_off = True
            threading.Thread(
                target=_restart_after_response,
                args=(client, restart_list, payload),
                daemon=True,
            ).start()
            return
        client.sendall(payload)
    except OSError:
        pass
    return handed_off


def handle_host_hotfix_rollback(client: socket.socket, headers: dict[str, str],
                               head_raw: bytes) -> None:
    """POST /api/host-hotfix-rollback：按 .hotfix-backup/<ver>/MANIFEST.json 复原。

    请求体：{"version": "2.6.21"}
    响应：  {"ok": true, "restored": ["proxy/app.py"], "restarted": ["host"]}

    约束与 host-hotfix 同级：管理员、只认白名单路径、只读备份目录里的文件。
    """
    handed_off = False
    try:
        req, err = _parse_hotfix_request(client, headers, head_raw)
        if err is not None:
            client.sendall(err)
            return
        ver = str(req.get("version") or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z._-]+", ver):
            client.sendall(_json_response(400, {"ok": False, "error": "version 不合法"}))
            return
        bdir = HOTFIX_BACKUP_ROOT / ver
        mpath = bdir / "MANIFEST.json"
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except OSError:
            client.sendall(_json_response(404, {"ok": False, "error": f"没有 v{ver} 的备份"}))
            return
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(500, {"ok": False, "error": "MANIFEST 解析失败"}))
            return

        rels = manifest.get("backed_up") or []
        restored: list[str] = []
        for rel in rels:
            rel = str(rel)
            if ".." in Path(rel).parts:
                continue
            src = bdir.joinpath(*Path(rel).parts)
            dst = HOST_REPO_ROOT.joinpath(*Path(rel).parts)
            root = HOST_REPO_ROOT.resolve()
            try:
                if root not in dst.resolve().parents and dst.resolve() != root:
                    continue
                if not src.is_file():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                restored.append(rel)
            except OSError as exc:
                client.sendall(_json_response(500, {"ok": False, "error": f"复原失败 {rel}: {exc}"}))
                return
        restart_list = [s for s in (manifest.get("restart") or [])
                        if str(s) in HOTFIX_RESTART_TARGETS]
        payload = _json_response(200, {
            "ok": True, "version": ver, "restored": restored,
            "restarted": restart_list})
        # 同 host-hotfix：含重启时先回包再重启（本进程可能正是被重启的对象）
        if restart_list:
            handed_off = True
            threading.Thread(
                target=_restart_after_response,
                args=(client, restart_list, payload),
                daemon=True,
            ).start()
            return
        client.sendall(payload)
    except OSError:
        pass
    finally:
        if not handed_off:
            try:
                client.close()
            except OSError:
                pass


# ── host-auto-update：一键在线热更新（网关自己拉包 + 校验 + 应用） ──────────────
#
# 用户侧零下载：前端只发 {"target":"2.7.3"}，网关查 GitHub/Gitee release 的
# fnmusic-ext-<ver>-hotfix.tar.gz + 同包 .sha256 → 下载 → 用官方 .sha256 验真 →
# 解包读 payload → 复用 _run_hotfix 应用。复用 host-hotfix 的全部安全边界
# （路径白名单 / 版本严格递增 / 写后复查 / 自动备份 / 重启规则）。
HOST_AUTO_UPDATE_PATHS = ("/app/fnmusic-ext/api/host-auto-update", "/api/host-auto-update")

# 官方发行仓库（硬编码，不信任客户端传入的 URL，杜绝指向任意地址拉代码）
_AUTO_GH = ("haonanren118", "fnos_music_ext")
_AUTO_GITEE = ("yygitee118", "fnos_music_ext")


class _HotfixFetchError(Exception):
    pass


def _gateway_github_token() -> str:
    """只读令牌：环境变量 > 令牌文件（与 webui-service 同源，互不重复读取）。"""
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        return tok
    for raw in ("/data/config/github_token.txt",
                str(HOST_REPO_ROOT / "sources-data" / "config" / "github_token.txt")):
        try:
            tok = Path(raw).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            return tok
    return ""


def _http_get_json(url: str, token: str = "") -> "dict | None":
    headers = {"User-Agent": "fnmusic-ext", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 404 / 限流 / 空响应一律视为"不可用"
        print(f"[hotfix] GET {url} failed: {exc}", file=sys.stderr)
        return None
    return obj if isinstance(obj, dict) else None


def _http_download(url: str, token: str = "") -> bytes:
    headers = {"User-Agent": "fnmusic-ext"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _find_hotfix_assets(release: dict, target: str) -> "tuple[str | None, str | None]":
    """从一条 release 里挑出 hotfix 包与其 sha256 的资产下载地址。"""
    tar_url = sha_url = None
    need_tar = f"fnmusic-ext-{target}-hotfix.tar.gz"
    need_sha = need_tar + ".sha256"
    for a in (release.get("assets") or []):
        if not isinstance(a, dict):
            continue
        n = str(a.get("name") or "")
        u = str(a.get("browser_download_url") or a.get("url") or "")
        if n == need_tar:
            tar_url = u
        elif n == need_sha:
            sha_url = u
    return tar_url, sha_url


def _fetch_hotfix_package(target: str) -> dict:
    """拉取并校验目标版本的热更新包，返回 {"files": {rel: bytes}, "restart": [...]}。

    任一源找不到包 / 校验失败都抛 _HotfixFetchError；调用方转成 502。
    """
    token = _gateway_github_token()
    tar_url = sha_url = None
    # 1) GitHub（带 Bearer 提额）；2) 失败回退 Gitee 镜像
    try:
        rel = _http_get_json(
            f"https://api.github.com/repos/{_AUTO_GH[0]}/{_AUTO_GH[1]}/releases/tags/v{target}",
            token)
        if rel:
            tar_url, sha_url = _find_hotfix_assets(rel, target)
    except Exception as exc:  # noqa: BLE001
        print(f"[hotfix] github lookup v{target} failed: {exc}", file=sys.stderr)
    if not tar_url:
        try:
            rel = _http_get_json(
                f"https://gitee.com/api/v5/repos/{_AUTO_GITEE[0]}/{_AUTO_GITEE[1]}/releases/tags/v{target}")
            if rel:
                tar_url, sha_url = _find_hotfix_assets(rel, target)
        except Exception as exc:  # noqa: BLE001
            print(f"[hotfix] gitee lookup v{target} failed: {exc}", file=sys.stderr)
    if not tar_url:
        raise _HotfixFetchError(f"GitHub / Gitee 均未找到 v{target} 的热更新包")

    raw = _http_download(tar_url, token)
    if sha_url:
        expected = _http_download(sha_url, token).decode("utf-8").split()[0].strip().lower()
        if _sha256_of(raw) != expected:
            raise _HotfixFetchError("热更新包 sha256 与官方 .sha256 不符")

    files: "dict[str, bytes]" = {}
    restart: list = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            manifest = None
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                data = tf.extractfile(m).read()
                if m.name == "MANIFEST.json":
                    manifest = json.loads(data.decode("utf-8"))
                    continue
                if m.name.startswith("payload/"):
                    rel = m.name[len("payload/"):]
                    if rel:
                        files[rel] = data
            if manifest:
                restart = manifest.get("restart") or []
    except tarfile.TarError as exc:
        raise _HotfixFetchError(f"解包失败：{exc}")
    if not files:
        raise _HotfixFetchError("包内没有 payload 文件")
    return {"files": files, "restart": restart}


def handle_host_auto_update(client: socket.socket, headers: dict[str, str],
                            head_raw: bytes) -> None:
    """POST /api/host-auto-update：一键在线热更新。

    请求体：{"target": "2.7.3"}
    成功复用 _run_hotfix 的响应体（applied/backup/restarted/...）。

    安全：target 必须严格大于当前基线；包只从官方仓库 release 拉取，且用官方
    .sha256 验真后才解包；之后与手动 hotfix 走完全相同的应用与备份路径。
    """
    handed_off = False
    try:
        req, err = _parse_hotfix_request(client, headers, head_raw)
        if err is not None:
            client.sendall(err)
            return
        target = str((req or {}).get("target") or "").strip().lstrip("vV")
        if not target or not re.search(r"\d", target):
            client.sendall(_json_response(400, {"ok": False, "error": "target 必须是有效版本号"}))
            return
        base = _current_hotfix_base()
        if base and _ver_tuple(target) <= _ver_tuple(base):
            client.sendall(_json_response(409, {
                "ok": False, "current": base,
                "error": f"目标版本 v{target} 必须大于当前 v{base}"}))
            return
        try:
            pkg = _fetch_hotfix_package(target)
        except _HotfixFetchError as e:
            client.sendall(_json_response(502, {"ok": False, "error": f"拉取热更新包失败：{e}"}))
            return
        except Exception as e:  # noqa: BLE001
            client.sendall(_json_response(502, {"ok": False, "error": f"拉取热更新包异常：{e}"}))
            return

        files = [{"path": f"/repo/{rel}",
                  "sha256": _sha256_of(raw),
                  "content_b64": base64.b64encode(raw).decode("ascii")}
                 for rel, raw in pkg["files"].items()]
        req2 = {"version": target, "files": files, "restart": pkg.get("restart") or []}
        handed_off = _run_hotfix(client, req2)
    except OSError:
        pass
    finally:
        if not handed_off:
            try:
                client.close()
            except OSError:
                pass


def _is_host_hotfix_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_HOTFIX_PATHS)


def _is_host_hotfix_rollback_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_HOTFIX_ROLLBACK_PATHS)


def _is_host_auto_update_request(head_raw: bytes) -> bool:
    return _is_post_to(head_raw, HOST_AUTO_UPDATE_PATHS)


def _handle(client: socket.socket, upstream: tuple[str, int]) -> None:
    head = _read_request_head(client)
    if head is None:
        client.close()
        return
    head_raw, headers = head
    if _is_host_file_request(head_raw):
        handle_host_file(client, headers, head_raw)
        return
    if _is_host_upgrade_request(head_raw):
        handle_host_upgrade(client, headers, head_raw)
        return
    if _is_host_publish_request(head_raw):
        handle_host_publish(client, headers, head_raw)
        return
    if _is_host_hotfix_request(head_raw):
        handle_host_hotfix(client, headers, head_raw)
        return
    if _is_host_hotfix_rollback_request(head_raw):
        handle_host_hotfix_rollback(client, headers, head_raw)
        return
    if _is_host_auto_update_request(head_raw):
        handle_host_auto_update(client, headers, head_raw)
        return
    try:
        remote = socket.create_connection(upstream, timeout=CONNECT_TIMEOUT)
        remote.settimeout(RELAY_TIMEOUT)
    except OSError:
        client.close()
        return
    left = threading.Thread(target=_relay, args=(client, remote, head_raw), daemon=True)
    right = threading.Thread(target=_relay, args=(remote, client), daemon=True)
    left.start()
    right.start()
    left.join()
    right.join()
    client.close()
    remote.close()


def _stop_previous() -> None:
    if not PID_FILE.is_file():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    base = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    path = socket_path_for(base)
    _stop_previous()
    _daemonize()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        serve(path)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
