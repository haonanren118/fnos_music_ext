"""标准音质转码会话：ffmpeg fMP4 HLS 生产与生命周期。

官方 App 选"标准音质"后不再直拉原文件，改走服务端转码 HLS：
POST /track/transcode → GET /track/hls/{guid}/preset.m3u8 → 逐拉 init.mp4 + 000NN.m4s
（fMP4/AAC），期间 10s 心跳、停止 quit。本模块把在线曲目转成同款规格：
ffmpeg 输出 fMP4 分片到 cache/hls/{safe_guid}/，playlist 由服务端按时长
预合成完整 VOD 清单（实测 App 只拉一次 playlist，不能等转码完再给）。

设计要点：
- 分片用 -hls_flags temp_file：ffmpeg 先写 .tmp 再改名，文件出现即完整，
  服务端无需猜测分片是否写完。
- 合成分片数取 ceil(duration/hls_time)：ffmpeg 每片只多不少，ceil 才覆盖全部音频。
  取 floor 会让清单短一片 → App 播到声明末尾即判整曲结束、提前切歌（实测事故）。
  ⚠ 该估算仅在**转码中**使用；done 后一律改用 ffmpeg 真实清单（见 playlist_text）。
- ⚠ 禁止给 ffmpeg 加 -hls_init_time：它会让 -hls_time 完全失效（实测片长被压到
  init_time 的值），磁盘片数暴涨而清单仍按 hls_time 算，直接造成上述提前切歌。
- 会话 heartbeat 续期；超时 / quit 杀进程；完整缓存在配额内 LRU 复用，
  半成品目录（running 中被杀 / failed）直接清理不残留。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field

logger = logging.getLogger("fnmusic-ext.transcode")

_SEGMENT_RE = re.compile(r"^\d{5}\.m4s$")
INIT_NAME = "init.mp4"
PLAYLIST_NAME = "preset.m3u8"
STATE_NAME = "state.json"

# 测试注入点：假 ffmpeg 脚本路径（ pytest 用，生产读环境探测）
FFMPEG_BIN = os.environ.get("FNMUSIC_FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE_BIN = os.environ.get("FNMUSIC_FFPROBE_BIN") or shutil.which("ffprobe")


@dataclass
class Session:
    guid: str
    directory: str
    duration_s: float
    hls_time: float
    declared_count: int
    bitrate: str = "128k"
    proc: asyncio.subprocess.Process | None = None
    status: str = "starting"          # starting|running|done|failed|aborted
    last_beat: float = field(default_factory=time.monotonic)
    started_ts: float = field(default_factory=time.time)
    done_ts: float = 0.0
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    watcher: asyncio.Task | None = None


# guid → Session；仅存活进程在内，done 会话按磁盘 state.json 复用
_SESSIONS: dict[str, Session] = {}
_SLOT_COND: asyncio.Condition | None = None
_ACTIVE = 0


def _slot_cond() -> asyncio.Condition:
    global _SLOT_COND
    if _SLOT_COND is None:
        _SLOT_COND = asyncio.Condition()
    return _SLOT_COND


def session_dir(root: str, guid: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)
    return os.path.join(root, "hls", safe)


def _read_state(directory: str) -> dict:
    try:
        with open(os.path.join(directory, STATE_NAME), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(sess: Session) -> None:
    try:
        with open(os.path.join(sess.directory, STATE_NAME), "w", encoding="utf-8") as f:
            json.dump({
                "guid": sess.guid,
                "status": sess.status,
                "duration_s": sess.duration_s,
                "hls_time": sess.hls_time,
                "declared_count": sess.declared_count,
                "bitrate": sess.bitrate,
                "started_ts": sess.started_ts,
                "done_ts": sess.done_ts,
            }, f)
    except Exception as e:  # noqa: BLE001
        logger.warning("write state failed for %s: %s", sess.guid, e)


def _remove_dir(directory: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _usable_cache(directory: str) -> dict | None:
    """磁盘上已完整（done + init 存在）的转码缓存，返回 state。"""
    state = _read_state(directory)
    if state.get("status") != "done":
        return None
    if not os.path.isfile(os.path.join(directory, INIT_NAME)):
        return None
    return state


def _declared_count(duration_s: float, hls_time: float) -> int:
    """预合成分片数（仅转码中使用；done 后一律用 ffmpeg 真实清单）。

    取 ceil：ffmpeg 每片时长只多不少（关键帧对齐），实际片数 = ceil(duration/hls_time)。
    取 floor 会让清单**短一片**，App 播到声明末尾就认为整曲结束 → 提前切下一首
    （2026-10-07 实测：185s 音频切出 19 片，floor 只声明 18 片）。
    多声明一片的最坏后果仅是该分片 404，远轻于提前掐掉尾音。
    """
    if duration_s <= 0 or hls_time <= 0:
        return 1
    return max(1, int(math.ceil(duration_s / hls_time)))


def _ffmpeg_playlist_as_vod(directory: str) -> str | None:
    """转码已完成：直接采用 ffmpeg 自己写的 preset.m3u8（片数与时长 100% 真实）。

    ffmpeg 写的是 EVENT 清单（转码中会持续变长，故仅 done 后可用）；这里改成
    VOD 返回，去掉 temp_file 等无关行。任何解析失败返回 None，退回预合成，
    绝不让「服务端猜的清单」与磁盘真实分片脱节（历史 bug：见下 playlist_text）。
    """
    try:
        with open(os.path.join(directory, PLAYLIST_NAME), encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if "#EXT-X-ENDLIST" not in raw:
        return None

    # 逐行处理：丢掉 ffmpeg 的 EVENT 头（改用 VOD 头重放），正文（EXTINF/分片/
    # MAP）原样保留，ENDLIST 只保留一次且放最后。
    body: list[str] = []
    seen_map = False
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s == "#EXTM3U":
            continue
        if s.startswith("#EXT-X-PLAYLIST-TYPE"):
            continue
        if s.startswith("#EXT-X-MEDIA-SEQUENCE"):
            continue
        if s.startswith("#EXT-X-VERSION"):     # 头里统一重放，避免重复
            continue
        if s.startswith("#EXT-X-ENDLIST"):
            continue
        if s.startswith("#EXT-X-MAP"):
            seen_map = True
        body.append(s)
    if not body or not seen_map:
        return None
    # 任一 #EXTINF 缺少后继分片名则视为残档，退回预合成
    if "#EXTINF" not in raw or ".m4s" not in raw:
        return None

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    # TARGETDURATION 沿用 ffmpeg 的真实值（在 body 里，位置不定，取出来前置）
    td = next((s for s in body if s.startswith("#EXT-X-TARGETDURATION")), None)
    rest = [s for s in body if not s.startswith("#EXT-X-TARGETDURATION")]
    if td:
        lines.append(td)
    lines.extend(rest)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def playlist_text(sess: Session) -> str:
    """按声明分片数预合成完整 VOD playlist（App 只拉一次，必须一次给全）。

    已完成的会话优先返回 ffmpeg 的真实清单——预合成依赖 declared_count 与实际
    切片参数一致，一旦参数被改偏（历史上 -hls_init_time 把片长从 10s 压到 4s），
    预合成清单就会**短于**真实音频，App 播完即提前切歌。
    """
    if sess.status == "done":
        real = _ffmpeg_playlist_as_vod(sess.directory)
        if real:
            return real
    hls_time = sess.hls_time
    count = sess.declared_count
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{int(hls_time) + 1}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for i in range(count):
        seg_len = hls_time if i < count - 1 else max(0.5, sess.duration_s - hls_time * (count - 1))
        lines.append(f"#EXTINF:{seg_len:.3f},")
        lines.append(f"{i:05d}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _ffmpeg_argv(src: str, headers: dict | None, sess: Session, directory: str) -> list[str]:
    argv = [FFMPEG_BIN or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if src.startswith(("http://", "https://")):
        if headers:
            blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            argv += ["-headers", blob]
        ua = (headers or {}).get("User-Agent") or (headers or {}).get("user-agent")
        if ua:
            argv += ["-user_agent", ua]
    argv += [
        "-i", src,
        "-vn", "-map_metadata", "-1",
        "-c:a", "aac", "-b:a", sess.bitrate,
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_playlist_type", "event",
        "-hls_flags", "temp_file+independent_segments",
        "-hls_time", str(sess.hls_time),
        # ⚠ 严禁在此加 -hls_init_time：
        #   实测（ffmpeg 60s 正弦波，2026-10-07）—— `-hls_time 10 -hls_init_time 4`
        #   会把**整轨**都按 4s 切片（16 片），-hls_time 10 完全失效（与 `-hls_time 4`
        #   结果完全一致）；只给 `-hls_time 10` 或两者相等时才是 7 片 / 10s。
        #   片长被压到 4s 后，磁盘实际片数是声明值的 ~2.5 倍，而清单按 10s 预合成，
        #   App 播完声明片数即提前判定整曲结束 → 「没播完就跳下一首」。
        "-hls_segment_filename", os.path.join(directory, "%05d.m4s"),
        os.path.join(directory, PLAYLIST_NAME),
    ]
    return argv


async def _watch(sess: Session) -> None:
    global _ACTIVE
    try:
        rc = await sess.proc.wait()
    except Exception:  # noqa: BLE001
        rc = -1
    finally:
        async with _slot_cond():
            _ACTIVE = max(0, _ACTIVE - 1)
            _slot_cond().notify(1)
    if sess.status in ("aborted",):
        _remove_dir(sess.directory)
    elif rc == 0 and os.path.isfile(os.path.join(sess.directory, INIT_NAME)):
        sess.status = "done"
        sess.done_ts = time.time()
        # 转正：LRU 访问时间 = 完成时间
        _write_state(sess)
        _SESSIONS.pop(sess.guid, None)
        _touch_state(sess.directory)
    else:
        logger.warning("transcode failed rc=%s guid=%s", rc, sess.guid)
        sess.status = "failed"
        _remove_dir(sess.directory)
        _SESSIONS.pop(sess.guid, None)
    sess.exit_event.set()


def _touch_state(directory: str) -> None:
    try:
        os.utime(os.path.join(directory, STATE_NAME), None)
    except OSError:
        pass


async def ensure_session(
    guid: str,
    src: str,
    duration_s: float,
    *,
    root: str,
    bitrate: str = "128k",
    hls_time: float = 10.0,
    max_active: int = 2,
    max_cache_bytes: int = 512 * 1024 * 1024,
    headers: dict | None = None,
) -> Session | None:
    """取或建转码会话；ffmpeg 不可用 / 启动失败返回 None（调用方回落单分片桩）。"""
    directory = session_dir(root, guid)

    alive = _SESSIONS.get(guid)
    if alive and alive.status in ("starting", "running"):
        alive.last_beat = time.monotonic()
        return alive

    state = _usable_cache(directory)
    if state:
        sess = Session(
            guid=guid, directory=directory,
            duration_s=float(state.get("duration_s") or duration_s or 0),
            hls_time=float(state.get("hls_time") or hls_time),
            declared_count=int(state.get("declared_count") or 1),
            bitrate=str(state.get("bitrate") or bitrate),
            status="done",
        )
        _touch_state(directory)
        return sess

    if not FFMPEG_BIN:
        return None
    _reap_expired(root=root, ttl_s=1e18)  # 顺带清半成品目录
    await _enforce_quota(root=root, max_bytes=max_cache_bytes)

    if duration_s <= 0:
        duration_s = await probe_duration(src, headers) or 0.0
    if duration_s <= 0:
        duration_s = 240.0

    global _ACTIVE
    async with _slot_cond():
        waited = 0.0
        while _ACTIVE >= max(1, max_active) and waited < 120:
            try:
                await asyncio.wait_for(_slot_cond().wait(), timeout=10.0)
            except asyncio.TimeoutError:
                waited += 10.0
        # 排队超时仍放行：超并发转码好过播放失败
        _ACTIVE += 1

    _remove_dir(directory)
    os.makedirs(directory, exist_ok=True)
    sess = Session(
        guid=guid, directory=directory, duration_s=duration_s,
        hls_time=hls_time, declared_count=_declared_count(duration_s, hls_time),
        bitrate=bitrate,
    )
    _write_state(sess)
    try:
        sess.proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_argv(src, headers, sess, directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ffmpeg spawn failed for %s: %s", guid, e)
        async with _slot_cond():
            _ACTIVE = max(0, _ACTIVE - 1)
            _slot_cond().notify(1)
        _remove_dir(directory)
        return None
    sess.status = "running"
    _SESSIONS[guid] = sess
    sess.watcher = asyncio.create_task(_watch(sess))
    return sess


def get_session(guid: str) -> Session | None:
    sess = _SESSIONS.get(guid)
    if sess:
        sess.last_beat = time.monotonic()
    return sess


def peek_cached(guid: str, root: str) -> Session | None:
    """磁盘上已完整的转码缓存直取（不解析源、不启进程），供重播秒开。"""
    directory = session_dir(root, guid)
    state = _usable_cache(directory)
    if not state:
        return None
    _touch_state(directory)
    return Session(
        guid=guid, directory=directory,
        duration_s=float(state.get("duration_s") or 0),
        hls_time=float(state.get("hls_time") or 10.0),
        declared_count=int(state.get("declared_count") or 1),
        bitrate=str(state.get("bitrate") or "128k"),
        status="done",
    )


def heartbeat(guid: str) -> None:
    sess = _SESSIONS.get(guid)
    if sess:
        sess.last_beat = time.monotonic()


async def quit_session(guid: str) -> None:
    """App 主动结束：杀进程；已完整缓存保留复用，半成品清理。"""
    sess = _SESSIONS.pop(guid, None)
    if not sess or not sess.proc:
        return
    if sess.status in ("done", "failed", "aborted"):
        return
    sess.status = "aborted"
    try:
        sess.proc.kill()
    except ProcessLookupError:
        pass


def _kill_expired(ttl_s: float) -> None:
    now = time.monotonic()
    for guid, sess in list(_SESSIONS.items()):
        if sess.status in ("starting", "running") and now - sess.last_beat > ttl_s:
            logger.info("transcode session TTL expired: %s", guid)
            sess.status = "aborted"
            try:
                sess.proc.kill()
            except (ProcessLookupError, AttributeError):
                pass


def _reap_expired(root: str, ttl_s: float) -> int:
    """TTL 杀进程 + 清理无主半成品目录；返回清理的目录数。"""
    _kill_expired(ttl_s)
    removed = 0
    hls_root = os.path.join(root, "hls")
    if not os.path.isdir(hls_root):
        return 0
    for name in os.listdir(hls_root):
        d = os.path.join(hls_root, name)
        if not os.path.isdir(d):
            continue
        if _usable_cache(d):
            continue
        # 半成品且无在管进程（内存注册表里没有指向它的会话）
        if not any(s.directory == d for s in _SESSIONS.values()):
            _remove_dir(d)
            removed += 1
    return removed


def _dir_size(directory: str) -> int:
    total = 0
    for name in os.listdir(directory):
        try:
            total += os.path.getsize(os.path.join(directory, name))
        except OSError:
            pass
    return total


async def _enforce_quota(root: str, max_bytes: int) -> None:
    """超出配额时按 state.json 的 atime（LRU）逐个删完整缓存目录。"""
    hls_root = os.path.join(root, "hls")
    if not os.path.isdir(hls_root):
        return
    entries = []
    total = 0
    for name in os.listdir(hls_root):
        d = os.path.join(hls_root, name)
        if not os.path.isdir(d) or not _usable_cache(d):
            continue
        size = _dir_size(d)
        try:
            atime = os.path.getmtime(os.path.join(d, STATE_NAME))
        except OSError:
            atime = 0
        entries.append((atime, d, size))
        total += size
    if total <= max_bytes:
        return
    for atime, d, size in sorted(entries):
        if total <= max_bytes:
            break
        logger.info("transcode cache LRU evict: %s (%d bytes)", d, size)
        _remove_dir(d)
        total -= size


async def probe_duration(src: str, headers: dict | None = None) -> float:
    """ffprobe 取时长（秒）；失败返回 0。"""
    if not FFPROBE_BIN:
        return 0.0
    argv = [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", src]
    if src.startswith(("http://", "https://")) and headers:
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        argv[2:2] = ["-headers", blob]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        return float(out.decode().strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def valid_segment_name(filename: str) -> bool:
    return filename == INIT_NAME or bool(_SEGMENT_RE.match(filename))


async def wait_file(path: str, sess: Session | None, timeout: float = 30.0) -> str | None:
    """等分片落盘（temp_file 改名后出现即完整）；会话失败立即返回。"""
    deadline = time.monotonic() + timeout
    while True:
        if os.path.isfile(path):
            return path
        if sess and sess.exit_event.is_set() and sess.status != "done":
            return None
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def maintain(root: str, ttl_s: float, max_bytes: int) -> None:
    """心跳/播放请求顺带触发的维护：TTL 回收 + 配额 LRU。"""
    _reap_expired(root=root, ttl_s=ttl_s)
    await _enforce_quota(root=root, max_bytes=max_bytes)


async def shutdown_all() -> None:
    for guid in list(_SESSIONS):
        await quit_session(guid)
