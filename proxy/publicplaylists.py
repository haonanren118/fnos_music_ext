"""公共平台歌单：发现结果由管理页预选，音乐页注入只读虚拟歌单。

职责：
1. 解析 FNMUSIC_PUBLIC_PLAYLISTS_SELECTION，保留管理员选择顺序；
2. 按已选平台 ID 拉摘要/曲目，统一稳定 guid 与缓存策略；
3. 摘要只读不扫发现池，曲目缓存失败回落 24h 磁盘快照。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from urllib.parse import quote, unquote
from uuid import uuid4

import httpx

logger = logging.getLogger("proxy.publicplaylists")

PUBLIC_GUID_PREFIX = "online:playlist:public:"
_SUMMARY_TTL_S = 30 * 60.0
_DISK_TTL_S = 24 * 3600.0
_FAIL_COOLDOWN_S = 120.0
_TRACKS_TTL_S = 10 * 60.0
_TRACKS_LRU_MAX = 8
_TRACKS_FETCH_TIMEOUT_S = 60.0
_MAX_TRACKS = 1000
_MAX_SELECTION = 50

_summary_cache: dict[str, tuple[dict, float]] = {}
_summary_fail_until: dict[str, float] = {}
_summary_tasks: dict[str, asyncio.Task] = {}
_tracks_cache: dict[str, tuple[list, float]] = {}
_tracks_fail_until: dict[str, float] = {}
_tracks_order: list[str] = []
_tracks_tasks: dict[str, asyncio.Task] = {}
_cover_urls: dict[str, str] = {}
_prefetch_tasks: set[asyncio.Task] = set()

# 曲目元数据索引（guid -> title/artist/album/cover/duration/…）。
# 注入公共歌单时这些字段本来就是全的（来自平台榜单/歌单接口），但 _online_info 只查
# 音源服务自己的 songInfo 缓存：洛雪榜单曲目从未进过那个缓存（只有搜索会填），
# 于是 audio-info 返回空歌名/时长/专辑 → 客户端判为不可播，歌词也一并为空。
# 这里把注入时已知的元数据留一份，供 _online_info 兜底回填。
_TRACK_META_INDEX: dict[str, dict] = {}
_TRACK_META_INDEX_MAX = 8000
_TRACK_META_FIELDS = ("title", "artist", "album", "cover_url", "duration_s", "ext", "file_size")


def _index_track_meta(tracks: "list[dict]") -> None:
    """把一批曲目的展示元数据登记进索引（满额时丢最旧的）。"""
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        guid = str(track.get("guid") or "")
        if not guid:
            continue
        meta = {}
        for field in _TRACK_META_FIELDS:
            value = track.get(field)
            if value not in (None, "", 0):
                meta[field] = value
        if not meta.get("title"):
            continue
        _TRACK_META_INDEX[guid] = meta
        if len(_TRACK_META_INDEX) > _TRACK_META_INDEX_MAX:
            for old in list(_TRACK_META_INDEX)[:len(_TRACK_META_INDEX) - _TRACK_META_INDEX_MAX]:
                _TRACK_META_INDEX.pop(old, None)


def track_meta(guid: str) -> "dict | None":
    """按曲目 guid 取公共歌单注入时登记的元数据（供代理 _online_info 兜底）。"""
    return _TRACK_META_INDEX.get(str(guid or ""))

# 卡片封面字段由 app.py 注入（避免本模块反向依赖 app）：
# 客户端「歌单」列表里 每日推荐/热门推荐 能显示封面，而公共歌单卡片一直是默认音符图标，
# 差别就在这两个字段 —— 推荐卡片 coverId 是**纯 32-hex** 且**同时下发 coverUrl**
# （同源封面端点），公共卡片此前只有 track_ 前缀的 coverId、没有 coverUrl。
_COVER_ID_HOOK = None      # guid -> 纯 32-hex coverId（会登记反查表）
_COVER_URL_HOOK = None     # coverId -> 同源封面端点 URL


def set_card_cover_hooks(cover_id_fn, cover_url_fn) -> None:
    global _COVER_ID_HOOK, _COVER_URL_HOOK
    _COVER_ID_HOOK = cover_id_fn
    _COVER_URL_HOOK = cover_url_fn

_DEFAULT_NETEASE = [
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3779629"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "19723756"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "2884035"},
]
def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    return env or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cache_dir() -> str:
    return os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_DIR") or os.path.join(home_dir(), "public_playlists_cache")


def _atomic_write_json(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        part = f"{path}.{uuid4().hex[:8]}.part"
        with open(part, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        logger.warning("public playlist cache write %s failed: %s", path, exc)


def _load_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return None
        saved = data.get("savedAt")
        if not isinstance(saved, (int, float)) or time.time() - saved >= _DISK_TTL_S:
            return None
        return data
    except Exception:
        return None


def _entry_key(entry: dict) -> str:
    return "|".join(str(entry.get(k) or "") for k in ("provider", "source", "kind", "id"))


def playlist_guid(entry: dict) -> str:
    provider = str(entry.get("provider") or "")
    source = str(entry.get("source") or "")
    kind = str(entry.get("kind") or "playlist")
    encoded = quote(str(entry.get("id") or ""), safe="")
    return f"{PUBLIC_GUID_PREFIX}{provider}:{source}:{kind}:{encoded}"


def entry_from_guid(guid: str) -> dict | None:
    raw = str(guid or "")
    if not raw.startswith(PUBLIC_GUID_PREFIX):
        return None
    tail = raw[len(PUBLIC_GUID_PREFIX):]
    parts = tail.split(":", 3)
    if len(parts) != 4 or not all(parts[:3]):
        return None
    provider, source, kind, encoded = parts
    identifier = unquote(encoded)
    if not identifier or kind not in ("playlist", "chart"):
        return None
    return {"provider": provider, "source": source, "kind": kind, "id": identifier}


def is_public_playlist_guid(guid: str | None) -> bool:
    return entry_from_guid(str(guid or "")) is not None


def fake_guid(real_guid: str) -> str:
    return hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()


def parse_selection(raw: object, conf: dict | None = None, default_provider: str = "") -> list[dict]:
    conf = conf or {}
    text = str(raw if raw is not None else os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", ""))
    text = text.strip()
    if not text:
        if default_provider == "lx":
            rows = []
        else:
            rows = _DEFAULT_NETEASE
    else:
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("invalid public playlist selection, fallback empty")
            return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = {
            "provider": str(row.get("provider") or "").strip(),
            "source": str(row.get("source") or "").strip(),
            "kind": str(row.get("kind") or "playlist").strip(),
            "id": str(row.get("id") or "").strip(),
        }
        if entry["provider"] not in ("musicbox", "lx"):
            continue
        if entry["kind"] not in ("playlist", "chart") or not entry["source"] or not entry["id"]:
            continue
        key = _entry_key({**entry, "kind": "playlist"}) if entry["provider"] == "musicbox" else _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= _MAX_SELECTION:
            break
    return out


def configured_selection(conf: dict) -> list[dict]:
    if not conf.get("public_playlists_enabled"):
        return []
    return parse_selection(conf.get("public_playlists_selection"), conf)


def _selection_runtime_usable(entry: dict, conf: dict) -> bool:
    if entry["provider"] == "musicbox":
        return bool(conf.get("netease_enabled") or conf.get("netease_playlist_resident"))
    if not conf.get("lx_enabled"):
        return False
    selected = conf.get("lx_sources") or []
    return entry["source"] in selected


def _provider_client(app_state, entry: dict) -> httpx.AsyncClient:
    if entry["provider"] == "musicbox":
        client = getattr(app_state, "musicbox_client", None)
        if client is None:
            client = httpx.AsyncClient(base_url=os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"), timeout=20.0)
            app_state.musicbox_client = client
        return client
    client = getattr(app_state, "lx_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=os.environ.get("FNMUSIC_LX_URL", "http://127.0.0.1:8772"), timeout=25.0)
        app_state.lx_client = client
    return client


def _source_label(source: str) -> str:
    return {
        "netease": "网易",
        "wy": "网易",
        "kw": "酷我",
        "kg": "酷狗",
        "tx": "QQ",
        "mg": "咪咕",
    }.get(source, source.upper())


# 音源平台图标（2026-10-20）：原生客户端歌单卡片只渲染 coverId/coverUrl 图标，
# 此前靠 name 文本前缀「[酷我]」区分来源 —— 文字标签既占位又不好看。
# 改为直接用平台图标当卡片封面，名称回归纯榜单名。
# 图标是随包发布的本地 PNG（proxy/static/platforms/<source>.png），
# 客户端只按 coverUrl 取图，代理用 /static/cover?coverId=platform:<source> 分发。
_PLATFORM_ICON_SOURCES = ("kw", "kg", "tx", "wy", "mg", "netease")


def platform_icon_cover_id(source: str) -> str:
    """平台图标 coverId（纯 32-hex 之外的专用形态，由封面端点识别）。"""
    src = str(source or "").strip().lower()
    if src not in _PLATFORM_ICON_SOURCES:
        return ""
    return f"platform:{src}"


def platform_icon_url(source: str) -> str:
    """平台图标的相对 URL（客户端按 coverUrl 取图）。"""
    icon_id = platform_icon_cover_id(source)
    if not icon_id:
        return ""
    return f"/music/api/v1/static/cover?coverId={quote(icon_id, safe='')}"


def _card_from_summary(entry: dict, summary: dict) -> dict:
    guid = playlist_guid(entry)
    cover_url = str(summary.get("cover_url") or "")
    if cover_url:
        _cover_urls[guid] = cover_url
    try:
        count = int(summary.get("track_count") or 0)
        updated = int(summary.get("updated_at") or 0)
    except (TypeError, ValueError):
        count, updated = 0, 0
    loaded = cached_tracks(guid)
    if loaded is not None:
        count = len(loaded)
    # 与「每日推荐/热门推荐」卡片对齐：coverId 用纯 32-hex（客户端只认这种形态），
    # 并同时下发 coverUrl —— 侧边栏歌单列表按 coverUrl 取图，缺它就是默认音符图标。
    cover_id = _COVER_ID_HOOK(guid) if _COVER_ID_HOOK else "track_" + fake_guid(guid)
    cover_ep = _COVER_URL_HOOK(cover_id) if (_COVER_URL_HOOK and cover_id) else ""
    # 2026-10-20：来源标识改用**平台图标**（客户端按 coverUrl 取图），
    # 名称里的「[酷我]」文本前缀去掉 —— 卡片图标已经表明来源，文字前缀是冗余的。
    source = str(entry.get("source") or "")
    icon_url = platform_icon_url(source)
    if icon_url:
        cover_ep = icon_url
        name = str(summary.get("name") or entry["id"])
    else:
        name = f"[{_source_label(source)}] {str(summary.get('name') or entry['id'])}"
    return {
        "guid": guid,
        "name": name,
        "coverId": cover_id,
        "coverUrl": cover_ep,
        "createdAt": updated or int(time.time()),
        "updatedAt": updated or int(time.time()),
        "trackCount": count,
        "isDaily": True,
    }


def _summary_disk_path(guid: str) -> str:
    digest = hashlib.sha256(guid.encode()).hexdigest()[:24]
    return os.path.join(cache_dir(), f"summary-{digest}.json")


# 缺封面的摘要视为过期（2026-10-17）：摘要落盘 24h 有效，一旦某次取到的是
# 「平台还没给出榜图/歌单图」的版本，卡片就会整天钉在无封面上（App 里是默认音符图标）。
# 缺封面且落盘超过 10 分钟就丢弃重取；带封面的摘要不受影响（不会反复打上游）。
_SUMMARY_COVER_STALE_S = 600.0


def _load_disk_summary(guid: str) -> "tuple[dict, dict] | None":
    data = _load_json(_summary_disk_path(guid))
    if not data:
        return None
    entry = entry_from_guid(str(data.get("guid") or guid))
    summary = data.get("summary")
    if entry is None or not isinstance(summary, dict):
        return None
    cover = str(summary.get("cover_url") or "").strip()
    if cover:
        _cover_urls.setdefault(guid, cover)
    else:
        try:
            saved_at = float(data.get("savedAt") or 0)
        except (TypeError, ValueError):
            saved_at = 0.0
        if time.time() - saved_at >= _SUMMARY_COVER_STALE_S:
            return None
    return entry, summary


async def _fetch_summary(client: httpx.AsyncClient, entry: dict) -> dict:
    guid = playlist_guid(entry)
    response = await client.get(
        f"/api/v1/public-playlists/{quote(entry['id'], safe='')}",
        params={"source": entry["source"], "kind": entry["kind"]},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"summary http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
        raise RuntimeError("invalid summary payload")
    summary = payload["data"]
    _summary_cache[guid] = (summary, time.monotonic() + _SUMMARY_TTL_S)
    _atomic_write_json(_summary_disk_path(guid), {
        "version": 1, "savedAt": int(time.time()), "guid": guid, "summary": summary,
    })
    return summary


async def load_summary(app_state, entry: dict) -> "dict | None":
    guid = playlist_guid(entry)
    hit = _summary_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return _card_from_summary(entry, hit[0])
    if time.monotonic() < _summary_fail_until.get(guid, 0.0):
        disk = _load_disk_summary(guid)
        return _card_from_summary(entry, disk[1]) if disk else None
    existing = _summary_tasks.get(guid)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> dict:
        try:
            summary = await _fetch_summary(_provider_client(app_state, entry), entry)
            _summary_fail_until.pop(guid, None)
            return _card_from_summary(entry, summary)
        except Exception as exc:
            _summary_fail_until[guid] = time.monotonic() + _FAIL_COOLDOWN_S
            disk = _load_disk_summary(guid)
            if disk is None:
                logger.info("public playlist summary failed (%s): %s", guid, exc)
                raise
            return _card_from_summary(entry, disk[1])

    task = asyncio.get_running_loop().create_task(run())
    _summary_tasks[guid] = task
    task.add_done_callback(lambda done: _finish_shared(_summary_tasks, guid, done))
    return await asyncio.shield(task)


async def peek_cards(app_state, conf: dict, build_track) -> list[dict]:
    entries = [entry for entry in configured_selection(conf) if _selection_runtime_usable(entry, conf)]
    if not entries:
        return []
    tasks = [asyncio.create_task(load_summary(app_state, entry)) for entry in entries]
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    # 后台请求继续完成，慢摘要不会让已经缓存的其他歌单消失。
    for task in pending:
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    cards: list[dict] = []
    for entry, task in zip(entries, tasks):
        result = task.result() if task in done and not task.cancelled() and task.exception() is None else cached_card(playlist_guid(entry))
        if isinstance(result, dict):
            cards.append(result)
        else:
            logger.debug("public playlist list card failed: %s", result)
    if cards:
        schedule_prefetch(app_state, conf, cards, build_track)
    return cards


def cached_card(guid: str) -> "dict | None":
    hit = _summary_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        entry = entry_from_guid(guid)
        return _card_from_summary(entry, hit[0]) if entry else None
    disk = _load_disk_summary(guid)
    return _card_from_summary(entry_from_guid(guid), disk[1]) if disk and disk[0] else None


def cover_url_for(guid: str) -> str:
    if guid in _cover_urls:
        return _cover_urls[guid]
    disk = _load_disk_summary(guid)
    return _cover_urls.get(guid, "") if disk else ""


def _musicbox_item(row: dict) -> dict:
    return {
        "id": f"netease:{row.get('song_id') or row.get('id')}",
        "source": "netease",
        "title": row.get("name") or row.get("song_name"),
        "artist": row.get("artist"),
        "album": row.get("album_name"),
        "duration_ms": row.get("duration_ms") or row.get("duration") or 0,
        "cover_url": row.get("album_pic_url") or row.get("cover_url"),
        "has_sq": row.get("has_sq"),
        "needs_vip": row.get("needs_vip"),
        # 我方 fork 适配（上游无此行）：proxy 的 [VIP] 展示是 fee 判定
        # （_vip_tag_prefix，fee in (1,4)），上游在此用 needs_vip；不带 fee
        # 公共歌单调的曲目就吃不到 VIP 标记。musicbox 侧 _map_song_detail
        # 已按上游同款改动输出 fee，这里负责把它透传到 build_online_track。
        "fee": row.get("fee"),
    }


def _tracks_disk_path(guid: str) -> str:
    digest = hashlib.sha256(guid.encode()).hexdigest()[:24]
    return os.path.join(cache_dir(), f"tracks-{digest}.json")


def _load_disk_tracks(guid: str) -> "list | None":
    data = _load_json(_tracks_disk_path(guid))
    tracks = data.get("tracks") if isinstance(data, dict) else None
    card = data.get("card") if isinstance(data, dict) else None
    if isinstance(card, dict) and card.get("cover_url"):
        _cover_urls.setdefault(guid, str(card["cover_url"]))
    if not isinstance(tracks, list):
        return None
    # 重启后磁盘缓存里的曲目也要能兜底元数据（见 _TRACK_META_INDEX 注释）
    _index_track_meta(tracks)
    return [dict(track) for track in tracks]


async def _fetch_tracks(app_state, entry: dict, build_track) -> list:
    guid = playlist_guid(entry)
    client = _provider_client(app_state, entry)
    response = await asyncio.wait_for(client.get(
        f"/api/v1/public-playlists/{quote(entry['id'], safe='')}/tracks",
        params={"source": entry["source"], "kind": entry["kind"]},
        timeout=_TRACKS_FETCH_TIMEOUT_S,
    ), timeout=_TRACKS_FETCH_TIMEOUT_S)
    if response.status_code != 200:
        raise RuntimeError(f"tracks http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), list):
        raise RuntimeError("invalid tracks payload")
    tracks: list[dict] = []
    for row in payload["data"][:_MAX_TRACKS]:
        if not isinstance(row, dict):
            continue
        item = _musicbox_item(row) if entry["provider"] == "musicbox" else row
        track = build_track(item)
        if isinstance(track, dict) and track.get("guid"):
            tracks.append(track)
    _tracks_cache[guid] = (tracks, time.monotonic() + _TRACKS_TTL_S)
    _index_track_meta(tracks)
    if guid in _tracks_order:
        _tracks_order.remove(guid)
    _tracks_order.append(guid)
    while len(_tracks_order) > _TRACKS_LRU_MAX:
        _tracks_cache.pop(_tracks_order.pop(0), None)
    card = _summary_cache.get(guid, ({}, 0))[0]
    _atomic_write_json(_tracks_disk_path(guid), {
        "version": 1, "savedAt": int(time.time()), "guid": guid,
        "card": {**card, "cover_url": _cover_urls.get(guid, "")}, "tracks": tracks,
    })
    return [dict(track) for track in tracks]


async def load_tracks(app_state, entry: dict, build_track) -> list:
    guid = playlist_guid(entry)
    hit = _tracks_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return [dict(track) for track in hit[0]]
    if time.monotonic() < _tracks_fail_until.get(guid, 0.0):
        disk = _load_disk_tracks(guid)
        if disk is not None:
            return disk
        raise RuntimeError("public playlist tracks recently failed")
    existing = _tracks_tasks.get(guid)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> list:
        try:
            tracks = await _fetch_tracks(app_state, entry, build_track)
            _summary_fail_until.pop(guid, None)
            _tracks_fail_until.pop(guid, None)
            return tracks
        except Exception as exc:
            _tracks_fail_until[guid] = time.monotonic() + _FAIL_COOLDOWN_S
            disk = _load_disk_tracks(guid)
            if disk is None:
                logger.warning("public playlist tracks failed (%s): %s", guid, exc)
                raise
            logger.info("public playlist tracks fallback to disk (%s): %s", guid, exc)
            return disk

    task = asyncio.get_running_loop().create_task(run())
    _tracks_tasks[guid] = task
    task.add_done_callback(lambda done: _finish_shared(_tracks_tasks, guid, done))
    return await asyncio.shield(task)


def _finish_shared(registry: dict, guid: str, task: asyncio.Task) -> None:
    if registry.get(guid) is task:
        registry.pop(guid, None)
    if not task.cancelled():
        task.exception()


async def shutdown() -> None:
    tasks = set(_summary_tasks.values()) | set(_tracks_tasks.values()) | _prefetch_tasks
    tasks = {task for task in tasks if task.get_loop() is asyncio.get_running_loop()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def cached_tracks(guid: str) -> "list | None":
    hit = _tracks_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return [dict(track) for track in hit[0]]
    return _load_disk_tracks(guid)


def schedule_prefetch(app_state, conf: dict, cards: list[dict], build_track) -> None:
    if any(not task.done() for task in _prefetch_tasks):
        return
    available = {_entry_key(entry) for entry in configured_selection(conf) if _selection_runtime_usable(entry, conf)}
    pending = []
    now = time.monotonic()
    for card in cards or []:
        entry = entry_from_guid(str(card.get("guid") or ""))
        if not entry or _entry_key(entry) not in available:
            continue
        guid = playlist_guid(entry)
        hit = _tracks_cache.get(guid)
        if hit and now < hit[1]:
            continue
        if guid not in pending:
            pending.append(guid)
        if len(pending) >= 4:
            break
    if not pending:
        return
    by_guid = {playlist_guid(entry): entry for entry in configured_selection(conf)}

    async def runner() -> None:
        for guid in pending:
            entry = by_guid.get(guid)
            if entry is None:
                continue
            try:
                await load_tracks(app_state, entry, build_track)
            except Exception as exc:
                logger.debug("public playlist prefetch failed (%s): %s", guid, exc)

    try:
        task = asyncio.get_running_loop().create_task(runner())
    except RuntimeError:
        return
    _prefetch_tasks.add(task)
    task.add_done_callback(_prefetch_tasks.discard)


def reset_for_test() -> None:
    _summary_cache.clear()
    _summary_fail_until.clear()
    _tracks_cache.clear()
    _tracks_fail_until.clear()
    _tracks_order.clear()
    _cover_urls.clear()
    for task in (*_summary_tasks.values(), *_tracks_tasks.values(), *_prefetch_tasks):
        if not task.done():
            task.cancel()
    _summary_tasks.clear()
    _tracks_tasks.clear()
    _prefetch_tasks.clear()
