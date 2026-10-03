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

这两个端点要求网关注入的 X-Trim-Isadmin: true。其余请求一律原样转发，不动字节。
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
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

# 容器把仓库挂在 /repo，本进程在宿主 —— 收到容器发来的路径要按前缀换算。
CONTAINER_REPO_PREFIX = "/repo"
HOST_REPO_ROOT = Path("/vol1/@appcenter/fnmusic-ext/repo")
# 容器内 WebUI 用它连宿主 root 网关（同在 /repo 下，容器可见）
UPGRADE_SOCK_NAME = ".upgrade-gw.sock"


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


def _read_full_body(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> "bytes | None":
    """按 Content-Length 收齐 POST 体（小请求：仅路径字符串）。"""
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length < 0 or length > HOST_FILE_MAX_BODY:
        return None
    head_end = head_raw.find(b"\r\n\r\n") + 4
    body = head_raw[head_end:]
    while len(body) < length:
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
