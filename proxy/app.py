"""fnmusic-ext 拦截代理 (FastAPI + httpx).

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，并行 musicdl）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
from uuid import uuid4

import httpx
import anyio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

try:
    from . import recommend as dailyrec
    from . import nmplaylists as nmpl
    from . import publicplaylists as pubpl
    from . import transcode as tc
    from .cache_gc import purge_rolling, sweep_orphan_lyrics
    from .env_merge import parse_env_file
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore
    import nmplaylists as nmpl  # type: ignore
    import publicplaylists as pubpl  # type: ignore
    import transcode as tc  # type: ignore
    from cache_gc import purge_rolling, sweep_orphan_lyrics  # type: ignore
    from env_merge import parse_env_file  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_HOME = dailyrec.home_dir()

# lx 平台别名 → 规范代码（与 lxmusic-service/app.py 的 _SOURCE_ALIASES 保持一致）
_LX_ALIASES = {
    "kg": "kg",
    "kugou": "kg",
    "wy": "wy",
    "netease": "wy",
    "163": "wy",
    "mg": "mg",
    "migu": "mg",
    "tx": "tx",
    "qq": "tx",
    "tencent": "tx",
    "kw": "kw",
    "kuwo": "kw",
}


def _normalize_lx_sources(raw: str) -> list[str]:
    """LX_SOURCES 环境变量 → 规范平台代码列表；空 = 不限制（跟随 lx 服务配置）。"""
    out: list[str] = []
    for part in (raw or "").split(","):
        code = _LX_ALIASES.get(part.strip().lower(), "")
        if code and code not in out:
            out.append(code)
    return out


CONF = {
    "musicdl_url": os.environ.get("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "lx_url": os.environ.get("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
    "musicdl_enabled": os.environ.get("FNMUSIC_MUSICDL_ENABLED", "false").lower() in ("true", "1", "yes"),
    "netease_enabled": os.environ.get("FNMUSIC_NETEASE_ENABLED", "false").lower() in ("true", "1", "yes"),
    "lx_enabled": os.environ.get("FNMUSIC_LX_ENABLED", "false").lower() in ("true", "1", "yes"),
    "lx_search_limit": int(os.environ.get("FNMUSIC_LX_SEARCH_LIMIT", "20")),
    "lx_quality": os.environ.get("FNMUSIC_LX_QUALITY", "lossless"),
    "netease_wait_s": float(os.environ.get("FNMUSIC_NETEASE_WAIT_S", "3.0")),
    # 输入停满这么久才向音源发起搜索。窗口内的新词会替换旧词并重新计时。
    "search_debounce_s": float(os.environ.get("FNMUSIC_SEARCH_DEBOUNCE_S", "1.0")),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": int(os.environ.get("FNMUSIC_NETEASE_SEARCH_LIMIT", "50")),
    # VIP/付费专辑曲目在歌名前加 [VIP] 提示（纯展示，不影响可播性判定）
    "show_vip_tag": os.environ.get("FNMUSIC_SHOW_VIP_TAG", "true").lower() in ("true", "1", "yes"),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": int(os.environ.get("FNMUSIC_ONLINE_LIMIT", "30")),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    # 飞牛官方封面缓存根目录：官方把本地曲库的专辑/歌手/曲目封面按
    # cover/{album,artist,track}/<guid前2位>/<guid>[_wNNN.jpg] 落盘。
    # 官方 /static/cover 只服务在线曲目，对本地 guid 固定 400，故本地展示页
    # （专辑页/歌手页/曲库列表）的封面直接读这些文件——比任何在线反查都快且
    # 零外部依赖。留空则按若干已知安装路径自动探测。
    "official_cover_dir": os.environ.get("FNMUSIC_OFFICIAL_COVER_DIR", ""),

    # 边听边存：默认开；保存路径空=自动探测飞牛共享曲库，不可用自动回退；
    # tee_cache_max 仅在关闭边听边存时生效（滚动保留最新 N 首试听缓存）
    "tee_save_enabled": os.environ.get("FNMUSIC_TEE_SAVE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "tee_save_dir": os.environ.get("FNMUSIC_TEE_SAVE_DIR", ""),
    "tee_cache_max": int(os.environ.get("FNMUSIC_TEE_CACHE_MAX", "2")),
    # 收藏/加入歌单自动绑定本地：默认关；在线歌曲收藏后后台自动整轨下载并绑定本地文件，
    # 官方曲库收录后再把收藏/歌单写进官方（成功后删本地映射，避免双列表重复显示）
    "fav_auto_bind": os.environ.get("FNMUSIC_FAV_AUTO_BIND", "false").lower() in ("true", "1", "yes"),
    # 官方绑定轮询窗口（秒）：下载落库后等待官方扫入库并完成官方写入的时间上限
    "official_bind_timeout_s": float(os.environ.get("FNMUSIC_OFFICIAL_BIND_TIMEOUT_S", "120")),
    # 边听边存切歌续传：允许并行完成的续传任务上限（防快速跳歌时的下载洪泛；0=关闭续传）
    "tee_handoff_max": int(os.environ.get("FNMUSIC_TEE_HANDOFF_MAX", "3")),
    # 自动下载封面：自动下载的音乐完整落库后，把源站封面内嵌进音频文件。
    # 飞牛扫描器只认内嵌图（dhowden/tag，无外置 cover.jpg 能力），这是官方
    # App 显示下载歌曲封面的唯一途径；封面在音乐落库成功后才处理，不存在
    # 音乐失败、封面先落污染目录的情况
    "auto_cover": os.environ.get("FNMUSIC_AUTO_COVER", "true").lower() in ("true", "1", "yes"),
    # 自动下载歌词（2.6.0）：默认关；仅边听边存开启时生效——音乐完整落库
    # 成功后才下载同名 .lrc 到歌曲所在目录（飞牛扫描入库即带歌词），音乐
    # 下载失败不产生任何歌词文件，不存在歌词先落污染目录的情况
    "lyric_auto_dl": os.environ.get("FNMUSIC_LYRIC_AUTO_DL", "false").lower() in ("true", "1", "yes"),
    # 在线取流 Range 探针：记录每条在线 /track/stream 的 Range 形态与落盘资格，
    # 用于真机确认手机播放器是否按定长窗口取流（那样边听边存永不触发）
    "stream_probe": os.environ.get("FNMUSIC_STREAM_PROBE", "true").lower() in ("true", "1", "yes"),
    # 在线取流启动预算（2026-10-10）：代理必须在「拿到上游直链 + 读出首字节」
    # 之后才敢向客户端回 200，这段等待的额度决定了慢音源能否播出来。
    # 洛雪 /track/url 是「逐档解析(单档 12s) + Range 探活(5s)」的串行流程，
    # 端点总预算 20s（LX_URL_TIMEOUT）；旧值单次 6s / 总 12s 会在洛雪还没
    # 试完候选音源时就掐断 —— 表现为「搜索能看到歌、点播 6 秒后跳下一首」
    # （实测日志：stream probe 20:02:45.751 → Stream startup failed 20:02:51.756，
    #  精确 6.005s，即 min(6.0, remaining) 触发）。
    # 单次额度取 22s：略高于洛雪端点总预算，让洛雪能跑完一次完整降档流程；
    # 总预算 30s：够第一个候选源跑一轮 + 换候选重试一次。
    # 代价：无人可解的曲目要等约 22s 才跳（旧值 6s），但可播的曲目能播出来。
    "stream_startup_timeout": float(os.environ.get("FNMUSIC_STREAM_STARTUP_TIMEOUT", "22")),
    "stream_total_timeout": float(os.environ.get("FNMUSIC_STREAM_TOTAL_TIMEOUT", "30")),
    # 搜索预热（2026-10-10）：搜索响应**返回前**把本页在线曲目的直链丢进后台并发
    # 预解析，结果由洛雪写进自己的缓存（_fresh_probe / _cache_put，TTL 900s）。
    # 用户点播时洛雪直接命中缓存 → 毫秒级出流，不再现场解析（慢源要 5~10s）。
    # 关键：只 create_task，**不 await** —— 搜索响应时间完全不受影响；
    # 失败的预热只影响后台，用户无感。cooldown 防止反复翻页时重复解析同一首。
    "prefetch_enabled": os.environ.get("FNMUSIC_PREFETCH_ENABLED", "true").lower() in ("true", "1", "yes"),
    "prefetch_limit": int(os.environ.get("FNMUSIC_PREFETCH_LIMIT", "10")),
    "prefetch_concurrency": int(os.environ.get("FNMUSIC_PREFETCH_CONCURRENCY", "4")),
    "prefetch_cooldown_s": float(os.environ.get("FNMUSIC_PREFETCH_COOLDOWN_S", "300")),
    # 标准音质转码（App 音质偏好=标准时走 HLS 转码而非直拉原文件）：
    # ffmpeg 输出 AAC fMP4 分片；宿主机无 ffmpeg 自动回落单分片桩
    "transcode_enabled": os.environ.get("FNMUSIC_TRANSCODE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "transcode_bitrate": os.environ.get("FNMUSIC_TRANSCODE_BITRATE", "128k"),
    "transcode_hls_time": float(os.environ.get("FNMUSIC_TRANSCODE_HLS_TIME", "10")),
    "transcode_max_sessions": int(os.environ.get("FNMUSIC_TRANSCODE_MAX_SESSIONS", "2")),
    "transcode_ttl_s": float(os.environ.get("FNMUSIC_TRANSCODE_TTL_S", "90")),
    "transcode_cache_max_mb": int(os.environ.get("FNMUSIC_TRANSCODE_CACHE_MAX_MB", "512")),
    # 下载"标准"档目标码率：官方实测 MP3 320k（prepare quality=standard, FLAC 源 → 320067bps）
    "transcode_dl_bitrate": os.environ.get("FNMUSIC_TRANSCODE_DL_BITRATE", "320k"),
    # 落盘进曲库后通知官方重扫的接口路径（POST）；空=禁用。官方无公开文档，
    # 真机在官方 App 手动点一次扫描、从代理请求日志捕获真实路径后填入启用
    "library_scan_path": (os.environ.get("FNMUSIC_LIBRARY_SCAN_PATH", "") or "").strip(),
    "merge_suggest": os.environ.get("FNMUSIC_MERGE_SUGGEST", "false").lower() in ("true", "1", "yes"),
    "online_sources": os.environ.get("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
    # lx 平台白名单（同 .env 的 LX_SOURCES；install.sh --sources lx-<平台> 写入）；
    # 空 = 不限制。GUID 第 3 段携带平台（online:lx:kg:xxx），据此过滤与透传 ?sources=
    "lx_sources": _normalize_lx_sources(os.environ.get("LX_SOURCES", "")),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": float(os.environ.get("FNMUSIC_SEARCH_TIMEOUT", "15")),
    "search_probe": os.environ.get("FNMUSIC_SEARCH_PROBE", "false").lower() in ("true", "1", "yes"),
    # ★ 2026-10-11 体验改进：搜索结果里把「片段/试听」条目**稳定降到末尾**（只降权不删除），
    #   让完整版优先露出。实测音源会下发「会开花的云 (25秒Live片段)」这类条目，
    #   排在完整版之前，用户容易误点（点进去只有 25 秒）。
    "search_demote_clips": os.environ.get("FNMUSIC_SEARCH_DEMOTE_CLIPS", "true").lower() in ("true", "1", "yes"),
    "search_cache_ttl": float(os.environ.get("FNMUSIC_SEARCH_CACHE_TTL", "604800")),
    "late_page_wait_s": float(os.environ.get("FNMUSIC_LATE_PAGE_WAIT_S", "5.0")),
    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    # 官方歌单内在线附加条目的存储目录（同 fav_dir 按用户分文件）
    "plt_dir": os.environ.get(
        "FNMUSIC_PLT_DIR", os.path.join(_HOME, "playlist_tracks")
    ),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),
    "llm_model": (os.environ.get("FNMUSIC_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini",
    # v2.0.0：音质模式 high|balanced|smooth（档序见 quality_order）；
    # 推荐双开关 / 封面补全 / .env 热重载默认开，均可被 .env 覆盖
    "quality_mode": (os.environ.get("FNMUSIC_QUALITY_MODE") or "high").strip().lower(),
    "recommend_hot": os.environ.get("FNMUSIC_RECOMMEND_HOT", "true").lower() in ("true", "1", "yes"),
    "recommend_daily": os.environ.get("FNMUSIC_RECOMMEND_DAILY", "true").lower() in ("true", "1", "yes"),
    # 公共平台歌单（#55）：默认关。开启后按 SELECTION（管理页预选）把公共歌单
    # 注入音乐页歌单列表（只读虚拟歌单，guid online:playlist:public:<provider>:<source>:<kind>:<id>）
    "public_playlists_enabled": os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_ENABLED", "false").lower() in ("true", "1", "yes"),
    "public_playlists_selection": os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", ""),
    # 网易账号歌单注入（2.6.0）：默认关；需网易盒子启用并扫码登录，
    # 音乐页在热门推荐与官方歌单之间展示账号自建歌单（只读，不回写网易）
    "netease_my_playlists": os.environ.get("FNMUSIC_NETEASE_MY_PLAYLISTS", "false").lower() in ("true", "1", "yes"),
    "cover_enrich": os.environ.get("FNMUSIC_COVER_ENRICH", "true").lower() in ("true", "1", "yes"),
    # 同源回源封面（2.6.5）：默认开。在线/歌单封面原本 302 跳第三方图床直链，
    # 远程 HTTPS 下 App 直连会被混合内容/防盗链/CORS 拦截→全回落波纹占位图
    # （与早前音频直链远程全挂同一根因）。开启后改为服务端同源回源（带浏览器
    # UA/Referer 抓取图床再流式返回），彻底绕开拦截。关闭则沿用旧 302 行为。
    "cover_proxy": os.environ.get("FNMUSIC_COVER_PROXY", "true").lower() in ("true", "1", "yes"),
    "env_watch": os.environ.get("FNMUSIC_ENV_WATCH", "true").lower() in ("true", "1", "yes"),
    # 官方端点取证：未拦截的 /music/api 请求首见 INFO、之后每 50 次采样一条；
    # =detail 时逐条记录。官方 App 更新引入新端点时（如 2.5.0 前的 download/*），
    # 日志可直接看到客户端在调什么，避免"Unknown 元数据"类问题无迹可循
    "trace_forward": os.environ.get("FNMUSIC_TRACE_FORWARD", "").strip().lower() in ("detail", "1", "true", "yes"),
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}
# 每个登录用户当前要搜的词。换词时作废这个用户其余关键词的缓存，不刷新 ts。
_USER_SEARCH_GEN: dict[str, int] = {}
_USER_SEARCH_WORD: dict[str, str] = {}
# 每个用户正在等待的输入窗口。新词到达时 set，让上一词立刻结束等待。
_SEARCH_DEBOUNCE: dict[str, asyncio.Event] = {}
# fetch_* 读这个范围：搜索框用凭证哈希，补链加 ":play"，联想加 ":suggest"。
_FETCH_SCOPE: ContextVar[str] = ContextVar("fnmusic_fetch_scope", default="")
_SCOPE_HEADER = "X-Fnmusic-Scope"


class _SupersededSearch:
    """同一用户的更新关键词替换了这次搜索。"""


_SUPERSEDED = _SupersededSearch()


class SourceSearchGate:
    """一个音源同时只跑一个关键词。每个用户在队列里只留最新词。

    cancellable 时，同一用户的新词取消正在跑的搜索并立刻开始。
    否则等当前请求自然结束，丢掉响应，再搜新词。其他用户按到达顺序排在后面。
    """

    def __init__(self, cancellable: bool):
        self.cancellable = cancellable
        self._epoch = 0
        self._running: dict | None = None
        self._queue: list[dict] = []

    def reset(self) -> None:
        self._epoch += 1
        job = self._running
        self._running = None
        queued = self._queue
        self._queue = []
        if job and job.get("task") and not job["task"].done():
            job["task"].cancel()
        for slot in queued:
            _resolve_waiters(slot, _SUPERSEDED)
        if job:
            _resolve_waiters(job, _SUPERSEDED)

    async def run(self, scope: str, keyword: str, factory):
        fut = asyncio.get_running_loop().create_future()
        self._admit(scope, keyword, factory, fut)
        self._pump()
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            self._detach(fut)
            raise

    def _admit(self, scope: str, keyword: str, factory, fut) -> None:
        running = self._running
        if running and running["scope"] == scope and running["keyword"] == keyword and not running.get("superseded"):
            running["waiters"].append(fut)
            return
        if running and running["scope"] == scope and running["keyword"] != keyword:
            running["superseded"] = True
            _resolve_waiters(running, _SUPERSEDED)
            if self.cancellable:
                task = running.get("task")
                if task and not task.done():
                    task.cancel()
            self._upsert(scope, keyword, factory, fut, front=True)
            return
        self._upsert(scope, keyword, factory, fut, front=False)

    def _upsert(self, scope: str, keyword: str, factory, fut, front: bool) -> None:
        for slot in self._queue:
            if slot["scope"] != scope:
                continue
            if slot["keyword"] != keyword:
                _resolve_waiters(slot, _SUPERSEDED)
                slot["keyword"] = keyword
                slot["factory"] = factory
                slot["waiters"] = [fut]
            else:
                slot["waiters"].append(fut)
            if front:
                self._queue.remove(slot)
                self._queue.insert(0, slot)
            return
        slot = {"scope": scope, "keyword": keyword, "factory": factory, "waiters": [fut]}
        if front:
            self._queue.insert(0, slot)
        else:
            self._queue.append(slot)

    def _detach(self, fut) -> None:
        running = self._running
        if running and fut in running["waiters"]:
            running["waiters"].remove(fut)
            if self.cancellable and not running["waiters"]:
                running["superseded"] = True
                task = running.get("task")
                if task and not task.done():
                    task.cancel()
            return
        for slot in list(self._queue):
            if fut in slot["waiters"]:
                slot["waiters"].remove(fut)
                if not slot["waiters"]:
                    self._queue.remove(slot)
                return

    def _pump(self) -> None:
        if self._running is not None or not self._queue:
            return
        slot = self._queue.pop(0)
        job = {
            "scope": slot["scope"],
            "keyword": slot["keyword"],
            "factory": slot["factory"],
            "waiters": slot["waiters"],
            "superseded": False,
            "task": None,
        }
        job["epoch"] = self._epoch
        self._running = job
        job["task"] = asyncio.get_running_loop().create_task(self._execute(job))

    async def _execute(self, job: dict) -> None:
        result = _SUPERSEDED
        try:
            result = await job["factory"]()
        except asyncio.CancelledError:
            result = _SUPERSEDED
        except Exception as exc:
            result = exc
        # 取消之后不能再 await，否则事件循环会把清理和下一词一起丢掉。
        self._finish(job, result)

    def _finish(self, job: dict, result) -> None:
        if job.get("epoch") != self._epoch:
            _resolve_waiters(job, _SUPERSEDED)
            return
        if self._running is job:
            self._running = None
        if job.get("superseded") or result is _SUPERSEDED:
            _resolve_waiters(job, _SUPERSEDED)
        elif isinstance(result, Exception):
            _resolve_waiters(job, result, error=True)
        else:
            _resolve_waiters(job, result)
        self._pump()


def _resolve_waiters(job: dict, result, error: bool = False) -> None:
    for fut in job.get("waiters", []):
        if fut.done():
            continue
        if error:
            fut.set_exception(result)
        else:
            fut.set_result(result)


def _note_user_keyword(scope: str, keyword: str) -> int:
    """记下这个用户正在搜的词。换词时清掉该用户其他词的缓存，ts 归零而不是续期。"""
    if _USER_SEARCH_WORD.get(scope) != keyword:
        _USER_SEARCH_GEN[scope] = _USER_SEARCH_GEN.get(scope, 0) + 1
        _USER_SEARCH_WORD[scope] = keyword
        for entry in _SEARCH_CACHE.values():
            if entry.get("credentials") == scope and entry.get("keyword") != keyword:
                entry["superseded"] = True
                entry["items"] = []
                entry["ts"] = 0
        pending = _SEARCH_DEBOUNCE.get(scope)
        if pending is not None:
            pending.set()
    return _USER_SEARCH_GEN.get(scope, 1)


async def _wait_search_debounce(scope: str) -> asyncio.Event | None:
    """停手满 search_debounce_s 才返回当前窗口。0 表示不延迟。"""
    delay = float(CONF.get("search_debounce_s") or 0)
    if delay <= 0:
        return None
    event = asyncio.Event()
    _SEARCH_DEBOUNCE[scope] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    return event


def _user_search_stale(entry: dict, scope: str) -> bool:
    return bool(entry.get("superseded")) or entry.get("gen") != _USER_SEARCH_GEN.get(scope)


def reset_source_search_gates() -> None:
    for gate in (_LX_SEARCH_GATE, _MUSICDL_SEARCH_GATE, _MUSICBOX_SEARCH_GATE):
        gate.reset()
    _USER_SEARCH_GEN.clear()
    _USER_SEARCH_WORD.clear()
    _SEARCH_DEBOUNCE.clear()


_LX_SEARCH_GATE = SourceSearchGate(cancellable=True)
_MUSICDL_SEARCH_GATE = SourceSearchGate(cancellable=False)
_MUSICBOX_SEARCH_GATE = SourceSearchGate(cancellable=False)


def _search_ttl(entry: dict) -> float:
    # Backend IDs/URLs are memory scoped; positive results revalidate in 5m.
    # 超时交回的空列表也是 partial。先看有没有歌，避免把 0 条缓存半分钟，
    # 用户紧接着再搜同词还是空的。
    if not entry.get("items"):
        return 10.0
    if entry.get("partial"):
        return 30.0
    return min(float(CONF.get("search_cache_ttl", 604800)), 300.0)


def _clean_search_cache() -> None:
    now = time.time()
    expired = [k for k, v in _SEARCH_CACHE.items() if now - v.get("accessed", v.get("ts", 0)) >= 900]
    if len(_SEARCH_CACHE) > 2000:
        expired += sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k].get("accessed", 0))[:1000]
    for key in expired:
        entry = _SEARCH_CACHE.pop(key, {})
        task = entry.get("task")
        if task and not task.done():
            task.cancel()


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


# === .env 热重载（FNMUSIC_ENV_WATCH=1 默认开） ===
# WebUI 切源 / 手工编辑 .env 后无需重启 proxy：白名单键同步进 CONF 与
# os.environ（recommend 的 LLM 配置直接读环境变量），并清空搜索缓存让
# 新选音源立即生效。路径/端口类配置不在白名单，仍需重启。
_ENV_WATCH_KEYS: dict[str, tuple[str, str]] = {
    "FNMUSIC_MUSICDL_ENABLED": ("musicdl_enabled", "bool"),
    "FNMUSIC_NETEASE_ENABLED": ("netease_enabled", "bool"),
    "FNMUSIC_LX_ENABLED": ("lx_enabled", "bool"),
    "FNMUSIC_ONLINE_SOURCES": ("online_sources", "str"),
    "LX_SOURCES": ("lx_sources", "lx_sources"),
    "FNMUSIC_SEARCH_PROBE": ("search_probe", "bool"),
    "FNMUSIC_QUALITY_MODE": ("quality_mode", "quality_mode"),
    "FNMUSIC_TEE_SAVE_ENABLED": ("tee_save_enabled", "bool"),
    "FNMUSIC_TEE_SAVE_DIR": ("tee_save_dir", "str"),
    "FNMUSIC_TEE_CACHE_MAX": ("tee_cache_max", "tee_cache_max"),
    "FNMUSIC_FAV_AUTO_BIND": ("fav_auto_bind", "bool"),
    "FNMUSIC_OFFICIAL_BIND_TIMEOUT_S": ("official_bind_timeout_s", "bind_timeout"),
    "FNMUSIC_TEE_HANDOFF_MAX": ("tee_handoff_max", "tee_handoff_max"),
    "FNMUSIC_AUTO_COVER": ("auto_cover", "bool"),
    "FNMUSIC_LYRIC_AUTO_DL": ("lyric_auto_dl", "bool"),
    "FNMUSIC_LIBRARY_SCAN_PATH": ("library_scan_path", "str"),
    "FNMUSIC_RECOMMEND_HOT": ("recommend_hot", "bool"),
    "FNMUSIC_RECOMMEND_DAILY": ("recommend_daily", "bool"),
    "FNMUSIC_PUBLIC_PLAYLISTS_ENABLED": ("public_playlists_enabled", "bool"),
    "FNMUSIC_PUBLIC_PLAYLISTS_SELECTION": ("public_playlists_selection", "str"),
    "FNMUSIC_NETEASE_MY_PLAYLISTS": ("netease_my_playlists", "bool"),
    "FNMUSIC_COVER_ENRICH": ("cover_enrich", "bool"),
    "FNMUSIC_COVER_PROXY": ("cover_proxy", "bool"),
    "FNMUSIC_TRACE_FORWARD": ("trace_forward", "bool"),
    "FNMUSIC_TRANSCODE_ENABLED": ("transcode_enabled", "bool"),
    "FNMUSIC_TRANSCODE_BITRATE": ("transcode_bitrate", "str"),
    "FNMUSIC_TRANSCODE_HLS_TIME": ("transcode_hls_time", "seconds"),
    "FNMUSIC_TRANSCODE_MAX_SESSIONS": ("transcode_max_sessions", "tee_cache_max"),
    "FNMUSIC_TRANSCODE_TTL_S": ("transcode_ttl_s", "ttl_seconds"),
    "FNMUSIC_TRANSCODE_CACHE_MAX_MB": ("transcode_cache_max_mb", "mb_int"),
    "FNMUSIC_TRANSCODE_DL_BITRATE": ("transcode_dl_bitrate", "str"),
    "FNMUSIC_LLM_BASE_URL": ("llm_base_url", "llm_url"),
    "FNMUSIC_LLM_API_KEY": ("", "str"),
    "FNMUSIC_LLM_MODEL": ("llm_model", "str"),
    "FNMUSIC_SEARCH_TIMEOUT": ("search_timeout", "seconds"),
    # 在线取流启动预算（2026-10-10）：放进热重载白名单，便于现场调值对比
    "FNMUSIC_STREAM_STARTUP_TIMEOUT": ("stream_startup_timeout", "seconds"),
    "FNMUSIC_STREAM_TOTAL_TIMEOUT": ("stream_total_timeout", "seconds"),
    # 搜索预热（2026-10-10）：可热重载，便于现场开关/调并发
    "FNMUSIC_PREFETCH_ENABLED": ("prefetch_enabled", "bool"),
    "FNMUSIC_PREFETCH_LIMIT": ("prefetch_limit", "tee_cache_max"),
    "FNMUSIC_PREFETCH_CONCURRENCY": ("prefetch_concurrency", "tee_cache_max"),
    # issue #29：推荐构建预算/候选数/逐首校验运行期可调（recommend.py 动态读 os.environ）
    "FNMUSIC_RECOMMEND_BUDGET_S": ("", "str"),
    "FNMUSIC_RECOMMEND_CANDIDATES": ("", "str"),
    "FNMUSIC_LLM_TIMEOUT_S": ("", "str"),
    "FNMUSIC_RECOMMEND_VERIFY_PLAYABLE": ("", "str"),
    "FNMUSIC_RECOMMEND_VERIFY_TIMEOUT_S": ("", "str"),
    "FNMUSIC_REC_SEARCH_CONCURRENCY": ("", "str"),
    "FNMUSIC_REC_SEARCH_INTERVAL": ("", "str"),
    # 【2026-10-06 补】把在线音源的搜索结果并入「搜索建议」。
    # 必须在白名单里 —— 不在的话 apply_env_hot_reload() 会跳过它，
    # .env 里写什么都没用（实测：.env 写了 true、解析也对，
    # 但重启后 CONF['merge_suggest'] 仍是 False）。
    # 背景：电脑端（浏览器）只调 search/suggest，从不调 search/track；
    # 而 suggest 的合并逻辑整段包在 if not CONF["merge_suggest"] 里，
    # 默认 false 时只转发上游、不补数据，于是电脑端永远「无搜索结果」。
    "FNMUSIC_MERGE_SUGGEST": ("merge_suggest", "bool"),
    # 洛雪源检索取多少条。默认 30 —— 实测原唱常排在第 6~48 位，
    # 取 5 条会永远匹配到翻唱账号（用户看到一堆陌生歌手）。
    "FNMUSIC_REC_LX_LIMIT": ("", "str"),
    # 默认 false —— 不允许用本地曲库随机补齐每日推荐（用户要求「不要有兜底」）
    "FNMUSIC_RECOMMEND_ALLOW_FALLBACK": ("", "str"),
}
_ENV_WATCH_INTERVAL_S = 2.0
_ENV_WATCH_DEBOUNCE_S = 0.5


def _env_watch_path() -> str:
    return os.environ.get("FNMUSIC_ENV_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".env"
    )


def _env_watch_parse(raw: str, kind: str):
    raw = str(raw or "").strip()
    if kind == "bool":
        return raw.lower() in ("true", "1", "yes")
    if kind == "tee_cache_max":
        try:
            return max(1, min(100, int(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "quality_mode":
        return raw.lower() if raw.lower() in ("high", "balanced", "smooth") else None
    if kind == "lx_sources":
        return _normalize_lx_sources(raw)
    if kind == "llm_url":
        return raw.rstrip("/")
    if kind == "seconds":
        try:
            return max(1.0, min(60.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "bind_timeout":
        try:
            return max(10.0, min(3600.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "ttl_seconds":
        try:
            return max(10.0, min(3600.0, float(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "mb_int":
        try:
            return max(1, min(102400, int(raw)))
        except (TypeError, ValueError):
            return None
    if kind == "tee_handoff_max":
        try:
            return max(0, min(20, int(raw)))
        except (TypeError, ValueError):
            return None
    return raw


def apply_env_hot_reload(env_path: "str | None" = None) -> list[str]:
    """解析 .env 应用白名单键；返回发生变化的 CONF 键名（空 = 无变化）。

    环境变量同步写原始字符串（recommend 等模块直接读 os.environ）。
    """
    path = env_path or _env_watch_path()
    kv = dict(parse_env_file(path)[0])
    changed: list[str] = []
    for env_key, (conf_key, kind) in _ENV_WATCH_KEYS.items():
        if env_key not in kv:
            continue
        os.environ[env_key] = str(kv[env_key])
        value = _env_watch_parse(kv[env_key], kind)
        if value is None:
            continue
        if conf_key and CONF.get(conf_key) != value:
            CONF[conf_key] = value
            changed.append(conf_key)
    return changed


def _reset_search_cache() -> None:
    tasks = [
        entry.get("task") for entry in _SEARCH_CACHE.values()
        if entry.get("task") and not entry["task"].done()
    ]
    for task in tasks:
        task.cancel()
    _SEARCH_CACHE.clear()


def _env_watch_stat(path: "str | None" = None):
    try:
        st = os.stat(path or _env_watch_path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# 音源集合相关的 CONF 键：任一变化意味着推荐缓存里的旧源曲目可能不可播（issue #22）
_SOURCE_CONF_KEYS = {"musicdl_enabled", "netease_enabled", "lx_enabled", "online_sources", "lx_sources"}


def _cancel_daily_tasks() -> None:
    for key in list(_DAILY_TASKS):
        old = _DAILY_TASKS.pop(key, None)
        if old is not None and not old.done():
            old.cancel()


async def _env_watch_loop() -> None:
    last = _env_watch_stat()
    while True:
        try:
            await asyncio.sleep(_ENV_WATCH_INTERVAL_S)
            cur = _env_watch_stat()
            if cur is None or cur == last:
                last = cur
                continue
            await asyncio.sleep(_ENV_WATCH_DEBOUNCE_S)
            settled = _env_watch_stat()
            if settled != cur:
                continue  # 仍在写入，下一轮再看
            last = settled
            changed = apply_env_hot_reload()
            if changed:
                _reset_search_cache()
                if _SOURCE_CONF_KEYS & set(changed):
                    # issue #22：切换音源后当日推荐立即失效，切回歌单按新音源重建，
                    # 旧源曲目不再残留到当日结束
                    removed = dailyrec.invalidate_today_cache_all_users()
                    _cancel_daily_tasks()
                    logger.info(
                        "音源配置变化(%s)：已失效当日推荐缓存 %d 个，推荐歌单将按新音源重建",
                        ",".join(sorted(_SOURCE_CONF_KEYS & set(changed))), removed,
                    )
                logger.info(".env 热重载生效: %s", ",".join(sorted(changed)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("env watch loop error: %s", e)


def _source_config() -> dict:
    return {k: v for k, v in CONF.items() if k.endswith(("_enabled", "_url", "_limit", "_quality", "_sources")) or k == "online_sources"}


def _search_scope(request: Request) -> str:
    auth = [request.headers.get(k, "") for k in ("cookie", "authorization", "x-trim-music-temp-token")]
    config = _source_config()
    filters = sorted((k, v) for k, v in request.query_params.multi_items() if k not in ("page", "q", "query", "keyword"))
    return hashlib.sha256(json.dumps([auth, config, filters, request.url.path], sort_keys=True).encode()).hexdigest()


def _source_enabled(guid: str) -> bool:
    source = source_from_online_guid(guid)
    if not CONF.get({"netease": "netease_enabled", "lx": "lx_enabled"}.get(source, "musicdl_enabled")):
        return False
    if source == "lx":
        # lx 平台白名单（online:lx:<platform>:<id>）；未配置 = 跟随 lx 服务全部已启用平台
        selected = CONF.get("lx_sources") or []
        if selected:
            parts = guid.split(":")
            return len(parts) >= 4 and parts[2] in selected
        return True
    if source != "netease" and CONF.get("online_sources"):
        selected = {name.strip().lower().removesuffix("musicclient") for name in str(CONF["online_sources"]).split(",")}
        return source.lower() in selected
    return True


ONLINE_TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def is_playable_online_track(item: dict, require_id: bool = False, allow_paywall: bool = False) -> bool:
    """最终防线校验：过滤无音频流或试听标记的不可播曲目。"""
    if not isinstance(item, dict):
        return False

    title = str(item.get("title") or item.get("name") or item.get("song_name") or "").strip()
    if not title:
        return False

    if require_id:
        sid = str(item.get("id") or item.get("song_id") or item.get("guid") or "").strip()
        if not sid:
            return False

    # 1. 标题含试听标记
    if any(marker in title for marker in ONLINE_TRIAL_MARKERS):
        return False

    # 2. 字段试听标记
    if item.get("is_trial") is True or item.get("freeTrialInfo") or item.get("freeTrialPrivilege"):
        return False
    if int(item.get("is_free_part") or 0) != 0 or int(item.get("fail_process") or 0) == 4:
        return False

    # 3. 收费/VIP 拦截（verified 条目已由服务端完成"直链解析+Range探活"验证，可播性有实证，跳过收费元数据拦截；
    #    当 allow_paywall=True 时同样跳过收费拦截，用于逐曲探活关闭时放行第三方源候选歌曲）
    if not allow_paywall and item.get("verified") is not True:
        if int(item.get("pay_type") or 0) != 0:
            return False
        if int(item.get("pkg_price") or 0) != 0 or int(item.get("price") or 0) != 0:
            return False
        fee = item.get("fee")
        if fee is not None:
            try:
                if int(fee) not in (0, 8):
                    return False
            except (ValueError, TypeError):
                pass

    # 4. 显式不可播/无流标记
    if item.get("unplayable") is True or item.get("playable") is False:
        return False
    if item.get("has_stream") is False:
        return False

    # 5. 音频流直链校验：若带有 download_url 或 url 键，则必须合法可用，绝不能是空串或 404
    if "download_url" in item:
        d_url = str(item.get("download_url") or "").strip()
        if not d_url or not d_url.startswith(("http://", "https://")) or "404/error.html" in d_url or "error.html" in d_url:
            return False
    if "url" in item:
        u = str(item.get("url") or "").strip()
        if not u or "404/error.html" in u or "error.html" in u:
            return False

    # 6. 片段时长校验（<=35s 且带有试听迹象）
    duration = item.get("duration_s") or (item.get("duration") or 0)
    try:
        duration_s = float(duration)
        if 0 < duration_s <= 35 and ("试听" in title or item.get("is_trial")):
            return False
    except (ValueError, TypeError):
        pass

    return True


# ---------------------------------------------------------------- 原唱优先排序 ---
#
# 目标：搜索结果里「原唱」排在翻唱/伴奏/remix 之前。
#
# 分两级，先硬后软：
#   1. 硬标识：酷狗的 trans_param.is_original（lx 服务侧已透传为 item["is_original"]）。
#      为 True → 最优先；为 False 且明确来自酷狗 → 明确非原唱，直接压到末档。
#   2. 启发式（其余源无标识）：标题去掉括号修饰后与搜索关键词高度一致 → 更像原版；
#      标题命中伴奏/翻唱/remix 等版本词 → 压到末档。
#
# ★ 软优先：本模块**只排序、不丢弃**。原唱排前面，非原唱仍在列表里可见，
#   避免小众歌因无原唱而搜不到任何结果。用户 2026-10-08 明确选择「软优先」。

# 命中即视为「非原唱」的标题版本词（与 lxmusic-service 的 _NON_ORIGINAL_TITLE_MARKERS 对齐）
_NON_ORIGINAL_TITLE_MARKERS = (
    "伴奏", "伴唱", "卡拉ok", "karaoke", "ktv",
    "翻唱", "cover", "致敬",
    "remix", "混音", "dj", "电音",
    "纯音乐", "instrumental", "演奏", "钢琴版", "吉他版", "口琴",
    "童声", "儿童", "女声版", "男声版",
    "铃音", "铃声", "彩铃",
    "现场", "live", "演唱会", "音乐会",
    "demo", "试唱",
)

_ORIGINAL_TRUE_TOKENS = ("1", "true", "yes", "y", "t")
_ORIGINAL_FALSE_TOKENS = ("0", "false", "no", "n", "f")


def _normalize_title_for_match(title: str) -> str:
    """去括号修饰与空白/连接符，用于与关键词比对（与 lx 的 _title_relevance 同思路）。"""
    s = re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】", "", title or "")
    return re.sub(r"[\s\-—·・&_/]", "", s).casefold()


def _title_matches_keyword(item: dict, keyword: str) -> bool:
    """标题与关键词是否「对得上」（归一化后相等 / 互为前缀 / 互相包含）。

    ★ 2026-10-09 新增：给「硬标识原唱」加一道相关性闸门。
    酷狗 isoriginal=1 的语义是「该条目是**其所标歌手**的原始版本」，并不等于
    「用户搜索的那首歌的原始演唱者」 —— 上游模糊召回会把同专辑/同歌手的其他
    原唱一并带回（实测：搜「晴天」时「好不容易 / 告五人」也被标 isoriginal=1，
    旧逻辑直接给 rank 0，导致它被顶到第 2 位、而标题与「晴天」零匹配）。

    关键词为空 → 返回 True（信息不足，不做判断，维持原有行为不降权）。
    """
    kw = _normalize_title_for_match(keyword)
    if not kw:
        return True
    t = _normalize_title_for_match(str(item.get("title") or item.get("name") or ""))
    if not t:
        return False
    return t == kw or t.startswith(kw) or kw.startswith(t) or kw in t or t in kw


def _original_rank(item: dict, keyword: str) -> int:
    """原唱优先级：数字越小越靠前
    （0 相关原唱 / 1 干净标题 / 2 普通 / 4 明确非原唱 / 5 无关原唱）。

    只影响展示顺序，不影响是否展示——这是「软优先」的落点。

    三态判断（is_original 由 lx 服务透传，见 lxmusic-service/app.py 的 _is_original_kg）：
      True  → 酷狗硬标识原唱 **且标题对得上关键词** → rank 0
              标题对不上 → rank 5（排到所有相关条目之后）
              （2026-10-09 加标题闸门：酷狗 isoriginal=1 只表示「是其所标歌手的
               原始版本」，上游模糊召回会把别的歌的原唱一并带回 —— 实测搜
               「晴天」时「好不容易/告五人」也被标 isoriginal=1，曾挤到第 2 位）
      False → 酷狗硬标识说这不是原唱    → rank 4
      None  → 上游没给（多数源如此）    → 落到下面的启发式，**不因此降权**
    """
    # ── 1. 硬标识（酷狗）─────────────────────────────────────────────
    flag = item.get("is_original")
    if flag is not None:
        token = str(flag).strip().casefold()
        if token in _ORIGINAL_TRUE_TOKENS:
            if _title_matches_keyword(item, keyword):
                return 0
            # 原唱但标题与关键词无关 → rank 5：必须排到所有**相关条目**之后，
            # 否则上游模糊召回的原唱（如搜「晴天」时的「好不容易/告五人」）会挤占前排。
            # ⚠ 不要再退回启发式 —— 那会得到 rank 2，反而**高于**「明确非原唱」的 4，
            #   结果仍是它排前面（2026-10-09 第一次修复就是这样漏掉的）。
            return 5
        elif token in _ORIGINAL_FALSE_TOKENS:
            return 4
        # 无法解析的取值当未知处理，继续走启发式

    # ── 2. 启发式 ────────────────────────────────────────────────────
    # 版本词优先看 lx 侧标记，其次自己再判一次（兼容未透传的源）
    marker = item.get("has_version_marker")
    title = str(item.get("title") or item.get("name") or "")
    has_marker = bool(marker) if marker is not None else any(
        m in title.casefold() for m in _NON_ORIGINAL_TITLE_MARKERS
    )
    if has_marker:
        return 4

    kw = _normalize_title_for_match(keyword)
    if not kw:
        return 2
    t = _normalize_title_for_match(title)
    if not t:
        return 2
    # 标题与关键词几乎一致（"晴天" vs 关键词"晴天"）→ 最像原版
    if t == kw:
        return 1
    if t.startswith(kw) or kw.startswith(t):
        return 1
    return 2


def _sort_by_original(items: list[dict], keyword: str) -> list[dict]:
    """稳定排序：按原唱优先级升序，同档内保持原有（源间）顺序。

    Python 的 sort 是稳定的，故同 rank 的条目相对顺序不变 —— 这样既能保证
    「原唱冒头」，又不会把各源原本的聚合顺序（网易→musicdl→lx）打乱得太厉害。
    """
    return sorted(items, key=lambda x: _original_rank(x, keyword))


def _entry_source(item: dict) -> str:
    """取条目来源标识：优先解析 `lx:<src>:<id>` 的源段，否则回退在线 guid。

    实测 `source_from_online_guid(online_guid_from_item(x))` 对 lx 条目返回空串，
    会让所有条目落进同一个桶、轮转完全失效（单测 `test_round_robin_interleaves_sources` 抓到）。
    """
    gid = str((item or {}).get("id") or "")
    parts = gid.split(":")
    if len(parts) >= 3 and parts[0] == "lx":
        return parts[1]
    try:
        return source_from_online_guid(online_guid_from_item(item)) or ""
    except Exception:  # noqa: BLE001
        return ""


def _round_robin_by_source(seg: list) -> list:
    """把一段条目按「源」分桶后轮转输出（桶内保持原顺序）。"""
    buckets: dict = {}
    order: list = []
    for it in seg or []:
        src = _entry_source(it)
        if src not in buckets:
            buckets[src] = []
            order.append(src)
        buckets[src].append(it)
    out: list = []
    idx = 0
    while True:
        added = False
        for src in order:
            bucket = buckets[src]
            if idx < len(bucket):
                out.append(bucket[idx])
                added = True
        if not added:
            break
        idx += 1
    return out


def _spread_sources(items: list, keyword: str) -> list:
    """**非原唱条目按源轮转**（rank 0 的相关原唱保持最前），避免单一源霸榜。

    ★ 2026-10-11：实测搜「会开花的云」时 kw 源返回 20 条同名翻唱且**全部排在
    tx 的「会开花的云 (Live) - 弦子 / 姚晓棠」之前**，导致后者落到第 52 位 ——
    客户端第一页（50 条）根本看不到，用户反馈「搜不到完整版」。
    轮转后各源的代表在前列交替出现，用户更容易在首页找到自己想要的版本。
    """
    if not items:
        return items
    # rank 0（**相关原唱**）保持在最前 —— 这是排序的核心语义，绝不能动；
    # 其余条目**合成一段整体按源轮转**。实测它们被 rank 拆成了 1~3 条的小段，
    # 段内轮转毫无效果（kw 的 20 条依旧连成一片）；合成一段后各源才真正交替。
    head = [it for it in items if _original_rank(it, keyword) == 0]
    rest = [it for it in items if _original_rank(it, keyword) != 0]
    return head + _round_robin_by_source(rest)


# ── 搜索结果「片段/试听」降权（★ 2026-10-11）──────────────────────────────
# 音源会下发 `会开花的云 (25秒Live片段)` 这类条目，且常排在完整版之前；用户点进去
# 只有 25 秒。这里把**标题强信号**命中的条目稳定挪到列表末尾（只降权、不删除 ——
# 完整版与其它版本全部保留），避免误点。刻意**不做时长过滤**：短曲、铃声、纯音乐
# 的正片本来就短，按时长判断会误伤。
_CLIP_TITLE_MARKERS = ("片段", "试听", "铃声")
_CLIP_SHORT_MAX_S = 45.0      # 短于此时长视为片段候选（搜索场景，见 _is_clip_entry）


def _is_clip_entry(item: dict) -> bool:
    """疑似「片段/试听」条目。两条判据（任一命中）：

    1) **标题强信号**：含 `片段 / 试听 / 铃声`，或带「NN秒」标注；
    2) **时长过短**：`0 < duration_s < _CLIP_SHORT_MAX_S`（45 秒）。
       搜索场景下 45 秒内的条目几乎不是用户要找的完整歌 —— 实测
       「会开花的云 (钢琴版)」26s、「会开花的云（抖音热播榜）」27s 都是片段。

    只降权不删除，且 `_demote_clip_entries` 用**稳定分区**：同类之间相对顺序不变，
    所以搜「铃声」这类本来就短的关键词时，列表顺序不受影响。
    时长缺失/无法解析时不判定（宁漏勿误）。
    """
    if not isinstance(item, dict):
        return False
    title = str(item.get("title") or "")
    if title and (any(m in title for m in _CLIP_TITLE_MARKERS)
                  or re.search(r"\d{1,3}\s*秒", title)):
        return True
    try:
        dur = float(item.get("duration_s") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < dur < _CLIP_SHORT_MAX_S


def _demote_clip_entries(items: list) -> list:
    """把疑似片段条目**稳定**挪到末尾（组内原顺序不变）。

    关闭方式：`.env` 里 `FNMUSIC_SEARCH_DEMOTE_CLIPS=false`（重启生效）。
    """
    if not CONF.get("search_demote_clips", True):
        return items
    head: list = []
    tail: list = []
    for it in items or []:
        (tail if _is_clip_entry(it) else head).append(it)
    if tail:
        logger.info("search demoted %d clip entries (of %d)", len(tail), len(items or []))
    return head + tail


def _same_recording(left: dict, right: dict) -> bool:
    """Conservative identity: never strip live/remix/version markers."""
    for key in ("title", "artist", "version"):
        a, b = (str(x.get(key) or "").strip().casefold() for x in (left, right))
        if a != b or (key != "version" and not a):
            return False
    try:
        a, b = float(left.get("duration_s") or 0), float(right.get("duration_s") or 0)
        return math.isfinite(a) and math.isfinite(b) and a > 0 and b > 0 and abs(a - b) <= 2.0
    except (TypeError, ValueError):
        return False


def _search_allow_paywall() -> bool:
    """搜索阶段是否**放行「付费/VIP」条目**。

    与 `_lx_search_request` 的语义保持一致：`search_probe` 关闭时**不按付费元数据
    过滤**，候选先全部呈现，可播性交给后台预热探活兜底（失败的进 `_PREFETCH_BAD`
    并从列表剔除）。

    ★ 2026-10-11 修复：此前 `_lx_search_request` 已按 `allow_paywall=True` 放行了
    VIP 条目，但下游 `deduplicate_online_items` / `merge_online_tracks` 仍用**默认值**
    （`allow_paywall=False`）又筛了一遍 —— 于是 VIP 曲（实测
    「会开花的云 (Live) - 弦子 / 姚晓棠」，`pay_type=1`，天赐的声音第五季）
    在搜索结果里**永远看不到**，用户反馈「搜不到完整版」。
    """
    return not bool(CONF.get("search_probe"))


def deduplicate_online_items(items: list[dict], keyword: str = "") -> list[dict]:
    """Keep the published representative and strict recording alternatives.

    keyword 非空时：同一录音的多个源候选里，**优先让原唱当代表**（软优先——
    非原唱仍保留在 _alternatives 里可切源）。keyword 为空时行为与旧版完全一致。
    """
    result: list[dict] = []
    seen = set()
    rank_of = (lambda x: _original_rank(x, keyword)) if keyword else (lambda x: 0)
    for item in items:
        if not is_playable_online_track(item, allow_paywall=_search_allow_paywall()):
            continue
        guid = online_guid_from_item(item)
        if guid in seen:
            continue
        seen.add(guid)
        representative = next((x for x in result if _same_recording(x, item)), None)
        if representative is None:
            representative = dict(item)
            representative["_alternatives"] = list(item.get("_alternatives", []))
            result.append(representative)
            continue
        alternatives = representative.setdefault("_alternatives", [])
        incoming = {k: v for k, v in item.items() if k != "_alternatives"}
        if keyword and rank_of(item) < rank_of(representative):
            # 新来的更「原唱」→ 它上位当代表，旧代表降为备选。
            # ⚠ 必须先从备选里剔掉 incoming 自己：上一步可能已把它当作
            #   「同录音的另一个源」追加进去了，不去重会让它同时出现在
            #   代表位和备选位（单测 test_deduplicate_prefers_original_representative 抓到）。
            old = {k: v for k, v in representative.items() if k != "_alternatives"}
            rest = [x for x in alternatives
                    if online_guid_from_item(x) != guid]
            representative.clear()
            representative.update(incoming)
            representative["_alternatives"] = rest + [old]
        else:
            if guid not in {online_guid_from_item(x) for x in alternatives}:
                alternatives.append(incoming)
    return result


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def _sanitize_content_disposition(value: str) -> str:
    """把 Content-Disposition 里的非 latin-1 文件名规范化为 RFC 6266/5987 格式。

    上游（官方 trim-music）在交付转码/下载文件时会在 filename="..." 填入原始中文歌名，
    同时在 filename*=UTF-8''... 填入 URL 编码的歌名。Starlette 要求所有响应头
    必须能被 latin-1 编码，否则抛出 UnicodeEncodeError 导致 500 报错。
    本函数将 filename="..." 中的非 ASCII 字符替换为安全字符，并确保 filename*= 存在。
    """
    m_star = re.search(r"filename\*=([^;]+)", value, re.IGNORECASE)
    m_plain = re.search(r'filename="([^"]+)"', value, re.IGNORECASE)
    if not m_plain:
        m_plain = re.search(r'filename=([^; ]+)', value, re.IGNORECASE)

    raw_fn = m_plain.group(1) if m_plain else "track"
    ext = "." + raw_fn.rsplit(".", 1)[-1] if "." in raw_fn else ""
    ascii_fn = re.sub(r'[^\x20-\x7e]', '_', raw_fn)
    if not ascii_fn.strip() or ascii_fn == ext:
        ascii_fn = f"track{ext}"

    if m_star:
        star_part = f"filename*={m_star.group(1).strip()}"
    else:
        quoted = quote(raw_fn, encoding="utf-8")
        star_part = f"filename*=UTF-8''{quoted}"

    disposition = value.split(";")[0].strip() or "attachment"
    return f'{disposition}; filename="{ascii_fn}"; {star_part}'


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    out = {}
    for k, v in headers.items():
        if k.lower() in exclude:
            continue
        try:
            str(v).encode("latin-1")
            out[k] = v
        except UnicodeEncodeError:
            if k.lower() == "content-disposition":
                out[k] = _sanitize_content_disposition(str(v))
            else:
                out[k] = str(v).encode("latin-1", errors="replace").decode("latin-1")
    return out


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。

    authx 为新版官方前端登录后的逐请求签名头（含时间戳与随机数），
    原样转发给上游即可通过校验；切勿缓存或复用其值。"""
    headers = filter_headers(request.headers, exclude_keys={"host", "content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token", "authx"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def _cover_endpoint_url(guid: str) -> str:
    """服务端封面端点（同源相对 URL）：客户端（含电脑端/web）用 coverUrl 取图时，
    统一走 /static/cover 的服务端解析（酷狗直构 / 网易直连兜底），不再依赖空的第三方 CDN coverUrl。"""
    return f"/music/api/v1/static/cover?coverId={quote(str(guid), safe='')}"


def _track_cover_id(guid: str) -> str:
    """曲目 coverId：在线 guid → 独立盐伪装的纯 32-hex（并登记 fake→real 反查）。

    客户端列表行只渲染**纯 32-hex**形态的 coverId（本地曲目即此形态，故能出封面）；
    track_+32hex 形态是歌单封面的惯例，列表行不识别、不会发起封面请求——在线曲目
    因此长期只显示默认色块。这里预先伪装成纯 32-hex，disguise_client_json 便不会
    再给它加 track_ 前缀（它只处理 online: 开头的值）。
    v2.6.16 起改用封面独立盐：与曲目 guid 伪装 id 解耦，换盐即换封面 URL，
    客户端缓存的旧黑胶占位图自然失效；曲目 guid 伪装保持旧盐，收藏/历史不受影响。
    """
    g = str(guid or "")
    if not g:
        return ""
    return _cover_fake_guid(g) if g.startswith("online:") else g


# 公共歌单卡片复用同一套封面形态（见 publicplaylists.set_card_cover_hooks 注释）：
# 卡片 coverId 用纯 32-hex + 同时下发同源 coverUrl，与每日推荐/热门推荐一致，
# 否则音乐页侧边栏的公共歌单卡片永远只显示默认音符图标。
pubpl.set_card_cover_hooks(_track_cover_id, _cover_endpoint_url)


def _public_cover_url(guid: str) -> str:
    """下发用的封面 URL：coverId 一律官方 track_+32hex 形态。

    客户端（App/电脑端 web）按官方 id 格式过滤封面——online: 原样的 id 不会被渲染成图标，
    也就根本不会发起请求。故 coverUrl 必须与 coverId 同形态，且 fake guid 在此登记反查。
    """
    g = str(guid or "")
    if not g:
        return ""
    cid = "track_" + _cover_fake_guid(g) if g.startswith("online:") else g
    return _cover_endpoint_url(cid)


# 网易 fee 语义：0=免费 1=VIP 4=付费专辑 8=非会员可听低音质。
# VIP 专属（无 VIP 不可播/需购买）标记为 1 与 4。
_VIP_FEE_VALUES = (1, 4)


def _vip_tag_prefix(item: dict, src: str = "") -> str:
    """网易 VIP/付费专辑曲目，在歌名前加 [VIP] 提示（纯展示，不影响可播性判定）。"""
    if not CONF.get("show_vip_tag") or src != "netease":
        return ""
    fee = item.get("fee")
    if fee is None:
        return ""
    try:
        return "[VIP] " if int(fee) in _VIP_FEE_VALUES else ""
    except (TypeError, ValueError):
        return ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    # [VIP] 前缀：网易 VIP/付费专辑曲目纯展示标记（幂等，避免重复拼接）
    _vip_pre = _vip_tag_prefix(item, src)
    if _vip_pre and not title.startswith("[VIP]"):
        title = _vip_pre + title
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "mp3") or "mp3"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    cover = str(item.get("cover_url") or "")
    # 登记封面直链：客户端只会按 coverId 回源，登记后封面端点不必再查上游
    # （并发渲染时 _online_info 的 4 秒预算会集体超时 → 整片占位图，见登记表注释）
    _track_cover_remember(guid, cover)
    # 第三方 CDN 封面缺失时，把 coverUrl/coverURL 兜底为服务端封面端点（同源相对 URL），
    # 让读 coverUrl 的客户端（电脑端/web）也能走服务端解析拿到真图，而不是永远空白。
    # cover_id 用纯 32-hex 伪装形态：客户端列表行只渲染这种形态的 coverId。
    cover_id = _track_cover_id(guid)
    cover_ep = _cover_endpoint_url(cover_id) if not cover else ""
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": cover_id,
    }
    # 专辑伪装 guid 同步登记（issue #22）：客户端点击专辑时按 fake 反解分源适配详情
    register_fake_album(guid, album, item)
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": cover_id,
        "cover_url": cover,
        "coverUrl": cover or cover_ep,
        "coverURL": cover or cover_ep,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def log_stream_probe(method: str, guid: str, range_header: str | None, cached: bool) -> None:
    """在线取流探针：一行记录 Range 形态与落盘资格，供真机确认播放器取流行为。"""
    if CONF.get("stream_probe"):
        logger.info(
            "stream probe: %s %s range=%r cached=%s tee_eligible=%s",
            method, guid, range_header, cached, should_cache(range_header),
        )


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    stem = _path_stem(media_path)
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(stem)
        # .ref 静默写丢会让已落库的歌重新出网、曲库出现重复文件，写后读回校验
        with open(media_ref_path(guid), encoding="utf-8") as f:
            if f.read().strip() != stem:
                raise OSError("read-back mismatch")
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: Any = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。

    album 可能是前端曲目对象 {name, guid, ...}；先归一成纯字符串再写入，
    避免把整个对象的文本写进专辑标签。
    """
    title, artist, album = _tag_fields({"title": title, "artist": artist, "album": album})
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


_COVER_FETCH_TRANSPORT: "httpx.SyncBaseTransport | None" = None
_COVER_FETCH_MAX_BYTES = 10 * 1024 * 1024
# 图床「默认封面」字节下限（2026-10-10）：网易 music.126.net 对**无封面歌曲**固定返回
# 一张 3955B 的灰底 PNG（md5 371a28b2bb8cdac67dd75250cc5ada7c），它 HTTP 200、是合法
# 图片、能过 mime/上限校验 —— 于是被当作「真封面」放行，客户端显示出来就是一块灰。
# 同类：酷狗 stdmusic 黑胶图 17853B（已按 md5 拦），本项目占位图池最小 41937B。
# 真专辑图实测普遍 > 20KB。低于此值一律判为无信息量默认图 → 返回 None 让调用方继续降级。
_COVER_MIN_REAL_BYTES = 12 * 1024
# 歌单/榜单卡片封面的字节下限（2026-10-17 #55 修复）：卡片封面是平台官方给的
# 榜单/歌单图，常见**纯色、透明或极简构图**，压缩后只有几 KB —— 实测：
#   网易云古典榜 482x482 = 7867B；「我喜欢的音乐」红心透明 PNG 320x320 = 2784B。
# 两者都被 12KB 门槛判成默认图，卡片因此完全没有封面。已知默认图（网易 3955B /
# 酷狗 17853B）都已被 _DEFAULT_COVER_MD5S 按 md5 精确拦下，卡片这条支路因此把
# 尺寸门槛放宽到 1KB —— 卡片「没有封面」比「偶尔放行一张低信息量图」更伤。
_COVER_CARD_MIN_REAL_BYTES = 1024


def _sniff_image_mime(data: bytes) -> str:
    """魔数嗅探图片类型；非图片返回空串（Content-Type 会说谎，字节不会）。"""
    if data.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def download_cover_bytes(url: str) -> "tuple[bytes, str] | None":
    """下载封面图字节（内嵌用）。非图片/超限/酷我文本假图/网络失败一律返回 None。"""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    if _KW_TEXT_COVER_HOST in url:
        return None
    try:
        kwargs: dict = {"timeout": 8.0, "follow_redirects": True}
        if _COVER_FETCH_TRANSPORT is not None:
            kwargs["transport"] = _COVER_FETCH_TRANSPORT
        with httpx.Client(**kwargs) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.content or b""
        if not data or len(data) > _COVER_FETCH_MAX_BYTES:
            return None
        mime = _sniff_image_mime(data)
        if not mime:
            return None
        return data, mime
    except Exception as e:
        logger.debug("cover fetch failed for %s: %s", url, type(e).__name__)
        return None


async def _proxy_cover_response(url: str, request: Request, fallback_guid: str) -> Response:
    """同源回源封面：服务端拉取第三方图床封面字节，以同源 Response 流式返回。

    原实现把在线/歌单封面 302 跳转到网易/QQ/酷我等图床直链。在远程 HTTPS 访问
    下，飞牛音乐 App 直连这些外部图床会被混合内容、CORS 或防盗链（Referer 校验）
    拦截，导致封面全部回落默认波纹占位图——与早前音频直链在远程 HTTPS 下全挂是
    同一类根因。

    改为同源回源：proxy 在 NAS 侧用浏览器 UA（必要时带 Referer）抓取图床字节，再
    原样返回给 App。App 收到的是与自身 API 同源的图，既不触发混合内容/CORS，也绕
    开图床防盗链；任何失败都回落占位图，绝不让封面 404。
    """
    if not CONF.get("cover_proxy", True):
        # 开关关闭：沿用旧 302 行为（留给需要直连图床的局域网场景）
        if url and url.startswith(("http://", "https://")) and _KW_TEXT_COVER_HOST not in url:
            return RedirectResponse(url, status_code=302)
        return _placeholder_cover_response(fallback_guid)
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")) or _KW_TEXT_COVER_HOST in url:
        _cover_log("COVER_FB", f"guid={fallback_guid} reason=bad_url src={url[:60]}")
        return _placeholder_cover_response(fallback_guid)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; fnmusic-ext/cover)"}
        # 网易/QQ 图床按 Referer 鉴权，带上官方域名显著提升命中率（酷我文本页已排除）
        if "music.126.net" in url or "126.net" in url:
            headers["Referer"] = "https://music.163.com/"
        elif "gtimg.cn" in url:
            headers["Referer"] = "https://y.qq.com/"
        _cached = _cover_bytes_get(url)
        if _cached is not None:
            return Response(
                content=_cached,
                media_type=_sniff_image_mime(_cached) or "image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        async with _cover_cdn_client() as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            _cover_log("COVER_FB", f"guid={fallback_guid} reason=http_{r.status_code} src={url[:60]}")
            return _placeholder_cover_response(fallback_guid)
        data = r.content or b""
        if not data or len(data) > _COVER_FETCH_MAX_BYTES:
            _cover_log("COVER_FB", f"guid={fallback_guid} reason=body_{len(data)} src={url[:60]}")
            return _placeholder_cover_response(fallback_guid)
        mime = _sniff_image_mime(data)
        if not mime:
            _cover_log("COVER_FB", f"guid={fallback_guid} reason=mime src={url[:60]}")
            return _placeholder_cover_response(fallback_guid)
        _cover_log("COVER_OK", f"guid={fallback_guid} bytes={len(data)} mime={mime} src={url[:60]}")
        _cover_bytes_put(url, data)
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception as e:
        _cover_log("COVER_FB", f"guid={fallback_guid} reason=exc_{type(e).__name__} src={url[:60]}")
        logger.debug("cover proxy failed for %s: %s", url, type(e).__name__)
        return _placeholder_cover_response(fallback_guid)


def _has_embedded_cover(path: str) -> bool:
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        tags = getattr(mf, "tags", None)
        if tags is None:
            return False
        if hasattr(tags, "pictures"):  # FLAC/APE
            return bool(tags.pictures)
        if hasattr(tags, "getall"):  # ID3
            return bool(tags.getall("APIC"))
        return bool(tags.get("covr") or tags.get("METADATA_BLOCK_PICTURE"))  # MP4/OGG
    except Exception:
        return False


def embed_audio_cover(path: str, data: bytes, mime: str) -> bool:
    """把封面字节内嵌进音频文件（MP3 APIC / FLAC Picture / MP4 covr / OGG）。

    飞牛扫描器（dhowden/tag）只认内嵌图，不读外置 cover 图片文件，内嵌是
    官方 App 显示封面的唯一途径。文件已有内嵌图（源站自带）则跳过；不支持的
    格式（wav 等）返回 False。
    """
    try:
        import base64

        from mutagen import File as MutagenFile
        from mutagen.flac import Picture
        from mutagen.id3 import APIC, ID3
        from mutagen.mp4 import MP4Cover

        audio = MutagenFile(path)
        if audio is None or _has_embedded_cover(path):
            return False
        cls = audio.__class__.__name__
        if cls == "MP3":
            try:
                id3 = ID3(path)
            except Exception:
                id3 = ID3()
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            id3.save(path, v2_version=3)
            return True
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                return False
        if cls == "MP4":
            if mime not in ("image/jpeg", "image/png"):
                return False
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
            return True
        if cls == "FLAC":
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio.add_picture(pic)
            audio.save()
            return True
        if cls in ("OggVorbis", "OggOpus", "OggSpeex"):
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()
            return True
        return False
    except Exception as e:
        logger.debug("cover embed failed for %s: %s", path, e)
        return False


def detect_library_dir() -> str:
    """优先环境变量，否则读飞牛 music.db 的共享库路径，最后回退到仓库 cache/。"""
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        return explicit
    db = str(CONF.get("music_db") or "")
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for (path,) in rows:
                if path and os.path.isdir(path):
                    return path
        except Exception as e:
            logger.warning("Failed to read shared_library path: %s", e)
    return CONF["cache_dir"]


_TEE_SAVE_DIR_WARNED = False


def tee_save_dir() -> str:
    """边听边存落盘目录：配置路径可用则用，否则回退自动探测的曲库目录。"""
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    if not explicit:
        return detect_library_dir()
    usable = False
    try:
        os.makedirs(explicit, exist_ok=True)
        usable = os.path.isdir(explicit) and os.access(explicit, os.W_OK)
    except Exception:
        usable = False
    if usable:
        return explicit
    global _TEE_SAVE_DIR_WARNED
    if not _TEE_SAVE_DIR_WARNED:
        _TEE_SAVE_DIR_WARNED = True
        logger.warning(
            "FNMUSIC_TEE_SAVE_DIR=%s 不可用（无法创建或不可写），边听边存回退到 %s",
            explicit, detect_library_dir(),
        )
    return detect_library_dir()


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    for d in (([explicit] if explicit else []) + [detect_library_dir(), CONF["cache_dir"]]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
    return _find_file_by_snapshot_tags(guid)


# 标签兜底查库的阴性缓存：确认不在曲库的 guid 短期内不再反复扫快照/查库
_TAGS_LOOKUP_MISS: dict[str, float] = {}
_TAGS_MISS_TTL_S = 300.0


def _find_file_by_snapshot_tags(guid: str) -> "str | None":
    """.ref 与精确名都 miss 时的自愈兜底：按快照标签在官方曲库反查文件。

    命中说明歌已落库只是 .ref 丢了（或本就有同名同曲的本地文件），
    remember_media_path 自愈后下次直接走 recall。必须 title+artist 双条件
    才可能命中（album 有则再收紧），查库只在 miss 时发生。
    """
    now = time.monotonic()
    miss_at = _TAGS_LOOKUP_MISS.get(guid)
    if miss_at is not None and now - miss_at < _TAGS_MISS_TTL_S:
        return None
    snap = _lookup_online_snapshot(guid)
    title = artist = album = ""
    if snap:
        title, artist, album = _tag_fields(snap)
    path = library_file_by_tags(title, artist, album) if title and artist else None
    if not path:
        if len(_TAGS_LOOKUP_MISS) > 1024:
            _TAGS_LOOKUP_MISS.clear()
        _TAGS_LOOKUP_MISS[guid] = now
        return None
    remember_media_path(guid, path)
    logger.info("Recalled library file for %s via tags: %s", guid, path)
    return path


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    # Bare legacy IDs cannot prove source/track identity.
    return audio_path


def _is_rolling_cache_stem(path: str, guid: str) -> bool:
    """是否为 cache 目录下该 guid 的滚动缓存产物（cache_safe_guid 命名）。"""
    rolling = os.path.join(CONF["cache_dir"], cache_safe_guid(guid))
    try:
        return os.path.abspath(os.path.splitext(path)[0]) == os.path.abspath(rolling)
    except Exception:
        return False


def library_media_path(guid: str, title: str, ext: str, artist: str = "", directory: str | None = None) -> str:
    lib = directory or detect_library_dir()
    recalled = recalled_media_path(guid)
    if recalled and not _is_rolling_cache_stem(recalled, guid):
        return recalled
    stem = recalled_media_stem(guid)
    if stem and not _is_rolling_cache_stem(stem, guid):
        return f"{stem}.{ext}"
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    """歌词三档归属（音频到哪，歌词到哪）：已有歌词复用原位；有音频落在音频旁；
    无音频只落 cache（guid 命名）。绝不把曲库目录当无音频时的兜底——那会制造
    只有歌词没有音频的孤儿 .lrc（曲库在云盘上时还会产生上传流量）。
    """
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


def promote_shadow_lyric(guid: str, audio_path: str) -> None:
    """音频落曲库后，把 cache 里的影子歌词（无音频时代的 guid 命名副本）提升为
    音频旁 sidecar，词曲贴身；曲库已有 sidecar 时不覆盖。跨文件系统时退化为复制。
    """
    dest = os.path.splitext(audio_path)[0] + ".lrc"
    if os.path.exists(dest):
        return
    shadow = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")
    if not os.path.exists(shadow):
        return
    try:
        os.replace(shadow, dest)
    except OSError:
        try:
            shutil.copyfile(shadow, dest)
            os.remove(shadow)
        except Exception as e:
            logger.warning("shadow lyric promote failed for %s: %s", guid, e)
            return
    adopt_library_perms(dest)


async def cache_lyrics_from_musicdl(musicdl_client: httpx.AsyncClient, guid: str) -> dict | None:
    """与音频 tee 并行：把 musicdl /info 里的 LRC 落到曲库同目录 sidecar。"""
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not (isinstance(data, dict) and data.get("ok") is not False):
            return None
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
        return data
    except Exception as e:
        logger.warning("lyric sidecar fetch failed for %s: %s", guid, e)
        return None


async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached

    if not _source_enabled(guid):
        return ""
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    l_data = res_data.get("data")
                    if isinstance(l_data, dict):
                        lyric_text = str(l_data.get("lyric") or "").strip()
                        if lyric_text:
                            info = await _online_info(request, guid)
                            write_lyric_cache(
                                guid,
                                lyric_text,
                                title=str((info or {}).get("title") or ""),
                                artist=str((info or {}).get("artist") or ""),
                            )
                            return lyric_text
        except Exception as e:
            logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
        return ""

    data = await _online_info(request, guid)
    text = str((data or {}).get("lyric") or "").strip()
    if text:
        write_lyric_cache(
            guid,
            text,
            title=str((data or {}).get("title") or ""),
            artist=str((data or {}).get("artist") or ""),
        )
    return text


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str,
                          extra_headers: dict | None = None) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    extra = extra_headers or {}
    if rng is None:
        headers = {
            "Content-Type": media_type,
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        }
        headers.update(extra)
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers=headers,
        )

    start, end = rng
    length = end - start + 1
    headers = {
        "Content-Type": media_type,
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
    }
    headers.update(extra)
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers=headers,
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicdl_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicdl_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicdl_url"], timeout=45.0)
        fastapi_app.state.musicdl_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_lx_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "lx_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["lx_url"], timeout=25.0)
        fastapi_app.state.lx_client = client
    return client


def get_llm_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "llm_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        fastapi_app.state.llm_client = client
    return client


async def forward_to_upstream(request: Request, client: httpx.AsyncClient) -> Response:
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    # 幂等方法在"连接还没换来任何响应"的失败（官方服务重启窗口的陈旧连接 /
    # uds 瞬断）上重试一次：响应已开始流式传输后的失败不重试（无法安全回放）。
    resp = None
    attempts = 2 if request.method.upper() in ("GET", "HEAD") else 1
    for attempt in range(attempts):
        try:
            resp = await client.send(req, stream=True)
            break
        except (httpx.TransportError, httpx.RemoteProtocolError):
            if attempt + 1 >= attempts:
                raise
            logger.info("upstream forward retry (%s %s): connection-level failure",
                        request.method, request.url.path)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    # 上游请求强制 accept-encoding: identity，其 Content-Length 即精确字节数；原样
    # 透传可让依赖总长度的播放内核走定长帧（对齐官方直连行为）。无 body 的状态
    # 码不能带；上游无视 identity 仍压缩时长度描述的是编码体，透传必错位，弃用。
    content_length = resp.headers.get("content-length")
    content_encoding = (resp.headers.get("content-encoding") or "").strip().lower()
    if (content_length and content_encoding in ("", "identity")
            and resp.status_code >= 200 and resp.status_code not in (204, 304)):
        resp_headers["content-length"] = content_length

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    # v2.6.18：本地专辑/歌手/歌曲列表走的是本函数的默认分支（StreamingResponse
    # 原样透传），JSON 从不被解析，所以 coverId=null 一直没被补 —— 实测
    # /album/list、/track/list、/artist/list 共 12 条无 coverId，客户端拿不到
    # id 便不会请求 /static/cover，那几行必然空白。
    # 这里只在「响应是 JSON 且含 coverId 字段」时才读进内存改写；其余
    # （音频、大文件、流式接口）保持流式直通不变，风险面严格受限。
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" in ctype and resp.status_code == 200:
        try:
            raw = await resp.aread()
        except Exception:
            raw = b""
        finally:
            await resp.aclose()
        if raw and b"coverId" in raw:
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, (dict, list)):
                hdrs = {k: v for k, v in resp_headers.items()
                        if k.lower() not in ("content-length", "content-encoding")}
                return JSONResponse(content=disguise_client_json(obj),
                                    status_code=resp.status_code, headers=hdrs)
        # 不含 coverId 或解析失败：按原样回放，行为与改写前一致
        return StreamingResponse(
            iter([raw]),
            status_code=resp.status_code,
            headers={k: v for k, v in resp_headers.items()
                     if k.lower() not in ("content-length", "content-encoding")},
            media_type="application/json",
        )

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_musicdl_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: str | None = None) -> dict | None:
    if not keyword:
        return None
    scope = _FETCH_SCOPE.get()

    async def _fetch_once(params: dict[str, Any], timeout: float) -> dict | None:
        try:
            r = await client.get("/search", params=params, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    raw_items = data.get("items")
                    if isinstance(raw_items, list):
                        data["items"] = [it for it in raw_items if is_playable_online_track(it)]
                    return data
        except Exception as e:
            logger.warning("Failed to fetch online search from musicdl: %s", e)
        return None

    async def _query():
        params: dict[str, Any] = {"keyword": keyword, "limit": limit}
        selected_sources = CONF["online_sources"] if sources is None else sources
        if selected_sources:
            params["sources"] = selected_sources
        timeout = max(1.0, float(CONF.get("search_timeout") or 15))
        started = time.monotonic()
        data = await _fetch_once(params, timeout)
        # musicdl 快速交回 0 条且带错误（全源 busy/熔断/瞬时失败）时不是
        # 终态：稍等片刻重试一次，避免把偶发空结果当成真"无结果"。
        if (isinstance(data, dict) and not data.get("items") and data.get("errors")
                and time.monotonic() - started < 5.0):
            await asyncio.sleep(1.5)
            retry = await _fetch_once(params, timeout)
            if isinstance(retry, dict) and retry.get("items"):
                data = retry
        if isinstance(data, dict) and data.get("errors"):
            logger.warning("musicdl search partial errors: %s", data.get("errors"))
        return data

    return await _MUSICDL_SEARCH_GATE.run(scope, keyword, _query)


async def fetch_musicbox_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    if not keyword:
        return None
    scope = _FETCH_SCOPE.get()

    async def _query():
        return await _musicbox_search_request(client, keyword, limit)

    return await _MUSICBOX_SEARCH_GATE.run(scope, keyword, _query)


async def _musicbox_search_request(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None
        items = []
        song_ids = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            title = str(it.get("song_name") or it.get("title") or it.get("name") or "")
            # [VIP] 前缀：网易 VIP/付费专辑曲目纯展示标记（幂等）
            _vip_pre = _vip_tag_prefix(it, "netease")
            if _vip_pre and not title.startswith("[VIP]"):
                title = _vip_pre + title
            artist = str(it.get("artist") or "")
            album = str(it.get("album_name") or it.get("album") or "")
            duration = it.get("duration") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            quality = str(it.get("quality") or "").upper()
            ext = "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3"
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": title,
                "name": title,
                "version": str(it.get("version") or ""),
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": "",
                "lyric": "",
            })
            song_ids.append(sid)

        if song_ids:
            try:
                detail_resp = await client.get(
                    "/api/v1/songs/detail",
                    params={"ids": ",".join(song_ids)},
                    timeout=15.0,
                )
                if detail_resp.status_code == 200:
                    detail_json = detail_resp.json()
                    if isinstance(detail_json, dict) and detail_json.get("ok") is not False:
                        detail_list = detail_json.get("data")
                        if isinstance(detail_list, list):
                            detail_map = {}
                            for d_item in detail_list:
                                if isinstance(d_item, dict):
                                    d_sid = str(d_item.get("song_id") or d_item.get("id") or "")
                                    if d_sid:
                                        detail_map[d_sid] = d_item
                            for item in items:
                                raw_sid = item["id"].split(":", 1)[-1]
                                d_info = detail_map.get(raw_sid)
                                if d_info:
                                    pic_url = str(d_info.get("album_pic_url") or "")
                                    if pic_url:
                                        item["cover_url"] = pic_url
                                    if d_info.get("has_sq") or d_info.get("has_hr"):
                                        item["ext"] = "flac"
            except Exception as detail_err:
                logger.warning("Failed to fetch songs detail for %s: %s", keyword, detail_err)

        return [it for it in items if is_playable_online_track(it)]
    except Exception as e:
        logger.warning("Failed to fetch musicbox search: %s", e)
        return None


class _SearchItems(list):
    """List-compatible normalized results with source degradation metadata."""
    def __init__(self, items, partial=False):
        super().__init__(items)
        self.partial = partial


async def fetch_lx_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: "list[str] | str | None" = None) -> list[dict]:
    """洛雪音乐源搜索：返回统一 item（id = "lx:<source>:<identifier>"）。

    sources：平台白名单（列表或逗号串），None = 用 CONF["lx_sources"]；空 = 跟随 lx 服务配置。
    """
    if not keyword:
        return None  # type: ignore[return-value]
    scope = _FETCH_SCOPE.get()

    async def _query():
        return await _lx_search_request(client, keyword, limit, sources, scope)

    return await _LX_SEARCH_GATE.run(scope, keyword, _query)


async def _lx_search_request(client: httpx.AsyncClient, keyword: str, limit: int, sources, scope: str) -> list[dict]:
    params: dict[str, Any] = {
        "keyword": keyword,
        "limit": limit,
        "probe": 1 if CONF.get("search_probe") else 0,
    }
    selected = CONF.get("lx_sources") if sources is None else sources
    if isinstance(selected, str):
        selected = [s.strip() for s in selected.split(",") if s.strip()]
    if selected:
        params["sources"] = ",".join(selected)
    timeout = max(1.0, float(CONF.get("search_timeout") or 15))
    headers = {_SCOPE_HEADER: scope} if scope else None
    try:
        r = await client.get(
            "/api/v1/search",
            params=params,
            headers=headers,
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("items")
        if not isinstance(raw_list, list):
            return None
        items = []
        allow_paywall = not bool(CONF.get("search_probe"))
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it, allow_paywall=allow_paywall):
                continue
            tid = str(it.get("id") or "")
            if not tid:
                continue
            duration = it.get("duration_s") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            try:
                file_size = int(it.get("file_size") or 0)
            except (TypeError, ValueError):
                file_size = 0
            items.append({
                "id": tid,
                "source": "lx",
                "lx_source": str(it.get("lx_source") or ""),
                "version": str(it.get("version") or ""),
                "title": str(it.get("title") or it.get("name") or ""),
                "artist": str(it.get("artist") or ""),
                "album": str(it.get("album") or ""),
                "duration_s": duration_s,
                "ext": str(it.get("ext") or "mp3") or "mp3",
                "cover_url": str(it.get("cover_url") or ""),
                # 原唱排序字段（lx 服务侧透传，见 lxmusic-service/app.py 的 _is_original_kg）
                # is_original 只有酷狗提供硬标识，其余源为 None → 交给 _original_rank 启发式
                "is_original": it.get("is_original"),
                "has_version_marker": it.get("has_version_marker"),
                "union_cover": str(it.get("union_cover") or ""),
                "file_size": file_size,
                "lyric": "",
                "verified": it.get("verified") is True,
            })
        return _SearchItems([it for it in items if is_playable_online_track(it, allow_paywall=allow_paywall)], partial=bool(data.get("errors")))
    except Exception as e:
        logger.warning("Failed to fetch online search from lxmusic: %s", e)
        return None


# 音质档（从低到高）：网易 = musicbox QUALITY_WHITELIST 全集；
# lx = 洛雪源脚本三档（128k/320k/flac 对应 standard/high/lossless）
_NETEASE_QUALITY_LADDER = ["standard", "higher", "exhigh", "lossless", "hires", "jymaster"]
_LX_QUALITY_LADDER = ["standard", "high", "lossless"]


def quality_order(ladder: "list[str] | tuple[str, ...]", mode: "str | None", primary: "str | None" = None) -> list[str]:
    """按音质模式排档序（ladder 从低到高）。

    high（默认）：锚定 primary（缺省取最高档）向下逐级，失败逐级降档；
    balanced：取中间档（偶数档取中间偏高），失败先向下再向上；
    smooth：从低到高，优先最省流量的档。
    """
    seq = [q for q in ladder if q]
    if not seq:
        return []
    mode = str(mode or "high").strip().lower()
    if mode == "smooth":
        return list(seq)
    if mode == "balanced":
        mid = len(seq) // 2
        return [seq[mid]] + list(reversed(seq[:mid])) + seq[mid + 1:]
    if primary in seq:
        idx = seq.index(primary)
    else:
        idx = len(seq) - 1
    return [seq[idx]] + list(reversed(seq[:idx]))


async def resolve_lx_url(client: httpx.AsyncClient, song_id: str) -> "dict | None":
    """洛雪音乐源直链解析：song_id 形如 "lx:kg:<hash>"。"""
    primary = str(CONF.get("lx_quality") or "lossless").strip()
    qualities = quality_order(_LX_QUALITY_LADDER, CONF.get("quality_mode"), primary)
    if primary and primary not in qualities:
        qualities.insert(0, primary)

    for q in qualities:
        try:
            r = await client.get(
                "/api/v1/track/url",
                params={"id": song_id, "quality": q},
                # 略高于 lxmusic 端点总预算（LX_URL_TIMEOUT，默认 20s）：让端点自己
                # 返回 404/502 完成降档缓存，而不是在 proxy 侧掐断后反复重解析
                timeout=22.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict) and inner.get("url"):
                        return inner
        except Exception as e:
            logger.warning("resolve_lx_url error for %s (quality=%s): %s", song_id, q, e)
    return None


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str) -> str | None:
    primary = str(CONF.get("netease_quality") or "lossless").strip()
    qualities = quality_order(_NETEASE_QUALITY_LADDER, CONF.get("quality_mode"), primary)
    if primary and primary not in qualities:
        qualities.insert(0, primary)

    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s", song_id, q, e)
    return None


def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
    selected: bool = False,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    online_limit = CONF["online_limit"]
    if not selected:
        start = 0 if page == 1 else online_limit + (page - 2) * size
        raw_page = raw_items[start:start + (online_limit if page == 1 else size)]
    else:
        raw_page = raw_items
    filtered_online = []
    for online_item in raw_page:
        if not is_playable_online_track(online_item, require_id=True,
                                        allow_paywall=_search_allow_paywall()):
            continue
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    page_online = filtered_online

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + sum(1 for item in raw_items if is_playable_online_track(item, require_id=True, allow_paywall=_search_allow_paywall()) and (title_from_track(item), artist_from_track(item)) not in existing_keys)

    return upstream_json


_FAKE_GUID_REVERSE: dict[str, str] = {}
# 封面 id 独立盐（v2.6.16）：早期版本封面与曲目共用同一伪装 id，故障窗口下发的
# 黑胶默认图/占位图被客户端按 URL 强缓存（max-age=86400），换真图后 URL 不变、
# 客户端永远不回源。封面 id 改用独立盐生成全新 32-hex，URL 全部刷新即绕开旧缓存；
# 曲目 guid 伪装保持旧盐不变，收藏/播放历史/歌单回读不受影响。
_FAKE_COVER_REVERSE: dict[str, str] = {}
# 2026-10-10 换盐 v2→v3：网易对无封面歌曲返回的 3955B 默认灰图曾被当作真图放行，
# 并以 max-age=86400 下发到客户端（App/Web 已强缓存该 URL，URL 不变就不回源）。
# 换盐使全部 coverId 重新生成 → URL 全刷新，立即绕开旧缓存；配套修复见
# _COVER_MIN_REAL_BYTES（默认封面判废）。旧 id 仍可由 cover_fake_map.json 反解，
# 收藏/播放历史/歌单回读不受影响。
_COVER_FAKE_SALT = "fnmusic-ext::cover::v3::"
_REGISTRY_WARMED = False
_ONLINE_ID_RE = re.compile(r"online:[A-Za-z0-9_:\-]+")

# 封面伪装 id 反查表落盘（★ 2026-10-10）。
# coverId 是 md5 单向伪装，重启后内存表清空，ensure_registry_warm 只能从
# 收藏/历史/歌单附加/推荐缓存重建 —— 而**搜索结果**不落任何快照，于是重启后
# 客户端仍持有的旧 coverId 反解失败 → 被误判为本地 guid → 走本地链路全空 →
# 封面整片回落占位图（本轮「最近添加专辑」即此因）。这里把映射持久化。
_COVER_FAKE_MAP_PATH = (
    os.environ.get("FNMUSIC_COVER_FAKE_MAP", "").strip()
    or os.path.join(_HOME, "cover_fake_map.json")
)
_COVER_FAKE_MAX = 10000
_COVER_PERSIST_MIN_INTERVAL_S = 10.0
_COVER_PERSIST_LAST = 0.0


def _persist_cover_fake_map(force: bool = False) -> None:
    """落盘封面伪装 id → 真实 guid 映射（节流 + 上限 + 原子替换）。

    ★ 2026-10-10：写盘前**先合并磁盘已有条目**。否则「进程刚启动、内存表里只有本次
    新生成的一两条」时会把上一轮持久化的整表覆盖掉 —— 实测 290 条被写成 1 条，
    客户端仍持有的旧 coverId 随即反解失败、被误判成本地 guid，封面整片回落占位图。
    合并时磁盘条目在前、内存条目在后（内存的更新），并顺带补全内存表。
    """
    global _COVER_PERSIST_LAST
    now = time.monotonic()
    if not force and now - _COVER_PERSIST_LAST < _COVER_PERSIST_MIN_INTERVAL_S:
        return
    _COVER_PERSIST_LAST = now
    try:
        directory = os.path.dirname(_COVER_FAKE_MAP_PATH) or "."
        os.makedirs(directory, exist_ok=True)
        merged: dict = {}
        try:
            with open(_COVER_FAKE_MAP_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict) and isinstance(disk.get("entries"), dict):
                merged.update({str(k): str(v) for k, v in disk["entries"].items() if k and v})
        except Exception:
            pass
        merged.update({str(k): str(v) for k, v in _FAKE_COVER_REVERSE.items() if k and v})
        _FAKE_COVER_REVERSE.update(merged)
        items = list(merged.items())
        if len(items) > _COVER_FAKE_MAX:
            items = items[-_COVER_FAKE_MAX:]
        payload = {"version": 1, "savedAt": int(time.time()), "entries": dict(items)}
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _COVER_FAKE_MAP_PATH)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:  # noqa: BLE001
        logger.debug("cover fake map persist failed: %s", type(e).__name__)


def _load_cover_fake_map_persisted() -> int:
    """重启后从磁盘恢复封面伪装 id 反查表。返回恢复条数。"""
    try:
        with open(_COVER_FAKE_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return 0
    n = 0
    for fake, real in entries.items():
        if fake and real and str(fake) not in _FAKE_COVER_REVERSE:
            _FAKE_COVER_REVERSE[str(fake)] = str(real)
            n += 1
    if n:
        logger.info("cover fake map restored: %d entries", n)
    return n


def _cover_fake_guid(real_guid: str) -> str:
    """封面专用伪装 id（独立盐，32-hex）：换盐即换 URL，破客户端对旧占位图的强缓存。"""
    real = str(real_guid or "")
    if not real:
        return ""
    fake = hashlib.md5(f"{_COVER_FAKE_SALT}{real}".encode()).hexdigest()
    if _FAKE_COVER_REVERSE.get(fake) != real:
        _FAKE_COVER_REVERSE[fake] = real
        _persist_cover_fake_map()      # ★ 节流落盘，供重启后反解（搜索结果不落快照）
    return fake


def fake_official_guid(real_guid: str) -> str:
    """在线 guid 确定性映射为官方 32-hex 形态。

    官方 App 会按 id 格式过滤条目（非 32-hex 的 online: 前缀 id 整条被丢弃，
    症状为收藏/播放历史列表空白或条目消失），且要求收藏/播放回读的 guid 与
    客户端自身持有的一致，因此所有下发的在线 id 必须统一伪装成官方形态。
    同时把封面盐的伪装 id 一并登记进反查表：ensure_registry_warm 重建时走本函数，
    曲目假 id 与封面假 id 即可一并恢复（客户端持有的封面 id 也要能反解回真实 guid）。
    """
    fake = hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()
    _FAKE_GUID_REVERSE.setdefault(fake, real_guid)
    _cover_fake_guid(real_guid)
    return fake


# ==== 在线曲目封面直链登记表（2026-10-17） ====
# 背景：音乐页每行曲目都带 coverId，客户端只按 coverId 回源；而服务端解析在线曲目
# 封面要靠 _online_info 现查上游（网易源还要 info+lyric 两次调用）。列表一次渲染
# 18 行时上游排队，_online_info 的 4 秒预算集体超时 → 整片回落占位图（ARM 榜单曲目
# 实测：串行每首约 4s 成功、并发 9 首全落占位图）。
# 实际上「下发曲目行」的那一刻我们就握有封面直链（musicbox 的 album_pic_url /
# lx 的 cover_url），这里按真实 guid 登记并持久化：封面端点直接命中，零上游调用。
# 与 coverId 反查表同思路（md5 单向，重启后必须靠落盘恢复）。
_TRACK_COVER_URLS: dict[str, str] = {}
_TRACK_COVER_MAX = 6000
_TRACK_COVER_PATH = (
    os.environ.get("FNMUSIC_TRACK_COVER_MAP", "").strip()
    or os.path.join(_HOME, "track_cover_map.json")
)
_TRACK_COVER_PERSIST_MIN_INTERVAL_S = 10.0
_TRACK_COVER_PERSIST_LAST = 0.0
_TRACK_COVER_LOADED = False


def _track_cover_load() -> None:
    """首次查询时懒加载落盘表（避免启动期多做一次 IO）。"""
    global _TRACK_COVER_LOADED
    if _TRACK_COVER_LOADED:
        return
    _TRACK_COVER_LOADED = True
    try:
        with open(_TRACK_COVER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            for k, v in entries.items():
                if k and v:
                    _TRACK_COVER_URLS[str(k)] = str(v)
    except Exception:
        pass


def _persist_track_cover_map(force: bool = False) -> None:
    """落盘（节流 + 上限 + 原子替换，写前合并磁盘已有条目，防止整表被覆盖）。"""
    global _TRACK_COVER_PERSIST_LAST
    now = time.monotonic()
    if not force and now - _TRACK_COVER_PERSIST_LAST < _TRACK_COVER_PERSIST_MIN_INTERVAL_S:
        return
    _TRACK_COVER_PERSIST_LAST = now
    try:
        directory = os.path.dirname(_TRACK_COVER_PATH) or "."
        os.makedirs(directory, exist_ok=True)
        merged: dict = {}
        try:
            with open(_TRACK_COVER_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict) and isinstance(disk.get("entries"), dict):
                merged.update({str(k): str(v) for k, v in disk["entries"].items() if k and v})
        except Exception:
            pass
        merged.update({str(k): str(v) for k, v in _TRACK_COVER_URLS.items() if k and v})
        _TRACK_COVER_URLS.update(merged)
        items = list(merged.items())
        if len(items) > _TRACK_COVER_MAX:
            items = items[-_TRACK_COVER_MAX:]
        payload = {"version": 1, "savedAt": int(time.time()), "entries": dict(items)}
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _TRACK_COVER_PATH)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:  # noqa: BLE001
        logger.debug("track cover map persist failed: %s", type(e).__name__)


def _track_cover_remember(guid: str, url: str) -> None:
    """登记曲目封面直链（幂等；非 online guid / 非 http 直链直接忽略）。"""
    g = str(guid or "")
    u = str(url or "").strip()
    if not g.startswith("online:") or not u.startswith(("http://", "https://")):
        return
    if _TRACK_COVER_URLS.get(g) == u:
        return
    _TRACK_COVER_URLS[g] = u
    if len(_TRACK_COVER_URLS) > _TRACK_COVER_MAX:
        for key in list(_TRACK_COVER_URLS)[: len(_TRACK_COVER_URLS) - _TRACK_COVER_MAX]:
            _TRACK_COVER_URLS.pop(key, None)
    _persist_track_cover_map()


def _track_cover_lookup(guid: str) -> str:
    _track_cover_load()
    return _TRACK_COVER_URLS.get(str(guid or ""), "")


# 专辑伪装 guid 登记表（issue #22）：fake 32-hex → 专辑元信息。
# 与 _FAKE_GUID_REVERSE 同生命周期（内存 + ensure_registry_warm 磁盘重建）；
# /search/album 的在线专辑锚点额外落盘持久化（这类锚点不存在于任何快照存储，
# 重启后 warm 重建不到，见 _persist_album_registry/_load_album_registry_persisted）。
_FAKE_ALBUM_REGISTRY: dict[str, dict] = {}
_ALBUM_REGISTRY_PATH = (
    os.environ.get("FNMUSIC_ALBUM_REGISTRY", "").strip()
    or os.path.join(_HOME, "album_registry.json")
)
_ALBUM_REGISTRY_PERSIST_MAX = 256
_ALBUM_REGISTRY_TTL_S = 14 * 86400
_ALBUM_PERSIST_MIN_INTERVAL_S = 5.0
_ALBUM_PERSIST_LAST = 0.0


def _album_name_from_obj(track_obj: dict) -> str:
    """从曲目对象（VO 快照或 info 形状）提取专辑名。"""
    album = track_obj.get("album")
    if isinstance(album, dict):
        return str(album.get("name") or "").strip()
    return str(track_obj.get("albumName") or track_obj.get("album") or "").strip()


def _person_name(value: Any) -> str:
    """歌手字段：字符串原样，对象取 name，列表取第一个有名字的成员。"""
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, list):
        for item in value:
            name = _person_name(item)
            if name:
                return name
        return ""
    return str(value or "").strip()


def _tag_fields(info: dict | None) -> tuple[str, str, str]:
    """落盘/历史用的标题、歌手、专辑。恒为纯字符串。

    推荐歌单等前端曲目里 album 是 {name, guid, artists, coverId}，
    不能 str() 整个对象。对象 name 为空时再看 albumName。
    """
    src = info if isinstance(info, dict) else {}
    title_raw = src.get("title")
    if isinstance(title_raw, dict) or not str(title_raw or "").strip():
        title = _person_name(title_raw) or _person_name(src.get("name"))
    else:
        title = str(title_raw).strip()
    artist = _person_name(src.get("artist")) or _person_name(src.get("artists"))
    album = _album_name_from_obj(src) or str(src.get("albumName") or "").strip()
    return title, artist, album


def _cover_url_of(info: dict | None) -> str:
    src = info if isinstance(info, dict) else {}
    return str(src.get("cover_url") or src.get("coverUrl") or src.get("coverURL") or "").strip()


def _item_meta_richness(it: dict) -> int:
    """条目元数据丰富度：title/album 各 1 分。用于"更全的快照覆盖残缺的"合并。"""
    it = it if isinstance(it, dict) else {}
    return int(bool(str(it.get("title") or "").strip())) + int(bool(str(it.get("album") or "").strip()))


def _persist_album_registry(force: bool = False) -> None:
    """把内存专辑登记表落盘（节流 + 上限 + TTL），供重启后 ensure_registry_warm 恢复。

    只持久化 persist=True 的登记（/search/album 的在线专辑锚点与聚合代表曲目），
    而非全部曲目衍生登记——后者可从收藏/历史/推荐缓存重建，落盘只会膨胀。
    """
    global _ALBUM_PERSIST_LAST
    now = time.monotonic()
    if not force and now - _ALBUM_PERSIST_LAST < _ALBUM_PERSIST_MIN_INTERVAL_S:
        return
    _ALBUM_PERSIST_LAST = now
    try:
        os.makedirs(os.path.dirname(_ALBUM_REGISTRY_PATH) or ".", exist_ok=True)
        now_ts = int(time.time())
        entries = []
        for entry in _FAKE_ALBUM_REGISTRY.values():
            if not entry.get("persist"):
                continue
            entries.append({
                "track_guid": str(entry.get("track_guid") or ""),
                "source": str(entry.get("source") or ""),
                "album": str(entry.get("album") or ""),
                "album_id": str(entry.get("album_id") or ""),
                "item": entry.get("item") if isinstance(entry.get("item"), dict) else {},
                "saved_at": now_ts,
            })
        payload = {"version": 1, "savedAt": now_ts, "entries": entries[-_ALBUM_REGISTRY_PERSIST_MAX:]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_ALBUM_REGISTRY_PATH) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _ALBUM_REGISTRY_PATH)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:
        logger.debug("album registry persist failed: %s", type(e).__name__)


def _load_album_registry_persisted() -> int:
    """重启后从磁盘恢复 /search/album 登记的在线专辑（跨会话可反解，消除
    "重启后点旧搜索结果进专辑回落官方无此专辑"的窗口）。返回恢复条数。"""
    try:
        with open(_ALBUM_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    cutoff = time.time() - _ALBUM_REGISTRY_TTL_S
    restored = 0
    for row in entries:
        if not isinstance(row, dict):
            continue
        track_guid = str(row.get("track_guid") or "")
        saved_at = row.get("saved_at")
        if not track_guid or (isinstance(saved_at, (int, float)) and saved_at < cutoff):
            continue
        fake = fake_official_guid(f"{track_guid}:album")
        if fake in _FAKE_ALBUM_REGISTRY:
            continue
        _FAKE_ALBUM_REGISTRY[fake] = {
            "source": str(row.get("source") or source_from_online_guid(track_guid)),
            "album": str(row.get("album") or ""),
            "track_guid": track_guid,
            "item": row.get("item") if isinstance(row.get("item"), dict) else {},
            "album_id": str(row.get("album_id") or ""),
            "persist": True,
        }
        restored += 1
    return restored


def register_fake_album(
    track_guid: str, album_name: str, item: dict | None = None, album_id: str = "", persist: bool = False
) -> None:
    """登记专辑伪装 guid → (source, 专辑名, 所属曲目条目)，供专辑详情拦截反解。

    曲目 VO 构造时同步登记（build_online_track 统一挂接，搜索/推荐/收藏/历史
    全路径覆盖）；假 id 是确定性 md5，重启后由 ensure_registry_warm 从收藏/
    历史/歌单附加/推荐缓存重建时同样登记，保证跨会话可反解。
    album_id：netease 真实专辑 id（/search/album 在线专辑直达详情用，可空）。
    persist：/search/album 的在线专辑登记传 True——这类锚点不落在任何持久快照里，
    重启后无法由 warm 重建，必须落盘（其余曲目衍生登记不需要，避免写放大）。
    """
    src = source_from_online_guid(track_guid)
    if not src:
        return
    fake = fake_official_guid(f"{track_guid}:album")
    entry = _FAKE_ALBUM_REGISTRY.get(fake)
    if entry is None:
        _FAKE_ALBUM_REGISTRY[fake] = {
            "source": src,
            "album": str(album_name or "").strip(),
            "track_guid": track_guid,
            "item": dict(item) if isinstance(item, dict) and item else {},
            "album_id": str(album_id or ""),
            "persist": bool(persist),
        }
        if persist:
            _persist_album_registry()
        return
    # 已登记：残缺快照可被更全的后到登记覆盖——首次解析失败时 build_metadata_payload
    # 会用 stub（仅 id/source，issue #28 真机复现）先占位，若"首个非空即终"，
    # 之后的全量信息永远进不来，专辑合成/封面/命名全部拿到残缺条目。
    if album_name and not entry.get("album"):
        entry["album"] = str(album_name).strip()
    if album_id and not entry.get("album_id"):
        entry["album_id"] = str(album_id)
    if isinstance(item, dict) and item:
        cur = entry.get("item") if isinstance(entry.get("item"), dict) else {}
        if _item_meta_richness(item) > _item_meta_richness(cur):
            entry["item"] = dict(item)
    if persist:
        # 更新路径同样尝试落盘（节流 5s/次；更全的专辑名/album_id/item 需要跟着持久化）
        if not entry.get("persist"):
            entry["persist"] = True
        _persist_album_registry()


def resolve_fake_album(candidate: str) -> dict | None:
    """客户端回传的专辑伪装 guid → 登记条目；非伪装专辑（含官方 guid）返回 None。"""
    if not candidate:
        return None
    fake = candidate[6:] if candidate.startswith("track_") else candidate
    if not re.fullmatch(r"[0-9a-f]{32}", fake or ""):
        return None
    ensure_registry_warm()
    entry = _FAKE_ALBUM_REGISTRY.get(fake)
    if entry is None:
        # 专辑登记丢失（如旧会话仅命中曲目反查表）：按确定性映射重建最小条目
        real = _FAKE_GUID_REVERSE.get(fake) or ""
        if real.endswith(":album") and is_online_guid(real):
            track_guid = real[: -len(":album")]
            entry = {
                "source": source_from_online_guid(track_guid),
                "album": "",
                "track_guid": track_guid,
                "item": {},
            }
            _FAKE_ALBUM_REGISTRY[fake] = entry
    return entry or None


def album_entry_from_real_guid(real_guid: str) -> "dict | None":
    """真实形态专辑 guid（"online:...:album"）→ 登记条目；封面拦截按 cover_url 直出用。"""
    raw = str(real_guid or "")
    if not raw.endswith(":album"):
        return None
    fake = fake_official_guid(raw)
    return _FAKE_ALBUM_REGISTRY.get(fake)


def _register_fakes_from_items(items) -> None:
    for it in items or []:
        if not isinstance(it, dict):
            continue
        g = str(it.get("guid") or "")
        nested = it.get("track") if isinstance(it.get("track"), dict) else None
        if is_online_guid(g):
            fake_official_guid(g)
            # 专辑假 id 一并登记（issue #22）：收藏/历史/歌单附加/推荐缓存里的
            # 快照带专辑名，重建时保住专辑详情的反解依据
            src_obj = nested if nested is not None else it
            register_fake_album(g, _album_name_from_obj(src_obj), _snapshot_to_info(src_obj))
        elif nested is not None:
            _register_fakes_from_items([nested])


def _iter_registry_jsons(directory: str):
    """列出目录下的 json 文件（含一层子目录，兼容 recommend_cache/<user>/x.json）。"""
    try:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if name.endswith(".json") and os.path.isfile(path):
                yield path
            elif os.path.isdir(path):
                for sub in sorted(os.listdir(path)):
                    if sub.endswith(".json"):
                        yield os.path.join(path, sub)
    except Exception:
        return


def _register_fakes_from_recommend_bundle(data) -> None:
    if not isinstance(data, dict):
        return
    _register_fakes_from_items(data.get("tracks"))
    playlist = data.get("playlist")
    if isinstance(playlist, dict):
        for key in ("guid", "coverId"):
            g = str(playlist.get(key) or "")
            if is_online_guid(g):
                fake_official_guid(g)


def _register_fakes_from_plt(data) -> bool:
    """playlist_tracks 存储：items 是 {歌单guid: [条目]} 字典形状，逐桶注册曲目假 id。"""
    if isinstance(data, dict) and isinstance(data.get("items"), dict):
        for bucket in data["items"].values():
            _register_fakes_from_items(bucket)
        return True
    return False


def ensure_registry_warm() -> None:
    """从收藏/历史/歌单附加/推荐缓存重建 fake→real 映射（假 id 是确定性 md5，可完整重建）。

    服务重启后内存注册表为空，而客户端仍持有重启前学到的假 id；此时上报的
    播放/收藏事件若反解失败会被当作官方事件透传而丢失。收藏与历史存储里
    出现过的 guid 覆盖客户端会回传的曲目假 id；歌单附加条目同理；推荐缓存
    里的歌单/曲目 guid 覆盖客户端会回传的歌单封面假 id（歌单 coverId 也走伪装下发）。
    """
    global _REGISTRY_WARMED
    if _REGISTRY_WARMED:
        return
    _REGISTRY_WARMED = True
    for directory in (CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites"),
                      CONF.get("plt_dir") or os.path.join(_HOME, "playlist_tracks"),
                      dailyrec.play_history_dir(),
                      dailyrec.recommend_cache_dir(),
                      nmpl.cache_dir(),
                      pubpl.cache_dir()):
        for path in _iter_registry_jsons(directory):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("tracks") is not None:
                _register_fakes_from_recommend_bundle(data)
                continue
            if _register_fakes_from_plt(data):
                continue
            _register_fakes_from_items(data.get("items") if isinstance(data, dict) else data)
    # /search/album 的在线专辑锚点（不在任何快照存储里）从落盘登记恢复
    try:
        _load_album_registry_persisted()
    except Exception as e:
        logger.debug("album registry restore failed: %s", type(e).__name__)
    # 封面伪装 id 反查表落盘恢复（★ 2026-10-10）：搜索结果不落任何快照，
    # 客户端重启后回传的旧 coverId 只能靠这个反解，否则封面整片回落占位图。
    try:
        _load_cover_fake_map_persisted()
    except Exception as e:
        logger.debug("cover fake map restore failed: %s", type(e).__name__)


def resolve_real_guid(candidate: str) -> str:
    if not candidate:
        return candidate
    # 封面盐伪装 id（v2.6.16 起下发的 coverId）先行反解
    if candidate in _FAKE_COVER_REVERSE:
        return _FAKE_COVER_REVERSE[candidate]
    if candidate in _FAKE_GUID_REVERSE:
        return _FAKE_GUID_REVERSE[candidate]
    if candidate.startswith("track_") and len(candidate) > 6:
        stripped = candidate[6:]
        if stripped in _FAKE_COVER_REVERSE:
            return _FAKE_COVER_REVERSE[stripped]
        if stripped in _FAKE_GUID_REVERSE:
            return _FAKE_GUID_REVERSE[stripped]
        if re.fullmatch(r"[0-9a-f]{32}", stripped):
            ensure_registry_warm()
            if stripped in _FAKE_COVER_REVERSE:
                return _FAKE_COVER_REVERSE[stripped]
            if stripped in _FAKE_GUID_REVERSE:
                return _FAKE_GUID_REVERSE[stripped]
    if re.fullmatch(r"[0-9a-f]{32}", candidate or ""):
        ensure_registry_warm()
        if candidate in _FAKE_COVER_REVERSE:
            return _FAKE_COVER_REVERSE[candidate]
        if candidate in _FAKE_GUID_REVERSE:
            return _FAKE_GUID_REVERSE[candidate]
    return candidate


def disguise_client_json(obj):
    """递归把客户端可见数据里的 online: 前缀 id 全部替换为官方 32-hex 形态。

    所有下发出口（搜索/元数据/歌词/收藏/历史/每日推荐曲目）统一伪装；
    入口（extract_guid/create/delete/event）经 resolve_real_guid 反解回真实 guid。
    coverId 按官方惯例带 track_ 前缀；字符串内嵌的 guid（如 audioSpec.path）
    一并替换。纯内存变换，不落盘、不触碰官方库，restore 后自然消失。
    """
    if isinstance(obj, dict):
        out = {}
        # v2.6.18：本地曲目 coverId 为 null 时，用本行 guid 补上。
        # 官方库里多数曲目 cover_guid 为空，列表下发 null，客户端便不会请求
        # /static/cover —— 哪怕服务端已能解析出封面，那几行仍是空白。
        # 必须是「本行自己的 guid」：服务端 _local_cover_response 会把它解析到
        # 所属专辑的封面文件（新增的 track→album 索引），零外部依赖、毫秒级。
        _local_guid = ""
        for _lk in ("guid", "trackGUID", "trackGuid"):
            _lv = obj.get(_lk)
            if isinstance(_lv, str) and not _lv.startswith("online:") \
                    and re.fullmatch(r"[0-9a-fA-F]{32}", _lv):
                _local_guid = _lv
                break
        # 在线曲目下发即登记封面直链（2026-10-17）：缓存曲目（tracks-*.json）不会重跑
        # build_online_track，只靠那条路径登记会漏掉缓存命中；这里统一兜一遍，
        # 保证「只要这行发给了客户端，服务端就握有它的封面直链」。
        _row_guid = obj.get("guid") or obj.get("trackGUID") or obj.get("trackGuid")
        if isinstance(_row_guid, str) and _row_guid.startswith("online:"):
            _track_cover_remember(
                _row_guid,
                obj.get("coverUrl") or obj.get("coverURL") or obj.get("cover_url") or "",
            )
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("online:"):
                if k in ("coverId", "cover_id"):
                    out[k] = "track_" + _cover_fake_guid(v)
                else:
                    out[k] = fake_official_guid(v)
            elif k in ("coverId", "cover_id") and not v and _local_guid:
                out[k] = _local_guid
            else:
                out[k] = disguise_client_json(v)
        return out
    if isinstance(obj, list):
        return [disguise_client_json(x) for x in obj]
    if isinstance(obj, str):
        return _ONLINE_ID_RE.sub(lambda m: fake_official_guid(m.group(0)), obj)
    return obj


def disguise_official_items(official: list) -> list:
    """就地改写「官方原样下发」的那半边列表（v2.6.18）。

    收藏/歌单/最近播放三个端点都是 online + official 合并下发：online 半边由
    build_*_track_obj 产出、已逐条 disguise 过；official 半边却是官方信封原样塞进
    data["list"] 后整体 JSONResponse 出去的，从未经改写。官方库里多数曲目的
    cover_guid 为 NULL，客户端拿到 coverId=null 就不会去请求 /static/cover ——
    于是服务端明明解析得出封面，那一行在 App 里仍是灰色音符占位符。

    这里对 official 半边补一次同样的改写：online: 前缀伪装 + coverId 空值兜底。
    幂等（对已伪装过的对象再跑一次结果不变），所以对 online 半边也无副作用。
    """
    if not isinstance(official, list):
        return official
    for i, item in enumerate(official):
        if isinstance(item, dict):
            official[i] = disguise_client_json(item)
    return official


def disguise_envelope_list(envelope: dict) -> dict:
    """改写信封里 data.list 的每个条目（v2.6.18）。

    用于「鉴权探测被拒→降级为官方原样」这类分支：那几条return 直接把官方信封
    原样下发，列表同样没经过 disguise_client_json，官方曲目 coverId=null 的行
    在App 里就还是占位符。降级只应砍掉本地注入，不该连带砍掉封面修复。
    """
    if not isinstance(envelope, dict):
        return envelope
    data = envelope.get("data")
    if isinstance(data, dict):
        disguise_official_items(data.get("list"))
    return envelope


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return resolve_real_guid(path_guid)
    return resolve_real_guid(
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return resolve_real_guid(str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        ))
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


def _background_jobs_enabled() -> bool:
    """后台任务只在真实服务环境启用（takeover 注入 FNMUSIC_BACKGROUND_JOBS=1）；
    默认关闭，单元测试与手动导入 app 永不触发，避免误扫真实曲库目录。
    """
    return os.environ.get("FNMUSIC_BACKGROUND_JOBS", "").lower() in ("1", "true", "yes")


async def _lyric_orphan_sweeper() -> None:
    """启动即清一次、之后每 30 分钟复扫：清掉本插件写进曲库、音频已消失的孤儿
    .lrc（安全边界见 cache_gc.sweep_orphan_lyrics，用户自有歌词永不触碰）。
    """
    while True:
        try:
            dirs: list[str] = []
            for d in (tee_save_dir(), detect_library_dir()):
                if d and d not in dirs and d != CONF["cache_dir"]:
                    dirs.append(d)
            removed = await asyncio.to_thread(sweep_orphan_lyrics, CONF["cache_dir"], dirs)
            if removed:
                logger.info("orphan lyric sweep removed %d file(s): %s", len(removed), removed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("orphan lyric sweep failed: %s", e)
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    # 【必须先加载 .env 再打印配置】CONF 全部由 os.environ 求值，而 .env 的值
    # 只有 apply_env_hot_reload() 才会写进 os.environ —— 它原先唯一的调用点在
    # _env_watch_loop() 里，**且仅当 .env 文件发生变化时才调**。
    # 后果：容器启动时若 .env 尚不存在（装应用时它才被创建），watch loop 会一直
    # 等到文件出现才加载，于是整个进程生命周期内 os.environ 都是空的：
    # 大模型三键拿不到 -> llm_enabled() 恒为 False -> 每日推荐跳过 LLM 层，
    # 直接落到 local-random（表现为「配了大模型，歌还是前几天的」）。
    # 启动时主动加载一次，三个症状一起消失。
    try:
        _loaded = apply_env_hot_reload()
        if _loaded:
            logger.info("启动时已加载 .env: %s", ",".join(sorted(_loaded)))
    except Exception as exc:  # noqa: BLE001 —— 加载失败不能挡住启动
        logger.warning("启动时加载 .env 失败（配置将用默认值）: %s", exc)

    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  llm_enabled = %s", dailyrec.llm_enabled())
    logger.info("==================================")

    sweeper_task = asyncio.create_task(_lyric_orphan_sweeper()) if _background_jobs_enabled() else None
    env_task = asyncio.create_task(_env_watch_loop()) if (
        _background_jobs_enabled() and CONF.get("env_watch", True)
    ) else None
    created_upstream = False
    created_musicdl = False
    created_musicbox = False
    created_lx = False
    created_llm = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicdl_client", None) is None:
        fastapi_app.state.musicdl_client = httpx.AsyncClient(
            base_url=CONF["musicdl_url"],
            timeout=45.0,
        )
        created_musicdl = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "lx_client", None) is None:
        fastapi_app.state.lx_client = httpx.AsyncClient(
            base_url=CONF["lx_url"],
            timeout=25.0,
        )
        created_lx = True

    if getattr(fastapi_app.state, "llm_client", None) is None:
        fastapi_app.state.llm_client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        created_llm = True

    try:
        yield
    finally:
        await pubpl.shutdown()
        tasks = [entry["task"] for entry in _SEARCH_CACHE.values() if entry.get("task") and not entry["task"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if sweeper_task:
            sweeper_task.cancel()
            await asyncio.gather(sweeper_task, return_exceptions=True)
        if env_task:
            env_task.cancel()
            await asyncio.gather(env_task, return_exceptions=True)
        try:
            await tc.shutdown_all()
        except Exception:  # noqa: BLE001
            pass
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicdl and getattr(fastapi_app.state, "musicdl_client", None):
            await fastapi_app.state.musicdl_client.aclose()
            fastapi_app.state.musicdl_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_lx and getattr(fastapi_app.state, "lx_client", None):
            await fastapi_app.state.lx_client.aclose()
            fastapi_app.state.lx_client = None
        if created_llm and getattr(fastapi_app.state, "llm_client", None):
            await fastapi_app.state.llm_client.aclose()
            fastapi_app.state.llm_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)


@app.middleware("http")
async def log_client_requests(request: Request, call_next):
    """记录 /music/ 请求的方法/路径/状态/UA，供 App 端兼容问题远程定位。

    不记 query（guid 无必要），UA 折叠空白并截断，配合 takeover 日志白名单的固定格式。
    """
    response = await call_next(request)
    if request.url.path.startswith("/music/"):
        ua = re.sub(r"\s+", " ", request.headers.get("user-agent") or "-")[:100]
        logger.info(
            "client request %s %s status=%s ua=%s",
            request.method,
            request.url.path,
            response.status_code,
            ua,
        )
    return response


@app.get("/_ext/livez")
async def ext_livez():
    return {"ok": True, "service": "fnmusic-ext", "pid": os.getpid()}


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    async def probe(name: str, client: httpx.AsyncClient, path: str) -> dict:
        try:
            response = await asyncio.wait_for(client.get(path, timeout=2.0), timeout=2.4)
            healthy = response.status_code < 500 if name == "upstream" else response.status_code == 200
            detail: dict = {"status": "ok" if healthy else "fail", "http_status": response.status_code}
            if name != "upstream" and healthy:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        detail["dependency"] = payload
                        if payload.get("ok") is False:
                            detail["status"] = "fail"
                    else:
                        detail["status"] = "fail"
                except ValueError:
                    detail["status"] = "fail"
                    detail["error"] = "invalid health JSON"
            return detail
        except Exception as exc:
            return {"status": "fail", "error": type(exc).__name__}

    checks = [("upstream", True, get_upstream_client, "/music/api/v1/search/track?keyword=healthz_probe"),
              ("musicdl", CONF.get("musicdl_enabled"), get_musicdl_client, "/healthz"),
              ("musicbox", CONF.get("netease_enabled"), get_musicbox_client, "/healthz"),
              ("lxmusic", CONF.get("lx_enabled"), get_lx_client, "/healthz")]
    enabled = [(name, getter, path) for name, on, getter, path in checks if on]
    results = await asyncio.gather(*(probe(name, getter(request.app), path) for name, getter, path in enabled))
    details = {name: {"status": "disabled"} for name, on, _, _ in checks if not on}
    details.update({name: result for (name, _, _), result in zip(enabled, results)})
    statuses = {name: value["status"] for name, value in details.items()}
    failed = [name for name, status in statuses.items() if status == "fail"]
    source_ok = any(statuses[name] == "ok" for name in ("musicdl", "musicbox", "lxmusic"))
    return {"ok": statuses["upstream"] == "ok" and source_ok, "version": get_version(),
            **statuses, "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
            "recommend": {
                "mode": "per-user: source-slot -> llm -> local-random",
                "netease": bool(CONF.get("netease_enabled")),
                "lx": bool(CONF.get("lx_enabled")),
                "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
                "source_slot_claimed": dailyrec.source_slot_claimed(),
                "recent": dailyrec.last_recommend_summary(),
            },
            "degraded": bool(failed), "failures": failed, "details": details}


# ── 搜索预热（2026-10-10）：让点播不必现场解析 ────────────────────────────
# 搜索接口本就已经拿到整页在线 guid，顺手在后台把它们的直链解好。用
# create_task 而非 await：搜索响应时间分毫不变，预热失败只写日志、用户无感。
# 解析/探活复用 resolve_lx_url（含逐档降级），成功后由洛雪侧
# _cache_put/_fresh_probe 缓存（TTL 900s），点播 /track/stream 时命中即毫秒级。
_PREFETCH_TASKS: set = set()
_PREFETCH_RECENT: dict = {}
# 预热判定「播不了」的 guid（含被洛雪判为广告/试听片段的）——组装列表时剔除，
# 让用户看不到注定点不开的条目。TTL 到期后允许重新预热：音源恢复即自动回来，
# 避免一次网络抽风把某首歌永久隐藏。
_PREFETCH_BAD: dict = {}
_PREFETCH_BAD_TTL_S = 1800.0


def _mark_unplayable(guid: str, reason: str = "") -> None:
    """记录「已确认播不了」的在线 guid → 组装列表时剔除，TTL 到期自动放回。

    ★ 2026-10-11：此前只有后台预热（`_prefetch_worker`）会写 `_PREFETCH_BAD`，
    而预热只覆盖列表**前 N 条**（`CONF["prefetch_limit"]`，默认 10）—— 排在后面的
    条目永远不会被探活。实测搜「会开花的云」时用户要的那条落在 **#40**
    （音源侧只有 60s 试听片段，`ad/clip rejected kbps=31.9`），点播 404 后刷新
    仍在列表里 → 反复白点。**点播失败是最可信的「播不了」信号**，直接采信。
    仅对 `lx` 源生效（netease / musicdl 各有自己的降级链）；TTL（30 分钟）到期
    自动放回，避免一次网络抽风把某首歌永久隐藏。
    """
    if not is_online_guid(guid) or source_from_online_guid(guid) != "lx":
        return
    _PREFETCH_BAD[guid] = time.time()
    # ★ 日志文本必须命中 takeover._SAFE_OUTCOME 白名单才会进 journal —— takeover
    # 的 safe_child_log() 会**丢弃所有未匹配的行**（其防泄漏设计，实测
    # `playback failed, marked unplayable: ...` 就是这样被静默吞掉的）。
    # 前缀与白名单条目保持同步；见 tests/test_mark_unplayable.py 的防回归断言。
    logger.warning("playback failed, marked unplayable for %s (%s)", guid, reason or "-")


def _prune_unplayable() -> None:
    """清掉过期的「播不了」记录（音源恢复后自动放回）。

    ★ 2026-10-11：原先只在 `_prefetch_candidates` 里清理，而该函数在预热关闭时
    **提前 return** → 黑名单永不过期、条目被永久隐藏。改为组装列表时也清一次。
    """
    if not _PREFETCH_BAD:
        return
    now = time.time()
    for k, ts in list(_PREFETCH_BAD.items()):
        if now - float(ts or 0) > _PREFETCH_BAD_TTL_S:
            _PREFETCH_BAD.pop(k, None)


def _prefetch_candidates(items) -> list:
    """挑出可预热的在线 guid：仅 lx、按列表顺序取前 N 个、跳过冷却期内的。"""
    if not CONF.get("prefetch_enabled") or not CONF.get("lx_enabled"):
        return []
    try:
        limit = max(1, int(CONF.get("prefetch_limit") or 10))
        cooldown = max(0.0, float(CONF.get("prefetch_cooldown_s") or 300.0))
    except (TypeError, ValueError):
        return []
    now = time.time()
    if len(_PREFETCH_RECENT) > 4000:  # 顺手清理，避免长期运行胀大
        for k, ts in list(_PREFETCH_RECENT.items()):
            if now - float(ts or 0) > cooldown:
                _PREFETCH_RECENT.pop(k, None)
    # 黑名单过期清理：音源恢复后允许重新预热（避免永久误杀）
    for k, ts in list(_PREFETCH_BAD.items()):
        if now - float(ts or 0) > _PREFETCH_BAD_TTL_S:
            _PREFETCH_BAD.pop(k, None)
    out: list = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        guid = online_guid_from_item(it)
        if not is_online_guid(guid) or source_from_online_guid(guid) != "lx":
            continue
        if guid in _PREFETCH_BAD:  # 已知播不了，别再浪费一次解析
            continue
        if now - float(_PREFETCH_RECENT.get(guid) or 0) < cooldown:
            continue
        _PREFETCH_RECENT[guid] = now
        out.append(guid)
        if len(out) >= limit:
            break
    return out


async def _prefetch_worker(fastapi_app: FastAPI, guids: list) -> None:
    """后台并发预解析（受 prefetch_concurrency 限制）。"""
    try:
        slots = max(1, int(CONF.get("prefetch_concurrency") or 4))
    except (TypeError, ValueError):
        slots = 4
    sem = asyncio.Semaphore(slots)
    client = get_lx_client(fastapi_app)

    bad: list = []

    async def one(guid: str) -> None:
        async with sem:
            try:
                res = await resolve_lx_url(client, song_id_from_online_guid(guid))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("prefetch %s error: %s", guid, type(exc).__name__)
                res = None
            if not res:
                bad.append(guid)

    await asyncio.gather(*(one(g) for g in guids), return_exceptions=True)
    if bad:
        now = time.time()
        for g in bad:
            _PREFETCH_BAD[g] = now
        logger.info("prefetch marked unplayable: %d/%d (%s)",
                    len(bad), len(guids), ",".join(bad[:3]))


def schedule_prefetch(fastapi_app: FastAPI, items) -> int:
    """把本页在线曲目排进后台预热；返回排入条数（0 = 无需/已跳过）。"""
    guids = _prefetch_candidates(items)
    if not guids:
        return 0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：先判循环再建协程，否则 _prefetch_worker(...)
        # 的协程对象已构造却无人 await，会留下 "never awaited" 告警与泄漏。
        return 0
    task = loop.create_task(_prefetch_worker(fastapi_app, guids))
    _PREFETCH_TASKS.add(task)
    task.add_done_callback(_PREFETCH_TASKS.discard)
    logger.info("prefetch scheduled: %d tracks (%s)", len(guids), ",".join(guids[:3]))
    return len(guids)


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)
    if keyword:
        _note_user_keyword(_credential_scope(request), keyword)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        # v2.6.18：原先这里原文返回，绕过了 disguise_client_json —— 而本地
        # 专辑/歌手/歌曲列表正是走这条路径。官方对 cover_guid 为空的条目下发
        # coverId=null，客户端拿不到 id 便不会请求 /static/cover，那几行必然空白。
        # 该函数对已有 coverId 与 online: 伪装的行为不变，只补 null 的本地行。
        return JSONResponse(content=disguise_client_json(upstream_json), status_code=upstream_resp.status_code, headers=resp_headers)

    scope = _credential_scope(request)
    if _USER_SEARCH_WORD.get(scope) != keyword:
        return JSONResponse(content=disguise_client_json(upstream_json), status_code=upstream_resp.status_code, headers=resp_headers)

    key = _search_scope(request) + ":" + keyword
    _clean_search_cache()
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        entry = {"items": [], "ts": 0, "keyword": keyword,
                 "scope": _search_scope(request), "credentials": scope, "config": _source_config(), "task": None}
        _set_search_cache(key, entry)
    entry["gen"] = _USER_SEARCH_GEN.get(scope, 0)
    entry["superseded"] = False
    entry["accessed"] = time.time()
    task = entry.get("task")
    if (not task or task.done()) and time.time() - entry["ts"] >= _search_ttl(entry):
        task = asyncio.create_task(_aggregate_search(request, keyword, entry))
        entry["task"] = task
    # 在线搜索只有一个超时。有在线条目就立刻回复（本地在前、在线在后）；
    # 到点仍没有，就放弃这次还没回来的结果，只交本地列表。
    if task and not task.done():
        budget = max(0.05, float(CONF["search_timeout"])) + max(0.0, float(CONF.get("search_debounce_s") or 0))
        deadline = asyncio.get_running_loop().time() + budget
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            if entry.get("items"):
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait({task}, timeout=min(0.05, remaining))
        if not entry.get("items") and not task.done():
            entry["abandoned"] = True
            entry["items"] = []
            entry["partial"] = True
            entry["ts"] = time.time()
            task.cancel()
    local_list = ensure_search_list(upstream_json)
    local_keys = {(title_from_track(x), artist_from_track(x)) for x in local_list}
    total_online = sum(1 for x in entry["items"] if (title_from_track(x), artist_from_track(x)) not in local_keys)
    original_total = upstream_json.get("data", {}).get("total", len(local_list))
    if not isinstance(original_total, int):
        original_total = len(local_list)
    # 官方搜索越界页不返回空列表而是钳制回第 1 页（total=4 时 page=2 仍返回
    # 同样 4 条，收藏/歌单条目接口无此行为）。本页全局起点已越过官方段时必须
    # 清空官方列表，否则官方条目会拼上在线切片在每个后续页重复出现。
    if (page - 1) * size >= original_total and local_list:
        local_list.clear()
    # 官方搜索忽略 size 全量返回结果集（total=11 时 size=10 的 page=1/2 均返回
    # 同样 11 条，2026-09-25 官方更新实测）：返回条数超过单页窗口说明拿到的是
    # 全量列表而非请求页，按请求窗口原地切片，保证本地段全局分页不跨页重复。
    if len(local_list) > size:
        win_start = (page - 1) * size
        local_list[:] = local_list[win_start:win_start + size]
    # 本地优先全局布局：本地条目占据全局前 local_total 位，在线条目紧随其后。
    # 本页在线切片 = 全局分页区间与在线区间的交集；纯本地页（区间未触及在线段）
    # 在线切片为空，上游结果原样透传，只有 total 计入在线条数驱动客户端继续翻页。
    selected = _online_window(entry, page, size, original_total)
    # 本页在线条目的直链后台预热（只 create_task，不阻塞本次响应）
    schedule_prefetch(request.app, selected)
    merged = merge_online_tracks(upstream_json, selected, page=1, size=size, selected=True)
    merged["data"]["total"] = original_total + total_online
    fav_set = await _online_favorite_set(request)
    if fav_set and isinstance(merged.get("data"), dict) and isinstance(merged["data"].get("list"), list):
        for it in merged["data"]["list"]:
            if isinstance(it, dict) and str(it.get("guid") or "") in fav_set:
                it["isFavorite"] = True
    return JSONResponse(content=disguise_client_json(merged), status_code=upstream_resp.status_code, headers=resp_headers)


async def _aggregate_search(request: Request, keyword: str, entry: dict) -> None:
    scope = entry.get("credentials") or _credential_scope(request)
    if _user_search_stale(entry, scope):
        entry["items"] = []
        entry["superseded"] = True
        entry["ts"] = 0
        return
    window = await _wait_search_debounce(scope)
    if _user_search_stale(entry, scope) or (window is not None and _SEARCH_DEBOUNCE.get(scope) is not window):
        entry["items"] = []
        entry["superseded"] = True
        entry["ts"] = 0
        return
    token = _FETCH_SCOPE.set(scope)
    sources = []
    if CONF.get("netease_enabled"):
        sources.append(fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"]))
    if CONF.get("musicdl_enabled"):
        sources.append(fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"]))
    if CONF.get("lx_enabled"):
        sources.append(fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"]))
    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    try:
        deadline = asyncio.get_running_loop().time() + max(0.05, float(CONF["search_timeout"]))
        while pending and not _user_search_stale(entry, scope):
            done, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                partial = True
                break
            for task in tasks:
                if task not in done:
                    continue
                try:
                    data = task.result()
                except Exception:
                    data = None
                if entry.get("abandoned") or data is _SUPERSEDED or _user_search_stale(entry, scope):
                    if data is _SUPERSEDED or _user_search_stale(entry, scope):
                        entry["superseded"] = True
                    entry["items"] = []
                    results[task] = []
                    continue
                partial |= data is None or bool(getattr(data, "partial", False)) or (isinstance(data, dict) and bool(data.get("errors") or data.get("ok") is False))
                items = data.get("items", []) if isinstance(data, dict) else (data or [])
                results[task] = items
                if not entry["items"]:
                    ordered = [item for source_task in tasks for item in results.get(source_task, [])]
                else:
                    ordered = entry["items"] + items
                # 原唱优先：先按优先级稳定排序（原唱冒头，同档保持源顺序），
                # 再按 keyword 去重——去重时同录音的多个源会优先挑原唱当代表。
                # 只排不丢：非原唱仍保留在 _alternatives 里可切源。
                ordered = _sort_by_original(ordered, keyword)
                # ★ 2026-10-11：同一 rank 内按源轮转，避免单源霸榜把其它源的版本挤出首页
                ordered = _spread_sources(ordered, keyword)
                # ★ 2026-10-11：疑似片段/试听条目降到末尾（完整版优先露出，不删除）
                entry["items"] = _demote_clip_entries(
                    deduplicate_online_items(ordered, keyword))[:2000]
        if entry.get("abandoned") or _user_search_stale(entry, scope):
            entry["items"] = []
            if _user_search_stale(entry, scope):
                entry["superseded"] = True
                entry["ts"] = 0
            elif not entry.get("ts"):
                entry["partial"] = True
                entry["ts"] = time.time()
            return
        entry["partial"] = partial
        # 全源失败（source busy/熔断/异常）交回的 0 条不是真"无结果"：不落缓存时间戳，
        # 同词紧跟着重搜会立即重新聚合，而不是吃 10 秒空缓存一直返回空。
        entry["ts"] = 0 if partial and not entry.get("items") else time.time()
    finally:
        _FETCH_SCOPE.reset(token)
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if entry.get("abandoned"):
                entry["items"] = []
                entry["partial"] = True
                if not entry.get("ts"):
                    entry["ts"] = time.time()


def _online_window(entry: dict, page: int, size: int, local_total: int) -> list[dict]:
    """本地优先布局下取本页的在线切片。

    全局布局：[本地 0..local_total) [在线 local_total..local_total+len(items))。
    本页全局区间 = [(page-1)*size, page*size)；与在线段求交集后映射到 items 下标。
    items 只追加不重排，同一 (page,size,local_total) 的切片稳定；items 后续
    增长只会让更靠后的页多出条目（已返回页的前缀不动）。
    """
    # 剔除已由【预热探活】或【点播失败】判定「播不了」的条目（含被洛雪判掉的
    # 广告/试听片段）。幂等：同一 entry 反复请求只删一次；新搜索把条目带回来时
    # 会再次被清。删在切片之前，下面的 total 统计与分页因此天然一致。
    # ★ 2026-10-11：顺手清理过期记录 —— 此前只在 `_prefetch_candidates` 里清，
    # 而预热关闭时那个函数会**提前 return** → 黑名单永不过期、条目被永久隐藏。
    _prune_unplayable()
    if _PREFETCH_BAD:
        _items = entry.get("items")
        if isinstance(_items, list):
            _kept = [it for it in _items if online_guid_from_item(it) not in _PREFETCH_BAD]
            if len(_kept) != len(_items):
                entry["items"] = _kept
    start_global = (page - 1) * size
    end_global = page * size
    start = max(0, start_global - local_total)
    end = max(0, end_global - local_total)
    if start >= end:
        return []
    window = [item for item in entry["items"][start:end] if _source_enabled(online_guid_from_item(item))]
    return window


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
async def search_suggest(request: Request):
    if not CONF["merge_suggest"]:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    keyword = extract_keyword(request)

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    musicdl_task: asyncio.Task | None = None
    lx_task: asyncio.Task | None = None
    suggest_scope = _FETCH_SCOPE.set(_credential_scope(request) + ":suggest")
    if keyword and CONF.get("musicdl_enabled"):
        musicdl_task = asyncio.create_task(fetch_musicdl_search(musicdl_client, keyword, 5))
    # 【2026-10-06 新增】洛雪源也要接进 suggest。
    # 实测用户搜「张学友」返回「无搜索结果」：客户端只调search/suggest
    # 拿候选列表，**从没调过 search/track**（20 分钟日志里 suggest 19 次、
    # track 0 次）。而原实现只在 musicdl_enabled 时补数据，而它是 false ——
    # 于是建议列表恒为空，客户端就此判定「搜不到」，不会再去请求真正的搜索。
    # 洛雪源实测能搜到（搜「张学友」返回 60 条），只是从来没被接进来。
    if keyword and CONF.get("lx_enabled"):
        lx_task = asyncio.create_task(
            fetch_lx_search(get_lx_client(request.app), keyword,
                             CONF["lx_search_limit"]))
    _FETCH_SCOPE.reset(suggest_scope)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        if musicdl_task:
            musicdl_task.cancel()
        if lx_task:
            lx_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        if musicdl_task:
            musicdl_task.cancel()
        if lx_task:
            lx_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        if musicdl_task:
            musicdl_task.cancel()
        if lx_task:
            lx_task.cancel()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    musicdl_data = None
    if musicdl_task:
        try:
            musicdl_data = await asyncio.wait_for(asyncio.shield(musicdl_task), timeout=10.0)
        except Exception as e:
            logger.warning("Suggest musicdl error: %s", e)
            musicdl_task.cancel()

    # 洛雪源结果取标题补进候选列表。**只放标题字符串**，与 musicdl 分支
    # 保持一致 —— suggest 的 data 是「候选关键词」数组，不是曲目对象。
    if lx_task:
        try:
            lx_rows = await asyncio.wait_for(asyncio.shield(lx_task), timeout=10.0)
            data_field = upstream_json.get("data")
            if isinstance(data_field, list):
                for item in (lx_rows or [])[:8]:
                    title = item.get("title") if isinstance(item, dict) else None
                    if title and title not in data_field:
                        data_field.append(title)
                # 候选太窄时补歌手名 —— 用户搜「张学友」这类「按人搜」时，
                # 洛雪源返回的多是「XX演唱会」这类专辑条目，补上歌手名
                # 能让候选里有「张学友」本身，客户端才有东西可发。
                if len(data_field) < 5:
                    for item in (lx_rows or [])[:8]:
                        art = item.get("artist") if isinstance(item, dict) else None
                        if art and art not in data_field:
                            data_field.append(art)
        except Exception as e:
            logger.warning("Suggest lx error: %s", e)
            lx_task.cancel()

    data_field = upstream_json.get("data")
    if isinstance(data_field, list) and isinstance(musicdl_data, dict) and musicdl_data.get("items"):
        for item in musicdl_data.get("items", [])[:5]:
            title = item.get("title")
            if title and title not in data_field:
                data_field.append(title)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


def _lookup_online_snapshot(guid: str) -> "dict | None":
    """按 guid 在在线播放历史/在线收藏的用户快照里反查曲目元数据（issue #28）。

    两个存储都按用户分文件且量小（历史 ≤500 条/用户），tee 转正是低频路径，
    顺序扫描可接受；命中即返回含 title/artist/album 的快照 dict。
    """
    # ① 在线播放历史（play_history/<user>.json，条目最新的在末尾，倒序找）
    try:
        root = dailyrec.play_history_dir()
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for it in reversed(items):
                if isinstance(it, dict) and it.get("guid") == guid:
                    snap = it.get("track") if isinstance(it.get("track"), dict) else None
                    if snap and (snap.get("title") or snap.get("artist")):
                        return snap
    except OSError:
        pass
    # ② 在线收藏（online_favorites/<user>.json，形状同收藏下发条目）
    try:
        fav_root = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
        for name in sorted(os.listdir(fav_root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(fav_root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for it in reversed(items):
                if isinstance(it, dict) and it.get("guid") == guid:
                    snap = it.get("track") if isinstance(it.get("track"), dict) else None
                    if snap and (snap.get("title") or snap.get("artist")):
                        return snap
    except OSError:
        pass
    # ③ 歌单附加条目（playlist_tracks/<user>.json，{"items": {歌单guid: [条目]}}）：
    # 只加过歌单、从未收藏/播放的歌也要能反查（官方绑定与 tee 兜底的元数据来源）
    try:
        plt_root = plt_dir()
        for name in sorted(os.listdir(plt_root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(plt_root, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            buckets = data.get("items") if isinstance(data, dict) else None
            if not isinstance(buckets, dict):
                continue
            for bucket in buckets.values():
                if not isinstance(bucket, list):
                    continue
                for it in reversed(bucket):
                    if isinstance(it, dict) and it.get("guid") == guid:
                        snap = it.get("track") if isinstance(it.get("track"), dict) else None
                        if snap and (snap.get("title") or snap.get("artist")):
                            return snap
    except OSError:
        pass
    return None


def _track_in_list(tracks: Any, guid: str) -> dict | None:
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if not isinstance(track, dict) or str(track.get("guid") or "") != guid:
            continue
        title, artist, _album = _tag_fields(track)
        if title or artist:
            return track
    return None


def _lookup_playlist_cache_track(guid: str) -> dict | None:
    """推荐歌单、网易账号歌单、公共平台歌单的磁盘缓存按 guid 反查下发曲目。

    这三类列表不进搜索缓存。缓存里的曲目是前端形态（album 为对象），
    调用方必须用 _tag_fields 取标签，不能 str(album)。
    """
    if not guid:
        return None
    for root in (dailyrec.recommend_cache_dir(), nmpl.cache_dir(), pubpl.cache_dir()):
        for path in _iter_registry_jsons(root):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            hit = _track_in_list(data.get("tracks"), guid)
            if hit:
                return hit
    return None


def _merge_missing_tags(title: str, artist: str, album: str, snap: dict) -> tuple[str, str, str]:
    """只补空字段。已经是纯字符串的专辑不被缓存里的另一份专辑覆盖。"""
    snap_title, snap_artist, snap_album = _tag_fields(snap)
    return title or snap_title, artist or snap_artist, album or snap_album


def _tee_metadata_fallback(
    guid: str, title: str, artist: str, album: str, dest_info: dict | None = None,
) -> tuple[str, str, str]:
    """tee 落盘元数据缺失时的回查兜底，避免以 unknown 进曲库。

    顺序：推荐/网易账号歌单缓存（首播时历史往往还没写）→ 播放历史/收藏/自建歌单。
    缺哪个字段补哪个；已有的纯字符串专辑保持不变。封面 URL 只在调用方还没有时写入 dest_info。
    """
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    need_cover = not _cover_url_of(dest_info)
    if not (title and artist and album and not need_cover):
        filled = False
        for snap in (_lookup_playlist_cache_track(guid), _lookup_online_snapshot(guid)):
            if not snap:
                continue
            title, artist, album = _merge_missing_tags(title, artist, album, snap)
            if need_cover and isinstance(dest_info, dict):
                cover = _cover_url_of(snap)
                if cover:
                    dest_info["cover_url"] = cover
                    need_cover = False
            filled = True
            if title and artist and album and not need_cover:
                break
        if filled:
            logger.info("tee metadata fallback hit for %s: %s - %s", guid, artist, title)
    if not (title and artist):
        logger.warning(
            "tee finalize missing metadata for %s (title=%r artist=%r)：将以 unknown 命名落盘，"
            "该曲目的元数据解析链路需要排查", guid, title, artist,
        )
    return title, artist, album


# ── 落盘「试听片段」校验（★ 2026-10-11）───────────────────────────────
# 背景：音源对部分曲目只交付 60 秒试听流 —— **精确裁剪、按正式码率编码、无
# trial/freeTrialInfo 标记、标题也不含「试听」字样**，于是三道既有判据
# （lx `_explicit_trial` / `_TRIAL_TITLE_MARKERS` / 平均码率 `_AD_CLIP_MIN_KBPS`）
# **全部躲得过**；而 `_tee_finalize` 此前只对无损档做解码校验，mp3 无任何时长
# 校验 → 片段被「边听边存」直接写进用户曲库。实测 2 例：
#   「会开花的云 (Live)」1,002,643 B / 60.000s（歌词却到 03:37）
#   「大江南」           995,223 B / 60.000s（歌词到 05:15）
# 判据：歌词按**完整曲目**下发，其时间轴终点与片段音频的时长差是最可靠的反证。
_CLIP_LYRIC_GAP_S = 45.0      # 歌词终点比音频长这么多 → 判片段
_CLIP_DECLARED_GAP_S = 45.0   # 声明时长比实际长这么多且 >1.5 倍 → 判片段
_CLIP_MIN_AUDIO_S = 20.0      # 音频短于此（或读不到时长）不下结论，避免误杀


def _audio_duration_s(path: str) -> float:
    """读音频真实时长（秒）。mutagen 缺失/读取失败返回 0（调用方据此跳过判断）。"""
    try:
        from mutagen import File as MutagenFile

        info = getattr(MutagenFile(path), "info", None)
        return float(getattr(info, "length", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _lyric_end_seconds(guid: str) -> float:
    """取该曲歌词（影子 + 正式 sidecar）时间轴的最后一个时间点（秒）；无则 0。"""
    texts: list = []
    try:
        with open(os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc"),
                  encoding="utf-8", errors="replace") as f:
            texts.append(f.read())
    except OSError:
        pass
    try:
        path = find_lyric_file(guid)
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
    except Exception:  # noqa: BLE001
        pass
    best = 0.0
    for text in texts:
        for m in re.finditer(r"\[(\d{1,3}):(\d{2})(?:[.:]\d{1,3})?\]", text or ""):
            best = max(best, int(m.group(1)) * 60 + int(m.group(2)))
    return best


def _reject_if_clip(part: str, guid: str, src: dict) -> bool:
    """判定待转正的落盘文件是否为「试听片段」；是则删除并返回 True。

    保守优先：只在能拿到可信音频时长、且明显不自洽时才判片段（宁可漏判不误杀）。
    不拉黑 guid —— 片段是「该源只给 60 秒」，换源/换档可能拿到完整版，
    直接拉黑会把整曲挡掉（`_LOSSLESS_BAD` 的语义是「无损档损坏→降级 mp3」，不可混用）。
    """
    actual = _audio_duration_s(part)
    if actual < _CLIP_MIN_AUDIO_S:
        return False
    try:
        declared = float((src or {}).get("duration_s") or 0)
    except (TypeError, ValueError):
        declared = 0.0
    lyric_end = _lyric_end_seconds(guid)
    reason = ""
    if lyric_end > actual + _CLIP_LYRIC_GAP_S:
        reason = "lyric %.0fs vs audio %.0fs" % (lyric_end, actual)
    elif declared > actual * 1.5 and (declared - actual) > _CLIP_DECLARED_GAP_S:
        reason = "declared %.0fs vs audio %.0fs" % (declared, actual)
    if not reason:
        return False
    logger.warning("tee finalize rejected clip for %s: %s", guid, reason)
    try:
        os.remove(part)
    except OSError:
        pass
    return True


def _tee_finalize(part: str, guid: str, ext: str, info: dict | None, tee_enabled: bool) -> dict | None:
    """落盘转正：边听边存开→进曲库（定名/权限/标签，歌词仅在 FNMUSIC_LYRIC_AUTO_DL
    开启时随迁），关→进滚动缓存（不写歌词）。

    part 必须已完整写好且长度校验通过；成功后由调用方触发曲库扫描通知。
    无损档解码校验不过直接丢弃（上游损坏流绝不入库）并拉黑该 guid。
    返回实际落盘元数据 {"dest","title","artist","album"}（官方绑定按此匹配官方曲库）。
    """
    if str(ext or "").lower() in _LOSSLESS_EXTS and not _audio_file_ok(part):
        _lossless_blacklist(guid)
        logger.warning("tee finalize rejected corrupt lossless for %s", guid)
        try:
            os.remove(part)
        except OSError:
            pass
        return None
    src = dict(info or {})
    # ★ 片段校验（2026-10-11）：试听片段绝不进用户曲库（判据见 _reject_if_clip）
    if _reject_if_clip(part, guid, src):
        return None
    title, artist, album = _tag_fields(src)
    if tee_enabled:
        title, artist, album = _tee_metadata_fallback(guid, title, artist, album, src)
        dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())
        os.replace(part, dest)
        remember_media_path(guid, dest)
        adopt_library_perms(dest)
        write_audio_tags(dest, title, artist, album)
        # 自动下载封面：音乐文件已完整落库才走到这里（下载失败根本进不了
        # finalize），封面字节直接内嵌进音频——不产生独立封面文件，无孤儿
        cover_url = _cover_url_of(src)
        if cover_url and CONF.get("auto_cover", True):
            try:
                fetched = download_cover_bytes(cover_url)
                if fetched and embed_audio_cover(dest, fetched[0], fetched[1]):
                    logger.info("cover embedded for %s: %s", guid, dest)
            except Exception as e:
                logger.debug("cover embed step skipped for %s: %s", guid, e)
        # 自动下载歌词开启时，无音频时代落在 cache 的影子歌词跟随音频进曲库，词曲贴身
        if CONF.get("lyric_auto_dl"):
            promote_shadow_lyric(guid, dest)
    else:
        # 边听边存关闭：只写滚动缓存（cache_safe_guid 命名，find_cache_file 精确名可命中）
        dest = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.{ext}")
        os.replace(part, dest)
    logger.info("tee finalize done for %s: dest=%s", guid, dest)
    # 歌词只随"音乐完整落库成功"写入（tee 转正即到此处），且仅在自动下载歌词
    # 开启时；下载失败根本进不了 finalize，绝不产生先落歌词的孤儿文件
    if tee_enabled and CONF.get("lyric_auto_dl"):
        lyric = str(src.get("lyric") or src.get("lrc") or "")
        if lyric.strip():
            write_lyric_cache(guid, lyric, title, artist)
    if not tee_enabled:
        try:
            purge_rolling(CONF["cache_dir"], keep=int(CONF.get("tee_cache_max", 2)))
        except Exception as e:
            logger.warning("Rolling cache purge failed: %s", e)
    return {"dest": dest, "title": title, "artist": artist, "album": album}


async def _auto_lyric_after_finalize(request: Request, guid: str, meta: dict | None) -> None:
    """音乐完整落库成功后补齐歌词（FNMUSIC_LYRIC_AUTO_DL，默认关）。

    仅边听边存转正路径调用，且必须排在 _schedule_library_scan 之前——保证
    官方扫描入库前 .lrc 已落在音频旁。本地已有歌词（含影子提升产物）零请求；
    缺失时向源站补拉（酷我上游接口已失效返回空即跳过）。下载失败不会走到
    这里，不会产生只有歌词没有音频的孤儿文件。
    """
    if not CONF.get("lyric_auto_dl"):
        return
    dest = str((meta or {}).get("dest") or "")
    if not dest or _is_rolling_cache_stem(dest, guid):
        return
    if find_lyric_file(guid):
        return
    try:
        text = await asyncio.wait_for(resolve_online_lyric(request, guid), timeout=10.0)
    except Exception as e:
        logger.info("auto lyric fetch failed for %s: %s", guid, type(e).__name__)
        return
    if text:
        logger.info("auto lyric saved for %s (%d chars)", guid, len(text))
    else:
        logger.info("auto lyric unavailable for %s (source returned no lyric)", guid)


_SCAN_TTL_S = 90.0
_SCAN_MERGE_S = 3.0
_scan_last_attempt = 0.0
_scan_task: "asyncio.Task | None" = None


def _schedule_library_scan(headers: dict | None) -> None:
    """落盘进曲库后通知官方重扫（FNMUSIC_LIBRARY_SCAN_PATH 为空=禁用）。

    官方扫描接口无公开资料：路径可配置、默认禁用；3 秒合并窗口（连续落盘多首
    只发一次）+ 90 秒 TTL（成功与失败都节流，路径配错不会风暴）。凭证头取自
    触发请求（copy_incoming_headers），只在任务内一次性使用，不落盘。
    """
    global _scan_task, _scan_last_attempt
    if not str(CONF.get("library_scan_path") or "").strip() or not headers:
        return
    if time.monotonic() - _scan_last_attempt < _SCAN_TTL_S:
        return
    if _scan_task is not None and not _scan_task.done():
        return

    async def _run() -> None:
        global _scan_last_attempt
        await asyncio.sleep(_SCAN_MERGE_S)
        scan_path = str(CONF.get("library_scan_path") or "").strip()
        if not scan_path:
            return
        _scan_last_attempt = time.monotonic()
        try:
            client = get_upstream_client(app)
            req = client.build_request("POST", scan_path, headers=dict(headers))
            resp = await client.send(req)
            if resp.status_code == 200:
                logger.info("Library scan triggered via %s", scan_path)
            else:
                logger.warning(
                    "Library scan %s returned %s (check FNMUSIC_LIBRARY_SCAN_PATH)",
                    scan_path, resp.status_code,
                )
        except Exception as e:
            logger.warning("Library scan trigger failed: %s", e)

    _scan_task = asyncio.create_task(_run())


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
    chunks: Any = None,
    first_chunk: bytes = b"",
    scan_headers_factory: Callable[[], dict] | None = None,
    post_finalize: Callable[[dict | None], Coroutine[Any, Any, None]] | None = None,
) -> Response:
    headers = {"Accept-Ranges": "bytes"}
    for key in ("content-type", "content-length", "content-range"):
        if resp.headers.get(key):
            headers[key] = resp.headers[key]
    if resolved_ext:
        headers["content-type"] = media_type_for_ext(resolved_ext)
    length = resp.headers.get("content-length", "")
    expected = int(length) if length.isdigit() else None
    full_resource = resp.status_code == 200
    if resp.status_code == 206:
        # Content-Length proves only this segment, not the entire recording.
        match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I)
        full_resource = bool(match and int(match[1]) + 1 == int(match[2]) and int(match[2]) > 0)
        if full_resource:
            total = int(match[2])
            full_resource = expected is None or expected == total
            expected = total
    ext = resolved_ext or ext_from_content_type(resp.headers.get("content-type", ""))

    async def body() -> AsyncGenerator[bytes, None]:
        # Pull-through provides backpressure: no unbounded producer queue and no
        # downloader outliving its consumer. Only a clean EOF may finalize.
        part = None
        fp = None
        written = 0
        eof = False
        info_task = None
        tee_active_token = False
        upstream_aborted = False
        try:
            tee_enabled = bool(CONF.get("tee_save_enabled"))
            if should_cache(range_header) and full_resource:
                directory = tee_save_dir() if tee_enabled else CONF["cache_dir"]
                os.makedirs(directory, exist_ok=True)
                part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
                fp = open(part, "wb")
                tee_active_token = _tee_active_acquire(guid)
                if pre_info is None and coro_factory:
                    info_task = asyncio.create_task(coro_factory())
            iterator = chunks if chunks is not None else resp.aiter_bytes()
            if first_chunk:
                if fp:
                    fp.write(first_chunk)
                written += len(first_chunk)
                yield first_chunk
            try:
                async for chunk in iterator:
                    if chunk:
                        if fp:
                            fp.write(chunk)
                        written += len(chunk)
                        yield chunk
            except Exception as exc:
                # An aborted stream must not enter the cache, but it must leave
                # a trace: CDN stalls mid-file were previously invisible in the
                # journal. Client disconnects raise CancelledError (not caught
                # here) and stay silent on purpose.
                upstream_aborted = True
                logger.warning("Stream aborted mid-way for %s: %s", guid, type(exc).__name__)
                raise
            eof = True
            if fp:
                fp.close()
                fp = None
            if part and eof and written >= 1024 and (expected is None or written == expected):
                # 无论客户端连接此时是否已关闭（curl 接收完直接 EOF 退出，Starlette 会 aclose 生成器），
                # 完整的音频已全部接收完毕，落盘与标签写入必须受 shield 保护完整执行完毕，
                # 且立即解绑 part，绝不能被 GeneratorExit / CancelledError 提前打断导致 part 在 finally 中被误删。
                to_finalize = part
                part = None
                with anyio.CancelScope(shield=True):
                    info = pre_info
                    if info is None and info_task:
                        try:
                            info = await asyncio.wait_for(info_task, timeout=8.0)
                        except Exception:
                            info = None
                    try:
                        meta = await asyncio.to_thread(_tee_finalize, to_finalize, guid, ext, info, tee_enabled)
                        # 自动下载歌词：补拉必须在通知官方扫描之前完成（入库即带歌词）
                        if post_finalize is not None and meta:
                            await post_finalize(meta)
                        scan_headers = scan_headers_factory() if scan_headers_factory is not None else None
                        if scan_headers:
                            _schedule_library_scan(scan_headers)
                        if meta and tee_enabled:
                            # 官方绑定意图（收藏/歌单）统一在下载落库完成后调度
                            _dispatch_official_binding(guid, scan_headers, meta)
                    except Exception as exc:
                        logger.warning("tee finalize execution failed for %s: %s", guid, type(exc).__name__)
                        if os.path.exists(to_finalize):
                            try:
                                os.remove(to_finalize)
                            except OSError:
                                pass
        finally:
            if fp:
                fp.close()
                fp = None
            resume_part = None
            if part and os.path.exists(part):
                # 切歌/客户端退出不中断下载：未写完的 part 交接给后台续传任务完成
                # （仅边听边存开启的整轨流且非上游错误；written 不足或续传满载时
                # 按旧语义删除，上游流错误绝不进续传）
                if (tee_enabled and full_resource and not upstream_aborted
                        and written >= 1024 and _tee_handoff_slot_available()):
                    resume_part = part
                    part = None
                else:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    part = None
            with anyio.CancelScope(shield=True):
                if tee_active_token:
                    _tee_active_release(guid)
                if info_task and not info_task.done():
                    info_task.cancel()
                    await asyncio.gather(info_task, return_exceptions=True)
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()
                if resume_part:
                    _register_tee_handoff(guid, resume_part, written, expected, ext, scan_headers_factory)

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


def _credential_scope(request: Request) -> str:
    return hashlib.sha256(json.dumps([request.headers.get(k, "") for k in
        ("cookie", "authorization", "x-trim-music-temp-token")]).encode()).hexdigest()


def _retained_track(request: Request, guid: str) -> tuple[dict | None, dict | None]:
    _clean_search_cache()
    for entry in reversed(list(_SEARCH_CACHE.values())):
        if entry.get("credentials") != _credential_scope(request) or entry.get("config", _source_config()) != _source_config():
            continue
        for item in entry.get("items", []):
            if online_guid_from_item(item) == guid:
                return item, entry
            for alternative in item.get("_alternatives", []):
                if online_guid_from_item(alternative) == guid:
                    return alternative, entry
    return None, None


async def _recover_source(request: Request, guid: str, entry: dict | None) -> bool:
    """One bounded source re-search after a backend loses its in-memory IDs."""
    if not entry or not _source_enabled(guid):
        return False
    source = source_from_online_guid(guid)
    recovery_key = "recovered:" + source
    if time.monotonic() - entry.get(recovery_key, -1000) < 30:
        return False
    entry[recovery_key] = time.monotonic()
    keyword = entry.get("keyword", "")
    token = _FETCH_SCOPE.set(_credential_scope(request) + ":play")
    try:
        if source == "netease":
            coro = fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"])
        elif source == "lx":
            # 按目标 GUID 的平台精确重搜（GUID 第 3 段），对齐 musicdl 分支按引擎重搜的行为
            parts = guid.split(":")
            coro = fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"],
                                   sources=[parts[2]] if len(parts) >= 4 else None)
        else:
            selected = [name.strip() for name in str(CONF.get("online_sources") or "").split(",")
                        if name.strip().lower().removesuffix("musicclient") == source.lower()]
            coro = fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"],
                                       ",".join(selected) or source)
        try:
            result = await asyncio.wait_for(coro, timeout=3.0)
        except Exception:
            return False
        if result is _SUPERSEDED or not result:
            return False
        items = result.get("items", []) if isinstance(result, dict) else result
        if not isinstance(items, list):
            return False
        return any(online_guid_from_item(item) == guid for item in items)
    finally:
        _FETCH_SCOPE.reset(token)


async def _open_online_stream(request: Request, guid: str, range_header: str | None,
                              force_mp3: bool = False):
    """Resolve and read first bytes before committing HTTP headers to the client."""
    source = source_from_online_guid(guid)
    info, _ = _retained_track(request, guid)
    # 部分 CDN 按 UA 黑名单拦截 python-*（2026-10-10 实测酷我 other-*.kuwo.cn：
    # -A python-httpx/0.27.0 与 python-requests/2.31.0 一律 403，而 curl 默认 UA /
    # 浏览器 UA / 干脆不带 UA 都是 206；python-httpx 的默认 UA 同样 403）。
    # 洛雪探活（lxmusic probe_url）本就带 UA所以能过，代理层却没带 —— 于是
    # 「探活通过、取流被拒」，可用直链被判失败 → 404 → 点播跳曲。
    headers = {"Accept-Encoding": "identity",
               "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
    if range_header:
        headers["Range"] = range_header
    via_musicdl = False
    owned = None
    resp = None
    ext = None
    try:
        if source in ("netease", "lx"):
            if source == "netease":
                url = await resolve_netease_url(get_musicbox_client(request.app), song_id_from_online_guid(guid).split(":")[-1])
                if not url:
                    return None
            else:
                resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid))
                if not resolved:
                    return None
                url = resolved["url"]
                ext = resolved.get("ext")
                for key, value in (resolved.get("headers") or {}).items():
                    lk = key.lower()
                    if lk in ("referer", "user-agent"):
                        # 规范成标准 key，否则小写的 user-agent 会与上面的默认
                        # UA 并存为两个同名头（httpx 会原样发出）
                        headers["Referer" if lk == "referer" else "User-Agent"] = str(value)
            if info is None:
                try:
                    info = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=0.75)
                except Exception as exc:
                    logger.info("online info fast-path missed for %s: %s", guid, type(exc).__name__)
            ext = ext or (info or {}).get("ext")
            # Read timeout must tolerate CDN throttling mid-file: a flat 10s
            # timeout kills long pauses between chunks and surfaces as playback
            # stuck at ~70-80% of the track.
            owned = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
                follow_redirects=True,
            )
            client = owned
            req = client.build_request("GET", url, headers=headers)
        else:
            client = get_musicdl_client(request.app)
            via_musicdl = True
            params = {"id": song_id_from_online_guid(guid), "proxy": "true"}
            # 无损档已被判定损坏的曲目（或上层明确要求）直接取 mp3 档；
            # 带断点续传的请求不切档——前后字节必须来自同一条流
            if (force_mp3 or _lossless_is_blacklisted(guid)) and not range_header:
                params["quality"] = "mp3"
            req = client.build_request("GET", "/stream", params=params, headers=headers)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get("content-type", "").lower()
        if (resp.status_code not in (200, 206)
                or any(x in content_type for x in ("text/", "json"))
                or resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")):
            # Reject servers ignoring identity: decoded bytes cannot use encoded
            # Content-Length/Range offsets, and must not enter the audio cache.
            return None
        chunks = resp.aiter_bytes()
        first = await anext(chunks, b"")
        if not first:
            return None
        # 仅 musicdl 通道会服务端透明降级（坏无损流→官方 mp3 档）：以实际
        # content-type 为准修正扩展名，避免 mp3 字节按 .flac 命名入库。
        # 网易/洛雪直链相反——上游会错报 audio/mpeg，扩展名以解析结果为准。
        if via_musicdl and "audio/mpeg" in content_type and (not ext or ext in _LOSSLESS_EXTS):
            ext = "mp3"
        result = (resp, owned, ext, info, chunks, first)
        resp = owned = None  # transfer ownership to response iterator
        return result
    finally:
        if resp:
            await resp.aclose()
        if owned:
            await owned.aclose()


_FULL_FETCH_COOLDOWN_S = 1800.0
_full_fetch_tasks: "dict[str, asyncio.Task]" = {}
_full_fetch_failed: "dict[str, float]" = {}

# --- 损坏流防护（2026-09-29 实测）-----------------------------------------
# kuwo 官方 CDN 的无损档交付不可解码字节（fLaC 头与 Content-Length 都正常，
# musicdl 库探活只验 URL/扩展名），坏文件一旦入库，官方转码器编到坏点即报
# "transcoding error"、App 端表现为转码下载失败。对策：入库前全量解码校验，
# 坏流丢弃并按 guid 拉黑无损档——此后该曲目取流自动降级 mp3 档（musicdl
# 服务 /stream?quality=mp3）。
_LOSSLESS_EXTS = {"flac", "wav", "ape", "m4a", "alac", "ogg", "opus"}
_LOSSLESS_BAD: "dict[str, float]" = {}
_LOSSLESS_BAD_TTL_S = 6 * 3600.0


def _lossless_blacklist(guid: str) -> None:
    _LOSSLESS_BAD[guid] = time.monotonic()


def _lossless_is_blacklisted(guid: str) -> bool:
    ts = _LOSSLESS_BAD.get(guid)
    if ts is None:
        return False
    if time.monotonic() - ts > _LOSSLESS_BAD_TTL_S:
        _LOSSLESS_BAD.pop(guid, None)
        return False
    return True


def _audio_file_ok(path: str) -> bool:
    """完整音频文件的解码校验：ffmpeg 无任何错误输出才算通过。

    只对已通过长度校验的完整文件调用——截断的完好文件同样会报解码错误，
    报错文本无法区分二者，完整性必须由字节数校验先行保证。
    ffmpeg 不可用（测试守卫/宿主机缺件）时无从校验，只能放行。
    """
    if not tc.FFMPEG_BIN:
        return True
    try:
        r = subprocess.run(
            [tc.FFMPEG_BIN, "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
        return r.returncode == 0 and not (r.stderr or "").strip()
    except Exception:  # noqa: BLE001
        return False


def _prune_full_fetch_state(now: float) -> None:
    if len(_full_fetch_failed) > 1000:
        for g in [g for g, at in _full_fetch_failed.items() if now - at >= _FULL_FETCH_COOLDOWN_S]:
            del _full_fetch_failed[g]


def _register_background_fetch(request: Request, guid: str, gate_key: str) -> bool:
    """后台整轨下载通用注册逻辑，按 gate_key 指定的 CONF 键做门禁。"""
    if not CONF.get(gate_key) or find_cache_file(guid):
        return False
    if guid in _tee_active:
        # tee 流式下载同 guid 进行中：不重复下载，tee 转正/切歌续传路径会调度后续动作
        return False
    task = _full_fetch_tasks.get(guid)
    if task is not None and not task.done():
        return False
    now = time.monotonic()
    _prune_full_fetch_state(now)
    if now - _full_fetch_failed.get(guid, -_FULL_FETCH_COOLDOWN_S) < _FULL_FETCH_COOLDOWN_S:
        return False
    headers = copy_incoming_headers(request)
    _full_fetch_tasks[guid] = asyncio.create_task(_full_fetch_download(guid, headers))
    return True


def _register_full_fetch(request: Request, guid: str) -> None:
    """定长窗口拉流客户端的边听边存兜底：注册后台整轨下载，客户端请求照常服务。

    should_cache 只认"无 Range / bytes=0-"，窗口内核（如 bytes=0-1048575 一段）
    永远过不了 tee 写盘门；由服务端另起整轨下载落盘补齐。同 guid 去重、
    失败后冷却期内不重试，防止坏源反复打上游。
    """
    _register_background_fetch(request, guid, "tee_save_enabled")


def _register_fav_autobind(request: Request, guid: str, user_guid: str = "",
                           playlist_guid: str | None = None) -> None:
    """收藏/加入歌单的在线歌曲自动绑定本地：登记官方绑定意图并确保整轨下载落库。

    下载不与边听边存重复：tee 正在流式下载或后台任务在途时只登记意图，由对应
    完成路径（tee 转正 / 后台整轨下载 / 切歌续传）统一调度官方绑定。文件已在
    库时无需下载，直接尝试官方绑定（官方可能已扫入库）。
    """
    if not CONF.get("fav_auto_bind"):
        return
    headers = copy_incoming_headers(request)
    if user_guid:
        _register_bind_intent(guid, user_guid, headers, playlist_guid)
    if find_cache_file(guid):
        _dispatch_official_binding(guid, headers, None)
        return
    _register_background_fetch(request, guid, "fav_auto_bind")


async def _info_for_background_save(request: Request, guid: str, info: dict | None) -> dict:
    """后台整轨/续传落盘前补齐元数据。

    本地推荐歌单、网易账号歌单、历史/收藏里已有标题就用那份，不再打源站。
    都没有时再拉一次歌曲信息（数秒，不占播放起播的 0.75 秒窗口）。
    """
    base = dict(info or {})
    if _tag_fields(base)[0]:
        return base
    local = _lookup_playlist_cache_track(guid) or _lookup_online_snapshot(guid)
    if local and _tag_fields(local)[0]:
        merged = dict(local)
        if base.get("ext"):
            merged["ext"] = base["ext"]
        if base.get("lyric"):
            merged["lyric"] = base["lyric"]
        return merged
    try:
        fetched = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=4.0)
    except Exception as exc:
        logger.info("background online info missed for %s: %s", guid, type(exc).__name__)
        return base
    if isinstance(fetched, dict) and _tag_fields(fetched)[0]:
        if base.get("ext") and not fetched.get("ext"):
            fetched = dict(fetched)
            fetched["ext"] = base["ext"]
        return fetched
    logger.info("background online info empty for %s", guid)
    return base


async def _full_fetch_download(guid: str, cred_headers: dict, force_mp3: bool = False) -> None:
    """后台整轨下载 online guid 并落盘（独立于客户端连接，不占播放路径预算）。

    无损档解码校验失败时按 guid 拉黑并自动以 mp3 档重试一次（服务端
    quality=mp3），两次都坏才宣告失败——坏字节绝不入库。
    """
    part = None
    resp = None
    owned = None
    try:
        # 合成最小 Request scope（触发请求的凭证头 + app 实例），完整复用在线取
        # 流的解析/元数据/首字节校验逻辑；Range 传 None 保证拿到完整资源。
        fake_request = _synth_request(cred_headers)
        opened = await _open_online_stream(fake_request, guid, None, force_mp3=force_mp3)
        if not opened:
            raise RuntimeError("open failed")
        resp, owned, ext, info, chunks, first = opened
        expected = None
        length = resp.headers.get("content-length", "")
        if length.isdigit():
            expected = int(length)
        directory = tee_save_dir()
        os.makedirs(directory, exist_ok=True)
        part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
        written = 0
        with open(part, "wb") as fp:
            if first:
                fp.write(first)
                written += len(first)
            async for chunk in chunks:
                if chunk:
                    fp.write(chunk)
                    written += len(chunk)
        if written < 1024 or (expected is not None and written != expected):
            raise RuntimeError(f"size mismatch written={written} expected={expected}")
        info = await _info_for_background_save(fake_request, guid, info)
        ext = ext or (info or {}).get("ext") or "mp3"
        if ext in _LOSSLESS_EXTS and not await asyncio.to_thread(_audio_file_ok, part):
            _lossless_blacklist(guid)
            logger.warning("audio decode check failed for %s (ext=%s bytes=%d)", guid, ext, written)
            # 坏流不落库：关掉当前连接后改要 mp3 档重来一次
            with anyio.CancelScope(shield=True):
                if resp:
                    await resp.aclose()
                if owned:
                    await owned.aclose()
                resp = owned = None
            if not force_mp3:
                logger.warning("retrying %s with mp3 tier after corrupt lossless stream", guid)
                await _full_fetch_download(guid, cred_headers, force_mp3=True)
                return
            raise RuntimeError("corrupt stream even at mp3 tier")
        meta = await asyncio.to_thread(_tee_finalize, part, guid, ext, info or {}, True)
        part = None
        logger.info("Background full fetch saved %s (%d bytes)", guid, written)
        await _auto_lyric_after_finalize(fake_request, guid, meta)
        _schedule_library_scan(cred_headers)
        _dispatch_official_binding(guid, cred_headers, meta)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _full_fetch_failed[guid] = time.monotonic()
        logger.warning("Background full fetch failed for %s: %s", guid, type(e).__name__)
    finally:
        if part and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        with anyio.CancelScope(shield=True):
            if resp:
                await resp.aclose()
            if owned:
                await owned.aclose()
        if _full_fetch_tasks.get(guid) is asyncio.current_task():
            del _full_fetch_tasks[guid]


# === 边听边存 tee 活跃跟踪 + 切歌续传交接 ===
# 需求：开启边听边存后听歌即下载，切歌不中断下载；收藏/加歌单与正在进行的
# tee 下载不重复拉流。

_tee_active: "dict[str, int]" = {}
_tee_handoff_active: "set[str]" = set()


def _tee_active_acquire(guid: str) -> bool:
    _tee_active[guid] = _tee_active.get(guid, 0) + 1
    return True


def _tee_active_release(guid: str) -> None:
    left = _tee_active.get(guid, 0) - 1
    if left > 0:
        _tee_active[guid] = left
    else:
        _tee_active.pop(guid, None)


def _tee_handoff_slot_available() -> bool:
    """续传并发上限（FNMUSIC_TEE_HANDOFF_MAX，0=关闭）：防快速跳歌形成下载洪泛。"""
    try:
        limit = int(CONF.get("tee_handoff_max") or 0)
    except (TypeError, ValueError):
        limit = 0
    return limit > 0 and len(_tee_handoff_active) < limit


def _remove_quiet(path: str | None) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _synth_request(cred_headers: dict) -> Request:
    """合成最小 Request scope（凭证头 + app 实例），供后台下载复用在线取流逻辑。"""
    scope_headers = [
        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (cred_headers or {}).items()
    ]
    return Request({"type": "http", "headers": scope_headers, "app": app})


def _register_tee_handoff(guid: str, part: str, written: int, expected: int | None,
                          ext: str | None, headers_factory: "Callable[[], dict] | None") -> None:
    """tee 流式下载被客户端中断（切歌/退出）后，把未完成的 part 交给后台续传完成。"""
    if find_cache_file(guid):
        _remove_quiet(part)
        return
    task = _full_fetch_tasks.get(guid)
    if task is not None and not task.done():
        # 已有在途整轨下载：交给它完成，不重复续传
        _remove_quiet(part)
        return
    now = time.monotonic()
    _prune_full_fetch_state(now)
    if now - _full_fetch_failed.get(guid, -_FULL_FETCH_COOLDOWN_S) < _FULL_FETCH_COOLDOWN_S:
        _remove_quiet(part)
        return
    headers = headers_factory() if headers_factory is not None else {}
    task = asyncio.create_task(_tee_handoff_download(guid, part, written, expected, ext, headers))
    _full_fetch_tasks[guid] = task
    _tee_handoff_active.add(guid)

    def _done(t: "asyncio.Task") -> None:
        _tee_handoff_active.discard(guid)
        if _full_fetch_tasks.get(guid) is t:
            del _full_fetch_tasks[guid]

    task.add_done_callback(_done)


async def _tee_handoff_download(guid: str, part: str, written: int, expected: int | None,
                                ext: str | None, cred_headers: dict) -> None:
    """续传被中断的 tee 下载；源不支持断点续传或长度不符时回退整轨重下。"""
    try:
        resumed = await _resume_part_download(guid, part, written, expected, ext, cred_headers)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Tee handoff resume error for %s: %s", guid, type(e).__name__)
        resumed = False
    if resumed:
        return
    _remove_quiet(part)
    await _full_fetch_download(guid, cred_headers)


async def _resume_part_download(guid: str, part: str, written: int, expected: int | None,
                                ext: str | None, cred_headers: dict) -> bool:
    """Range 断点续拉剩余字节追加进 part；不可续传返回 False（调用方回退整轨重下）。"""
    fake_request = _synth_request(cred_headers)
    opened = await _open_online_stream(fake_request, guid, f"bytes={written}-")
    if not opened:
        return False
    resp, owned, stream_ext, info, chunks, first = opened
    try:
        if resp.status_code != 206:
            # 源忽略 Range 返回 200：无法续传
            return False
        match = re.fullmatch(
            r"bytes\s+(\d+)-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I,
        )
        if not match or int(match.group(1)) != written:
            return False
        total = int(match.group(3))
        if total <= 0 or (expected is not None and total != expected):
            return False
        append_written = 0
        with open(part, "ab") as fp:
            if first:
                fp.write(first)
                append_written += len(first)
            async for chunk in chunks:
                if chunk:
                    fp.write(chunk)
                    append_written += len(chunk)
        final_size = written + append_written
        if final_size < 1024 or final_size != total:
            raise RuntimeError(f"resume size mismatch final={final_size} total={total}")
        info = await _info_for_background_save(fake_request, guid, info)
        resolved_ext = ext or stream_ext or (info or {}).get("ext") or "mp3"
        meta = await asyncio.to_thread(_tee_finalize, part, guid, resolved_ext, info or {}, True)
        logger.info("Tee handoff resumed %s (+%d bytes, total %d)", guid, append_written, final_size)
        await _auto_lyric_after_finalize(fake_request, guid, meta)
        _schedule_library_scan(cred_headers)
        _dispatch_official_binding(guid, cred_headers, meta)
        return True
    finally:
        with anyio.CancelScope(shield=True):
            await resp.aclose()
            if owned:
                await owned.aclose()


# === 官方绑定：下载落库 → 等官方扫入库 → 写官方收藏/歌单 → 删本地映射 ===
# 官方接口只认曲库内 guid：先只读轮询官方 music.db 拿到新入库曲目的官方 guid，
# 再用触发时刻的凭证头直调官方写接口；成功后删除本地映射条目（官方列表唯一
# 显示，避免同一首歌官方/本地映射重复出现两次），失败保留本地映射兜底并
# 由列表读取路径自愈重试。凭证头仅留内存，绝不落盘。

_BIND_POLL_INTERVAL_S = 3.0
_BIND_RETRY_THROTTLE_S = 60.0
_BIND_RETRY_BATCH = 5
# guid -> user_guid -> {"fav": bool, "playlists": set[str], "headers": dict}
_bind_pending: "dict[str, dict[str, dict]]" = {}
_bind_tasks: "dict[tuple[str, str], asyncio.Task]" = {}
_bind_retry_last: "dict[str, float]" = {}


def _register_bind_intent(guid: str, user_guid: str, headers: "dict | None",
                          playlist_guid: str | None = None) -> None:
    """登记官方绑定意图（收藏或加入歌单），由下载完成路径统一调度执行。"""
    if not guid or not user_guid:
        return
    users = _bind_pending.setdefault(guid, {})
    intent = users.get(user_guid)
    if intent is None:
        intent = {"fav": False, "playlists": set(), "headers": dict(headers or {})}
        users[user_guid] = intent
    elif headers:
        intent["headers"] = dict(headers)
    if playlist_guid:
        intent["playlists"].add(playlist_guid)
    else:
        intent["fav"] = True


def official_track_guid_by_tags(title: str, artist: str, album: str = "") -> "str | None":
    """只读官方 music.db，按落盘标签匹配曲目的官方 guid（最新一条优先）。

    标签由 write_audio_tags 写入、官方扫描入库时读取，精确匹配即高保真；
    精确未命中再用大小写不敏感兜底一次（官方扫描器可能做过归一化）。
    """
    title_s = (title or "").strip()
    artist_s = (artist or "").strip()
    album_s = (album or "").strip()
    if not title_s or not artist_s:
        return None
    db = str(CONF.get("music_db") or "")
    if not db or not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception as e:
        logger.warning("Official bind db open failed: %s", e)
        return None
    try:
        for nocase in (False, True):
            collate = " COLLATE NOCASE" if nocase else ""
            sql = (
                "SELECT t.guid FROM track t "
                "JOIN track_artist ta ON ta.track_id = t.id "
                "JOIN artist a ON a.id = ta.artist_id "
                f"WHERE t.title = ?{collate} AND a.name = ?{collate}"
            )
            params: list = [title_s, artist_s]
            if album_s:
                sql += (
                    " AND EXISTS (SELECT 1 FROM album al"
                    f" WHERE al.id = t.album_id AND al.name = ?{collate})"
                )
                params.append(album_s)
            sql += " ORDER BY t.id DESC LIMIT 1"
            try:
                row = con.execute(sql, params).fetchone()
            except Exception as e:
                logger.warning("Official bind db query failed: %s", e)
                return None
            if row and row[0]:
                return str(row[0])
        return None
    finally:
        con.close()


def library_file_by_tags(title: str, artist: str, album: str = "") -> "str | None":
    """按标签在官方 music.db 反查曲库文件路径（audio_file.path，最新优先）。

    find_cache_file 的兜底数据源：与 official_track_guid_by_tags 同款两遍
    精确/NOCASE 查询，但取 a.path 且逐行校验文件确实存在（官方库里的
    悬空记录不算命中）。
    """
    title_s = (title or "").strip()
    artist_s = (artist or "").strip()
    album_s = (album or "").strip()
    if not title_s or not artist_s:
        return None
    db = str(CONF.get("music_db") or "")
    if not db or not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception as e:
        logger.warning("Library tags lookup db open failed: %s", e)
        return None
    try:
        for nocase in (False, True):
            collate = " COLLATE NOCASE" if nocase else ""
            sql = (
                "SELECT a.path FROM track t "
                "JOIN audio_file a ON a.id = t.audio_file_id "
                "JOIN track_artist ta ON ta.track_id = t.id "
                "JOIN artist ar ON ar.id = ta.artist_id "
                f"WHERE t.title = ?{collate} AND ar.name = ?{collate}"
            )
            params: list = [title_s, artist_s]
            if album_s:
                sql += (
                    " AND EXISTS (SELECT 1 FROM album al"
                    f" WHERE al.id = t.album_id AND al.name = ?{collate})"
                )
                params.append(album_s)
            sql += " ORDER BY t.id DESC"
            try:
                rows = con.execute(sql, params).fetchall()
            except Exception as e:
                logger.warning("Library tags lookup query failed: %s", e)
                return None
            for row in rows:
                path = str(row[0] or "").strip()
                if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                    return path
        return None
    finally:
        con.close()


async def _official_upstream_write(method: str, path: str, headers: dict, payload: dict) -> bool:
    """以扩展名义直调官方写接口（复用触发请求凭证头）；code==0 视为成功。"""
    try:
        client = get_upstream_client(app)
        req = client.build_request(method, path, headers=dict(headers or {}), json=payload)
        resp = await client.send(req)
        if resp.status_code != 200:
            logger.warning("Official bind %s %s -> http %s", method, path, resp.status_code)
            return False
        body = resp.json()
    except Exception as e:
        logger.warning("Official bind %s %s failed: %s", method, path, type(e).__name__)
        return False
    if isinstance(body, dict) and body.get("code") == 0:
        return True
    logger.warning("Official bind %s %s -> %s", method, path, body)
    return False


def _fav_bind_pending(guid: str, user_guid: str) -> bool:
    try:
        return any(
            it.get("guid") == guid and it.get("bind") == "pending"
            for it in load_online_favorites(user_guid)
        )
    except Exception:
        return False


async def _remove_fav_bound(guid: str, user_guid: str) -> bool:
    """官方收藏写入成功后删本地映射条目（仅 bind=pending；老数据不动）。"""
    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            kept = [it for it in items if not (it.get("guid") == guid and it.get("bind") == "pending")]
            if len(kept) == len(items):
                return False
            save_online_favorites(user_guid, kept)
            return True
        except Exception as e:
            logger.warning("Error removing bound favorite for %s/%s: %s", user_guid, guid, e)
            return False


def _plt_bind_pending(playlist_guid: str, guid: str, user_guid: str) -> bool:
    try:
        bucket = load_playlist_tracks(user_guid).get(playlist_guid) or []
        return any(it.get("guid") == guid and it.get("bind") == "pending" for it in bucket)
    except Exception:
        return False


async def _remove_plt_bound(playlist_guid: str, guid: str, user_guid: str) -> bool:
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.get(playlist_guid)
            if not bucket:
                return False
            kept = [it for it in bucket if not (it.get("guid") == guid and it.get("bind") == "pending")]
            if len(kept) == len(bucket):
                return False
            if kept:
                items[playlist_guid] = kept
            else:
                items.pop(playlist_guid, None)
            save_playlist_tracks(user_guid, items)
            return True
        except Exception as e:
            logger.warning("Error removing bound playlist track for %s/%s: %s", user_guid, guid, e)
            return False


def _dispatch_official_binding(guid: str, headers: "dict | None", meta: "dict | None") -> None:
    """下载落库完成后调度官方绑定：逐用户去重起任务；无意图时零开销。"""
    if not CONF.get("fav_auto_bind"):
        return
    users = _bind_pending.get(guid)
    if not users:
        return
    for user_guid in list(users):
        if headers:
            users[user_guid]["headers"] = dict(headers)
        key = (guid, user_guid)
        existing = _bind_tasks.get(key)
        if existing is not None and not existing.done():
            continue
        task = asyncio.create_task(_bind_official_task(guid, user_guid, meta))
        _bind_tasks[key] = task

        def _done(t: "asyncio.Task", key=key) -> None:
            if _bind_tasks.get(key) is t:
                del _bind_tasks[key]

        task.add_done_callback(_done)


async def _bind_official_task(guid: str, user_guid: str, meta: "dict | None") -> None:
    """把单个用户的收藏/歌单意图写入官方，成功后删本地映射（官方列表唯一显示）。"""
    intent = (_bind_pending.get(guid) or {}).get(user_guid)
    if not intent:
        return
    headers = dict(intent.get("headers") or {})
    title = str((meta or {}).get("title") or "")
    artist = str((meta or {}).get("artist") or "")
    album = str((meta or {}).get("album") or "")
    if not (title.strip() and artist.strip()):
        snap = _lookup_online_snapshot(guid)
        snap = _snapshot_to_info(snap) if isinstance(snap, dict) and snap else {}
        title = title.strip() or str(snap.get("title") or "")
        artist = artist.strip() or str(snap.get("artist") or "")
        album = album.strip() or str(snap.get("album") or "")
    if not (title.strip() and artist.strip()):
        logger.info("Official bind skip for %s: no usable metadata", guid)
        return
    # 官方只认曲库内 guid：等待官方把刚落盘文件扫入库（只读轮询，绝不写官方库）
    _schedule_library_scan(headers)
    try:
        timeout_s = max(1.0, float(CONF.get("official_bind_timeout_s") or 120))
    except (TypeError, ValueError):
        timeout_s = 120.0
    deadline = time.monotonic() + timeout_s
    official_guid = await asyncio.to_thread(official_track_guid_by_tags, title, artist, album)
    while not official_guid and time.monotonic() < deadline:
        await asyncio.sleep(_BIND_POLL_INTERVAL_S)
        official_guid = await asyncio.to_thread(official_track_guid_by_tags, title, artist, album)
    if not official_guid:
        logger.info(
            "Official bind wait timeout for %s (%s - %s)：官方曲库尚未收录，保留本地映射待自愈重试",
            guid, artist, title,
        )
        return
    if intent.get("fav") and _fav_bind_pending(guid, user_guid):
        ok = await _official_upstream_write(
            "POST", "/music/api/v1/favorite-track/create", headers, {"trackGUID": official_guid},
        )
        if ok and await _remove_fav_bound(guid, user_guid):
            _record_bind(user_guid, guid, official_guid)
            logger.info("Official favorite bound for %s (%s) -> %s", guid, user_guid, official_guid)
        elif ok:
            # 写官方期间用户已取消收藏：补偿撤销官方收藏，不留幽灵状态
            await _official_upstream_write(
                "POST", "/music/api/v1/favorite-track/delete", headers, {"trackGUID": official_guid},
            )
    for playlist_guid in sorted(set(intent.get("playlists") or ())):
        if not _plt_bind_pending(playlist_guid, guid, user_guid):
            continue
        ok = await _official_upstream_write(
            "POST", "/music/api/v1/playlist/add-track", headers,
            {"guid": playlist_guid, "trackGUIDs": [official_guid]},
        )
        if ok and await _remove_plt_bound(playlist_guid, guid, user_guid):
            _record_bind(user_guid, guid, official_guid, playlist_guid=playlist_guid)
            logger.info("Official playlist bound for %s (%s -> %s)", guid, playlist_guid, official_guid)
        elif ok:
            await _official_upstream_write(
                "POST", "/music/api/v1/playlist/remove-track", headers,
                {"guid": playlist_guid, "trackGUIDs": [official_guid]},
            )
    # 意图清理：本地已无 pending 条目则移除；失败项保留待列表读取自愈重试
    live = _bind_pending.get(guid, {}).get(user_guid)
    if live is not None:
        still_pending = (live.get("fav") and _fav_bind_pending(guid, user_guid)) or any(
            _plt_bind_pending(pl, guid, user_guid) for pl in (live.get("playlists") or ())
        )
        if not still_pending and _bind_pending.get(guid, {}).get(user_guid) is live:
            _bind_pending[guid].pop(user_guid, None)
            if not _bind_pending[guid]:
                _bind_pending.pop(guid, None)


# === 绑定登记（online guid -> 官方 guid，bind_registry.json） ===
# 官方绑定完成后本地映射已删，但客户端旧会话可能仍持伪装 id 操作取消收藏/
# 移出歌单；登记命中时翻译成官方 guid 转发官方删除，避免"取消没生效"。

_BIND_REGISTRY_LIMIT = 1000
_BIND_REGISTRY_TTL_S = 30 * 86400.0
_bind_registry: "dict[str, dict[str, dict]]" = {}
_bind_registry_loaded = False


def bind_registry_path() -> str:
    return os.path.join(_HOME, "bind_registry.json")


def _ensure_bind_registry() -> None:
    global _bind_registry_loaded
    if _bind_registry_loaded:
        return
    _bind_registry_loaded = True
    try:
        with open(bind_registry_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for user, entries in data.items():
                if isinstance(entries, dict):
                    _bind_registry[str(user)] = {
                        str(g): e for g, e in entries.items() if isinstance(e, dict)
                    }
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to load bind registry: %s", e)


def _persist_bind_registry() -> None:
    path = bind_registry_path()
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump(_bind_registry, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
    except Exception as e:
        logger.warning("Failed to persist bind registry: %s", e)
        _remove_quiet(part_path)


def _prune_bind_registry() -> None:
    now = time.time()
    for user in list(_bind_registry):
        entries = _bind_registry[user]
        for g, entry in list(entries.items()):
            try:
                stale = now - float(entry.get("at") or 0) >= _BIND_REGISTRY_TTL_S
            except (TypeError, ValueError):
                stale = True
            if stale:
                entries.pop(g, None)
        if not entries:
            _bind_registry.pop(user, None)
    while sum(len(v) for v in _bind_registry.values()) > _BIND_REGISTRY_LIMIT:
        oldest: "tuple[float, str, str] | None" = None
        for user, entries in _bind_registry.items():
            for g, entry in entries.items():
                try:
                    at = float(entry.get("at") or 0)
                except (TypeError, ValueError):
                    at = 0.0
                if oldest is None or at < oldest[0]:
                    oldest = (at, user, g)
        if oldest is None:
            break
        _bind_registry[oldest[1]].pop(oldest[2], None)
        if not _bind_registry[oldest[1]]:
            _bind_registry.pop(oldest[1], None)


def _bind_entry_alive(entry: dict) -> bool:
    return bool(entry.get("fav") or entry.get("playlists"))


def _record_bind(user_guid: str, guid: str, official_guid: str,
                 playlist_guid: str | None = None) -> None:
    """官方写入成功后登记 online guid → 官方 guid 映射（旧会话假 id 删除时翻译转发）。"""
    _ensure_bind_registry()
    entries = _bind_registry.setdefault(user_guid, {})
    entry = entries.get(guid)
    if not isinstance(entry, dict) or entry.get("official") != official_guid:
        entry = {"official": official_guid, "fav": False, "playlists": [], "at": time.time()}
        entries[guid] = entry
    if playlist_guid:
        pls = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
        pls.append(playlist_guid)
        entry["playlists"] = pls
    else:
        entry["fav"] = True
    entry["at"] = time.time()
    _prune_bind_registry()
    _persist_bind_registry()


def lookup_bind_entry(user_guid: str, guid: str) -> "dict | None":
    _ensure_bind_registry()
    entry = _bind_registry.get(user_guid, {}).get(guid)
    return dict(entry) if isinstance(entry, dict) else None


def _forget_bind(user_guid: str, guid: str, fav: bool = False,
                 playlist_guid: str | None = None) -> None:
    """撤销登记中的收藏/歌单标记；两边都空则整条删除。"""
    _ensure_bind_registry()
    entries = _bind_registry.get(user_guid)
    if not entries:
        return
    entry = entries.get(guid)
    if not isinstance(entry, dict):
        return
    if fav:
        entry["fav"] = False
    if playlist_guid:
        entry["playlists"] = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
    if not _bind_entry_alive(entry):
        entries.pop(guid, None)
    if not entries:
        _bind_registry.pop(user_guid, None)
    _persist_bind_registry()


def _forget_bind_playlist(user_guid: str, playlist_guid: str) -> None:
    """删除歌单后清掉登记里该歌单的标记（防孤儿转发）。"""
    _ensure_bind_registry()
    entries = _bind_registry.get(user_guid)
    if not entries:
        return
    changed = False
    for g in list(entries):
        entry = entries[g]
        pls = [p for p in (entry.get("playlists") or []) if p != playlist_guid]
        if pls != (entry.get("playlists") or []):
            entry["playlists"] = pls
            changed = True
            if not _bind_entry_alive(entry):
                entries.pop(g, None)
    if not entries:
        _bind_registry.pop(user_guid, None)
        changed = True
    if changed:
        _persist_bind_registry()


def _lookup_bind_any_user(raw_guid: str) -> "tuple[str, str, dict] | None":
    """跨用户按在线 guid 或其伪装 32hex 形态反查绑定登记。

    返回 (user_guid, online_guid, entry)。官方绑定完成后本地映射已删、伪装
    反查表可能不再认识该 id，这里兜底直接比对确定性 md5。
    """
    _ensure_bind_registry()
    raw = str(raw_guid or "").strip()
    if not raw or not _bind_registry:
        return None
    resolved = resolve_real_guid(raw)
    for user, entries in _bind_registry.items():
        if is_online_guid(resolved) and resolved in entries:
            return user, resolved, dict(entries[resolved])
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        for user, entries in _bind_registry.items():
            for g, entry in entries.items():
                if fake_official_guid(g) == raw:
                    return user, g, dict(entry)
    return None


def _maybe_retry_official_binds(request: Request, user_guid: str,
                                fav_items: "list[dict] | None" = None,
                                plt_map: "dict[str, list[dict]] | None" = None) -> None:
    """列表读取时的自愈：bind=pending 条目补发官方绑定或下载（用户级 60s 节流）。

    覆盖代理重启丢内存意图、官方迟扫入库、下载中断等场景；凭证头取当前请求，
    绝不持久化。
    """
    if not CONF.get("fav_auto_bind") or not user_guid:
        return
    fav_guids = [
        str(it.get("guid") or "") for it in (fav_items or [])
        if it.get("bind") == "pending" and it.get("guid")
    ]
    plt_guids: "dict[str, list[str]]" = {}
    for playlist_guid, bucket in (plt_map or {}).items():
        pending = [
            str(it.get("guid") or "") for it in (bucket or [])
            if it.get("bind") == "pending" and it.get("guid")
        ]
        if pending:
            plt_guids[playlist_guid] = pending
    if not fav_guids and not plt_guids:
        return
    now = time.monotonic()
    if now - _bind_retry_last.get(user_guid, -_BIND_RETRY_THROTTLE_S) < _BIND_RETRY_THROTTLE_S:
        return
    _bind_retry_last[user_guid] = now
    headers = copy_incoming_headers(request)
    uniq = list(dict.fromkeys(fav_guids + [g for gs in plt_guids.values() for g in gs]))
    for guid in fav_guids[:_BIND_RETRY_BATCH]:
        _register_bind_intent(guid, user_guid, headers, None)
    for playlist_guid, guids in plt_guids.items():
        for guid in guids[:_BIND_RETRY_BATCH]:
            _register_bind_intent(guid, user_guid, headers, playlist_guid)
    for guid in uniq[:_BIND_RETRY_BATCH]:
        if find_cache_file(guid):
            _dispatch_official_binding(guid, headers, None)
        else:
            _register_background_fetch(request, guid, "fav_auto_bind")


async def _stream_head_response(request: Request, guid: str, cached: str | None, range_header: str | None) -> Response:
    """HEAD 探测（部分手机播放器先 HEAD 后 GET）：缓存命中回真实大小头；在线源轻量试开一次即关。"""
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        full = serve_file_with_range(cached, range_header, media_type_for_ext(ext))
        return Response(status_code=full.status_code, headers=dict(full.headers))
    opened = None
    try:
        opened = await asyncio.wait_for(_open_online_stream(request, guid, range_header), timeout=4.0)
    except Exception:
        opened = None
    if not opened:
        return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)
    resp, owned, _ext, _info, _chunks, _first = opened
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for key in ("content-type", "content-length", "content-range"):
        val = resp.headers.get(key)
        if val:
            headers[key] = val
    with anyio.CancelScope(shield=True):
        await resp.aclose()
        if owned:
            await owned.aclose()
    return Response(status_code=206 if "content-range" in headers else 200, headers=headers)


def _candidate_kbps(item: dict) -> float:
    """同录音候选的估算码率 kbps（file_size*8/duration）；未知返回 0。"""
    try:
        dur = float(item.get("duration_s") or 0)
        size = float(item.get("file_size") or 0)
        if dur > 0 and size > 0:
            return size * 8.0 / dur / 1000.0
    except (TypeError, ValueError):
        pass
    return 0.0


def ordered_stream_alternatives(item: dict) -> list[dict]:
    """musicdl 同录音跨平台候选按音质模式排序。

    high：码率高者优先；balanced：中间码率先向下再向上；smooth：码率低者优先。
    无码率信息时保持原始顺序。
    """
    alts = [
        x for x in (item.get("_alternatives") or [])
        if isinstance(x, dict) and _same_recording(item, x)
    ]
    mode = str(CONF.get("quality_mode") or "high").strip().lower()
    known = sorted({round(_candidate_kbps(x), 1) for x in alts if _candidate_kbps(x) > 0})
    if not known:
        return list(alts)
    if mode == "smooth":
        order = known
    elif mode == "balanced":
        mid = len(known) // 2
        order = [known[mid]] + list(reversed(known[:mid])) + known[mid + 1:]
    else:
        order = list(reversed(known))
    rank = {v: i for i, v in enumerate(order)}
    return sorted(alts, key=lambda x: rank.get(round(_candidate_kbps(x), 1), len(order)))


@app.api_route("/music/api/v1/track/stream", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/track/stream/{subpath:path}", methods=["GET", "HEAD"])
async def stream_track(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    log_stream_probe(request.method, guid, range_header, bool(cached))
    if request.method == "HEAD":
        return await _stream_head_response(request, guid, cached, range_header)
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))
    item, entry = _retained_track(request, guid)
    candidates = [guid]
    # Byte offsets are encoding-specific: do not cross sources on seek/probe.
    if should_cache(range_header) and item and request.query_params.get("_ext_rendition") != "1":
        candidates += [online_guid_from_item(x) for x in ordered_stream_alternatives(item)]
    # 总预算：够第一个候选源跑完一轮（洛雪端点总预算 20s）+ 换候选重试一次。
    # 见 CONF["stream_total_timeout"] 上方注释（旧值 12.0）。
    deadline = asyncio.get_running_loop().time() + float(CONF.get("stream_total_timeout") or 30.0)
    for candidate in list(dict.fromkeys(candidates))[:3]:
        if not _source_enabled(candidate):
            continue
        for attempt in range(2):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                # 单次解析预算：略高于洛雪 /track/url 端点总预算（LX_URL_TIMEOUT=20s，
                # 其内部为「逐档解析 12s + Range 探活 5s」串行），让洛雪有机会试完
                # 候选音源再下结论。旧值 6.0s 会在洛雪解析中途掐断 → 404 → 点播跳曲。
                # 见 CONF["stream_startup_timeout"] 上方注释。
                opened = await asyncio.wait_for(
                    _open_online_stream(request, candidate, range_header),
                    timeout=min(float(CONF.get("stream_startup_timeout") or 22.0), remaining),
                )
            except Exception as exc:
                logger.warning("Stream startup failed for %s: %s", candidate, type(exc).__name__)
                opened = None
            if opened:
                resp, owned, ext, info, chunks, first = opened
                if candidate != guid:
                    # Publish the selected source identity before any audio.
                    # The client owns B's URL for later Range requests even if
                    # this proxy's search session expires; never alias B as A.
                    with anyio.CancelScope(shield=True):
                        await resp.aclose()
                        if owned:
                            await owned.aclose()
                    # Behind the fnOS gateway over a unix socket the request
                    # scheme is always http and the Host header carries no
                    # port, so an absolute URL would strand https/wan clients
                    # on an unreachable address; only a relative Location is
                    # safe for every client entry point.
                    target = request.url.include_query_params(guid=candidate, _ext_rendition="1")
                    location = target.path + (f"?{target.query}" if target.query else "")
                    return RedirectResponse(location, status_code=307, headers={"Cache-Control": "no-store"})
                # Cache the selected source's bytes under its own GUID, never
                # splice a failed stream or alias different encodings for seeks.
                if not should_cache(range_header):
                    # 定长窗口客户端：本响应过不了 tee 写盘门，注册后台整轨下载兜底
                    _register_full_fetch(request, candidate)
                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate, include_lyric=bool(CONF.get("lyric_auto_dl"))),
                    client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first,
                    scan_headers_factory=lambda: copy_incoming_headers(request),
                    post_finalize=lambda meta: _auto_lyric_after_finalize(request, candidate, meta))
            if attempt or deadline - asyncio.get_running_loop().time() <= 3:
                break
            if not await _recover_source(request, candidate, entry):
                break
    # 所有候选源都播不了：把入口 guid 记为「播不了」，组装列表时剔除（TTL 自动恢复）。
    # ★ 2026-10-11：点播失败是最可信的判据 —— 此前只有预热会写该黑名单，而预热只
    # 覆盖前 N 条（prefetch_limit，默认 10），排在后面的条目永远不被探活，
    # 用户点一次 404 后刷新仍在列表里 → 反复白点。
    _mark_unplayable(guid, "stream 404")
    return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)


async def _transcode_source(request: Request, guid: str) -> "tuple[str | None, dict | None]":
    """转码输入源：已落库/已缓存文件优先（源关了也能转），其次在线直链。"""
    cached = find_cache_file(guid)
    if cached:
        return cached, None
    if not _source_enabled(guid):
        return None, None
    source = source_from_online_guid(guid)
    try:
        if source == "netease":
            url = await resolve_netease_url(
                get_musicbox_client(request.app), song_id_from_online_guid(guid).split(":")[-1])
            return (url or None, None)
        if source == "lx":
            resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid))
            if not resolved or not resolved.get("url"):
                return None, None
            headers = {k: str(v) for k, v in (resolved.get("headers") or {}).items()
                       if k.lower() in ("referer", "user-agent")}
            return str(resolved["url"]), headers or None
        url = f"{str(CONF['musicdl_url']).rstrip('/')}/stream?id={quote(song_id_from_online_guid(guid))}&proxy=true"
        if _lossless_is_blacklisted(guid):
            url += "&quality=mp3"
        return url, None
    except Exception as e:  # noqa: BLE001
        logger.warning("transcode source resolve failed for %s: %s", guid, e)
        return None, None


async def _start_transcode_session(request: Request, guid: str) -> "tc.Session | None":
    """启动/复用标准音质转码会话；不可用返回 None（调用方回落单分片桩）。"""
    try:
        info = await _online_info(request, guid) or {}
        duration_s = float(info.get("duration_s") or 0)
        src, headers = await _transcode_source(request, guid)
        if not src:
            return None
        return await tc.ensure_session(
            guid, src, duration_s,
            root=CONF["cache_dir"],
            bitrate=str(CONF.get("transcode_bitrate") or "128k"),
            hls_time=float(CONF.get("transcode_hls_time") or 10),
            max_active=max(1, int(CONF.get("transcode_max_sessions") or 2)),
            max_cache_bytes=max(16, int(CONF.get("transcode_cache_max_mb") or 512)) * 1024 * 1024,
            headers=headers,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("transcode session start failed for %s: %s", guid, e)
        return None


def _transcode_live(guid: str) -> "tc.Session | None":
    """取在跑或磁盘已完整的转码会话（不解析在线源）。"""
    return tc.get_session(guid) or tc.peek_cached(guid, CONF["cache_dir"])


def _legacy_hls_stub(guid: str, info: "dict | None") -> Response:
    """无 ffmpeg / 源不可用时的兜底：单分片=原始文件（历史行为）。"""
    try:
        duration_s = int(float((info or {}).get("duration_s") or 0))
    except (TypeError, ValueError):
        duration_s = 0
    if duration_s <= 0:
        duration_s = 240
    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/hls/{guid}/preset.m3u8", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/track/hls/{guid}/{filename}", methods=["GET", "HEAD"])
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    guid = resolve_real_guid(guid)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    if filename != "preset.m3u8" and not tc.valid_segment_name(filename):
        return JSONResponse(content={"code": 404, "msg": "not found", "data": None}, status_code=404)

    sess = _transcode_live(guid)
    if sess is None and CONF.get("transcode_enabled") and tc.FFMPEG_BIN:
        # App 有时不先 POST /track/transcode 直接拉 playlist（实测），此处惰性补启
        sess = await _start_transcode_session(request, guid)

    if sess is not None:
        if filename == "preset.m3u8":
            return Response(content=tc.playlist_text(sess), media_type="application/vnd.apple.mpegurl")
        path = await tc.wait_file(os.path.join(sess.directory, filename), sess, timeout=30.0)
        if path:
            media = "audio/mp4" if filename == tc.INIT_NAME else "video/iso.segment"
            return serve_file_with_range(path, request.headers.get("range"), media)
        return JSONResponse(content={"code": 404, "msg": "segment unavailable", "data": None}, status_code=404)

    if filename != "preset.m3u8":
        return JSONResponse(content={"code": 404, "msg": "segment unavailable", "data": None}, status_code=404)
    info = await _online_info(request, guid)
    return _legacy_hls_stub(guid, info)


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    if request.url.path.endswith("/heartbeat"):
        tc.heartbeat(guid)
        try:
            await tc.maintain(CONF["cache_dir"], float(CONF.get("transcode_ttl_s") or 90),
                              max(16, int(CONF.get("transcode_cache_max_mb") or 512)) * 1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
    else:
        await tc.quit_session(guid)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    if CONF.get("transcode_enabled") and tc.FFMPEG_BIN:
        sess = _transcode_live(guid)
        if sess is None:
            sess = await _start_transcode_session(request, guid)
        if sess is not None and sess.status in ("starting", "running") and not find_cache_file(guid):
            # 标准音质播放不经过 /track/stream，tee 永不触发；挂后台整轨下载保住边听边存
            _register_full_fetch(request, guid)
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        }
    )


# === App 下载（音质偏好=标准 → 转码下载）仿真 ===
# 协议按官方实测对齐（本地歌曲透传抓包，2026-09-29）：
#   prepare POST {trackGUID, quality}  → data{status:"success", errno, errmsg, downloadId}
#   status  GET ?downloadId=           → data{status:"waiting|transcoding|ready|failed",
#                                             errno, errmsg, downloadId, percent}
#   file    GET ?downloadId=           → 音频字节（audio/mpeg，支持断点）
#   delete  POST {downloadId}          → data{downloadId, deleted:true}
# 官方"标准"档 = MP3 320kbps（FLAC 源实测 320067bps）；源已是 mp3 时直接供原文件
# 不做无意义重编码。官方服务不认识在线曲目的伪装 guid，透传必失败；此处仅当
# guid 是在线曲目才接管，本地曲目与未知 downloadId 一律透传官方。
_DL_TASKS: dict[str, dict] = {}
_DL_TASK_TTL_S = 3600.0
_DL_TRANSCODE_SUBDIR = "dltrans"


def _dl_transcode_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], _DL_TRANSCODE_SUBDIR, f"{cache_safe_guid(guid)}.mp3")


def _dl_sweep() -> None:
    now = time.monotonic()
    for key, task in list(_DL_TASKS.items()):
        if now - float(task.get("touched", 0)) > _DL_TASK_TTL_S:
            _DL_TASKS.pop(key, None)


async def _dl_body_json(request: Request) -> dict:
    try:
        raw = await request.body()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _dl_find_task(request: Request, body: dict) -> "dict | None":
    for key in ("downloadId", "taskGuid", "taskId", "id", "guid", "trackGUID"):
        value = request.query_params.get(key) or body.get(key)
        if value:
            hit = _DL_TASKS.get(str(value))
            if hit:
                hit["touched"] = time.monotonic()
                return hit
    return None


def _task_guid_of(task: dict) -> str:
    for key, value in _DL_TASKS.items():
        if value is task:
            task["downloadId"] = key
            return key
    return str(task.get("downloadId") or "")


def _dl_state_word(task: dict) -> str:
    """内部状态 → 官方 status 字段用词（completed=ready）。"""
    state = str(task.get("state") or "waiting")
    return {"completed": "ready"}.get(state, state)


async def _dl_produce(task: dict, cred_headers: dict) -> None:
    """后台产文件：标准档按官方规格 MP3 320k（源已是 mp3 直接供原文件）；原始档供原文件。"""
    guid = task["guid"]
    try:
        src = find_cache_file(guid)
        if not src and _source_enabled(guid):
            # 无本地文件：先走整轨下载管线（带元数据命名/落库，与边听边存同款）
            await _full_fetch_download(guid, cred_headers)
            src = find_cache_file(guid)
        if not src:
            task["state"] = "failed"
            task["errmsg"] = "online source unavailable"
            return
        if task.get("quality") == "original" or os.path.splitext(src)[1].lower() == ".mp3":
            task["path"] = src
        else:
            target = _dl_transcode_path(guid)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.isfile(target):
                task["state"] = "transcoding"
                task["percent"] = 0
                proc = await asyncio.create_subprocess_exec(
                    tc.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", src, "-vn", "-map_metadata", "-1",
                    "-c:a", "libmp3lame", "-b:a", str(CONF.get("transcode_dl_bitrate") or "320k"),
                    "-f", "mp3", target,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                if await proc.wait() != 0:
                    task["state"] = "failed"
                    task["errmsg"] = "transcode failed"
                    return
            task["path"] = target
        task["state"] = "completed"
        task["percent"] = 100
        try:
            task["size"] = os.path.getsize(task["path"])
        except OSError:
            task["size"] = 0
    except Exception as e:  # noqa: BLE001
        logger.warning("download transcode produce failed for %s: %s", guid, e)
        task["state"] = "failed"
        task["errmsg"] = "internal error"


async def _dl_forward(request: Request):
    """download/* 透传官方；trace_forward=detail 时抓双向往返报文（协议对齐取证）。"""
    if not CONF.get("trace_forward"):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    body = await request.body()
    if body:
        logger.info("[dl-capture] REQ %s %s body=%s", request.method, request.url.path, body[:2000])
    resp = await forward_to_upstream(request, get_upstream_client(request.app))
    captured: list[bytes] = []

    async def _tee():
        async for chunk in resp.body_iterator:
            captured.append(chunk)
            yield chunk
        if captured:
            logger.info("[dl-capture] RESP %s %s status=%s body=%s",
                        request.method, request.url.path, resp.status_code, b"".join(captured)[:2000])

    return StreamingResponse(_tee(), status_code=resp.status_code,
                             headers=filter_headers(resp.headers), media_type=resp.media_type)


@app.api_route("/music/api/v1/download/track/transcode/prepare", methods=["GET", "POST"])
async def dl_transcode_prepare(request: Request):
    body = await _dl_body_json(request)
    if CONF.get("trace_forward"):
        logger.info("[dl-capture] PREPARE req q=%s body=%s", dict(request.query_params), body)
    raw_guid = ""
    for key in ("guid", "trackGUID", "trackGuid"):
        value = request.query_params.get(key) or body.get(key)
        if value:
            raw_guid = str(value)
            break
    guid = resolve_real_guid(raw_guid)
    if not is_online_guid(guid):
        return await _dl_forward(request)
    quality = "standard"
    for key in ("quality", "qualityType"):
        value = str(body.get(key) or request.query_params.get(key) or "").lower()
        if value in ("original", "standard"):
            quality = value
            break
    if body.get("isOriginal") is True:
        quality = "original"
    _dl_sweep()
    download_id = uuid4().hex
    task = {"guid": guid, "quality": quality, "state": "waiting", "percent": 0,
            "downloadId": download_id, "touched": time.monotonic(), "started": time.time()}
    _DL_TASKS[download_id] = task
    asyncio.create_task(_dl_produce(task, copy_incoming_headers(request)))
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"status": "success", "errno": "", "errmsg": "", "downloadId": download_id},
    })


@app.api_route("/music/api/v1/download/track/transcode/status", methods=["GET", "POST"])
async def dl_transcode_status(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"status": _dl_state_word(task), "errno": "",
                 "errmsg": str(task.get("errmsg") or ""), "downloadId": _task_guid_of(task),
                 "percent": int(task.get("percent", 0))},
    })


@app.api_route("/music/api/v1/download/track/transcode/file", methods=["GET", "HEAD"])
async def dl_transcode_file(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    path = task.get("path")
    if task["state"] != "completed" or not path or not os.path.isfile(path):
        return JSONResponse(content={"code": 404, "msg": "not ready", "data": None}, status_code=404)
    media = "audio/mpeg" if os.path.splitext(path)[1].lower() == ".mp3" else "audio/mp4"
    fname = os.path.basename(path)
    fn_ext = os.path.splitext(fname)[1]
    quoted = quote(fname, encoding="utf-8")
    ascii_fn = re.sub(r'[^\x20-\x7e]', '_', fname)
    if not ascii_fn.strip() or ascii_fn == fn_ext:
        ascii_fn = f"track{fn_ext}"
    disposition = f'attachment; filename="{ascii_fn}"; filename*=UTF-8\'\'{quoted}'
    return serve_file_with_range(path, request.headers.get("range"), media,
                                 extra_headers={"Content-Disposition": disposition})


@app.api_route("/music/api/v1/download/track/transcode/delete", methods=["GET", "POST", "DELETE"])
async def dl_transcode_delete(request: Request):
    body = await _dl_body_json(request)
    task = _dl_find_task(request, body)
    if task is None:
        return await _dl_forward(request)
    download_id = ""
    for key, value in list(_DL_TASKS.items()):
        if value is task:
            _DL_TASKS.pop(key, None)
            download_id = key
    return JSONResponse(content={
        "code": 0, "msg": "",
        "data": {"downloadId": download_id, "deleted": True},
    })


_HISTORY_META_CACHE: "dict[str, tuple[dict, float]]" = {}
_HISTORY_META_TTL_S = 300.0


def _history_meta(guid: str) -> "dict | None":
    """从「播放历史」落盘快照里查该 guid 的元数据（title/artist/album/duration_s）。

    ★ 2026-10-09 新增：封面链的最后一层兜底。
    历史条目（如「最近播放」）的搜索缓存早已失效、上游详情也可能拿不到 —— 但播放时
    落盘的 `play_history/<user>.json` 里**存有** title/artist，足以让封面链走通
    「网易同名曲反查」。酷我（kw）源没有可用的免登录详情接口，全靠这条兜底。

    结果（含未命中）缓存 TTL 300s，避免列表每次渲染都扫盘。
    """
    now = time.time()
    hit = _HISTORY_META_CACHE.get(guid)
    if hit and now - hit[1] < _HISTORY_META_TTL_S:
        return hit[0] or None
    found = None
    try:
        root = dailyrec.play_history_dir()
        for fn in os.listdir(root):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as fh:
                    blob = json.load(fh)
            except Exception:
                continue
            items = blob if isinstance(blob, list) else (blob.get("items") or blob.get("list") or [])
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict) or str(it.get("guid") or "") != guid:
                    continue
                tr = it.get("track") if isinstance(it.get("track"), dict) else {}
                title = str(tr.get("title") or tr.get("name") or "").strip()
                if not title:
                    continue
                found = {
                    "id": guid,
                    "source": source_from_online_guid(guid),
                    # 注意 source_from_online_guid 对 online:lx:kw:x 只给到 "lx"，
                    # 真正的平台名在快照的 source 字段（如 "kw"）。
                    "lx_source": str(tr.get("source") or ""),
                    "title": title,
                    "artist": str(tr.get("artist") or ""),
                    "album": str(tr.get("album") or ""),
                    "cover_url": str(tr.get("coverUrl") or tr.get("cover_url") or ""),
                    "duration_s": float(tr.get("duration_s") or 0),
                    "ext": str(tr.get("ext") or "mp3"),
                    "file_size": int(tr.get("file_size") or 0),
                    "lyric": "",
                }
                break
            if found:
                break
    except Exception as e:  # noqa: BLE001
        logger.debug("history meta lookup failed for %s: %s", guid, e)
    _HISTORY_META_CACHE[guid] = (found or {}, now)
    return found


async def _online_info(request: Request, guid: str, include_lyric: bool = True, timeout_s: float = 4.0) -> dict | None:
    retained, entry = _retained_track(request, guid)
    if not _source_enabled(guid):
        # 源已关闭（例：切到网易后残留的 lx 历史条目）：上游不可用，但「播放历史」落盘
        # 快照里仍有 title/artist，足以让封面链走网易同名曲反查。
        # ★ 2026-10-17：这里以前直接 return retained，导致「最近播放」整片无封面 ——
        #   _history_meta 的两个兜底点都在下面，永远走不到。
        if not str((retained or {}).get("title") or "").strip():
            hist = _history_meta(guid)
            if hist:
                return _merge_public_track_meta(guid, hist)
        return _merge_public_track_meta(guid, retained)
    try:
        data = await asyncio.wait_for(_fetch_online_info(request, guid, include_lyric=include_lyric), timeout=timeout_s)
        if data:
            # ★ 2026-10-09：上游返回了结构但 title 为空（lx 源在内存缓存未命中时就是
            #   这样，返回的是空 stub）→ 用播放历史快照补全，否则封面链拿不到歌名，
            #   只能回落占位图。酷我（kw）源没有可用的免登录详情接口，全靠这条补全。
            if not str(data.get("title") or "").strip():
                hist = _history_meta(guid)
                if hist:
                    data = {**data, **{k: v for k, v in hist.items() if v}}
            return _merge_public_track_meta(guid, data)
        if await _recover_source(request, guid, entry):
            data = await asyncio.wait_for(_fetch_online_info(request, guid, include_lyric=include_lyric), timeout=max(3.0, timeout_s * 0.75))
            if data:
                return _merge_public_track_meta(guid, data)
    except Exception:
        pass
    # ★ 2026-10-09：搜索会话（_SEARCH_CACHE）没有、上游也拿不到时，回落到
    #   「播放历史落盘快照」（含 title/artist）—— 封面链据此可走网易同名曲反查。
    if not str((retained or {}).get("title") or "").strip():
        hist = _history_meta(guid)
        if hist:
            return _merge_public_track_meta(guid, hist)
    return _merge_public_track_meta(guid, retained)


def _merge_public_track_meta(guid: str, data: "dict | None") -> "dict | None":
    """用公共歌单注入时登记的元数据补齐空字段（★ 2026-10-18）。

    洛雪榜单/歌单曲目不进音源服务的 songInfo 缓存，`_online_info` 拿不到歌名与时长，
    客户端 audio-info 因此为空（判为不可播），歌词也随之缺失。注入时这些字段本来就是
    全的（平台榜单接口直接给了歌名/歌手/专辑/封面/时长），这里只补空、不覆盖已有值。
    """
    meta = pubpl.track_meta(guid)
    if not meta:
        return data
    merged = dict(data or {})
    for field, value in meta.items():
        if merged.get(field) in (None, "", 0):
            merged[field] = value
    return merged or data


async def _fetch_online_info(request: Request, guid: str, include_lyric: bool = True) -> dict | None:
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    data = res_data.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "")
                        ar = data.get("ar") or []
                        ar_names = []
                        if isinstance(ar, list):
                            for x in ar:
                                if isinstance(x, dict) and x.get("name"):
                                    ar_names.append(str(x["name"]))
                                elif isinstance(x, str):
                                    ar_names.append(x)
                        artist = " / ".join(ar_names)
                        al = data.get("al") or {}
                        album_name = str(al.get("name") or "") if isinstance(al, dict) else ""
                        cover_url = str(al.get("picUrl") or "") if isinstance(al, dict) else ""
                        dt = data.get("dt") or 0
                        duration_s = float(dt) / 1000.0 if dt else 0.0
                        sq = data.get("sq")
                        hr = data.get("hr")
                        h = data.get("h") or {}
                        ext = "flac" if (sq or hr) else "mp3"
                        size_obj = sq or h or {}
                        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

                        lyric_text = ""
                        if include_lyric:
                            try:
                                lr = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
                                if lr.status_code == 200:
                                    l_res = lr.json()
                                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                        l_data = l_res.get("data")
                                        if isinstance(l_data, dict):
                                            lyric_text = str(l_data.get("lyric") or "").strip()
                            except Exception as l_err:
                                logger.warning("musicbox lyric fetch in _online_info failed for %s: %s", guid, l_err)

                        return {
                            "id": f"netease:{song_id}",
                            "source": "netease",
                            "title": name,
                            "artist": artist,
                            "album": album_name,
                            "cover_url": cover_url,
                            "duration_s": duration_s,
                            "ext": ext,
                            "file_size": file_size,
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("musicbox /info failed for %s: %s", guid, e)
        return None

    if src == "lx":
        lx_client = get_lx_client(request.app)
        song_id = song_id_from_online_guid(guid)
        try:
            r = await lx_client.get("/api/v1/track/info", params={"id": song_id}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        lyric_text = str(inner.get("lyric") or "").strip()
                        if not lyric_text and include_lyric:
                            try:
                                lr = await lx_client.get(
                                    "/api/v1/track/lyric", params={"id": song_id}, timeout=10.0
                                )
                                if lr.status_code == 200:
                                    l_res = lr.json()
                                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                        lyric_text = str((l_res.get("data") or {}).get("lyric") or "").strip()
                            except Exception as l_err:
                                logger.warning("lxmusic lyric fetch failed for %s: %s", guid, l_err)
                        return {
                            "id": song_id,
                            "source": "lx",
                            "lx_source": str(inner.get("lx_source") or ""),
                            "title": str(inner.get("title") or ""),
                            "artist": str(inner.get("artist") or ""),
                            "album": str(inner.get("album") or ""),
                            # ★ 2026-10-09：酷狗源 cover_url 可能为空（v3 搜索接口无该字段，
                            #   实测 kg_search 全部条目 cover_url 空、union_cover 有值）→
                            #   兜底用 union_cover（带日期的真专辑图）。lx 侧已同步兜底，
                            #   此处再兜一层，兼容 lx 未更新或缓存里的旧数据。
                            "cover_url": str(inner.get("cover_url") or inner.get("union_cover") or ""),
                            "duration_s": float(inner.get("duration_s") or 0),
                            "ext": str(inner.get("ext") or "mp3") or "mp3",
                            "file_size": int(inner.get("file_size") or 0),
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("lxmusic /info failed for %s: %s", guid, e)
        return None

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                return data
    except Exception as e:
        logger.warning("musicdl /info failed for %s: %s", guid, e)
    return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=disguise_client_json(build_lyric_list_payload(guid, lyric_text)))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=disguise_client_json(res))
    return empty_ok()


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    payload = build_metadata_payload(guid, data)
    if guid in await _online_favorite_set(request):
        if isinstance(payload.get("data"), dict):
            payload["data"]["isFavorite"] = True
            if isinstance(payload["data"].get("track"), dict):
                payload["data"]["track"]["isFavorite"] = True
    return JSONResponse(content=disguise_client_json(payload))


# === 封面兜底链：cover_url 302 → 源 CDN 直构（kw rid / qq albummid）→
# 网易 cloudsearch 补全 → 本地缓存文件内嵌图 → 本地占位图池。
# 在线曲目封面永不 404（空封面是客户端裂图的主要来源）。 ===
_COVER_CDN_CACHE: dict[str, tuple[str, float]] = {}
_COVER_CDN_TTL_S = 7 * 86400.0
_KW_TEXT_COVER_HOST = dailyrec.KW_TEXT_COVER_HOST
_PLACEHOLDER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "covers")
_PLACEHOLDER_COUNT = 6
_PLACEHOLDER_CACHE: dict[str, bytes] = {}
# 1x1 灰点：占位图文件意外缺失时的最终兜底，保证响应仍是合法图片
_TINY_GRAY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\xf0\x1f\x00\x05\x05\x02\x00_\xc8\xe7A\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _cover_cache_get(key: str) -> str:
    hit = _COVER_CDN_CACHE.get(key)
    if hit and time.time() - hit[1] < _COVER_CDN_TTL_S:
        return hit[0]
    _COVER_CDN_CACHE.pop(key, None)
    return ""


def _cover_cache_put(key: str, url: str) -> None:
    if len(_COVER_CDN_CACHE) > 2000:
        _COVER_CDN_CACHE.clear()
    _COVER_CDN_CACHE[key] = (url, time.time())


def _kw_rid_from_guid(guid: str) -> str:
    """酷我 rid：lx 平台 online:lx:kw:<rid>；musicdl online:kuwo:<rid>。"""
    parts = (guid or "").split(":")
    if len(parts) >= 4 and parts[1] == "lx" and parts[2] == "kw":
        return parts[3]
    if len(parts) == 3 and parts[1] in ("kw", "kuwo"):
        return parts[2]
    return ""


_KG_HASH_RE = re.compile(r"^[0-9A-Za-z]{32}$")


def _kg_cover_by_hash(guid: str) -> str:
    """酷狗 hash 直构封面：online:lx:kg:<hash> → imge.kugou.com/stdmusic/480/<hash>.jpg。

    该 URL 模式为酷狗公开静态资源，免鉴权免 Referer（2026-10 实测 200 image/jpeg），
    覆盖 lx 酷狗源全部曲目；酷我源（kw）无此模式，走 artistpicserver/网易补全。
    """
    parts = (guid or "").split(":")
    if len(parts) >= 4 and parts[1] == "lx" and parts[2] == "kg":
        h = parts[3]
    elif len(parts) == 3 and parts[1] in ("kg", "kugou"):
        h = parts[2]
    else:
        return ""
    if not _KG_HASH_RE.match(str(h or "")):
        return ""
    return f"https://imge.kugou.com/stdmusic/480/{h}.jpg"


# 封面解析失败负缓存：识别不出的源（如 kw artistpicserver 403、网易搜索无命中）
# 短期内不会变好，TTL 内直接落占位图，避免每次列表渲染都重跑整条失败链
# （18 首 × kw 403 + 网易搜索，既慢又扰上游）。成功结果仍走 _COVER_CDN_CACHE。
#
# ⚠ 2026-10-08 修正（保守修）：
#   旧版**无条件**对「整条链失败」写负缓存，于是网络抖动（DNS 超时、连接重置、
#   上游 5xx）也会被固化 10 分钟 —— 实测 08:06~08:09 一批请求集中失败后，
#   25 个 guid 被锁死，之后两小时内即使上游恢复也一直返回占位图
#   （同一 guid 在 10-02/10-06 都成功过，证明不是「天生没封面」）。
#   现改为：
#     1. 只有**确定性失败**（明确的 404/410、解析出空 URL、命中默认封面黑名单）
#        才写负缓存；
#     2. 网络类失败（超时/连接错误/5xx）**不写**，下次请求立即重试；
#     3. TTL 从 600s 降到 120s，即便误判也能快速自愈。
_COVER_NEG_TTL_S = 120.0
_COVER_NEG_CACHE: "dict[str, float]" = {}

# 确定性失败：上游明确告知「这个资源不存在」，重试也没用
_COVER_DETERMINISTIC_STATUS = frozenset({400, 403, 404, 410, 451})

# 「本次封面解析过程中是否出现过网络类失败」的标记。
# 由 _fetch_cover_bytes_checked / _netease_direct_cover 置位，调用方读后清零。
# 用模块级变量而不是穿参数：这些函数在链路上被多处调用，改签名影响面太大。
_COVER_NETWORK_FAILED = False


def _cover_take_network_failed() -> bool:
    """读取并清零「网络类失败」标记（每次解析开头调用一次即可）。"""
    global _COVER_NETWORK_FAILED
    flag = _COVER_NETWORK_FAILED
    _COVER_NETWORK_FAILED = False
    return flag


def _cover_negative_get(key: str) -> bool:
    ts = _COVER_NEG_CACHE.get(key)
    if ts is None:
        return False
    if time.time() - ts < _COVER_NEG_TTL_S:
        return True
    _COVER_NEG_CACHE.pop(key, None)
    return False


def _cover_negative_put(key: str, *, deterministic: bool = True) -> None:
    """记录封面解析失败。

    deterministic=False 表示失败源于网络抖动（超时/连接错误/上游 5xx），
    这类失败不写负缓存 —— 否则会把「暂时拉不到」误判成「这歌没封面」。
    """
    if not deterministic:
        return
    if len(_COVER_NEG_CACHE) > 4000:
        _COVER_NEG_CACHE.clear()
    _COVER_NEG_CACHE[key] = time.time()


def _cover_cdn_client() -> httpx.AsyncClient:
    # 直构 CDN 探测用独立短超时客户端；transport 仅供测试注入 MockTransport
    kwargs: dict = {"timeout": 5.0, "follow_redirects": True}
    if _COVER_CDN_TRANSPORT is not None:
        kwargs["transport"] = _COVER_CDN_TRANSPORT
    return httpx.AsyncClient(**kwargs)


_COVER_CDN_TRANSPORT: "httpx.AsyncBaseTransport | None" = None


async def _kw_cover_by_rid(rid: str) -> str:
    """酷我 artistpicserver 按 rid 取真图（接口返回文本，需解析出图片地址）。"""
    rid = str(rid or "").strip()
    if not rid.isdigit():
        return ""
    cached = _cover_cache_get(f"kw:{rid}")
    if cached:
        return cached
    url = (
        "https://artistpicserver.kuwo.cn/pic?corp=kuwo&type=rid_pic"
        f"&pictype=500&size=500&rid={rid}"
    )
    try:
        async with _cover_cdn_client() as client:
            r = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.kuwo.cn/"},
            )
        if r.status_code != 200:
            return ""
        m = re.search(r"https?://[^\s'\"<>]+?\.(?:jpg|jpeg|png|webp)", r.text, re.IGNORECASE)
        if not m:
            return ""
        real = m.group(0)
    except Exception as e:
        logger.debug("kw artistpic resolve failed for rid=%s: %s", rid, e)
        return ""
    _cover_cache_put(f"kw:{rid}", real)
    return real


def _qq_cover_by_albummid(albummid: "str | None") -> str:
    mid = re.sub(r"[^0-9A-Za-z]", "", str(albummid or ""))
    if len(mid) < 8:
        return ""
    return f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{mid}.jpg?max_age=2592000"


# 图床"默认封面"黑名单：这些图技术上合法（HTTP 200 真图片），但内容是无信息量的
# 默认占位风格图（如酷狗 stdmusic 的黑胶唱片图），直接出图会以假乱真顶掉真实专辑封面。
# 命中黑名单 → 视为该直构源不可用，继续走下一级兜底（网易同名曲反查）。
_DEFAULT_COVER_MD5S = {
    "7d79006af51cdd1fe1aca16482a2ef3e",  # 酷狗 stdmusic 黑胶默认封面（480x480, 17853B）
    "371a28b2bb8cdac67dd75250cc5ada7c",  # 网易 music.126.net 默认封面（3955B PNG，2026-10-10 实测）
}


# 封面字节缓存：网易/酷狗图床的图每次都要重新下载（实测 0.8-1.6s/首），18 首列表
# 并发解析会让页面长时间停在波纹占位。缓存已下载的字节后，重复渲染毫秒级出图。
_COVER_BYTES_CACHE: "dict[str, bytes]" = {}
_COVER_BYTES_ORDER: "list[str]" = []
_COVER_BYTES_LIMIT = 48 * 1024 * 1024
_COVER_BYTES_MAX_ITEM = 4 * 1024 * 1024


def _cover_bytes_get(url: str) -> "bytes | None":
    return _COVER_BYTES_CACHE.get(url)


def _cover_bytes_put(url: str, data: bytes) -> None:
    if not data or len(data) > _COVER_BYTES_MAX_ITEM:
        return
    if url in _COVER_BYTES_CACHE:
        return
    _COVER_BYTES_CACHE[url] = data
    _COVER_BYTES_ORDER.append(url)
    total = sum(len(_COVER_BYTES_CACHE[k]) for k in _COVER_BYTES_ORDER)
    while _COVER_BYTES_ORDER and total > _COVER_BYTES_LIMIT:
        old = _COVER_BYTES_ORDER.pop(0)
        b = _COVER_BYTES_CACHE.pop(old, b"")
        total -= len(b)


async def _fetch_cover_bytes_checked(url: str, min_bytes: "int | None" = None) -> "bytes | None":
    """拉取封面字节并做可用性校验；不可用（失败/默认占位风格图）返回 None。

    与 _proxy_cover_response 的差异：不直接产出响应，允许调用方继续下一级兜底。

    **副作用**：失败源于网络抖动（超时/连接错误/上游 5xx）时置
    `_COVER_NETWORK_FAILED = True`，供调用方决定是否写负缓存 —— 网络抖动不该
    被固化成「这首歌没封面」（2026-10-08 修正，见 _cover_negative_put 注释）。

    min_bytes 可覆盖默认的 12KB 下限：歌单/榜单卡片封面常见极简构图（7~8KB 的真图），
    卡片支路传 _COVER_CARD_MIN_REAL_BYTES，避免把真封面判成默认图。
    """
    global _COVER_NETWORK_FAILED
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")) or _KW_TEXT_COVER_HOST in url:
        return None
    _cached = _cover_bytes_get(url)
    if _cached is not None:
        return _cached
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; fnmusic-ext/cover)"}
        if "music.126.net" in url or "126.net" in url:
            headers["Referer"] = "https://music.163.com/"
        elif "gtimg.cn" in url:
            headers["Referer"] = "https://y.qq.com/"
        async with _cover_cdn_client() as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            if r.status_code >= 500:
                _COVER_NETWORK_FAILED = True
            return None
        data = r.content or b""
        if not data or len(data) > _COVER_FETCH_MAX_BYTES:
            return None
        if not _sniff_image_mime(data):
            return None
        if len(data) < (_COVER_MIN_REAL_BYTES if min_bytes is None else int(min_bytes)):
            return None  # 图床「默认封面」这类无信息量小图，视为不可用继续降级
        if hashlib.md5(data).hexdigest() in _DEFAULT_COVER_MD5S:
            return None
        _cover_bytes_put(url, data)
        return data
    except Exception:
        # 超时 / 连接重置 / DNS 失败 —— 均为临时性，不写负缓存
        _COVER_NETWORK_FAILED = True
        return None


def _cover_bytes_response(data: bytes) -> Response:
    return Response(
        content=data,
        media_type=_sniff_image_mime(data) or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ==== 音源平台图标（2026-10-20）====
#
# 歌单卡片用平台图标表明来源（酷我/酷狗/QQ/网易/咪咕），替代名称里的
# 「[酷我]」文本前缀。图标是随包发布的本地 PNG（proxy/static/platforms/），
# 尺寸与既有占位图一致（300x300），客户端按 coverUrl 取图即可。

_PLATFORM_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "platforms")
_PLATFORM_ICON_CACHE: dict[str, bytes] = {}


def _platform_icon_bytes(source: str) -> bytes:
    """读取平台图标 PNG；不存在返回空字节。"""
    src = str(source or "").strip().lower()
    if src not in ("kw", "kg", "tx", "wy", "mg", "netease"):
        return b""
    if src in _PLATFORM_ICON_CACHE:
        return _PLATFORM_ICON_CACHE[src]
    path = os.path.join(_PLATFORM_ICON_DIR, f"{src}.png")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return b""
    if not data:
        return b""
    _PLATFORM_ICON_CACHE[src] = data
    return data


def _tx_songmid_from_guid(guid: str) -> str:
    """从 lx 腾讯源 guid（online:lx:tx:<songmid>）取 songmid；非 tx 源返回空。"""
    parts = str(guid or "").split(":")
    if len(parts) >= 4 and parts[0] == "online" and parts[1] == "lx" and parts[2] == "tx":
        return parts[3]
    return ""


async def _tx_track_meta(songmid: str) -> dict:
    """腾讯（lx:tx）曲目元信息：songmid → {albummid, title, artist}。

    lx 服务 /api/v1/track/info 对 tx 源返回全空（title/artist/cover_url 皆空），
    且 tx 曲目常不在推荐缓存里 → 无法从缓存 VO 回填歌名，网易同名曲兜底无关键词可搜，
    整条链落占位图（电脑端热门推荐歌单大图即由此无封面）。
    这里改用 QQ 音乐公开接口按 songmid 取 albummid（直构 gtimg 封面）+ 真实歌名/歌手
    （供网易兜底）。结果内存缓存，失败也缓存（短 TTL）避免每次渲染都拖一次外网。
    """
    songmid = str(songmid or "").strip()
    if not songmid:
        return {}
    hit = _cover_cache_get(f"tx:{songmid}")
    if hit == "__MISS__":
        return {}
    if hit:
        try:
            return json.loads(hit)
        except Exception:
            pass
    meta: dict = {}
    try:
        async with _cover_cdn_client() as client:
            r = await client.get(
                "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg",
                params={"songmid": songmid, "format": "json", "platform": "yqq"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"},
            )
        if r.status_code == 200:
            item = ((r.json() or {}).get("data") or [{}])[0]
            if isinstance(item, dict):
                al = item.get("album") or {}
                albummid = str(al.get("mid") or "")
                if len(re.sub(r"[^0-9A-Za-z]", "", albummid)) >= 8:
                    meta["albummid"] = albummid
                title = str(item.get("title") or item.get("name") or "")
                singers = [str(s.get("name")) for s in (item.get("singer") or []) if isinstance(s, dict) and s.get("name")]
                if title:
                    meta["title"] = title
                if singers:
                    meta["artist"] = " / ".join(singers)
    except Exception as e:
        logger.debug("tx track meta fetch failed for songmid=%s: %s", songmid, e)
    _cover_cache_put(f"tx:{songmid}", json.dumps(meta) if meta else "__MISS__")
    return meta


async def _prefetch_playlist_covers(tracks: list) -> None:
    """后台预热推荐曲目的封面解析结果（只填 _cover_cache，不取图片字节）。

    列表渲染时每首曲目都要现查：酷狗直构（1 次外网）+ 网易 cloudsearch（1 次外网）。
    18 首并发解析需要数秒，期间页面停在波纹占位图——容易被误判为"没有封面"。
    下发列表后后台预热，用户真正请求封面时多数已命中内存缓存，可毫秒级出真图。
    """
    try:
        sem = asyncio.Semaphore(4)

        async def _one(t):
            if not isinstance(t, dict):
                return
            title = str(t.get("title") or t.get("name") or "")
            artist = str(t.get("artist") or "")
            guid = str(t.get("coverId") or t.get("guid") or "")
            if not title and not guid:
                return
            async with sem:
                if title and not _cover_cache_get(f"wyc:{title.casefold()}|{artist.casefold()}"):
                    try:
                        await asyncio.wait_for(_netease_direct_cover(title, artist), timeout=8.0)
                    except Exception:
                        pass
                # 腾讯源：songmid→albummid 元信息也预热（gtimg 直构依赖它）
                if guid.startswith("online:lx:tx:") and not _cover_cache_get("tx:" + guid.split(":")[3]):
                    try:
                        await asyncio.wait_for(_tx_track_meta(guid.split(":")[3]), timeout=8.0)
                    except Exception:
                        pass

        await asyncio.gather(*[_one(t) for t in tracks[:24]], return_exceptions=True)
    except Exception as e:
        logger.debug("playlist cover prefetch failed: %s", type(e).__name__)


async def _enrich_cover_via_netease(request: Request, guid: str, data: dict) -> str:
    """musicdl/lx 空封面 → 网易 cloudsearch 同名曲补全（FNMUSIC_COVER_ENRICH=1）。"""
    if not CONF.get("cover_enrich", True):
        return ""
    title = str((data or {}).get("title") or "").strip()
    if not title:
        return ""
    artist = str((data or {}).get("artist") or "").strip()
    key = f"wy:{title.casefold()}|{artist.casefold()}"
    cached = _cover_cache_get(key)
    if cached:
        return cached
    musicbox_client = get_musicbox_client(request.app)
    keyword = " ".join(x for x in (artist, title) if x)
    target = ""
    try:
        r = await musicbox_client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": 5, "type": "song"},
            timeout=8.0,
        )
        if r.status_code != 200:
            return ""
        payload = r.json()
        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return ""
        title_l = title.casefold()
        artist_l = artist.casefold()
        for it in raw:
            if not isinstance(it, dict):
                continue
            it_title = str(it.get("song_name") or it.get("title") or "")
            if it_title.casefold() != title_l:
                continue
            it_artist = str(it.get("artist") or "")
            if artist_l and artist_l not in it_artist.casefold() and it_artist.casefold() not in artist_l:
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            d = await musicbox_client.get("/api/v1/songs/detail", params={"ids": sid}, timeout=8.0)
            if d.status_code == 200:
                dj = d.json()
                dl = dj.get("data") if isinstance(dj, dict) else None
                first = dl[0] if isinstance(dl, list) and dl and isinstance(dl[0], dict) else {}
                target = str(first.get("album_pic_url") or "")
            break
    except Exception as e:
        logger.debug("cover enrich via netease failed for %s: %s", guid, e)
        return ""
    if target:
        _cover_cache_put(key, target)
    return target


async def _netease_direct_cover(title: str, artist: str) -> str:
    """网易 cloudsearch/pc 直连同名曲补全（免 musicbox 依赖）。

    该端点无需 weapi 加密、无需登录 Cookie，仅带 Referer/UA 即可命中（2026-10 实测
    稳定 200，返回 result.songs[].al.picUrl）。当 musicbox（网易源）未启用时，它是
    酷我(lx kw)等无公开图床直构模式曲源的兜底封面来源；酷狗(lx kg)优先走
    _kg_cover_by_hash，无需走本兜底。

    匹配策略：先去掉标题里的括号修饰（如「（深情版）」）与前缀表演者（如「吉他 」），
    再做精确/包含匹配，score 高者优先，歌手命中再加分。网易搜索本身已按关键词模糊召回，
    若严格匹配无果但搜索有返回，退而取首个结果（其专辑图通常即所求），最大化封面覆盖率，
    避免「标题带版本后缀就对不上」而落占位图。
    """
    title = str(title or "").strip()
    if not title:
        return ""
    artist = str(artist or "").strip()

    # 抽取「核心曲名」：去括号修饰与前缀表演者，避免 祝福祖国（深情版）/吉他 祝福祖国 对不上
    def _core(t: str) -> str:
        t = re.sub(r"[\(（\[【].*?[\)）\]】]", " ", t)  # 去括号内容
        t = re.sub(r"^(吉他|钢琴|小提琴|大提琴|伴奏|dj|remix|翻唱|纯音乐|instrumental|现场|live)\s*", " ", t, flags=re.I)
        return re.sub(r"\s+", " ", t).strip()

    key = f"wyc:{title.casefold()}|{artist.casefold()}"
    cached = _cover_cache_get(key)
    if cached:
        return cached
    keyword = " ".join(x for x in (artist, title) if x)
    target = ""
    try:
        async with _cover_cdn_client() as client:
            r = await client.post(
                "https://music.163.com/api/cloudsearch/pc",
                data={"s": keyword, "type": 1, "limit": 8, "offset": 0},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            )
        if r.status_code != 200:
            # 5xx = 上游临时故障；不写负缓存，下次请求立即重试
            if r.status_code >= 500:
                globals()["_COVER_NETWORK_FAILED"] = True
            return ""
        payload = r.json()
        songs = payload.get("result", {}).get("songs") if isinstance(payload, dict) else None
        if not isinstance(songs, list) or not songs:
            return ""
        tl = _core(title).casefold()
        al_l = _core(artist).casefold()
        best_score = 0
        best_pic = ""
        for song in songs:
            if not isinstance(song, dict):
                continue
            s_name = str(song.get("name") or "")
            nl = _core(s_name).casefold()
            if nl == tl:
                score = 3
            elif tl and (tl in nl or nl in tl):
                score = 1
            else:
                continue
            ars = song.get("ar") or []
            if isinstance(ars, list) and ars and isinstance(ars[0], dict):
                s_artist = " / ".join(str(a.get("name")) for a in ars if isinstance(a, dict))
            else:
                s_artist = str(song.get("artist") or "")
            s_artist = _core(s_artist).casefold()
            if al_l and (al_l in s_artist or s_artist in al_l):
                score += 1
            al = song.get("al") or {}
            pic = str(al.get("picUrl") or "")
            if pic and score > best_score:
                best_score = score
                best_pic = pic.replace("http://", "https://")
        # 严格匹配无果：网易已按关键词模糊召回，首个结果通常相关，兜底取之提升覆盖率
        if not best_pic:
            al0 = (songs[0].get("al") or {}) if isinstance(songs[0], dict) else {}
            p0 = str(al0.get("picUrl") or "")
            if p0:
                best_pic = p0.replace("http://", "https://")
        target = best_pic
    except Exception as e:
        # 超时/连接错误属临时性 —— 标记后不写负缓存
        globals()["_COVER_NETWORK_FAILED"] = True
        logger.debug("netease direct cover failed for %s: %s", keyword, e)
        return ""
    if target:
        _cover_cache_put(key, target)
    return target


def _cover_title_core(title: str) -> str:
    """标题清洗：去括号备注 / 版本后缀 / DJ·Remix·Live 等修饰，取核心名。

    ★ v2.7.1：网易 cloudsearch 对超长混合关键词（如「琵琶曲 (马丽沈腾合唱DJ版)」）
    常直接召回为空 songs → 反查无日志静默失败。核心名「琵琶曲」则稳定命中。
    实测 10-14 五个失败专辑名（Invincible / 月亮桥 / 金玉良缘 / 琵琶曲 /
    微加幸福）清洗后均可召回。
    """
    t = str(title or "").strip()
    if not t:
        return ""
    t = re.sub(r"[\(（\[【][^\)）\]】]*[\)）\]】]", " ", t)  # 去括号内容
    t = re.sub(r"(?i)\b(dj|remix|live|cover|version|mix|edition|伴奏|纯音乐|现场|翻唱)\s*(版|Version)?\b", " ", t)
    t = re.sub(r"(?i)[\s\-—–_]+(dj|remix|live|cover|mix)\s*(版)?$", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -—–_")


def _cover_query_candidates(title: str, artist: str) -> "list[tuple[str, str]]":
    """反查关键词候选：原样优先，清洗后的核心名兜底；去重保序。

    原样词也可能命中（如标准专辑名），所以不替换而是**追加**候选，
    逐个尝试直到拿到真实封面。
    """
    out: "list[tuple[str, str]]" = []
    seen: "set[tuple[str, str]]" = set()

    def _add(t: str, a: str) -> None:
        t = str(t or "").strip()
        a = str(a or "").strip()
        if not t:
            return
        k = (t.casefold(), a.casefold())
        if k not in seen:
            seen.add(k)
            out.append((t, a))

    _add(title, artist)
    core = _cover_title_core(title)
    if core and core != title:
        _add(core, artist)
        _add(core, "")  # 歌手名也可能干扰召回（DJ铁柱 这类网易没有的艺人）
    return out


_VO_META_CACHE: "dict[str, tuple]" = {}
_VO_META_TTL = 60.0  # ★ v2.7.1：负结果只缓存 60s。推荐缓存会刷新，guid 的
# title 可能后来才有；旧实现负结果也永久缓存，等于把「暂时查不到」锁成
# 「永远查不到」（与 music.db 索引永久快照同源）。


def _vo_meta_from_cache(guid: str) -> "tuple[str, str]":
    """从推荐缓存 VO 反查曲目的 (title, artist)。

    _online_info 对 lx 酷我源（online:lx:kw:）拿不到标题，导致网易同名曲补全兜底无关键词
    可搜、整条链落占位图。而推荐缓存（热门/每日）里的 VO 曲目始终带 title/artist，借此回填，
    让 KW 等无公开图床直构模式的源也能走网易补全拿到真实封面。结果内存缓存，避免重复扫盘。
    """
    now = time.monotonic()
    hit = _VO_META_CACHE.get(guid)
    if hit is not None:
        res, ts = hit
        if now - ts < _VO_META_TTL or res[0]:
            # 命中且未过期；有结果的一直有效（内容不会变），空结果到期重查
            if res[0]:
                return res
            if now - ts < _VO_META_TTL:
                return res
        else:
            del _VO_META_CACHE[guid]
    res: "tuple[str, str]" = ("", "")
    try:
        import glob as _glob

        base = dailyrec.recommend_cache_dir()
        files = sorted(_glob.glob(os.path.join(base, "*", "*.json")), key=os.path.getmtime)
        for f in files[-200:]:
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            tracks = (data.get("tracks") or []) if isinstance(data, dict) else []
            for t in tracks:
                if not isinstance(t, dict):
                    continue
                if (t.get("guid") or "") == guid:
                    res = (
                        str(t.get("title") or t.get("song_name") or ""),
                        str(t.get("artist") or ""),
                    )
                    break
            if res[0]:
                break
    except Exception as e:
        logger.debug("vo meta lookup failed for %s: %s", guid, e)
    _VO_META_CACHE[guid] = (res, time.monotonic())
    return res


def _embedded_cover_bytes(guid: str) -> "tuple[bytes, str] | None":
    """本地边听边存缓存文件的内嵌专辑图（mutagen 读 ID3/FLAC/MP4 封面）。"""
    path = find_cache_file(guid)
    if not path:
        return None
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        tags = getattr(mf, "tags", None)
        if tags is None:
            return None
        pics: list = []
        if hasattr(tags, "pictures"):  # FLAC/APE
            pics = list(tags.pictures or [])
        elif hasattr(tags, "getall"):  # ID3
            pics = list(tags.getall("APIC") or [])
        elif hasattr(tags, "get"):
            covr = tags.get("covr")  # MP4
            pics = list(covr) if covr else []
        for pic in pics:
            data = getattr(pic, "data", None)
            if not data:
                continue
            mime = str(getattr(pic, "mime", "") or "")
            if "/" not in mime:
                mime = "image/jpeg"
            return bytes(data), mime
    except Exception as e:
        logger.debug("embedded cover extract failed for %s: %s", guid, e)
    return None


async def _netease_artist_cover(artist: str) -> str:
    """按歌手名取网易歌手头像直链 —— 封面链的最后一层兜底（占位图之前）。

    动机（实测 2026-10-10）：酷我对冷门曲目经常**连专辑图都不返回** ——
    「演唱会 王杰」40 条里只有 11 条带 web_albumpic_short（28%），而按
    title+artist 反查网易同名曲也命中不了（关键词太泛）。歌手头像虽然不是
    这张专辑的封面，但远好过一块灰占位图。

    必须**精确匹配歌手名**：模糊取首条曾把「声音恋人」搜成「恋人」。
    """
    name = str(artist or "").strip()
    if not name:
        return ""
    for sep in ("/", "、", "&", "，", ",", "；", ";"):
        if sep in name:
            name = name.split(sep)[0].strip()
            break
    if not name:
        return ""
    key = f"wya:{name.casefold()}"
    cached = _cover_cache_get(key)
    if cached:
        return cached
    target = ""
    try:
        async with _cover_cdn_client() as client:
            r = await client.post(
                "https://music.163.com/api/cloudsearch/pc",
                data={"s": name, "type": 100, "limit": 5, "offset": 0},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            )
        if r.status_code != 200:
            if r.status_code >= 500:
                globals()["_COVER_NETWORK_FAILED"] = True
            return ""
        payload = r.json()
        artists = (payload.get("result") or {}).get("artists") if isinstance(payload, dict) else None
        if isinstance(artists, list):
            want = name.casefold()
            for a in artists:
                if not isinstance(a, dict):
                    continue
                if str(a.get("name") or "").strip().casefold() != want:
                    continue  # 只认精确同名，避免「声音恋人」命中「恋人」
                target = str(a.get("picUrl") or a.get("img1v1Url") or "").strip()
                break
    except Exception as e:  # noqa: BLE001
        logger.debug("netease artist cover lookup failed for %r: %s", name, e)
        return ""
    if target:
        _cover_cache_put(key, target)
    return target


def _placeholder_cover_response(guid: str) -> Response:
    """占位图池确定性选取（guid 哈希）。

    关键：占位图绝不可被客户端长缓存（旧实现 max-age=86400 会让「封面解析失败回落的
    占位图」在 App/WebView 里冻住一整天——即便服务端后来修好，客户端仍显示波纹）。
    改为 no-cache：每次都回源确认，配合服务端负缓存（_COVER_NEG_CACHE）只在 TTL 内
    跳过昂贵的失败链，既不受上游拖累，也不会把占位图冻在客户端。真实封面（_proxy_cover
    / 内嵌图）仍带 max-age=86400 长缓存，不受影响。
    """
    digest = hashlib.sha256(str(guid or "").encode()).hexdigest()
    name = f"placeholder-{int(digest[:8], 16) % _PLACEHOLDER_COUNT}.png"
    content = _PLACEHOLDER_CACHE.get(name)
    if content is None:
        try:
            with open(os.path.join(_PLACEHOLDER_DIR, name), "rb") as f:
                content = f.read()
            _PLACEHOLDER_CACHE[name] = content
        except OSError:
            logger.warning("占位图缺失: %s", os.path.join(_PLACEHOLDER_DIR, name))
            content = _TINY_GRAY_PNG
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


def _cover_log(tag: str, msg: str) -> None:
    """封面端点诊断日志（临时）：写到 /tmp/fnmusic_cover_req.log，定位客户端真实取图行为。"""
    try:
        ts = time.strftime("%m-%d %H:%M:%S")
        with open("/tmp/fnmusic_cover_req.log", "a", encoding="utf-8") as _f:
            _f.write(f"{ts} [{tag}] {msg}\n")
    except Exception:
        pass


def _read_embedded_cover(path: str) -> "tuple[bytes, str] | None":
    """从任意音频文件读取内嵌专辑图（路径已知，不依赖 guid 反查）。

    飞牛扫描器只认内嵌图，本地曲库封面即存于音频文件本身；mutagen 读
    ID3(APIC)/FLAC(Picture)/MP4(covr)/OGG(METADATA_BLOCK_PICTURE)。
    """
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        tags = getattr(mf, "tags", None)
        if tags is None:
            return None
        pics: list = []
        if hasattr(tags, "pictures"):
            pics = list(tags.pictures or [])
        elif hasattr(tags, "getall"):
            pics = list(tags.getall("APIC") or [])
        elif hasattr(tags, "get"):
            covr = tags.get("covr")
            pics = list(covr) if covr else []
        for pic in pics:
            data = getattr(pic, "data", None)
            if not data:
                continue
            mime = str(getattr(pic, "mime", "") or "")
            if "/" not in mime:
                mime = "image/jpeg"
            return bytes(data), mime
    except Exception as e:
        logger.debug("read embedded cover failed for %s: %s", path, e)
    return None


_VO_PATH_CACHE: "dict[str, str]" = {}


def _vo_audio_path(guid: str) -> str:
    """从推荐缓存 VO 反查本地曲目的音频文件路径（audioSpec.path），免鉴权、快。"""
    cached = _VO_PATH_CACHE.get(guid)
    if cached is not None:
        return cached
    path = ""
    try:
        for cdir in dailyrec.recommend_cache_dirs():
            for fn in os.listdir(cdir):
                if not (fn.startswith("daily-") or fn.startswith("hot-")):
                    continue
                fpath = os.path.join(cdir, fn)
                if not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                tracks = (data.get("tracks") or []) if isinstance(data, dict) else []
                for t in tracks:
                    if not isinstance(t, dict):
                        continue
                    if (t.get("guid") or "") == guid:
                        ap = t.get("audioSpec") or {}
                        p = str(ap.get("path") or "") if isinstance(ap, dict) else ""
                        if p:
                            path = p
                        break
                if path:
                    break
            if path:
                break
    except Exception as e:
        logger.debug("vo audio path lookup failed for %s: %s", guid, e)
    _VO_PATH_CACHE[guid] = path
    return path


# ==== 本地曲库官方封面（album_/artist_/track_ 前缀 guid）====================
#
# 背景：飞牛官方为本地曲库条目生成的封面 id 形如 album_<32hex> / artist_<32hex> /
# track_<32hex>。这些 id 不是本项目的伪装 guid（那套是 md5(盐+真实guid) 登记在
# _FAKE_COVER_REVERSE 的纯 32-hex），resolve_real_guid() 无从反解，于是
# _local_cover_response 拿不到歌名与路径，整条链必然落占位图——实测各展示页
# （专辑页/歌手页/曲库列表/搜索结果）192 次解析中 131 次（68%）三字段全空。
#
# 而官方本身已把封面落盘了：<cover_root>/{album,artist,track}/<guid[:2]>/<guid>
# （无后缀为原图，另有 _w120/_w160/_w400/_w600.jpg 尺寸变体）。直接读它，
# 精确、零外部依赖、毫秒级返回，是本地展示页封面最可靠的来源。
#
# 读取顺序：
#   1) 官方封面目录直读（命中即返回，含 size 参数选尺寸变体）
#   2) 音频内嵌图（原有逻辑）
#   3) 目录扫描建立 官方guid → 音频文件 索引，再走在线反查
#   4) 占位图

_OFFICIAL_COVER_DIR_CANDIDATES = (
    "/vol1/@appmeta/trim.music/cover",
    "/usr/local/apps/@appdata/trim.music/cover",
    "/var/apps/@appdata/trim.music/cover",
    "/vol1/@appmeta/music/cover",
)

# 官方尺寸变体：客户端要 600/400/160/120，按需就近取；都没有则用无后缀原图
_OFFICIAL_COVER_SIZES = (600, 400, 160, 120)

_OFFICIAL_COVER_DIR: "str | None" = None
_OFFICIAL_COVER_DIR_PROBED = False

# 官方 guid → 本地音频文件路径（扫描音乐库建立）
_OFFICIAL_GUID_INDEX: "dict[str, str]" = {}
_OFFICIAL_GUID_INDEXED = False

# (title|artist) 归一键 → 音频文件路径，在线反查命中后可直接读内嵌图
_LIBRARY_TAG_INDEX: "dict[str, str]" = {}


def _official_cover_root() -> str:
    """定位官方封面缓存根目录（探测一次后缓存结果，含探测失败以免反复 stat）。"""
    global _OFFICIAL_COVER_DIR, _OFFICIAL_COVER_DIR_PROBED
    if _OFFICIAL_COVER_DIR_PROBED:
        return _OFFICIAL_COVER_DIR or ""
    _OFFICIAL_COVER_DIR_PROBED = True
    explicit = str(CONF.get("official_cover_dir") or "").strip()
    candidates = (explicit,) + _OFFICIAL_COVER_DIR_CANDIDATES if explicit else _OFFICIAL_COVER_DIR_CANDIDATES
    for root in candidates:
        if not root:
            continue
        try:
            if os.path.isdir(root) and any(
                os.path.isdir(os.path.join(root, kind)) for kind in ("album", "artist", "track")
            ):
                _OFFICIAL_COVER_DIR = root
                logger.info("official cover dir resolved: %s", root)
                return root
        except OSError:
            continue
    logger.warning("official cover dir not found, tried: %s", ", ".join(c for c in candidates if c))
    return ""


_OFFICIAL_GUID_PREFIX_KIND = {"album": "album", "artist": "artist", "track": "track"}


def _official_cover_file(guid: str, size: "int | None" = None) -> str:
    """官方 guid → 磁盘封面文件路径；无则空串。

    目录布局 <root>/<kind>/<guid[:2]>/<guid>，其中 <guid> 为 32-hex 主体。
    带 _wNNN.jpg 的是尺寸变体，优先按请求 size 就近选取。

    **【v2.6.18 修复】guid 形态与 kind 的关系（此前写错，导致本地曲库全面无封面）**

    官方 music.db 里 album.cover_guid / artist.cover_guid / track.cover_guid 存的就是
    **裸 32-hex**，而客户端列表行下发的 coverId 也正是这个裸值（见 _track_cover_id：
    本地 guid 原样返回）。该裸值同时就是官方封面目录里的文件名，即
        cover/album/c5/c5c1a420a7b6422eb51be7ea62d9761c
    所以 **kind 不能从 guid 字面推断**——裸值必须跨 album/artist/track 三类目录探测。

    带 album_/artist_/track_ 前缀是客户端在歌单大图等场景的包装形态，此时 kind 已知，
    可直接定位，不必探测。

    旧实现对裸值一律 `return ""`，等于把最可靠的一级（官方磁盘直读，零外部依赖、
    毫秒级）整个废掉，本地曲目只能落到内嵌图（实测覆盖率 7%）→ 在线反查 → 占位图，
    于是本地曲库/专辑页/歌手页集体"没有封面"。实测 music.db 的 19 个 cover_guid
    与磁盘文件名 100% 命中。
    """
    raw = str(guid or "").strip()
    if not raw:
        return ""
    hinted = ""
    for prefix in ("album", "artist", "track"):
        if raw.startswith(prefix + "_"):
            hinted = prefix
            raw = raw[len(prefix) + 1:]
            break
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return ""
    root = _official_cover_root()
    if not root:
        return ""
    # 带前缀 → kind 已知，优先只查该类；裸值 → 三类全查（album 命中率最高，放最前）
    kinds = (hinted,) if hinted else ("album", "artist", "track")
    for kind in kinds:
        p = _official_cover_file_in(root, kind, raw, size)
        if p:
            return p
    return ""


def _official_cover_file_in(root: str, kind: str, raw: str, size: "int | None") -> str:
    """在指定 kind 目录下定位 raw 的封面文件（按 size 选变体，再退无后缀原图）。"""
    base = os.path.join(root, kind, raw[:2])
    # 先按请求尺寸取变体（客户端 size 与官方变体名一致：600/400/160/120）
    if size:
        want = int(size)
        if want in _OFFICIAL_COVER_SIZES:
            order = [want] + [w for w in _OFFICIAL_COVER_SIZES if w != want]
        else:
            order = list(_OFFICIAL_COVER_SIZES)
        for w in order:
            p = os.path.join(base, f"{raw}_w{w}.jpg")
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
    # 再退到无后缀原图
    p = os.path.join(base, raw)
    if os.path.isfile(p) and os.path.getsize(p) > 0:
        return p
    # 无请求尺寸时也看一眼最大的变体（客户端不传 size 但要大图的情况）
    for w in _OFFICIAL_COVER_SIZES:
        p = os.path.join(base, f"{raw}_w{w}.jpg")
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return ""


def _library_roots() -> list:
    """音乐库根目录列表：优先 CONF 探测结果，其次常见默认位置。"""
    out: list = []
    lib = str(CONF.get("library_dir") or "").strip()
    if lib:
        out.append(lib)
    for p in (
        "/vol1/1000/音乐",
        "/vol1/1000/Music",
        "/vol1/music",
    ):
        if p not in out:
            out.append(p)
    return [p for p in out if os.path.isdir(p)]


_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".ape", ".wma", ".alac")


def _norm_tag(s: str) -> str:
    """标签归一：去括号修饰与常见前后缀，便于宽松匹配。"""
    t = str(s or "").strip()
    t = re.sub(r"[\(（\[【].*?[\)）\]】]", " ", t)
    t = re.sub(
        r"^(吉他|钢琴|小提琴|大提琴|伴奏|dj|remix|翻唱|纯音乐|instrumental|现场|live)\s*",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(r"[\s\-_·・]+", " ", t)
    return t.strip().casefold()


def _scan_library_index() -> None:
    """扫描音乐库，建立 官方guid→音频路径 与 (歌名|歌手)→音频路径 索引。

    官方 guid 是官方扫描器入库时生成的，无法从文件直接推算，故这里不试图
    反推 guid，只按标签建索引——供在线反查到封面直链后二次确认，也供
    内嵌图按标签回溯读取。扫描有 TTL，冷启动一次即可。
    """
    global _OFFICIAL_GUID_INDEXED
    if _OFFICIAL_GUID_INDEXED:
        return
    _OFFICIAL_GUID_INDEXED = True
    roots = _library_roots()
    if not roots:
        return
    n = 0
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(_AUDIO_EXTS):
                    continue
                full = os.path.join(dirpath, fn)
                title = artist = ""
                stem = re.sub(r"\.[^.]+$", "", fn)
                parts = [x.strip() for x in re.split(r"\s+-\s+", stem)]
                if len(parts) >= 2:
                    artist, title = parts[0], " - ".join(parts[1:])
                else:
                    title = stem
                # 标签优先（官方写过的 title/artist 更准）
                try:
                    from mutagen import File as MutagenFile

                    mf = MutagenFile(full)
                    tags = getattr(mf, "tags", None)
                    if tags is not None:
                        t = a = ""
                        for key in ("TIT2", "title", "\xa9nam"):
                            try:
                                v = tags.get(key)
                                if v:
                                    t = str(v[0] if isinstance(v, list) else v)
                                    break
                            except Exception:
                                continue
                        for key in ("TPE1", "artist", "\xa9ART"):
                            try:
                                v = tags.get(key)
                                if v:
                                    a = str(v[0] if isinstance(v, list) else v)
                                    break
                            except Exception:
                                continue
                        if t:
                            title = t
                        if a:
                            artist = a
                except Exception:
                    pass
                if not title:
                    continue
                n += 1
                if artist:
                    _LIBRARY_TAG_INDEX.setdefault(f"{_norm_tag(title)}|{_norm_tag(artist)}", full)
                _LIBRARY_TAG_INDEX.setdefault(f"{_norm_tag(title)}|", full)
    logger.info("library index scanned: files=%d keys=%d roots=%s", n, len(_LIBRARY_TAG_INDEX), roots)


def _library_file_by_tags(title: str, artist: str) -> str:
    """按归一后的歌名/歌手在扫描索引里找音频文件路径。"""
    _scan_library_index()
    t, a = _norm_tag(title), _norm_tag(artist)
    if not t:
        return ""
    for key in (f"{t}|{a}", f"{t}|"):
        p = _LIBRARY_TAG_INDEX.get(key)
        if p:
            return p
    return ""


async def _official_local_cover_response(guid: str, size: "int | None") -> "Response | None":
    """官方本地 guid（album_/artist_/track_ 前缀）封面：直读官方封面目录。

    命中即返回真实封面，未命中返回 None 由调用方继续后续兜底。
    """
    path = _official_cover_file(guid, size)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.debug("official cover read failed for %s: %s", path, e)
        return None
    if not data:
        return None
    return _cover_bytes_response(data)


async def _local_cover_response(request: Request, guid: str, size: "int | None" = None) -> Response:
    """本地曲库封面解析（v2.6.17：官方封面目录直读优先）。

    飞牛 /music/api/v1/static/cover 对本地曲库 guid 固定返回 400（上游根本不服务本地
    封面），原实现 forward_to_upstream 必 400。解析顺序：
      0) **官方封面目录直读**（新增）：官方已把本地封面落盘到
         <cover_root>/{album,artist,track}/<guid[:2]>/<guid>。这是 album_/artist_/track_
         这类官方本地 guid 唯一可靠的封面来源——它们既无内嵌图（实测本地库内嵌
         覆盖率仅 7%），也无法从 md5 反查表还原，只能直接读官方缓存文件。
      1) 内嵌图：从音频文件（audioSpec.path）读 ID3/FLAC/MP4 封面；
      2) 标签索引 + 在线反查：扫音乐库建 (歌名|歌手)→文件 索引，配合网易 cloudsearch
         同名曲取真实专辑图（实测命中率 100%）；
      3) 占位图（记负缓存）。

    标题/路径优先查推荐缓存（免鉴权、快）；缓存缺失时带鉴权探 track/metadata。
    """
    # 0) 官方封面目录直读：对 album_/artist_/track_ 前缀的官方 guid 命中率最高，
    #    且零外部依赖、毫秒级返回，放在最前面短路掉绝大部分请求。
    if _is_official_local_guid(guid):
        hit = await _official_local_cover_response(guid, size)
        if hit is not None:
            _cover_log("OFFICIAL_DISK", f"guid={guid} size={size}")
            return hit

    # 0.5) 本地曲目 guid -> 所属专辑封面（v2.6.18 新增）。
    #      官方 music.db 里绝大多数曲目 cover_guid 为空，客户端列表行下发的是裸
    #      track.guid，而官方封面目录只按 cover_guid 落盘（track.guid 命中 0/31）。
    #      不补这一级，本地曲库列表的每一行都会落占位图。
    if re.fullmatch(r"[0-9a-fA-F]{32}", guid or ""):
        mapped = _track_cover_index().get(guid)
        if mapped:
            hit = await _official_local_cover_response(mapped, size)
            if hit is not None:
                _cover_log("TRACK_ALBUM_DISK", f"guid={guid} cover={mapped} size={size}")
                return hit

    if _cover_negative_get(guid):
        return _placeholder_cover_response(guid)
    # 1) 标题/路径
    title, artist = _vo_meta_from_cache(guid)
    path = _vo_audio_path(guid)
    if not path and request is not None:
        try:
            mc = get_upstream_client(request.app)
            mh = copy_incoming_headers(request)
            mr = mc.build_request(
                "GET", f"/music/api/v1/track/metadata?guid={quote(guid, safe='')}", headers=mh
            )
            mresp = await mc.send(mr)
            if mresp.status_code == 200:
                try:
                    mb = mresp.json()
                except Exception:
                    mb = None
                if isinstance(mb, dict):
                    data = mb.get("data") if isinstance(mb.get("data"), dict) else mb
                    if isinstance(data, dict):
                        if not title:
                            title = str(data.get("title") or data.get("name") or "")
                        if not artist:
                            art = data.get("artist") or data.get("artists")
                            if isinstance(art, list) and art and isinstance(art[0], dict):
                                artist = str(art[0].get("name") or "")
                            else:
                                artist = str(art or "")
                        ap = data.get("audioSpec") or data.get("audio") or {}
                        if isinstance(ap, dict):
                            p = str(ap.get("path") or "")
                            if p:
                                path = p
        except Exception as e:
            logger.debug("local cover metadata probe failed for %s: %s", guid, e)
    # 官方本地 guid 拿不到 title/artist（无反查表），用音乐库扫描索引兜底：
    # 至少能定位到音频文件从而读到内嵌图，并为在线反查提供关键词。
    if not path:
        if not title:
            t2, a2 = _vo_meta_from_cache(guid)
            title, artist = title or t2, artist or a2
        guessed = _library_file_by_tags(title, artist) if title else ""
        if guessed:
            path = guessed
    # 2.0) **补齐歌名/歌手**（v2.6.18 修复）：本地曲目的 title/artist 常一路拿不到
    #   ——推荐缓存没有、track/metadata 对无 cover_guid 的条目返空、标签索引又需要
    #   title 才能定位文件。结果第 3 级 `if title:` 为假，在线反查被整段跳过，
    #   明明网易能搜到封面（实测 4/4 命中）却直接落占位图。
    #   这里按可靠性从高到低补：music.db 规范名 → 音频文件名反解。
    if not title:
        dt, da = _track_meta_from_db(guid)
        if dt:
            title, artist = dt, (artist or da)
    if not title and path:
        pt, pa = _title_artist_from_path(path)
        title, artist = title or pt, artist or pa
    if not path and title:
        path = _library_file_by_tags(title, artist)
    _cover_log("LOCAL_RESOLVE", f"guid={guid} title={title!r} artist={artist!r} path={path!r}")
    # 2) 内嵌图
    if path and os.path.exists(path):
        emb = _read_embedded_cover(path)
        if emb:
            data_b, mime = emb
            return Response(
                content=data_b, media_type=mime,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    # 3) 在线反查（★ v2.7.1：多候选 —— 原标题召回为空时用清洗后的核心名再试，
    #    治「琵琶曲 (马丽沈腾合唱DJ版)」这类超长混合词搜不出结果）
    # 2026-10-10：改用带校验的取字节（`_proxy_cover_response` 不拦图床默认封面）。
    # 判废时不 return，继续走 3.5（按 GUID 反查出的名称再试）→ 占位，避免把灰图当真图。
    if title:
        for kw_t, kw_a in _cover_query_candidates(title, artist):
            cover = await _netease_direct_cover(kw_t, kw_a)
            if cover:
                _b = await _fetch_cover_bytes_checked(cover)
                if _b:
                    _cover_log("LOCAL_NETEASE", f"guid={guid} bytes={len(_b)} src={cover[:52]}")
                    return _cover_bytes_response(_b)
                _cover_log("NETEASE_DEFAULT_SKIP", f"guid={guid} src={cover[:52]}")
    # 3.5) **按 guid 反查出的专辑/歌手名再试一次**（v2.6.18 新增）。
    #      官方库里部分专辑/歌手 cover_guid 为 NULL，官方封面目录对它们没有
    #      文件，磁盘直读天生无效；而裸 guid 本身不含任何名称信息，于是前面
    #      拿不到 title，这一段以前根本不会执行 —— 直接落占位图。
    #      实测本机 8 个这样的条目，用名称去网易反查 8/8 都能拿到真实封面。
    if not title:
        nm, alt = _name_by_guid(guid)
        for kw in ((nm, ""), (alt, nm), (nm, nm)):
            if not kw[0]:
                continue
            for kw_t, kw_a in _cover_query_candidates(kw[0], kw[1]):
                cover = await _netease_direct_cover(kw_t, kw_a)
                if cover:
                    # 2026-10-10：默认封面判废 → 继续试下一个关键词组合（而不是直接出灰）
                    _b = await _fetch_cover_bytes_checked(cover)
                    if _b:
                        _cover_log("NAME_NETEASE", f"guid={guid} kw={kw_t[:24]!r} bytes={len(_b)}")
                        return _cover_bytes_response(_b)
                    _cover_log("NETEASE_DEFAULT_SKIP", f"guid={guid} kw={kw_t[:24]!r}")
    # 4) 占位（负缓存，避免频繁重试）
    # 2026-10-08：网络抖动（超时/5xx）不写负缓存，避免把「暂时拉不到」锁成
    # 「这歌没封面」；只有走完整条链仍无果（确定性失败）才固化。
    _cover_negative_put(guid, deterministic=not _cover_take_network_failed())
    return _placeholder_cover_response(guid)


# === music.db 变更检测：内存索引按文件签名失效（v2.7.1） ===
#
# 2026-10-14 实测回归（用户报「又没有封面」）：三个内存索引
# （_TRACK_META_INDEX / _ALBUM_ARTIST_NAME_INDEX / _TRACK_COVER_INDEX）
# 构建成功后**永久缓存、无任何失效机制**。fnOS 曲库却在持续增长 ——
# 实测用户 10-12/10-13 新入库的 5 张专辑（Invincible / 月亮桥 / 金玉良缘 /
# 琵琶曲 / 微加幸福）在 album 表里明明有名字，却因 proxy 索引快照停在进程
# 启动时刻（10-11 17:55）而全部解析失败，整批落占位图，直到 proxy 重启。
#
# 这是 2026-10-10「构建失败也置位 → 空索引永久固化」的**同源变种**：
# 那次修「没建成功也当成功」，这次修「建成功后新数据进不来」。
#
# 注意：WAL 模式下新写入只进 `music.db-wal`，主库 mtime 不变 ——
# 签名必须同时覆盖 `-wal`，只看主库会漏掉绝大部分变更。
#
# ★ v2.7.1：签名状态**按索引各自独立**（_MUSIC_DB_IDX_STATE）。若共用一个
# 全局时间戳，一个索引刚重建就会把其他索引的重建也抑制在节流窗口内
# （实测：album 名索引重建后，曲目元数据索引 5s 内仍返回旧快照）。
_MUSIC_DB_IDX_STATE: "dict[str, dict]" = {}
_MUSIC_DB_REBUILD_MIN_INTERVAL = 5.0  # 节流：防官方 App 高频写入导致反复重建


def _music_db_state(name: str) -> "dict":
    return _MUSIC_DB_IDX_STATE.setdefault(name, {"sig": None, "at": 0.0})


def _music_db_path() -> str:
    return str(CONF.get("music_db") or "")


def _music_db_signature(db: str) -> "tuple":
    sig = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(db + suffix)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _music_db_stale(db: str, state: "dict | None" = None) -> bool:
    """索引是否已过期（db 签名变化且过了节流窗口）。"""
    state = state if state is not None else {}
    if not db or not os.path.isfile(db):
        return False
    now = time.monotonic()
    at = state.get("at") or 0.0
    if at and now - at < _MUSIC_DB_REBUILD_MIN_INTERVAL:
        return False
    try:
        return _music_db_signature(db) != state.get("sig")
    except OSError:
        return False


def _music_db_mark_fresh(db: str, state: "dict | None" = None) -> None:
    state = state if state is not None else {}
    try:
        state["sig"] = _music_db_signature(db)
    except OSError:
        state["sig"] = None
    state["at"] = time.monotonic()


# === 本地曲目 guid -> 专辑封面 cover_guid 映射（v2.6.18） ===
#
# 官方 music.db 里 31 首本地曲目有 29 首 cover_guid 为空，客户端列表行下发的是
# 裸 track.guid；而官方封面目录只按 cover_guid 落盘（实测 track.guid 命中 0/31），
# 于是本地曲库列表的每一行都落占位图 —— 表现为"歌都有，封面没有"。
#
# track.guid 无法从封面文件名反推，但 track -> album_id -> album.cover_guid 这条
# 关联链在 music.db 里是现成的（实测可覆盖 27/31 首）。这里建一张
# track.guid -> cover_guid 的内存索引，把这一级补上：仍然零外部依赖、毫秒级返回。
#
# 只用 album.cover_guid / track.cover_guid 本身确实存在于官方封面目录的值，
# 避免把没有封面的专辑（实测有 6 张专辑 cover_guid 为空）也塞进索引。

_TRACK_COVER_INDEX: "dict[str, str]" = {}
_TRACK_COVER_INDEXED = False


def _track_cover_index() -> "dict[str, str]":
    """建 track.guid -> cover_guid 索引（扫一次 music.db，进程内缓存）。

    ★ 2026-10-10：构建**失败不置** _TRACK_COVER_INDEXED，下次调用重试。
    旧实现在构建前就置位、异常时直接 return，于是 music.db 一时不可读
    （官方正在扫描/写 WAL）就把**空索引永久固化** —— 表现为「重启后短暂正常，
    之后本地曲库封面又整片回落占位图」。
    """
    global _TRACK_COVER_INDEXED
    db = _music_db_path()
    _st = _music_db_state("track_cover")
    if _TRACK_COVER_INDEXED:
        if not _music_db_stale(db, _st):
            return _TRACK_COVER_INDEX
        # ★ v2.7.1：签名变化（有新歌/新专辑入库）→ 清空重建，
        #   否则索引永久停在进程启动时的快照，新入库条目永远查不到。
        _TRACK_COVER_INDEX.clear()
        _TRACK_COVER_INDEXED = False
    if not db or not os.path.isfile(db):
        return _TRACK_COVER_INDEX      # 不置位：db 就绪后下次重试
    n = 0
    try:
        import sqlite3

        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            cur = con.cursor()
            # 只取确实能在官方封面目录定位到的 cover_guid（track 自己的优先，
            # 为空则回退所属专辑的 —— 与官方 App 的取图优先级一致）
            rows = cur.execute(
                """
                SELECT t.guid,
                       COALESCE(NULLIF(t.cover_guid, ''), a.cover_guid)
                FROM track t
                LEFT JOIN album a ON a.id = t.album_id
                """
            ).fetchall()
            for g, c in rows:
                g, c = str(g or ""), str(c or "")
                if not g or not c:
                    continue
                if not _official_cover_file(c):
                    continue  # 该专辑官方也没落盘封面，索引它只会把请求推向占位图
                _TRACK_COVER_INDEX.setdefault(g, c)
                n += 1
        finally:
            con.close()
    except Exception as e:
        logger.warning("track cover index build failed (will retry): %s", e)
        return _TRACK_COVER_INDEX      # ★ 不置位，下次重试
    _TRACK_COVER_INDEXED = True        # ★ 仅在成功后置位
    _music_db_mark_fresh(db, _st)      # ★ v2.7.1：记录构建时的 db 签名
    logger.info("track cover index: tracks=%d db=%s", n, db)
    return _TRACK_COVER_INDEX


def _is_official_local_guid(guid: str) -> bool:
    """是否为飞牛官方的本地曲库封面 id（且官方磁盘确有该封面文件）。

    这些 id 由官方扫描入库时生成，无法从 md5 反查表还原，是本地各展示页
    （专辑页/歌手页/曲库列表）封面请求的主要形态。

    **【v2.6.18 修复】必须同时接受裸 32-hex。** 官方 music.db 的
    album/artist/track.cover_guid 存的就是裸 32-hex，客户端列表行下发的也是它，
    而它正是官方封面目录里的文件名。旧实现只认 <kind>_<32hex>，于是本地曲库
    100% 走不进「官方磁盘直读」这一级，只能落到内嵌图（覆盖率 7%）→ 在线反查 →
    占位图，表现为本地各页面集体没有封面。

    仍以「官方磁盘上是否真有该文件」为准，理由同前：本项目下发的在线伪装封面 id
    也是 <prefix>_<32hex>，字面形态不足以区分，靠磁盘事实判定可避免走错分支。
    """
    raw = str(guid or "").strip()
    if not raw:
        return False
    body = raw
    for prefix in ("album", "artist", "track"):
        if raw.startswith(prefix + "_"):
            body = raw[len(prefix) + 1:]
            break
    if not re.fullmatch(r"[0-9a-fA-F]{32}", body or ""):
        return False
    return bool(_official_cover_file(raw))


def _title_artist_from_path(path: str) -> "tuple[str, str]":
    """从音频文件路径反解 (歌名, 歌手)。

    与 _scan_library_index 的切分规则保持一致：去掉扩展名后按 " - " 切，
    前段为歌手、后段为歌名（"夏鸣 - 孟婆的碗.flac" → ("孟婆的碗", "夏鸣")）。
    mutagen 里若写了标签则优先用标签（官方写入的更准）。
    """
    if not path:
        return "", ""
    title = artist = ""
    try:
        from mutagen import File as MutagenFile

        m = MutagenFile(path, easy=True)
        if m is not None:
            t = (m.get("title") or [""])[0]
            a = (m.get("artist") or m.get("albumartist") or [""])[0]
            if t:
                title = str(t).strip()
            if a:
                artist = str(a).strip()
    except Exception:
        pass
    if title and artist:
        return title, artist
    stem = re.sub(r"\.[^.]+$", "", os.path.basename(path))
    parts = [x.strip() for x in re.split(r"\s+-\s+", stem)]
    if len(parts) >= 2:
        artist = artist or parts[0]
        title = title or " - ".join(parts[1:])
    else:
        title = title or stem
    return title, artist


_TRACK_META_INDEX: "dict[str, tuple]" = {}
_TRACK_META_INDEXED = False


_ALBUM_ARTIST_NAME_INDEX: "dict[str, tuple]" = {}
_ALBUM_ARTIST_NAME_INDEXED = False


def _name_by_guid(guid: str) -> "tuple[str, str]":
    """裸 guid -> (名称, 备用关键词)；查 album / artist 两张表。

    官方库里不少专辑/歌手的 cover_guid 为 NULL（实测本机 4 张专辑 + 4 位歌手），
    官方封面目录对它们没有文件，磁盘直读这一级天生无效。此时唯一的出路是
    拿名称去在线反查真实专辑/歌手图，所以这里把 guid → 名称补上。

    专辑额外返回其下第一首曲目标题作为备用关键词：专辑名与网易上的专辑名
    往往不完全一致（如"菩提劝我莫回头" vs 曲名"菩提劝我莫回头（看那花开花又落）"），
    用曲名搜反而更容易命中。
    """
    global _ALBUM_ARTIST_NAME_INDEXED
    g = str(guid or "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", g):
        return "", ""
    db = _music_db_path()
    _st = _music_db_state("album_artist_name")
    if _ALBUM_ARTIST_NAME_INDEXED:
        if not _music_db_stale(db, _st):
            return _ALBUM_ARTIST_NAME_INDEX.get(g, ("", ""))
        # ★ v2.7.1：有新专辑/歌手入库 → 清空重建（旧实现永久缓存快照，
        #   10-12/13 新入库的 5 张专辑因此全部查不到名字，整批落占位图）
        _ALBUM_ARTIST_NAME_INDEX.clear()
        _ALBUM_ARTIST_NAME_INDEXED = False
    if not db or not os.path.isfile(db):
        return _ALBUM_ARTIST_NAME_INDEX.get(g, ("", ""))
    try:
        import sqlite3

        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            cur = con.cursor()
            for gid, nm in cur.execute("SELECT guid, name FROM album"):
                if gid and nm:
                    _ALBUM_ARTIST_NAME_INDEX.setdefault(str(gid), (str(nm), ""))
            for gid, nm in cur.execute("SELECT guid, name FROM artist"):
                if gid and nm:
                    _ALBUM_ARTIST_NAME_INDEX.setdefault(str(gid), (str(nm), ""))
            # 专辑的备用关键词：其下第一首曲目标题
            for gid, t in cur.execute("""
                SELECT a.guid, MIN(t.title)
                FROM album a JOIN track t ON t.album_id = a.id
                GROUP BY a.guid
            """):
                if gid and t and str(gid) in _ALBUM_ARTIST_NAME_INDEX:
                    nm, _old = _ALBUM_ARTIST_NAME_INDEX[str(gid)]
                    _ALBUM_ARTIST_NAME_INDEX[str(gid)] = (nm, str(t))
        finally:
            con.close()
    except Exception as e:
        # ★ 2026-10-10：失败不置位，下次调用重试（旧实现把空索引永久固化，
        #   于是「重启后短暂正常，之后本地曲库封面又整片占位图」）。
        logger.warning("album/artist name index build failed (will retry): %s", e)
        return _ALBUM_ARTIST_NAME_INDEX.get(g, ("", ""))
    _ALBUM_ARTIST_NAME_INDEXED = True   # ★ 仅在成功后置位
    _music_db_mark_fresh(db, _st)       # ★ v2.7.1：记录构建时的 db 签名
    return _ALBUM_ARTIST_NAME_INDEX.get(g, ("", ""))


def _track_meta_from_db(guid: str) -> "tuple[str, str]":
    """直查 music.db 取本地曲目的 (歌名, 歌手)。

    比路径反解更准：库里存的就是官方整理过的歌名/歌手，路径里的
    "歌手 - 歌名" 只是扫描器按文件名猜的（实测部分条目文件名带版本后缀，
    如 "（石红豆版)"，与库里的规范名不一致，网易反查用规范名命中率更高）。
    """
    global _TRACK_META_INDEXED
    db = _music_db_path()
    _st = _music_db_state("track_meta")
    if _TRACK_META_INDEXED:
        if not _music_db_stale(db, _st):
            return _TRACK_META_INDEX.get(str(guid or ""), ("", ""))
        # ★ v2.7.1：有新曲目入库 → 清空重建（同 _name_by_guid 的永久快照病）
        _TRACK_META_INDEX.clear()
        _TRACK_META_INDEXED = False
    if not db or not os.path.isfile(db):
        return _TRACK_META_INDEX.get(str(guid or ""), ("", ""))
    try:
        import sqlite3

        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            for g, t, p in con.execute("""
                SELECT t.guid, t.title, MIN(ar.name)
                FROM track t
                LEFT JOIN track_artist ta ON ta.track_id = t.id
                LEFT JOIN artist ar ON ar.id = ta.artist_id
                GROUP BY t.guid
            """):
                if g and t:
                    _TRACK_META_INDEX[str(g)] = (str(t), str(p or ""))
        finally:
            con.close()
    except Exception as e:
        # ★ 2026-10-10：失败不置位，下次调用重试（避免空索引被永久固化）。
        logger.warning("track meta index build failed (will retry): %s", e)
        return _TRACK_META_INDEX.get(str(guid or ""), ("", ""))
    _TRACK_META_INDEXED = True   # ★ 仅在成功后置位
    _music_db_mark_fresh(db, _st)  # ★ v2.7.1：记录构建时的 db 签名
    return _TRACK_META_INDEX.get(str(guid or ""), ("", ""))


def _request_cover_size(request: Request) -> "int | None":
    """客户端请求的封面边长（size 参数）；非法/缺省返回 None。"""
    try:
        raw = request.query_params.get("size") or request.query_params.get("w")
        if raw is None:
            return None
        v = int(str(raw).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    _ua = request.headers.get("user-agent", "")[:150]
    _q = {k: (str(v)[:70]) for k, v in request.query_params.items()}
    _cover_log("REQ", f"subpath={subpath!r} ua={_ua!r} q={_q}")
    # 2026-10-20：音源平台图标（coverId=platform:<source>）—— 歌单卡片用它表明来源，
    # 替代原先名称里的「[酷我]」文本前缀。图标随包发布，命中即返回，不查上游。
    _icon_id = str(request.query_params.get("coverId") or "").strip()
    if _icon_id.startswith("platform:"):
        _icon = _platform_icon_bytes(_icon_id.split(":", 1)[1])
        if _icon:
            _cover_log("PLATFORM_ICON", f"id={_icon_id}")
            return _cover_bytes_response(_icon)
        return Response(status_code=404)
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath
    playlist_kind = dailyrec.online_playlist_kind(guid)
    if playlist_kind:
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            _cover_log("AUTH_FAIL", f"guid={guid} subpath={subpath}")
            return auth_resp
        cached = dailyrec.load_daily_cache(user_guid, dailyrec.today_key(), playlist_kind)
        tracks = (cached or {}).get("tracks") or []
        picked = dailyrec.pick_playlist_cover_track(tracks)
        # 歌单大图：优先按封面曲目的「歌名+歌手」网易反查真实专辑图。
        # 在线图床（酷狗 stdmusic 等）不少歌曲的"封面"就是黑胶默认图，直构出来
        # 以假乱真（看起来像没封面）；网易同名曲搜到的基本是真实专辑封面。
        _pt_title = str((picked or {}).get("title") or "")
        _pt_artist = str((picked or {}).get("artist") or "")
        if _pt_title:
            _wy = await _netease_direct_cover(_pt_title, _pt_artist)
            if _wy:
                # 2026-10-10：默认封面判废 → 不 return，继续走下面的封面曲目本地/在线链
                _b = await _fetch_cover_bytes_checked(_wy)
                if _b:
                    _cover_log("PL_NETEASE", f"kind={playlist_kind} title={_pt_title[:30]} bytes={len(_b)}")
                    return _cover_bytes_response(_b)
                _cover_log("NETEASE_DEFAULT_SKIP", f"kind={playlist_kind} src={_wy[:52]}")
        picked_guid = str((picked or {}).get("guid") or "")
        if not is_online_guid(picked_guid):
            if picked_guid:
                # 本地曲目封面：飞牛 /static/cover 对本地 guid 固定 400（不服务本地封面），
                # 改为本地解析（官方封面目录直读 → 内嵌图 → 在线反查）。
                return await _local_cover_response(request, picked_guid, _request_cover_size(request))
            # 歌单里没有可用封面：不显示图标，客户端回落自带默认样式
            _cover_log("PL_NOCOVER_404", f"guid={guid}")
            return Response(status_code=404)
        guid = picked_guid
    # 公共平台歌单封面（#55）：预选登记里的封面直链同源直出
    if pubpl.is_public_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, _user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        cover = pubpl.cover_url_for(guid)
        if cover:
            # 卡片封面放宽尺寸门槛：榜单/歌单图为极简构图，7~8KB 的真图很常见
            _b = await _fetch_cover_bytes_checked(cover, _COVER_CARD_MIN_REAL_BYTES)
            if _b:
                return _cover_bytes_response(_b)
        return Response(status_code=404)
    # 网易账号歌单封面：登记的封面直链直接 302（兼容重启后反查表未重建的裸假 id）
    nm_pl_id = nmpl.playlist_id_from_cover_request(guid, request.query_params.get("coverId") or subpath)
    if nm_pl_id:
        cover = nmpl.cover_url_for(nm_pl_id)
        if cover:
            # 2026-10-10：默认封面判废（否则灰图会被当封面长缓存）
            # 2026-10-17：歌单卡片同样放宽尺寸门槛 —— 「我喜欢的音乐」红心图只有 2784B，
            # 按曲目的 12KB 门槛会被判废，卡片直接没有封面。
            _b = await _fetch_cover_bytes_checked(cover, _COVER_CARD_MIN_REAL_BYTES)
            if _b:
                return _cover_bytes_response(_b)
            _cover_log("NETEASE_DEFAULT_SKIP", f"guid={guid} kind=nmplaylist src={cover[:52]}")
        return Response(status_code=404)
    if not is_online_guid(guid):
        # 本地曲库封面：飞牛 /static/cover 对本地 guid 固定返回 400（不服务本地封面），
        # 故不再转发上游，改为本地解析（官方封面目录直读 → 内嵌图 → 在线反查）。
        _cover_log("LOCAL", f"guid={guid} ua={_ua[:60]}")
        return await _local_cover_response(request, guid, _request_cover_size(request))

    # 已登记的在线曲目封面直链（下发曲目行时登记）：直接出图，不查上游、不落负缓存。
    # 放在负缓存之前 —— 旧失败不该挡住后来才登记到的直链。
    if is_online_guid(guid):
        registered = _track_cover_lookup(guid)
        if registered:
            _b = await _fetch_cover_bytes_checked(registered, _COVER_CARD_MIN_REAL_BYTES)
            if _b:
                _cover_log("TRACK_REG", f"guid={guid} bytes={len(_b)}")
                return _cover_bytes_response(_b)
            _cover_log("TRACK_REG_SKIP", f"guid={guid} src={registered[:50]}")

    # 专辑锚点 guid（/search/album 在线专辑的 coverId）：登记时存了封面直链，直接 302
    album_entry = album_entry_from_real_guid(guid)
    if album_entry is not None:
        _aitem = album_entry.get("item") or {}
        cover = str(_aitem.get("cover_url") or "")
        if cover and _KW_TEXT_COVER_HOST not in cover:
            # 2026-10-10：默认封面判废；判废后不直接出灰，改用专辑名+歌手再走一次网易反查
            _b = await _fetch_cover_bytes_checked(cover)
            if _b:
                return _cover_bytes_response(_b)
            _cover_log("NETEASE_DEFAULT_SKIP", f"guid={guid} kind=album src={cover[:52]}")
        _at = str(_aitem.get("title") or _aitem.get("name") or "")
        if _at:
            _wy = await _netease_direct_cover(_at, str(_aitem.get("artist") or ""))
            if _wy:
                _b = await _fetch_cover_bytes_checked(_wy)
                if _b:
                    _cover_log("PROXY_NETEASE", f"guid={guid} kind=album title={_at[:24]!r} bytes={len(_b)}")
                    return _cover_bytes_response(_b)
        return _placeholder_cover_response(guid)

    # 负缓存：整条解析链曾失败（kw artistpicserver 403 / 网易无命中）的 guid，TTL 内
    # 直接落占位图，避免每次列表渲染都重跑昂贵失败链（慢且扰上游）。成功解析的 guid
    # 不会进负缓存；服务重启会清空负缓存，重新尝试解析（部署新版本即自动刷新）。
    if _cover_negative_get(guid):
        _cover_log("NEG_PH", f"guid={guid}")
        return _placeholder_cover_response(guid)

    # 封面只要元数据（歌名/歌手/封面直链），歌词纯属浪费：include_lyric=False 把
    # 网易源每次 2 次上游调用减到 1 次，并发渲染时不再挤爆 4s 预算（2026-10-17）。
    data = await _online_info(request, guid, include_lyric=False)
    cover = str((data or {}).get("cover_url") or "")
    # ① 已知直链封面同源回源；酷我文本封面（artistpicserver 返回的是文本页）除外
    # 2026-10-10：改用带校验的取字节（拦下图床默认封面小图），拿到默认图不再直接出灰，
    # 而是继续降级到 ②/③/④.5，避免「看起来有封面实际是灰块」。
    if cover and _KW_TEXT_COVER_HOST not in cover:
        _b = await _fetch_cover_bytes_checked(cover)
        if _b:
            _cover_log("PROXY_COVER", f"guid={guid} bytes={len(_b)}")
            return _cover_bytes_response(_b)
        _cover_log("COVER_SKIP_DEFAULT", f"guid={guid} src={cover[:52]}")

    # ② 按源+ID 直构 CDN：
    #    kg hash → 酷狗公开静态图（免鉴权，覆盖 lx 酷狗全部曲目，实测稳定 200）
    #    kw rid  → 酷我 artistpicserver 真图（实测 403 反爬，失败回落，后续进负缓存）
    #    tx songmid → QQ 公开接口换 albummid → 腾讯 gtimg（lx info 全空时的唯一可靠来源）
    #    qq albummid → 腾讯 gtimg
    _tx_meta: dict = {}
    direct = _kg_cover_by_hash(guid)
    if not direct:
        kw_rid = _kw_rid_from_guid(guid)
        if kw_rid:
            direct = await _kw_cover_by_rid(kw_rid)
    if not direct:
        tx_mid = _tx_songmid_from_guid(guid)
        if tx_mid:
            _tx_meta = await _tx_track_meta(tx_mid)
            direct = _qq_cover_by_albummid(_tx_meta.get("albummid"))
    if not direct:
        direct = _qq_cover_by_albummid((data or {}).get("albummid"))
    if direct:
        _b = await _fetch_cover_bytes_checked(direct)
        if _b:
            _cover_log("PROXY_DIRECT", f"guid={guid} src={direct[:55]}")
            return _cover_bytes_response(_b)
        # 直构图不可用（拉取失败或命中默认封面黑名单）→ 继续网易同名曲兜底
        _cover_log("DIRECT_SKIP", f"guid={guid} src={direct[:55]}")

    # ③ 网易 cloudsearch/pc 直连同名曲补全（免 musicbox 依赖；musicbox 未启用时唯一兜底）
    title = str((data or {}).get("title") or "")
    artist = str((data or {}).get("artist") or "")
    # lx info 对 tx 源同样返回空歌名，而 tx 曲目常不在推荐缓存里 → 用 QQ 接口取回的真名回填
    if not title and _tx_meta.get("title"):
        title = str(_tx_meta["title"])
    if not artist and _tx_meta.get("artist"):
        artist = str(_tx_meta["artist"])
    # _online_info 对 lx 酷我源拿不到标题 → 从推荐缓存 VO 回填，保证 KW 等源也能搜同名曲
    if not title or not artist:
        vt, va = _vo_meta_from_cache(guid)
        if not title:
            title = vt
        if not artist:
            artist = va
    enriched = await _netease_direct_cover(title, artist)
    if not enriched:
        enriched = await _enrich_cover_via_netease(request, guid, data or {})
    if enriched:
        _b = await _fetch_cover_bytes_checked(enriched)
        if _b:
            _cover_log("PROXY_NETEASE", f"guid={guid} bytes={len(_b)}")
            return _cover_bytes_response(_b)
        # ★ 关键（2026-10-10）：网易命中但其返回的是**网易自己的默认封面**（该曲在网易也
        #   无图，实测固定 3955B PNG）——这类图看不出是灰块却仍是无信息量图。不能当成功
        #   放行，必须继续降级到 ④ 内嵌图 / ④.5 歌手头像，否则客户端永远是一块灰。
        _cover_log("NETEASE_DEFAULT_SKIP", f"guid={guid} src={enriched[:52]}")

    # ④ 本地边听边存文件的内嵌专辑图
    embedded = _embedded_cover_bytes(guid)
    if embedded:
        art, mime = embedded
        _cover_log("EMBEDDED", f"guid={guid}")
        return Response(content=art, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

    # ④.5 歌手头像兜底（2026-10-10）：专辑图全缺时用同名歌手头像代替灰块。
    # 实测「演唱会 王杰」这类冷门曲目，酷我连专辑图都不返回（40 条仅 11 条有图），
    # 按 title+artist 反查同名曲也命中不了（关键词太泛）。放在内嵌图之后、占位图之前。
    _artist = str((data or {}).get("artist") or "").strip()
    if _artist:
        _artist_pic = await _netease_artist_cover(_artist)
        if _artist_pic:
            _b = await _fetch_cover_bytes_checked(_artist_pic)
            if _b:
                _cover_log("ARTIST_NETEASE", f"guid={guid} artist={_artist[:18]!r} bytes={len(_b)}")
                return _cover_bytes_response(_b)

    # ⑤ 本地占位图池：在线曲目封面永不 404。整条链失败 → 记负缓存，TTL 内不再重跑
    # 2026-10-08：同 _local_cover_response —— 网络抖动不写负缓存，下次立即重试。
    _cover_negative_put(guid, deterministic=not _cover_take_network_failed())
    _cover_log("PH", f"guid={guid}")
    return _placeholder_cover_response(guid)


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def official_track_template(official_list: list) -> dict | None:
    """取一条真实官方 track 作结构模板：App 端反序列化严格，在线条目字段集需与官方对齐。"""
    for item in official_list:
        if isinstance(item, dict):
            g = str(item.get("guid") or "")
            if g and not is_online_guid(g):
                return deepcopy(item)
    return None


def _snapshot_to_info(track: dict) -> dict:
    """把落盘的 track 对象（最终形状）还原成 build_online_track 的 info 输入形状。

    收藏/历史快照是最终 track 对象（artists 数组、duration 毫秒、audioSpec），
    重建时需映射回 artist/duration_s/ext；info 形状的历史快照字段已对齐则原样保留。
    """
    info = dict(track)
    artists = track.get("artists")
    if not info.get("artist") and isinstance(artists, list) and artists and isinstance(artists[0], dict):
        info["artist"] = artists[0].get("name") or ""
    album = track.get("album")
    if isinstance(album, dict):
        info["album"] = album.get("name") or ""
    if not info.get("duration_s") and track.get("duration"):
        try:
            info["duration_s"] = float(track["duration"]) / 1000.0
        except (TypeError, ValueError):
            pass
    spec = track.get("audioSpec")
    if isinstance(spec, dict):
        info.setdefault("ext", spec.get("format") or "mp3")
        info.setdefault("file_size", spec.get("size") or 0)
        if not info.get("cover_url"):
            info["cover_url"] = track.get("coverUrl") or track.get("cover_url") or ""
    return info


def build_favorite_track_obj(
    guid: str,
    info: dict | None = None,
    created_at: int | None = None,
    template: dict | None = None,
) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    # App 端解析严格：artists 永不为空、album.name 永不为空，避免整列表被丢弃。
    # 官方习惯：artist.coverId 为 null（无独立歌手封面），不能填歌曲 guid。
    artist_name = vo.get("artist") or "未知艺术家"
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": None,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ]

    album_name = (
        vo.get("albumName")
        or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "")
        or "未知专辑"
    )
    # 官方 album 对象没有 artists 键；releaseDate/barcode 缺省为 null 而非 0/""
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "coverId": guid,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}

    obj = {
        "guid": guid,
        "title": vo.get("title") or "",
        "duration": vo.get("duration") or 0,
        "isFavorite": True,
        "isCue": False,
        "genres": [],
        "artists": artists_list,
        "album": album_obj,
        "audioSpec": audio_spec,
        "accessStatus": 0,
        "coverId": guid,
        "year": None,
        "discNo": None,
        "trackNo": None,
        "isrc": None,
        "createdAt": ts,
        "updatedAt": ts,
    }

    if isinstance(template, dict) and template:
        # 以官方真实条目为结构底版，再用在线字段全覆盖：本侧未构造的官方字段
        # 保留官方结构，避免 App 因缺字段整列表解析失败。
        merged = deepcopy(template)
        merged.update(obj)
        return merged
    return obj


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


async def _online_favorite_set(request: Request) -> set[str]:
    """当前用户在线收藏 guid 集合（探测失败回退 shared/空集，绝不抛错）。"""
    try:
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, _resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return set()
        async with _FAV_LOCK:
            return {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
    except Exception:
        return set()


async def _best_effort_online_info(request: Request, guid: str) -> dict:
    """尽力获取在线曲目元数据：拉不到时从缓存反查兜底，绝不为 None（收藏/歌单快照共用）。"""
    info = await _online_info(request, guid)
    if info:
        return info
    cached_lyric = read_lyric_cache(guid)
    title = ""
    artist = ""
    cached_media = find_cache_file(guid)
    if cached_media:
        base = os.path.splitext(os.path.basename(cached_media))[0]
        if " - " in base:
            artist, title = base.split(" - ", 1)
        else:
            title = base
    return {
        "id": song_id_from_online_guid(guid),
        "source": source_from_online_guid(guid),
        "title": title,
        "artist": artist,
        "lyric": cached_lyric,
    }


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = resolve_real_guid(str(body.get("trackGUID") or body.get("guid") or "").strip())

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _best_effort_online_info(request, guid)
    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    saved = False
    autobind = bool(CONF.get("fav_auto_bind"))
    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新（重新收藏视为新操作，官方绑定开关开启时补 pending 标记）
                items[idx]["track"] = track_obj
                if autobind:
                    items[idx]["bind"] = "pending"
            else:
                item = {
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                }
                if autobind:
                    item["bind"] = "pending"
                items.append(item)
            save_online_favorites(user_guid, items)
            saved = True
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    if saved:
        _register_fav_autobind(request, guid, user_guid)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    raw_guid = ""
    if isinstance(body, dict):
        raw_guid = str(body.get("trackGUID") or body.get("guid") or "").strip()
        guid = resolve_real_guid(raw_guid)

    if not is_online_guid(guid):
        # 官方绑定已完成的歌曲本地映射已删，客户端旧会话可能仍持伪装 id 取消
        # 收藏：登记命中则翻译成官方 guid 撤销官方收藏；未命中照旧透传官方
        hit = _lookup_bind_any_user(raw_guid)
        if hit and hit[2].get("fav"):
            bound_user, bound_guid, entry = hit
            ok = await _official_upstream_write(
                "POST", "/music/api/v1/favorite-track/delete",
                copy_incoming_headers(request), {"trackGUID": entry["official"]},
            )
            if ok:
                _forget_bind(bound_user, bound_guid, fav=True)
            return JSONResponse(content={"code": 0, "msg": "", "data": None})
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    entry = lookup_bind_entry(user_guid, guid)
    if entry and entry.get("fav"):
        # 该曲已完成官方绑定（旧会话假 id）：同步撤销官方收藏
        if await _official_upstream_write(
            "POST", "/music/api/v1/favorite-track/delete",
            copy_incoming_headers(request), {"trackGUID": entry["official"]},
        ):
            _forget_bind(user_guid, guid, fav=True)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份。官方列表已取回成功（同一组请求头），此时探测被拒
    # （如 App 一次性票据已被首次请求消耗）不能回传鉴权错误导致整列表空白，
    # 降级为仅返回官方列表。
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        logger.warning("favorite list degraded to official-only: auth probe rejected after official list ok")
        return JSONResponse(content=disguise_envelope_list(upstream_json), status_code=upstream_resp.status_code, headers=resp_headers)

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 自愈：bind=pending 的收藏在此补发官方绑定/下载（含代理重启后的恢复）
    _maybe_retry_official_binds(request, user_guid, fav_items=fav_items)

    # 官方真实条目作为结构模板：App 端解析严格，在线条目字段集需与官方对齐；
    # 旧数据存的快照也统一重建，避免历史遗留形状继续下发。
    template = official_track_template(official_list)

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        g = str(it.get("guid") or "")
        if not g:
            continue
        snapshot = it.get("track") if isinstance(it.get("track"), dict) else None
        obj = build_favorite_track_obj(g, _snapshot_to_info(snapshot) if snapshot else None, created_at=it.get("createdAt"), template=template)
        obj["isFavorite"] = True
        online_tracks.append(disguise_client_json(obj))

    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)
    disguise_official_items(official_list)
    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}  # key: f"{user}:{kind}:{day}"
# 空结果构建冷却：key 同 _DAILY_TASKS，value=该次空结果的 builtAt（见 _ensure_daily_task）
_EMPTY_BUILD_COOLDOWN: dict[str, float] = {}
_EMPTY_BUILD_COOLDOWN_S = 300.0


def _prune_stale_daily_tasks(day: str) -> None:
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(f":{day}")]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()
    for k in [k for k in _EMPTY_BUILD_COOLDOWN if not str(k).endswith(f":{day}")]:
        _EMPTY_BUILD_COOLDOWN.pop(k, None)


def _recommend_kind_enabled(kind: str) -> bool:
    if kind == "hot":
        return bool(CONF.get("recommend_hot", True))
    return bool(CONF.get("recommend_daily", True))


def _recommend_kinds_enabled() -> list[str]:
    """按开关返回要注入的推荐歌单类型（顺序即歌单列表顺序）。"""
    return [k for k in ("daily", "hot") if _recommend_kind_enabled(k)]


def _is_shared_user_guid(user_guid: str) -> bool:
    """身份探测未解析到真实用户 guid（保守回落 shared）的会话。"""
    return str(user_guid or "").strip() == "shared"


def _recommend_injectable_kinds(user_guid: str) -> list[str]:
    """按用户身份返回可注入（可构建）的推荐歌单类型，注入/生成侧统一判定。

    issue #25 多用户隔离：_probe_upstream_auth 失败/解析异常/guid 缺失时所有用户
    都回落 user_guid="shared"。数据来源结论（recommend.py）：
      - daily 是个性化歌单——种子取个人 play_history（本地+在线）与个人收藏，
        网易日推也是已登录账号的个性化内容；shared 会话注入会让身份探测失败的
        所有用户共享同一份"每日推荐"，混合历史生成的歌单跨用户泄露，必须跳过。
      - hot 是公共榜单——构建链（网易热歌榜/lx 免登录榜单）不读任何个人历史与
        收藏、不做按用户排除，内容与用户身份无关（缓存仅按用户分目录存放，
        不含任何用户数据），shared 注入无隔离风险。
    生成侧同样收口：seeds 只按各自 user_guid 文件读取，shared 历史
    （play_history/shared.json）不会进入任何真实用户的 daily 种子；shared 的
    daily 经本判定在 _peek/_load 处直接短路，绝不触发构建。
    """
    kinds = _recommend_kinds_enabled()
    if _is_shared_user_guid(user_guid):
        return [k for k in kinds if k == "hot"]
    return kinds


async def _ensure_daily_task(request: Request, user_guid: str, kind: str = "daily") -> asyncio.Task:
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{kind}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and len(result.get("tracks") or []) >= dailyrec.PLAYLIST_SIZE:
                    return task
                if isinstance(result, dict) and not result.get("tracks"):
                    # 空结果冷却：音源全挂/LLM 不可用时构建产出 0 首，缓存为空会让
                    # 每次打开列表都重新触发一轮构建（反复打搜索与 LLM）。5 分钟内
                    # 不重建，冷却过后自动重试（缓存非空时本分支不会到达）。
                    built_at = result.get("builtAt")
                    last = float(built_at) if isinstance(built_at, (int, float)) else time.time()
                    if time.time() - last < _EMPTY_BUILD_COOLDOWN_S:
                        _EMPTY_BUILD_COOLDOWN[key] = last
                        return task
                    _EMPTY_BUILD_COOLDOWN[key] = last
        except (asyncio.CancelledError, Exception):
            pass
    if time.time() - _EMPTY_BUILD_COOLDOWN.get(key, 0.0) < _EMPTY_BUILD_COOLDOWN_S:
        # 冷却期内：不重复构建（上方已把可返回的 done task 返回；此处是异常/无结果的兜底）
        done = _DAILY_TASKS.get(key)
        if done is not None:
            return done
    _EMPTY_BUILD_COOLDOWN.pop(key, None)
    async with _FAV_LOCK:
        favs = load_online_favorites(user_guid)
    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicdl_client=get_musicdl_client(request.app) if CONF.get("musicdl_enabled") else None,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            llm_http=get_llm_client(request.app) if dailyrec.llm_enabled() else None,
            build_track=build_online_track,
            netease_enabled=CONF["netease_enabled"],
            favorite_items=favs,
            lx_client=get_lx_client(request.app) if CONF.get("lx_enabled") else None,
            lx_enabled=bool(CONF.get("lx_enabled")),
            lx_sources=CONF.get("lx_sources") or None,
            recommend_hot=bool(CONF.get("recommend_hot", True)),
            recommend_daily=bool(CONF.get("recommend_daily", True)),
            kind=kind,
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _wait_progressive_bundle(
    request: Request, user_guid: str, kind: str, timeout_s: float,
) -> dict:
    """issue #23 渐进上架：后台构建期间轮询磁盘 checkpoint，出现任意一首即返回。

    recommend 构建已改为逐首 checkpoint（2s 节流落盘），这里 0.5s 轮询一次：
    歌单/曲目列表首屏必有歌（构建仍在继续，用户重进列表可见增长），不再
    空等整链构建完成。task 完成则返回最终结果；超时仍无任何曲目回占位歌单。
    """
    day = dailyrec.today_key()
    task = await _ensure_daily_task(request, user_guid, kind)
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.warning("daily recommend build failed: %s", e)
            break
        cached = dailyrec.load_daily_cache(user_guid, day, kind)
        if cached and cached.get("tracks"):
            return cached
        if task.done():
            break
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return dailyrec.empty_daily_bundle(user_guid, kind)


async def _peek_daily_bundle(request: Request, user_guid: str, kind: str = "daily") -> dict:
    """歌单列表用：有缓存立刻返回；否则等渐进 checkpoint，最多 2s，超时仍返回占位歌单。"""
    if kind not in _recommend_injectable_kinds(user_guid):
        # 开关关闭或身份探测失败（shared）的个性化 daily：注入与生成一并跳过
        return dailyrec.empty_daily_bundle(user_guid, kind)
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return await _wait_progressive_bundle(request, user_guid, kind, timeout_s=2.0)


async def _load_daily_bundle(request: Request, user_guid: str, kind: str = "daily") -> dict:
    if kind not in _recommend_injectable_kinds(user_guid):
        # 同 _peek_daily_bundle：shared 会话不构建/不下发个性化 daily（issue #25）
        return dailyrec.empty_daily_bundle(user_guid, kind)
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day, kind)
    if cached and cached.get("tracks"):
        return cached
    return await _wait_progressive_bundle(request, user_guid, kind, timeout_s=20.0)


# === playlist tracks（官方歌单内的在线附加条目）===
# 官方 add-track/remove-track 对 DB 中不存在的 track guid 返回 100002，
# 在线曲目（客户端持有伪装 32-hex 假 id）直达官方必被拒。与收藏三接口同款
# 分工：在线条目记本地 JSON（按用户分文件，桶键=官方歌单 guid），官方条目
# 照旧透传官方；读取接口透传官方信封后合并下发。

_PLT_LOCK = asyncio.Lock()


def plt_dir() -> str:
    return CONF.get("plt_dir") or os.path.join(_HOME, "playlist_tracks")


def plt_dir_has_data() -> bool:
    """目录里存在任一用户文件才启用读接口拦截路径，否则纯透传（零开销零风险）。"""
    try:
        return any(name.endswith(".json") for name in os.listdir(plt_dir()))
    except OSError:
        return False


def user_plt_path(user_guid: str) -> str:
    return os.path.join(plt_dir(), f"{sanitize_user_guid(user_guid)}.json")


def load_playlist_tracks(user_guid: str) -> dict[str, list[dict]]:
    """返回 {歌单guid: [{"guid","addedAt","track"}, ...]}。"""
    path = user_plt_path(user_guid)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return {str(k): v for k, v in data["items"].items() if isinstance(v, list)}
    except Exception as e:
        logger.warning("Failed to load playlist tracks for %s from %s: %s", user_guid, path, e)
    return {}


def save_playlist_tracks(user_guid: str, items: dict[str, list[dict]]) -> bool:
    path = user_plt_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save playlist tracks for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def playlist_track_counts(user_guid: str) -> dict[str, int]:
    return {k: len(v) for k, v in load_playlist_tracks(user_guid).items()}


async def _parse_playlist_track_body(request: Request) -> tuple[str, list[str]]:
    """解析 add/remove-track 的 {"guid": 歌单, "trackGUIDs": [...]}（官方契约，批量）。"""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return "", []
    playlist_guid = str(body.get("guid") or "").strip()
    raw = body.get("trackGUIDs")
    track_guids = [str(g or "").strip() for g in raw if str(g or "").strip()] if isinstance(raw, list) else []
    return playlist_guid, track_guids


async def _forward_official_playlist_tracks(
    request: Request, client: httpx.AsyncClient, action: str,
    playlist_guid: str, official: list[str],
) -> Response | None:
    """混合批次中官方部分先行。返回 None=官方成功，否则返回官方错误响应（本地不动）。"""
    headers = copy_incoming_headers(request)
    req = client.build_request(
        "POST",
        f"/music/api/v1/playlist/{action}",
        headers=headers,
        json={"guid": playlist_guid, "trackGUIDs": official},
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not (isinstance(payload, dict) and payload.get("code") == 0):
        return JSONResponse(content=payload or {"code": -1, "msg": "upstream error", "data": None})
    return None


@app.post("/music/api/v1/playlist/add-track")
async def playlist_add_track(request: Request):
    """往歌单加曲目：在线条目记本地存储（幂等，重复加刷新 addedAt 对齐官方语义）。"""
    upstream_client = get_upstream_client(request.app)
    playlist_guid, track_guids = await _parse_playlist_track_body(request)
    if not playlist_guid:
        return await forward_to_upstream(request, upstream_client)

    playlist_guid = resolve_real_guid(playlist_guid)
    if pubpl.is_public_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    if dailyrec.is_recommend_playlist_guid(playlist_guid):
        # 推荐歌单是按天重建的虚拟歌单，保持只读：吸收请求，避免伪装 id 直达官方被拒
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    if nmpl.is_nm_playlist_guid(playlist_guid):
        # 网易账号歌单只读（不回写网易）：同样吸收请求
        return JSONResponse(content={"code": 0, "msg": "", "data": None})

    online = [g for g in (resolve_real_guid(g) for g in track_guids) if is_online_guid(g)]
    if not online:
        # 全官方（或空列表）：原样透传，官方语义兜底（含参数校验错误回传）
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in (resolve_real_guid(g) for g in track_guids) if not is_online_guid(g)]
    if official:
        err = await _forward_official_playlist_tracks(request, upstream_client, "add-track", playlist_guid, official)
        if err is not None:
            return err

    now = int(time.time())
    snapshots = {}
    for g in online:
        snapshots[g] = build_favorite_track_obj(g, await _best_effort_online_info(request, g), created_at=now)

    saved = False
    autobind = bool(CONF.get("fav_auto_bind"))
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.setdefault(playlist_guid, [])
            for g in online:
                idx = next((i for i, it in enumerate(bucket) if it.get("guid") == g), None)
                if idx is not None:
                    bucket[idx]["addedAt"] = now
                    bucket[idx]["track"] = snapshots[g]
                else:
                    entry = {"guid": g, "addedAt": now, "track": snapshots[g]}
                    if autobind:
                        entry["bind"] = "pending"
                    bucket.append(entry)
            save_playlist_tracks(user_guid, items)
            saved = True
        except Exception as e:
            logger.warning("Error updating playlist tracks for user %s: %s", user_guid, e)

    if saved:
        for g in online:
            _register_fav_autobind(request, g, user_guid, playlist_guid=playlist_guid)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/playlist/remove-track")
async def playlist_remove_track(request: Request):
    """从歌单移除曲目：在线条目删本地存储（幂等），官方条目照旧透传。"""
    upstream_client = get_upstream_client(request.app)
    playlist_guid, track_guids = await _parse_playlist_track_body(request)
    if not playlist_guid:
        return await forward_to_upstream(request, upstream_client)

    playlist_guid = resolve_real_guid(playlist_guid)
    if pubpl.is_public_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    if dailyrec.is_recommend_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    if nmpl.is_nm_playlist_guid(playlist_guid):
        return JSONResponse(content={"code": 0, "msg": "", "data": None})

    online = [g for g in (resolve_real_guid(g) for g in track_guids) if is_online_guid(g)]
    if not online:
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in (resolve_real_guid(g) for g in track_guids) if not is_online_guid(g)]
    # 旧会话假 id 中已完成官方绑定的：翻译成官方 guid 一并从官方歌单移除
    bound_pairs: "list[tuple[str, str]]" = []
    for g in online:
        entry = lookup_bind_entry(user_guid, g)
        if entry and playlist_guid in (entry.get("playlists") or []):
            bound_pairs.append((g, str(entry.get("official") or "")))
    official += [og for _, og in bound_pairs if og]
    if official:
        err = await _forward_official_playlist_tracks(request, upstream_client, "remove-track", playlist_guid, official)
        if err is not None:
            return err
        for g, _og in bound_pairs:
            _forget_bind(user_guid, g, playlist_guid=playlist_guid)

    online_set = set(online)
    async with _PLT_LOCK:
        try:
            items = load_playlist_tracks(user_guid)
            bucket = items.get(playlist_guid)
            if bucket:
                kept = [it for it in bucket if it.get("guid") not in online_set]
                if kept:
                    items[playlist_guid] = kept
                else:
                    items.pop(playlist_guid, None)
                save_playlist_tracks(user_guid, items)
        except Exception as e:
            logger.warning("Error removing playlist tracks for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/playlist/delete")
async def playlist_delete(request: Request):
    """删除歌单：透传官方；成功后清掉本地为该歌单存的在线附加条目（防孤儿数据）。"""
    upstream_client = get_upstream_client(request.app)
    try:
        _body = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        _body = {}
    _pl_guid = str(_body.get("guid") or "").strip() if isinstance(_body, dict) else ""
    if pubpl.is_public_playlist_guid(resolve_real_guid(_pl_guid)):
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    if nmpl.is_nm_playlist_guid(resolve_real_guid(_pl_guid)):
        # 网易账号歌单只读：吸收删除（不透传官方必被拒的假 id），下次刷新卡片仍在
        return JSONResponse(content={"code": 0, "msg": "ok", "data": None})
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})

    if envelope.get("code") == 0:
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:
            body = {}
        playlist_guid = str(body.get("guid") or "").strip() if isinstance(body, dict) else ""
        resolved = resolve_real_guid(playlist_guid)
        if playlist_guid and not dailyrec.is_recommend_playlist_guid(resolved) and plt_dir_has_data():
            is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
            if is_authed:
                async with _PLT_LOCK:
                    try:
                        items = load_playlist_tracks(user_guid)
                        if items.pop(resolved, None) is not None or items.pop(playlist_guid, None) is not None:
                            save_playlist_tracks(user_guid, items)
                    except Exception as e:
                        logger.warning("Error purging playlist tracks for user %s: %s", user_guid, e)
                # 官方已写入该歌单的绑定登记同步清理，防孤儿转发
                _forget_bind_playlist(user_guid, resolved)

    return JSONResponse(content=envelope, headers=headers)


def _playlist_public_fields(record: dict, tracks: list | None = None) -> dict:
    # 封面取曲在下发时重算（兼容当天旧缓存）。coverId 统一用**纯 32-hex** 伪装形态：
    # - 每日推荐（本地曲目）历来就是纯 32-hex，手机/电脑端歌单卡片渲染正常（实证）；
    # - 在线曲目原下发 track_+32hex，但该形态 URL 长期不变，早前的黑胶默认图/占位图
    #   被浏览器强缓存（真图 max-age=86400），换真图后客户端仍用旧图且不重新请求；
    #   改纯 32-hex 后 URL 变化，旧缓存自然失效。fake→real 反查已登记，服务端可还原。
    cover_real = ""
    picked = dailyrec.pick_playlist_cover_track(tracks)
    if picked:
        cover_real = str(picked.get("coverId") or picked.get("guid") or "")
    if not cover_real:
        cover_real = str(record.get("coverId") or record.get("guid") or "")
    cover_id = _track_cover_id(cover_real)
    return {
        "guid": record.get("guid"),
        "name": record.get("name") or "每日推荐",
        "coverId": cover_id,
        # 同步下发 coverUrl（同源封面端点，与 coverId 同形态纯 32-hex）
        "coverUrl": _cover_endpoint_url(cover_id) if cover_id else "",
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=disguise_envelope_list(envelope), headers=headers)

    kinds = _recommend_injectable_kinds(user_guid)
    # 网易账号歌单：实例级内容（同热门推荐），shared 会话同样可见
    nm_on = bool(CONF.get("netease_my_playlists")) and bool(CONF.get("netease_enabled"))
    # 公共平台歌单（#55）：管理页预选，只读注入音乐页歌单列表
    public_on = bool(CONF.get("public_playlists_enabled"))
    if not kinds and not nm_on and not public_on:
        # 推荐/账号歌单/公共歌单全关（或 shared 会话无任何可注入类型）：不注入
        return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official
    recs: list[dict] = []
    for kind in kinds:
        try:
            bundle = await _peek_daily_bundle(request, user_guid, kind)
        except Exception as e:
            logger.warning("%s recommend list inject failed: %s", kind, e)
            continue
        tracks = bundle.get("tracks") or []
        if kind == "hot" and not tracks:
            # 热门歌单构建失败/无可用榜单时不挂空壳（每日推荐保留占位等待后台生成）
            continue
        rec = _playlist_public_fields(bundle.get("playlist") or {}, tracks)
        rec["trackCount"] = len(tracks)
        recs.append(rec)
    public_cards: list[dict] = []
    if public_on:
        try:
            public_cards = await pubpl.peek_cards(request.app.state, CONF, build_online_track)
            for card in public_cards:
                g = str(card.get("guid") or "")
                if is_online_guid(g):
                    fake_official_guid(g)
        except Exception as e:
            logger.warning("public-playlists inject failed: %s", e)
            public_cards = []
    nm_cards: list[dict] = []
    if nm_on:
        try:
            nm_cards = await nmpl.peek_summaries(get_musicbox_client(request.app))
            for card in nm_cards:
                g = str(card.get("guid") or "")
                if is_online_guid(g):
                    # 登记 guid/封面假 id 反查（coverId 与 guid 同源 md5）
                    fake_official_guid(g)
        except Exception as e:
            logger.warning("netease my-playlists inject failed: %s", e)
            nm_cards = []
        if nm_cards:
            nmpl.schedule_prefetch(get_musicbox_client(request.app), build_online_track, nm_cards)
    if nm_cards:
        # 同一网易歌单被两侧同时选中时，账号歌单（可看登录态）优先，公共侧去重
        nm_ids = {str(nmpl.nm_playlist_id_from_guid(card.get("guid"))) for card in nm_cards}
        nm_ids.discard("0")
        public_cards = [
            card for card in public_cards
            if (pubpl.entry_from_guid(card.get("guid")) or {}).get("provider") != "musicbox"
            or str((pubpl.entry_from_guid(card.get("guid")) or {}).get("id")) not in nm_ids
        ]
    official = [
        it for it in official
        if not (isinstance(it, dict) and dailyrec.is_recommend_playlist_guid(str(it.get("guid") or "")))
    ]
    data["list"] = recs + public_cards + nm_cards + disguise_official_items(official)
    total = data.get("total")
    data["total"] = (total if isinstance(total, int) else len(official)) + len(recs) + len(public_cards) + len(nm_cards)
    try:
        _cover_log(
            "PL_LIST_OUT",
            "ua=%s recs=%s"
            % (
                request.headers.get("user-agent", "")[:70],
                json.dumps(
                    [{k: r.get(k) for k in ("guid", "name", "coverId", "coverUrl")} for r in recs],
                    ensure_ascii=False,
                )[:900],
            ),
        )
    except Exception:
        pass
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    public_entry = pubpl.entry_from_guid(guid) or pubpl.entry_from_guid(resolve_real_guid(guid))
    if public_entry:
        is_authed, _user_guid, auth_resp = await _probe_upstream_auth(request, get_upstream_client(request.app))
        if not is_authed:
            return auth_resp or JSONResponse(content={"code": -1, "msg": "unauthorized", "data": None}, status_code=401)
        try:
            card = await pubpl.load_summary(request.app.state, public_entry)
        except Exception:
            card = pubpl.cached_card(pubpl.playlist_guid(public_entry))
        if card is None:
            return JSONResponse(content={"code": -1, "msg": "playlist not found", "data": None})
        tracks = pubpl.cached_tracks(pubpl.playlist_guid(public_entry))
        if tracks is not None:
            card = {**card, "trackCount": len(tracks)}
        fake_official_guid(str(card.get("guid") or ""))
        return JSONResponse(content={"code": 0, "msg": "ok", "data": card})
    nm_pl_id = nmpl.nm_playlist_id_from_guid(guid) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(guid))
    if nm_pl_id:
        # 网易账号歌单：只读虚拟歌单，卡片从摘要（内存→磁盘）取；trackCount 用
        # 已缓存曲目数更准（可播过滤后可能少于网易侧计数）
        card = nmpl.card_for(nm_pl_id)
        if card is None:
            return JSONResponse(content={"code": -1, "msg": "playlist not found", "data": None})
        tracks = nmpl.cached_tracks(nm_pl_id)
        if tracks is not None:
            card = {**card, "trackCount": len(tracks)}
        return JSONResponse(content={"code": 0, "msg": "ok", "data": card})
    kind = dailyrec.online_playlist_kind(guid)
    if not kind:
        # 官方歌单：本地存在在线附加条目才拦截修正 trackCount，否则纯透传
        if not plt_dir_has_data():
            return await forward_to_upstream(request, get_upstream_client(request.app))
        upstream_client = get_upstream_client(request.app)
        envelope = await fetch_upstream_envelope(request, upstream_client)
        if isinstance(envelope, Response):
            return envelope
        headers = envelope.pop("_ext_headers", {})
        if envelope.get("code") != 0:
            return JSONResponse(content=envelope, headers=headers)
        # 官方明细已成功，此时探测被拒不能回传鉴权错误，降级为官方原样（同收藏列表）
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)
        extras = load_playlist_tracks(user_guid).get(resolve_real_guid(guid)) or []
        if extras:
            data = envelope.get("data")
            if isinstance(data, dict):
                tc = data.get("trackCount")
                data["trackCount"] = (tc if isinstance(tc, int) else 0) + len(extras)
        return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid, kind)
    rec = _playlist_public_fields(bundle.get("playlist") or {}, bundle.get("tracks") or [])
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]
    recommend_ids = [g for g in guids if dailyrec.is_recommend_playlist_guid(g)]
    public_ids = [resolve_real_guid(g) for g in guids if pubpl.is_public_playlist_guid(resolve_real_guid(g))]
    nm_ids = [
        pid for pid in (
            nmpl.nm_playlist_id_from_guid(g) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(g))
            for g in guids
        ) if pid
    ]
    if not recommend_ids and not public_ids and not nm_ids and not plt_dir_has_data():
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [
        g for g in guids
        if not dailyrec.is_recommend_playlist_guid(g)
        and not pubpl.is_public_playlist_guid(resolve_real_guid(g))
        and not nmpl.is_nm_playlist_guid(resolve_real_guid(g))
    ]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    # 官方歌单 trackCount 并上本地在线附加条目
    if official_list:
        counts = playlist_track_counts(user_guid)
        for it in official_list:
            if isinstance(it, dict):
                extra = counts.get(str(it.get("guid") or ""))
                if extra:
                    tc = it.get("trackCount")
                    it["trackCount"] = (tc if isinstance(tc, int) else 0) + extra
    recs: list[dict] = []
    for g in recommend_ids:
        kind = dailyrec.online_playlist_kind(g) or "daily"
        bundle = await _load_daily_bundle(request, user_guid, kind)
        rec = _playlist_public_fields(bundle.get("playlist") or {}, bundle.get("tracks") or [])
        rec["trackCount"] = len(bundle.get("tracks") or [])
        recs.append(rec)
    for pid in nm_ids:
        card = nmpl.card_for(pid)
        if card is not None:
            tracks = nmpl.cached_tracks(pid)
            if tracks is not None:
                card = {**card, "trackCount": len(tracks)}
            recs.append(card)
    for g in public_ids:
        entry = pubpl.entry_from_guid(g)
        if entry is None:
            continue
        try:
            card = await pubpl.load_summary(request.app.state, entry)
        except Exception:
            card = pubpl.cached_card(g)
        if card is not None:
            tracks = pubpl.cached_tracks(g)
            if tracks is not None:
                card = {**card, "trackCount": len(tracks)}
            recs.append(card)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": recs + official_list}})


# ==== 洛雪公共歌单「可播预筛」（2026-10-18）====
# 背景：洛雪榜单里混着大量音源脚本解析不出直链的曲目（实测酷我的 VIP 曲、咪咕的
# 部分曲目 → music_url 抛错或返回空）。这些条目一旦摆进歌单，用户点开全是不能播的。
# 已有 `_mark_unplayable`（点播失败即剔除），但要用户先点一次才知道。
# 这里在曲目载入后**后台**探一次（不阻塞首屏），结果按歌单缓存 10 分钟：
#   - 有结果 → 组装列表时只给可播条目，total 也随之收敛；
#   - 没结果（首开、进行中）→ 原样返回，用户无感等待一轮。
_LX_SCREEN_CACHE: dict[str, tuple[set, float]] = {}
_LX_SCREEN_TTL_S = 600.0
_LX_SCREEN_MAX = 200
_LX_SCREEN_CONCURRENCY = 10
_LX_SCREEN_RUNNING: set = set()
_LX_SCREEN_PENDING: set = set()


def _lx_screen_filter(playlist_guid: str, tracks: list) -> "tuple[list, int | None]":
    """按最近一次预筛结果过滤曲目；没有结果则原样返回。"""
    hit = _LX_SCREEN_CACHE.get(playlist_guid)
    if not hit or time.monotonic() >= hit[1]:
        return tracks, None
    keep = hit[0]
    return [t for t in tracks if str(t.get("guid") or "") in keep], len(keep)


async def _lx_screen_run(request: Request, playlist_guid: str, tracks: list) -> None:
    """后台预筛：并发问音源脚本「这首批曲子能不能拿到直链」，只记结果。"""
    if playlist_guid in _LX_SCREEN_RUNNING:
        return
    candidates = [t for t in tracks[:_LX_SCREEN_MAX]
                  if is_online_guid(str(t.get("guid") or "")) and source_from_online_guid(t["guid"]) == "lx"]
    if not candidates:
        return
    _LX_SCREEN_RUNNING.add(playlist_guid)
    sem = asyncio.Semaphore(_LX_SCREEN_CONCURRENCY)
    client = get_lx_client(request.app)

    async def one(track: dict) -> bool:
        async with sem:
            try:
                resolved = await resolve_lx_url(client, song_id_from_online_guid(track["guid"]))
            except Exception:
                return False
        return bool(resolved and resolved.get("url"))

    try:
        results = await asyncio.gather(*[one(t) for t in candidates], return_exceptions=True)
        playable = set()
        for track, ok in zip(candidates, results):
            if ok is True:
                playable.add(str(track.get("guid") or ""))
            elif ok is False:
                _mark_unplayable(str(track.get("guid") or ""), "preflight unplayable")
        if playable:
            _LX_SCREEN_CACHE[playlist_guid] = (playable, time.monotonic() + _LX_SCREEN_TTL_S)
        logger.info("lx preflight %s: %d/%d playable", playlist_guid, len(playable), len(candidates))
    finally:
        _LX_SCREEN_RUNNING.discard(playlist_guid)


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()
    kind = dailyrec.online_playlist_kind(guid)
    public_entry = pubpl.entry_from_guid(guid) or pubpl.entry_from_guid(resolve_real_guid(guid))
    if public_entry:
        upstream_client = get_upstream_client(request.app)
        is_authed, _user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        try:
            tracks = dailyrec.stamp_playlist_tracks(
                await pubpl.load_tracks(request.app.state, public_entry, build_online_track)
            )
        except Exception as e:
            logger.warning("public playlist track list failed (%s): %s", guid, e)
            return JSONResponse(content={"code": -1, "msg": "public playlist load failed", "data": None})
        if public_entry.get("provider") != "musicbox":
            entry_guid = pubpl.playlist_guid(public_entry)
            tracks, _known_total = _lx_screen_filter(entry_guid, tracks)
            # 本歌单还没有有效预筛结果（首开或已过期）：原样返回，同时后台跑一轮，
            # 下一次打开即为可播子集。首屏不做阻塞等待。
            hit = _LX_SCREEN_CACHE.get(entry_guid)
            if (not hit or hit[1] <= time.monotonic()) and entry_guid not in _LX_SCREEN_RUNNING:
                task = asyncio.create_task(_lx_screen_run(request, entry_guid, tracks))
                task.add_done_callback(lambda t: t.exception())
                _LX_SCREEN_PENDING.add(task)
            # 已被点播/预筛判定「播不了」的条目不再摆给用户（TTL 到期自动放回）。
            # 覆盖预筛全灭的平台（如咪咕：歌词与直链上游都取不到）——那种情况
            # 没有可缓存的可播子集，靠这张黑名单把整份歌单收敛掉。
            _prune_unplayable()
            if _PREFETCH_BAD:
                tracks = [t for t in tracks
                          if str(t.get("guid") or "") not in _PREFETCH_BAD]
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            size = max(len(tracks), 1)
        if size < 1:
            size = 50
        start = (page - 1) * size
        return JSONResponse(
            content=disguise_client_json({
                "code": 0, "msg": "ok",
                "data": {"list": tracks[start:start + size], "total": len(tracks), "sort": request.query_params.get("sort") or ""},
            })
        )
    nm_pl_id = nmpl.nm_playlist_id_from_guid(guid) or nmpl.nm_playlist_id_from_guid(resolve_real_guid(guid))
    if nm_pl_id:
        # 网易账号歌单曲目：可播过滤后的 VO 分页下发（与推荐歌单同款分页语义）
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        tracks = dailyrec.stamp_playlist_tracks(
            await nmpl.load_tracks(get_musicbox_client(request.app), nm_pl_id, build_online_track)
        )
        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            size = max(len(tracks), 1)
        if size < 1:
            size = 50
        start = (page - 1) * size
        page_tracks = tracks[start:start + size]
        return JSONResponse(
            content=disguise_client_json({
                "code": 0,
                "msg": "ok",
                "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
            })
        )
    if not kind:
        # 官方歌单：本地存在在线附加条目才拦截合并，否则纯透传
        if not plt_dir_has_data():
            return await forward_to_upstream(request, get_upstream_client(request.app))
        upstream_client = get_upstream_client(request.app)
        envelope = await fetch_upstream_envelope(request, upstream_client)
        if isinstance(envelope, Response):
            return envelope
        headers = envelope.pop("_ext_headers", {})
        if envelope.get("code") != 0:
            return JSONResponse(content=envelope, headers=headers)
        # 官方列表已成功，此时探测被拒不能回传鉴权错误，降级为官方原样（同收藏列表）
        is_authed, user_guid, _ = await _probe_upstream_auth(request, upstream_client)
        if not is_authed:
            return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)
        resolved_pl_guid = resolve_real_guid(guid)
        extras = load_playlist_tracks(user_guid).get(resolved_pl_guid) or []
        if not extras:
            return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)
        # 自愈：bind=pending 的歌单条目在此补发官方绑定/下载
        _maybe_retry_official_binds(request, user_guid, plt_map={resolved_pl_guid: extras})

        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {"list": [], "total": 0}
            envelope["data"] = data
        official_list = data.get("list") if isinstance(data.get("list"), list) else []
        official_total = data.get("total") if isinstance(data.get("total"), int) else len(official_list)

        # 在线附加条目接在官方条目之后（addedAt 倒序），逻辑区间
        # [official_total, official_total+len)；按请求页窗口切片，翻页不重不漏
        extras_sorted = sorted(extras, key=lambda x: x.get("addedAt", 0), reverse=True)
        fav_guids = {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
        template = official_track_template(official_list)
        online_objs = []
        for it in extras_sorted:
            g = str(it.get("guid") or "")
            if not g:
                continue
            snapshot = it.get("track") if isinstance(it.get("track"), dict) else None
            obj = build_favorite_track_obj(
                g, _snapshot_to_info(snapshot) if snapshot else None,
                created_at=it.get("addedAt"), template=template,
            )
            obj["isFavorite"] = g in fav_guids
            online_objs.append(obj)

        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        try:
            size = int(request.query_params.get("size") or 50)
        except (TypeError, ValueError):
            size = 50
        if size == -1:
            online_page = online_objs
        else:
            if size < 1:
                size = 50
            start = (page - 1) * size
            lo = max(0, start - official_total)
            hi = max(0, start + size - official_total)
            online_page = online_objs[lo:hi]

        data["list"] = official_list + online_page
        data["total"] = official_total + len(online_objs)
        return JSONResponse(content=disguise_client_json(envelope), headers=headers)


    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid, kind)
    tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    # 电脑端/web 读 coverUrl 取图：推荐缓存 VO 的 coverUrl 多为空，这里按封面 guid 兜底为
    # 服务端封面端点（在线 online:xxx 走在线解析链，本地 32hex 走本地解析），与手机端 coverId 一致。
    for _t in tracks:
        if not isinstance(_t, dict):
            continue
        _cid = str(_t.get("coverId") or _t.get("guid") or "")
        if not _cid:
            continue
        # 列表行封面：coverId 必须是纯 32-hex（客户端只认此形态），否则不会请求封面
        _fake = _track_cover_id(_cid)
        _t["coverId"] = _fake
        _alb = _t.get("album")
        if isinstance(_alb, dict):
            _alb["coverId"] = _fake
        _ep = _cover_endpoint_url(_fake)
        if not _t.get("coverUrl"):
            _t["coverUrl"] = _ep
        if not _t.get("coverURL"):
            _t["coverURL"] = _ep
    try:
        asyncio.create_task(_prefetch_playlist_covers(tracks))
    except Exception:
        pass
    try:
        _cover_log(
            "PL_TRACKS_OUT",
            "kind=%s n=%d sample=%s"
            % (
                kind,
                len(tracks),
                json.dumps(
                    [
                        {k: t.get(k) for k in ("guid", "title", "coverId", "coverUrl", "coverURL")}
                        for t in tracks[:3]
                    ],
                    ensure_ascii=False,
                )[:900],
            ),
        )
    except Exception:
        pass
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size == -1:
        # -1 是"返回全部"的约定值，须在 <1 兜底之前归一，否则永远到不了全量分支
        size = max(len(tracks), 1)
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size]
    return JSONResponse(
        content=disguise_client_json({
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        })
    )


# === album detail（在线曲目伪装专辑详情，issue #22）===
# 曲目 VO 的 album.guid 形如 "online:<src>:<id>:album"，disguise 后客户端拿到
# 官方 32-hex 形态；点击专辑时客户端请求官方专辑详情端点，原样转发官方必返回
# "无此专辑"。此处拦截分源适配：netease 经 musicbox 调真实专辑接口；musicdl/lx
# 从保留的搜索缓存/收藏/历史聚合同专辑名曲目合成；无法合成返回业务错误，
# 绝不透传官方"无此专辑"。官方专辑（guid 非 fake）原样转发不受影响。
#
# 取证说明：仓库内（proxy/static、webui-service/static、docs、tests）未找到客户端
# 专辑跳调端点的直接证据；官方 API 命名风格为 /music/api/v1/<资源>/detail?guid=
# （playlist/detail 同款），故按 /music/api/v1/album 前缀 + 携带 guid 参数保守
# 拦截——反解不到已登记伪装专辑的请求一律原样转发官方，行为与官方直连一致。


def _artist_names_joined(row: dict) -> str:
    """原生 netease 行的 ar/artists 列表 → "A / B" 拼接。"""
    ar = row.get("ar") or row.get("artists") or []
    if not isinstance(ar, list):
        return ""
    return " / ".join(str(a.get("name") or "") for a in ar if isinstance(a, dict) and a.get("name"))


def _album_detail_payload(album_guid: str, album_name: str, artist: str, tracks: list[dict]) -> dict:
    """官方专辑详情 VO：字段结构对齐 track VO 的 artists/album 对象风格。"""
    ts = int(time.time())
    artist_name = str(artist or "").strip() or "未知艺术家"
    artists_list = [{
        "guid": f"{album_guid}:artist",
        "name": artist_name,
        "coverId": None,
        "createdAt": ts,
        "updatedAt": ts,
    }]
    # 封面取第一首曲目 guid（/static/cover 按曲目封面解析），无曲目回落专辑自身
    cover_id = str((tracks[0] or {}).get("guid") or album_guid) if tracks else album_guid
    album_obj = {
        "guid": album_guid,
        "name": str(album_name or "").strip() or "未知专辑",
        "artists": artists_list,
        "coverId": cover_id,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": len(tracks),
        "tracks": dailyrec.stamp_playlist_tracks(tracks),
    }
    return {"code": 0, "msg": "ok", "data": album_obj}


async def _netease_album_detail_payload(request: Request, entry: dict) -> dict | None:
    """netease 专辑：曲目详情解析真实专辑 ID，经 musicbox /api/v1/album/{id} 拉曲目列表。

    entry 带 album_id（/search/album 在线专辑登记）时跳过 song/info 二跳直达。
    """
    if not CONF.get("netease_enabled"):
        return None
    musicbox_client = get_musicbox_client(request.app)
    track_guid = str(entry.get("track_guid") or "")
    album_id = str(entry.get("album_id") or "")
    album_name = str(entry.get("album") or "")
    cover_url = ""
    if not album_id:
        song_id = song_id_from_online_guid(track_guid).rsplit(":", 1)[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and body.get("ok") is not False:
                    data = body.get("data")
                    if isinstance(data, dict):
                        al = data.get("al") if isinstance(data.get("al"), dict) else {}
                        album_id = str(al.get("id") or "")
                        album_name = album_name or str(al.get("name") or "")
                        cover_url = str(al.get("picUrl") or "")
        except Exception as e:
            logger.warning("musicbox song info for album detail failed (%s): %s", track_guid, e)
    if not album_id:
        return None

    rows: list = []
    try:
        r = await musicbox_client.get(f"/api/v1/album/{album_id}", timeout=25.0)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict):
                rows = body.get("data") if isinstance(body.get("data"), list) else []
            elif isinstance(body, list):
                rows = body
    except Exception as e:
        logger.warning("musicbox album detail failed for %s: %s", album_id, e)

    # 行归一化：兼容 dig_info 形状（song_name/artist/duration 秒）与原生形状（name/ar/dt 毫秒）
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("song_id") or row.get("id") or "")
        if not sid:
            continue
        title = str(row.get("song_name") or row.get("name") or "")
        artist = str(row.get("artist") or "") or _artist_names_joined(row)
        try:
            dur_f = float(row.get("duration") or row.get("dt") or 0)
        except (TypeError, ValueError):
            dur_f = 0.0
        items.append({
            "id": f"netease:{sid}",
            "source": "netease",
            "title": title,
            "artist": artist,
            "album": album_name,
            "duration_s": dur_f / 1000.0 if dur_f > 10000 else dur_f,
            "ext": "flac" if (row.get("has_sq") or row.get("sq") or row.get("has_hr")) else "mp3",
            "cover_url": cover_url,
        })
    if not items:
        return None
    tracks = [build_online_track(it) for it in items]
    return _album_detail_payload(f"{track_guid}:album", album_name, str(tracks[0].get("artist") or ""), tracks)


def _user_album_snapshots(user_guid: str) -> list[dict]:
    """当前用户的收藏/播放历史/歌单附加曲目快照（仅本用户，绝不扫其他用户数据）。"""
    out: list[dict] = []
    for loader in (load_online_favorites, dailyrec.load_online_play_history):
        try:
            for it in loader(user_guid) or []:
                snapshot = it.get("track") if isinstance(it, dict) and isinstance(it.get("track"), dict) else None
                if snapshot:
                    out.append(snapshot)
        except Exception:
            continue
    try:
        for bucket in load_playlist_tracks(user_guid).values():
            for it in bucket:
                snapshot = it.get("track") if isinstance(it, dict) and isinstance(it.get("track"), dict) else None
                if snapshot:
                    out.append(snapshot)
    except Exception:
        pass
    return out


def _normalize_album_candidate(it: dict) -> dict:
    """聚合候选归一为 info 形状：VO 快照还原，搜索条目原样。"""
    if isinstance(it.get("artists"), list) or isinstance(it.get("audioSpec"), dict):
        return _snapshot_to_info(it)
    return dict(it)


async def _aggregate_album_detail_payload(request: Request, entry: dict) -> dict | None:
    """musicdl/lx 专辑：无真实专辑接口，从内存搜索缓存 + 当前用户收藏/历史聚合同名曲目合成。"""
    album_name = str(entry.get("album") or "").strip()
    src = str(entry.get("source") or "")
    track_guid = str(entry.get("track_guid") or "")
    if not album_name:
        # 无专辑名无法按专辑聚合（搜索结果常缺专辑字段），走业务错误
        return None

    anchor = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    candidates: list[dict] = [anchor] if anchor else []
    # ① 内存搜索缓存：音源目录数据，与用户身份无关
    _clean_search_cache()
    for cache_entry in _SEARCH_CACHE.values():
        for it in cache_entry.get("items") or []:
            if isinstance(it, dict):
                candidates.append(it)
    # ② 当前用户的收藏/历史/歌单附加快照（探测失败则跳过磁盘源，只用缓存）
    upstream_client = get_upstream_client(request.app)
    try:
        is_authed, user_guid, _resp = await _probe_upstream_auth(request, upstream_client)
    except Exception:
        is_authed, user_guid = False, ""
    if is_authed and user_guid:
        candidates.extend(_user_album_snapshots(user_guid))

    infos: list[dict] = []
    seen_guids: set[str] = set()
    for it in candidates:
        if not isinstance(it, dict) or not it:
            continue
        try:
            g = online_guid_from_item(it)
        except Exception:
            continue
        if not g or not is_online_guid(g) or g in seen_guids:
            continue
        if source_from_online_guid(g) != src:
            continue
        info = _normalize_album_candidate(it)
        if str(info.get("album") or "").strip() != album_name:
            continue
        seen_guids.add(g)
        infos.append(info)
    if not infos:
        return None
    tracks = [build_online_track(info) for info in infos]
    return _album_detail_payload(f"{track_guid}:album", album_name, str(tracks[0].get("artist") or ""), tracks)


# 专辑详情合成缓存（fake guid → (过期时间, payload)）：客户端进专辑页会先调
# /album/detail 再调 /track/album-detail/list，避免同专辑合成两次；TTL 短，仅省重复请求
_ALBUM_PAYLOAD_CACHE: dict[str, tuple[float, dict]] = {}
_ALBUM_PAYLOAD_TTL_S = 120.0


async def _synthesize_album_payload(request: Request, entry: dict, raw_guid: str) -> "dict | None":
    """按源合成专辑详情 payload（带 120s TTL 缓存）；合成失败返回 None。"""
    now = time.monotonic()
    cached = _ALBUM_PAYLOAD_CACHE.get(raw_guid)
    if cached and cached[0] > now:
        return cached[1]
    src = str(entry.get("source") or "")
    payload = None
    try:
        if src == "netease":
            payload = await _netease_album_detail_payload(request, entry)
        else:
            payload = await _aggregate_album_detail_payload(request, entry)
    except Exception as e:
        logger.warning("album detail adapt failed for %s: %s", raw_guid, e)
    if payload is not None:
        if len(_ALBUM_PAYLOAD_CACHE) > 256:
            _ALBUM_PAYLOAD_CACHE.clear()
        _ALBUM_PAYLOAD_CACHE[raw_guid] = (now + _ALBUM_PAYLOAD_TTL_S, payload)
    return payload


def _album_list_envelope(request: Request, tracks: list[dict]) -> dict:
    """官方专辑曲目列表信封：与 /track/playlist-detail/list 同款 {list,total,sort}。"""
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size == -1:
        size = max(len(tracks), 1)
    if size < 1:
        size = 50
    start = (page - 1) * size
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "list": tracks[start:start + size],
            "total": len(tracks),
            "sort": request.query_params.get("sort") or "",
        },
    }


@app.api_route("/music/api/v1/album", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/album/{subpath:path}", methods=["GET", "HEAD"])
async def album_detail(request: Request, subpath: str = ""):
    raw_guid = str(
        request.query_params.get("guid")
        or request.query_params.get("albumGUID")
        or request.query_params.get("albumGuid")
        or request.query_params.get("id")
        or request.query_params.get("albumId")
        or ""
    ).strip()
    entry = resolve_fake_album(raw_guid)
    if entry is None:
        # 官方专辑或未知 guid：原样转发官方，官方专辑请求不受影响
        return await forward_to_upstream(request, get_upstream_client(request.app))

    payload = await _synthesize_album_payload(request, entry, raw_guid)
    if payload is not None:
        return JSONResponse(content=disguise_client_json(payload))
    logger.info("album detail not synthesizable: source=%s album=%r guid=%s", entry.get("source"), entry.get("album"), raw_guid)
    return JSONResponse(content={"code": -1, "msg": "该音源暂不支持专辑详情", "data": None})


@app.api_route("/music/api/v1/track/album-detail/list", methods=["GET", "HEAD"])
async def track_album_detail_list(request: Request):
    """专辑页曲目列表（官方 2.5 版客户端在 /album/detail 之后调用）。

    官方专辑原样透传；在线伪装专辑复用专辑详情合成结果分页下发，
    合成失败返回业务错误空列表，绝不透传官方"无此专辑"。
    """
    raw_guid = str(
        request.query_params.get("albumGUID")
        or request.query_params.get("albumGuid")
        or request.query_params.get("guid")
        or request.query_params.get("id")
        or ""
    ).strip()
    entry = resolve_fake_album(raw_guid)
    if entry is None:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    payload = await _synthesize_album_payload(request, entry, raw_guid)
    if payload is None:
        return JSONResponse(content={"code": -1, "msg": "该音源暂不支持专辑详情", "data": None})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tracks = list(data.get("tracks") or [])
    return JSONResponse(content=disguise_client_json(_album_list_envelope(request, tracks)))


# === /search/album 在线专辑合并（issue #22，官方 2.5 版客户端专辑搜索 Tab）===
# 官方端点搜的是本地曲库专辑；这里透传官方结果后合并在线专辑：
#   - netease：musicbox type=album 真实专辑搜索，登记真实 album_id 直达详情
#   - musicdl/lx：无专辑接口，按关键词搜曲目后按"专辑名+源"聚合合成，
#     代表曲目 guid 注册伪装专辑（详情走聚合适配）
# 官方专辑永远排在前面；同名专辑（与官方结果撞名）不重复注入。

_ALBUM_SEARCH_LIMIT = 10


def _album_list_obj(album_guid: str, album_name: str, artist: str, cover_ref: str, track_count: int) -> dict:
    """专辑列表/搜索结果 VO（与 _album_detail_payload 同字段风格，无 tracks）。"""
    ts = int(time.time())
    artist_name = str(artist or "").strip() or "未知艺术家"
    return {
        "guid": album_guid,
        "name": str(album_name or "").strip() or "未知专辑",
        "artists": [{
            "guid": f"{album_guid}:artist",
            "name": artist_name,
            "coverId": None,
            "createdAt": ts,
            "updatedAt": ts,
        }],
        "coverId": cover_ref or album_guid,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": int(track_count or 0),
    }


def _normalize_netease_album_row(row: dict) -> "tuple[str, str, str, str] | None":
    """网易专辑搜索行（CLI 与 web 兜底两种形状）→ (album_id, 名称, 歌手, 封面)。"""
    album_id = str(row.get("album_id") or row.get("albumId") or row.get("id") or "")
    name = str(row.get("album_name") or row.get("name") or row.get("title") or "").strip()
    if not album_id or album_id in ("0", "None") or not name:
        return None
    ar = row.get("artists") or row.get("artist") or []
    if isinstance(ar, list):
        artist = " / ".join(str(a.get("name") or "") for a in ar if isinstance(a, dict) and a.get("name"))
    else:
        artist = str(ar or "")
    cover = str(row.get("pic_url") or row.get("picUrl") or row.get("coverImgUrl") or row.get("cover") or "")
    return album_id, name, artist, cover


async def _netease_album_search_rows(request: Request, keyword: str, limit: int) -> list[dict]:
    if not CONF.get("netease_enabled"):
        return []
    client = get_musicbox_client(request.app)
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "type": "album", "limit": max(limit, 5)},
            timeout=8.0,
        )
    except Exception as e:
        logger.debug("netease album search failed: %s", e)
        return []
    if r.status_code != 200:
        return []
    body = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/json")) else {}
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        norm = _normalize_netease_album_row(row)
        if norm is None:
            continue
        album_id, name, artist, cover = norm
        if album_id in seen:
            continue
        seen.add(album_id)
        # 锚点 guid 仅供专辑伪装登记/反解，不对应任何真实曲目；
        # 形态对齐曲目锚点约定：专辑 guid = 锚点 + ":album"（register 内部拼）
        anchor = f"online:netease:album:{album_id}"
        register_fake_album(anchor, name, {"cover_url": cover} if cover else {}, album_id=album_id, persist=True)
        out.append(_album_list_obj(f"{anchor}:album", name, artist, f"{anchor}:album", 0))
        if len(out) >= limit:
            break
    return out


async def _aggregate_album_search_rows(request: Request, keyword: str, limit: int) -> list[dict]:
    """musicdl/lx：关键词搜曲目后按"专辑名+源"聚合合成专辑卡片。"""
    jobs: list = []
    if CONF.get("musicdl_enabled"):
        jobs.append(("musicdl", get_musicdl_client(request.app).get(
            "/search", params={"keyword": keyword, "limit": 20}, timeout=6.0,
        )))
    if CONF.get("lx_enabled"):
        params: dict = {"keyword": keyword, "limit": 20}
        lx_sources = CONF.get("lx_sources") or None
        if lx_sources:
            params["sources"] = ",".join(lx_sources)
        jobs.append(("lx", get_lx_client(request.app).get("/api/v1/search", params=params, timeout=6.0)))

    async def _run(coro):
        try:
            return await coro
        except Exception as e:
            logger.debug("album aggregate source search failed: %s", e)
            return None

    groups: "dict[tuple[str, str], list[dict]]" = {}
    for fut in asyncio.as_completed([_run(coro) for _name, coro in jobs]):
        r = await fut
        if r is None or r.status_code != 200:
            continue
        try:
            rows = r.json().get("items")
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for it in rows:
            if not isinstance(it, dict):
                continue
            # lx 行的 id 是裸平台 id（如 kg:xxx），补 online: 前缀统一 guid 形态
            raw_id = str(it.get("id") or "")
            if raw_id and not raw_id.startswith(("online:", "lx:")):
                it = dict(it, id=f"lx:{raw_id}" if str(it.get("source") or "") == "lx" else raw_id)
            try:
                guid = online_guid_from_item(it)
            except Exception:
                continue
            src = source_from_online_guid(guid)
            album_name = str(it.get("album") or "").strip()
            if not guid or not is_online_guid(guid) or not album_name:
                continue
            groups.setdefault((src, album_name), []).append(it)

    out: list[dict] = []
    # 每组取曲目数最多的前 N 个专辑；代表曲目注册伪装专辑（详情走聚合适配）
    for (src, album_name), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rep = items[0]
        try:
            rep_guid = online_guid_from_item(rep)
        except Exception:
            continue
        register_fake_album(rep_guid, album_name, rep, persist=True)
        out.append(_album_list_obj(
            f"{rep_guid}:album", album_name,
            str(rep.get("artist") or ""), rep_guid, len(items),
        ))
        if len(out) >= limit:
            break
    return out


async def _online_album_search(request: Request, keyword: str, limit: int) -> list[dict]:
    """在线专辑搜索：netease 真实接口优先，musicdl/lx 聚合补充（并行）。"""
    netease_coro = _netease_album_search_rows(request, keyword, limit)
    agg_coro = _aggregate_album_search_rows(request, keyword, max(limit // 2, 3))
    results = await asyncio.gather(netease_coro, agg_coro, return_exceptions=True)
    out: list[dict] = []
    seen: set[str] = set()
    for part in results:
        if isinstance(part, Exception):
            logger.debug("online album search branch failed: %s", part)
            continue
        for obj in part or []:
            name_key = str(obj.get("name") or "").strip().lower()
            if name_key and name_key in seen:
                continue
            seen.add(name_key)
            out.append(obj)
    return out[:limit]


@app.api_route("/music/api/v1/search/album", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/search/album/{subpath:path}", methods=["GET", "HEAD"])
async def search_album(request: Request, subpath: str = ""):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        # 官方错误（含未登录 401 信封）原样透传，不吞官方语义
        return envelope if not isinstance(envelope, dict) else JSONResponse(content=envelope)

    keyword = str(request.query_params.get("q") or request.query_params.get("keyword") or request.query_params.get("wd") or "").strip()
    online: list[dict] = []
    if keyword:
        try:
            online = await asyncio.wait_for(
                _online_album_search(request, keyword, _ALBUM_SEARCH_LIMIT), timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.debug("online album search timed out for %r", keyword)
        except Exception as e:
            logger.warning("online album search failed: %s", e)

    if online:
        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {}
            envelope["data"] = data
        lst = data.get("list") if isinstance(data.get("list"), list) else None
        if lst is None:
            data["list"] = lst = []
        official_names = {
            str(x.get("name") or "").strip().lower()
            for x in lst if isinstance(x, dict)
        }
        add = [o for o in online if str(o.get("name") or "").strip().lower() not in official_names]
        data["list"] = lst + add
        if isinstance(data.get("total"), int):
            data["total"] = data["total"] + len(add)
        elif "total" not in data:
            data["total"] = len(data["list"])
    return JSONResponse(content=disguise_client_json(envelope))


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[tuple[str, dict]] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = resolve_real_guid(str(payload.get("trackGUID") or payload.get("guid") or ""))
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append((guid, payload))
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid, payload in online_plays:
                    info = stub_online_info(guid)
                    title = str(info.get("title") or "").strip()
                    artist = str(info.get("artist") or "").strip()
                    album = ""
                    duration_s = None
                    if payload:
                        # 客户端上报若带元数据（App/Web 字段名可能不同）优先采信，
                        # 避免历史条目只有 guid、标题歌手为空。专辑可能是对象，先解开。
                        p_title, p_artist, p_album = _tag_fields({
                            "title": payload.get("title"),
                            "name": payload.get("name"),
                            "artist": payload.get("artist") or payload.get("artistName"),
                            "artists": payload.get("artists"),
                            "album": payload.get("album"),
                            "albumName": payload.get("albumName"),
                        })
                        title = p_title or title
                        artist = p_artist or artist
                        album = p_album or album
                        raw_dur = payload.get("duration") or payload.get("durationMs") or payload.get("duration_ms")
                        try:
                            dv = float(raw_dur)
                            duration_s = dv / 1000.0 if dv > 10000 else dv
                        except (TypeError, ValueError):
                            duration_s = None
                    if not title or not artist or not album:
                        # 事件不带元数据时从内存搜索会话补齐（播放时会话必在）
                        retained, _entry = _retained_track(request, guid)
                        if retained:
                            r_title, r_artist, r_album = _tag_fields(retained)
                            title = title or r_title
                            artist = artist or r_artist
                            album = album or r_album
                            if not duration_s:
                                try:
                                    duration_s = float(retained.get("duration_s") or 0) or None
                                except (TypeError, ValueError):
                                    duration_s = None
                        if not title or not artist or not album:
                            served = _lookup_playlist_cache_track(guid)
                            if served:
                                s_title, s_artist, s_album = _tag_fields(served)
                                title = title or s_title
                                artist = artist or s_artist
                                album = album or s_album
                    cached = find_cache_file(guid)
                    if cached and (not title or not artist):
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            file_artist, file_title = base.split(" - ", 1)
                            artist = artist or file_artist
                            title = title or file_title
                        else:
                            title = title or base
                    snapshot = {
                        "guid": guid,
                        "title": title,
                        "artist": artist,
                        "source": source_from_online_guid(guid),
                    }
                    if album:
                        snapshot["album"] = album
                    if duration_s:
                        snapshot["duration_s"] = duration_s
                    dailyrec.record_online_play(user_guid, guid, snapshot)
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    # 官方历史已取回成功（同一组请求头），此时探测被拒不回传鉴权错误，
    # 降级为仅返回官方列表（同收藏列表）。
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        logger.warning("play history list degraded to official-only: auth probe rejected after official list ok")
        return JSONResponse(content=disguise_envelope_list(envelope), headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    fav_set = {str(it.get("guid") or "") for it in load_online_favorites(user_guid)}
    template = official_track_template(official)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        info = _snapshot_to_info(track) if track else None
        if not (info or {}).get("title"):
            # 历史快照可能没带元数据：从内存搜索会话补齐（无网络开销）
            retained, _entry = _retained_track(request, guid)
            if retained:
                r_title, r_artist, r_album = _tag_fields(retained)
                info = {
                    **(info or {}),
                    "title": r_title,
                    "artist": r_artist,
                    "album": r_album,
                    "duration_s": retained.get("duration_s") or 0,
                    "ext": retained.get("ext") or "",
                }
        obj = build_favorite_track_obj(guid, info, created_at=int(it.get("playedAt") or time.time()), template=template)
        obj["isFavorite"] = guid in fav_set
        online_tracks.append(disguise_client_json(obj))

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    data["list"] = merged_online + disguise_official_items(official)
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


async def _forward_official_play_history_delete(
    request: Request, client: httpx.AsyncClient, official: list[str],
) -> Response | None:
    """混合批次中官方部分先行。返回 None=官方成功，否则返回官方错误响应（本地不动）。"""
    req = client.build_request(
        "POST",
        "/music/api/v1/play-history/delete",
        headers=copy_incoming_headers(request),
        json={"trackGUIDs": official},
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not (isinstance(payload, dict) and payload.get("code") == 0):
        return JSONResponse(content=payload or {"code": -1, "msg": "upstream error", "data": None})
    return None


@app.api_route("/music/api/v1/play-history/delete", methods=["POST", "DELETE"])
async def play_history_delete(request: Request):
    """删除播放历史：online 条目删本地存储，官方条目转发官方后端。

    列表是代理拼的（list 合并本地在线历史），删除闭环也必须在代理完成，否则
    带（伪装成官方 32-hex 的）在线 id 的删除请求直达官方被拒。官方契约是批量
    字段 trackGUIDs（前端单条移除与"编辑"多选移除共用，官方后端 binding:"required"），
    兼容单数 trackGUID/guid（含 query 透传）；混合批次官方部分先行，官方失败
    本地不动，与 playlist/remove-track 同款分工。
    """
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    raw = body.get("trackGUIDs") if isinstance(body, dict) else None
    if isinstance(raw, list) and raw:
        candidates = [str(g or "").strip() for g in raw]
    else:
        single = ""
        if isinstance(body, dict):
            single = str(body.get("trackGUID") or body.get("guid") or "").strip()
        if not single:
            single = str(request.query_params.get("trackGUID") or request.query_params.get("guid") or "").strip()
        candidates = [single] if single else []

    resolved = [resolve_real_guid(g) for g in candidates if g]
    online = [g for g in resolved if is_online_guid(g)]
    if not online:
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    official = [g for g in resolved if not is_online_guid(g)]
    if official:
        err = await _forward_official_play_history_delete(request, upstream_client, official)
        if err is not None:
            return err

    async with _HISTORY_LOCK:
        for guid in online:
            try:
                dailyrec.remove_online_play(user_guid, guid)
            except Exception as e:
                logger.warning("Error deleting from online play history for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


# 官方端点取证：未拦截路径 -> 转发次数（仅内存，重启清零；用于首见告警与采样）
_FORWARDED_PATH_STATS: dict[str, int] = {}


def _trace_forwarded_endpoint(request: Request) -> None:
    """catch_all 透传前的官方端点取证日志。

    未拦截的 /music/api 请求：同一路径首见打 INFO（官方 App 更新引入新端点时
    日志立刻可见），之后每 50 次采样一条；FNMUSIC_TRACE_FORWARD=detail 逐条记录。
    """
    path = request.url.path
    if not path.startswith("/music/api/"):
        return
    count = _FORWARDED_PATH_STATS.get(path, 0) + 1
    _FORWARDED_PATH_STATS[path] = count
    if CONF.get("trace_forward") or count == 1 or count % 50 == 0:
        logger.info(
            "forward-unhandled %s %s (#%d) —— 官方端点未被代理拦截，仅透传",
            request.method,
            path,
            count,
        )


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    _trace_forwarded_endpoint(request)
    return await forward_to_upstream(request, get_upstream_client(request.app))
