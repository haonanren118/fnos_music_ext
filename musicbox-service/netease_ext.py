"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("musicbox_service.netease_ext")

_api_lock = threading.Lock()
_api_instance = None

# 登录态探测结果缓存：(结果, 探测时刻单调钟)。失败短缓存、成功长缓存，
# 过期时强制重建实例重读磁盘 cookie 再下结论，避免常驻实例揣着过期 cookie。
_login_state_lock = threading.Lock()
_login_state: "tuple[bool, float] | None" = None
_LOGIN_TTL_OK_S = 300.0
_LOGIN_TTL_FAIL_S = 60.0

# 进程内取链结果缓存：(item, 过期时刻单调钟)。网易直链自带过期时间，只做短缓存。
_url_cache_lock = threading.Lock()
_url_cache: "dict[tuple[int, str], tuple[dict, float]]" = {}
_URL_CACHE_TTL_OK_S = 600.0
_URL_CACHE_TTL_FAIL_S = 60.0
_URL_CACHE_MAX = 4096


def _get_api_locked():
    """调用方必须已持有 _api_lock：取实例 + 用实例在同一次持锁内完成。"""
    global _api_instance
    if _api_instance is None:
        from runner import ensure_xdg_dirs

        ensure_xdg_dirs()
        from NEMbox.api import NetEase

        _api_instance = NetEase()
    return _api_instance


def _get_api():
    with _api_lock:
        return _get_api_locked()


def reset_api(reason: str = "") -> None:
    """丢弃常驻 NetEase 实例与相关缓存，下次 _get_api() 重建并重读磁盘 cookie。

    登录由 CLI 子进程完成并写盘，而 NEMbox 的 NetEase 只在构造时读一次盘；
    磁盘 cookie 变化（扫码成功等）后不重建实例，服务进程内就永远是旧登录态。
    """
    global _api_instance, _login_state
    with _api_lock:
        if _api_instance is not None:
            try:
                _api_instance.session.close()
            except Exception:
                pass
        _api_instance = None
    with _login_state_lock:
        _login_state = None
    with _url_cache_lock:
        _url_cache.clear()
    if reason:
        logger.info("netease api instance reset: %s", reason)


def _map_song_detail(item: dict[str, Any]) -> dict[str, Any]:
    sid = item.get("id") or item.get("song_id")
    song_id = int(sid) if sid is not None else 0
    name = str(item.get("name") or "")
    ar_list = item.get("ar") or item.get("artists") or []
    if isinstance(ar_list, list):
        artist = " / ".join(
            str(a.get("name")) for a in ar_list if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = ""
    al = item.get("al") or item.get("album") or {}
    if isinstance(al, dict):
        album_name = str(al.get("name") or "")
        album_pic_url = str(al.get("picUrl") or al.get("pic_url") or "")
    else:
        album_name = ""
        album_pic_url = ""
    duration_ms = int(item.get("dt") or item.get("duration") or 0)
    try:
        fee = int(item.get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
        # fee 收费类型（0=免费 1=VIP 4=购买专辑 8=非会员免费低音质）。
        # 纯展示字段：proxy 侧 _vip_tag_prefix 据此在歌名前加 [VIP] 前缀，
        # 不影响可播性判定（可播过滤只看 freeTrialInfo / url / code）。
        "fee": fee,
    }


# ==== 账号详情（VIP 标识，2026-10-17） ====
# WebUI「网易扫码登录」面板要显示 VIP 标识，但 musicbox CLI 的 `auth status`
# 只回 logged_in/user_id/nickname。这里用 NEMbox 的 get_account_info 补 VIP 档位。
# vipType 是档位枚举（实测付费号 110，历史值 10/11）：0 = 非会员。
_ACCOUNT_CACHE: dict = {"ts": 0.0, "value": None}
_ACCOUNT_TTL_S = 60.0


def _vip_label(vip_type: int) -> str:
    if vip_type <= 0:
        return ""
    if vip_type == 10:
        return "音乐包"
    if vip_type >= 11:
        return "黑胶VIP"
    return "VIP"


def account_detail() -> dict:
    """账号详情（含 VIP 档位）：供 WebUI 显示 VIP 标识。60 秒内存缓存。

    失败返回空 dict（调用方按「无 VIP 信息」处理），绝不抛错打断登录态查询。
    """
    now = time.monotonic()
    cached = _ACCOUNT_CACHE.get("value")
    if isinstance(cached, dict) and now - float(_ACCOUNT_CACHE.get("ts") or 0) < _ACCOUNT_TTL_S:
        return cached
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info() or {}
    except Exception:
        return {}
    profile = info.get("profile") if isinstance(info.get("profile"), dict) else {}
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    try:
        vip_type = int(profile.get("vipType") or account.get("vipType") or 0)
    except (TypeError, ValueError):
        vip_type = 0
    detail = {
        "user_id": profile.get("userId") or account.get("id") or "",
        "nickname": str(profile.get("nickname") or ""),
        "avatar_url": str(profile.get("avatarUrl") or ""),
        "vip_type": vip_type,
        "vip_label": _vip_label(vip_type),
    }
    _ACCOUNT_CACHE.update({"ts": now, "value": detail})
    return detail


def check_is_logged_in() -> bool:
    global _login_state
    with _login_state_lock:
        state = _login_state
    if state is not None:
        logged, at = state
        if time.monotonic() - at < (_LOGIN_TTL_OK_S if logged else _LOGIN_TTL_FAIL_S):
            return logged
        # 缓存过期：磁盘 cookie 可能已换（重新扫码/在别处登录），先重建实例再探测
        with _login_state_lock:
            _login_state = None
        reset_api("login-state ttl expired")
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
        logged = bool(info and (info.get("account") or info.get("profile")))
    except Exception:
        logged = False
    with _login_state_lock:
        _login_state = (logged, time.monotonic())
    return logged


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID。

    - 未登录时：使用 api.songs_url 批量获取真实可播状态。凡是 url 为空/404、或者带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉。
    - 已登录时：如果有账号权限能取到完整真实 url 且非试听，则允许返回；若无权限仍过滤。
    - 只能试听30~45秒片段（带 freeTrialInfo/试听限制）的歌曲，绝不能当作可播放曲目返回。
    """
    if not ids:
        return set()
    with _api_lock:
        try:
            api = _get_api_locked()
            urls_data = api.songs_url(ids)
        except Exception:
            return set()
    if not isinstance(urls_data, list):
        return set()

    logged_in = check_is_logged_in()
    playable_ids: set[int] = set()
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)
        free_trial = item.get("freeTrialInfo")

        # 核心铁律：url 为空或 code == 404，坚决过滤
        if not url or not str(url).strip() or code == 404:
            continue
        # 凡是带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉
        # 并且只能试听片段的歌曲绝不当作可播返回
        if free_trial:
            continue
        # 未登录状态下，收费/VIP/专辑曲目坚决不返回
        if not logged_in and fee != 0 and fee not in (0, 8):
            continue

        playable_ids.add(sid_int)
    return playable_ids


def get_song_url(song_id: int, quality: str) -> "dict[str, Any] | None":
    """进程内解析单曲播放链接（复用常驻实例，免起 CLI 子进程）。

    返回对齐 CLI `song url --json` 的 data 字段（含 url/code/fee 等）；接口
    异常或返回空时返回 None，由调用方降级 CLI（保留结构化错误语义）。
    结果短缓存（成功 600s / 失败 60s，键含音质），reset_api 时随实例一并清空。
    """
    key = (int(song_id), str(quality))
    now = time.monotonic()
    with _url_cache_lock:
        hit = _url_cache.get(key)
        if hit and now < hit[1]:
            return hit[0]
    try:
        with _api_lock:
            api = _get_api_locked()
            # songs_url 的音质取自全局 Config：临时改写再恢复（CLI cmd_song_url 同款做法）
            from NEMbox.config import Config

            config = Config()
            old_quality = config.get("music_quality")
            config.config.setdefault("music_quality", {})["value"] = quality
            try:
                urls = api.songs_url([int(song_id)])
            finally:
                config.config["music_quality"]["value"] = old_quality
    except Exception:
        return None
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], dict):
        return None
    item = urls[0]
    ok = item.get("code") == 200 and item.get("url")
    with _url_cache_lock:
        if len(_url_cache) >= _URL_CACHE_MAX:
            expire = time.monotonic()
            for k in [k for k, v in _url_cache.items() if v[1] <= expire]:
                del _url_cache[k]
        _url_cache[key] = (item, time.monotonic() + (_URL_CACHE_TTL_OK_S if ok else _URL_CACHE_TTL_FAIL_S))
    return item


def batch_song_details(ids: list[int], playable_only: bool = True) -> list[dict[str, Any]]:
    """批量歌曲详情（名称/歌手/专辑/封面/时长/有无无损/fee），按请求顺序返回。

    playable_only=True（默认，既有行为，勿改）：过 filter_playable_song_ids
    （songs_url 真实取链探测），搜索/推荐入口依赖该过滤。
    playable_only=False（#55 端口新增）：全量返回，与上游 v2.8.0「封面与可播性
    解耦」同语义——详情行是纯展示元数据，不因取链判定的可播状态整行丢弃
    （登录态抖动/VIP 波动/风控都会让 songs_url 拿不全 url）。公共歌单的曲目
    行同理：可播性由点播侧自然呈现，不该让榜单少歌。
    """
    if not ids:
        return []
    with _api_lock:
        api = _get_api_locked()
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    playable_ids = filter_playable_song_ids(ids) if playable_only else None

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"] and (playable_ids is None or mapped["song_id"] in playable_ids):
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_lyric_pair(song_id: int) -> dict[str, str]:
    with _api_lock:
        api = _get_api_locked()
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}


def search_web_fallback(keyword: str, stype: str = "song", limit: int = 20) -> list[dict[str, Any]]:
    """网易官方 Web 搜索接口降级容错（https://music.163.com/api/search/get/web）。

    当主接口被风控（如 405 操作频繁）或失败时，调用官方备用接口获取歌曲列表。
    """
    if not keyword or not keyword.strip():
        return []
    url = "https://music.163.com/api/search/get/web"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com",
        "Cookie": "os=pc",
    }
    type_map = {
        "song": 1,
        "album": 10,
        "artist": 100,
        "playlist": 1000,
    }
    data = {
        "s": keyword.strip(),
        "type": str(type_map.get(stype, 1)),
        "limit": str(limit),
        "offset": "0",
    }
    res = None
    try:
        if httpx is not None:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, data=data)
                resp.raise_for_status()
                res = resp.json()
        elif requests is not None:
            resp = requests.post(url, headers=headers, data=data, timeout=10.0)
            resp.raise_for_status()
            res = resp.json()
        else:
            import urllib.parse
            import urllib.request
            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(url, data=encoded, headers=headers)
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                res = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("search_web_fallback request failed: %s", e)
        return []

    if not isinstance(res, dict):
        return []
    result = res.get("result")
    if not isinstance(result, dict):
        return []

    songs = result.get("songs")
    if not isinstance(songs, list):
        return []

    out: list[dict[str, Any]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        name = str(s.get("name") or s.get("title") or "")
        artists = s.get("artists") or []
        if isinstance(artists, list):
            artist = " / ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name"))
        else:
            artist = str(s.get("artist") or "")
        album = s.get("album") or {}
        album_name = str(album.get("name") or "") if isinstance(album, dict) else str(s.get("album_name") or "")
        duration_raw = s.get("duration") or 0
        try:
            dur = float(duration_raw)
            if dur > 10000:
                dur = dur / 1000.0
        except (ValueError, TypeError):
            dur = 0.0

        out.append({
            "song_id": sid_int,
            "id": sid_int,
            "song_name": name,
            "title": name,
            "name": name,
            "artist": artist,
            "album_name": album_name,
            "album": album_name,
            "duration": dur,
            "quality": "lossless",
        })
    return out


def _int_or_zero(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _ms_to_epoch_s(val: Any) -> int:
    """网易接口的毫秒时间戳 -> 秒；异常回落当前时间。"""
    n = _int_or_zero(val)
    if n > 0:
        return n // 1000
    return int(time.time())


def _map_playlist_summary(item: dict) -> dict:
    pid = _int_or_zero(item.get("id") or item.get("playlist_id"))
    return {
        "playlist_id": pid,
        "name": str(item.get("name") or ""),
        "cover_url": str(item.get("coverImgUrl") or item.get("cover_img_url") or ""),
        "track_count": _int_or_zero(item.get("trackCount") or item.get("track_count")),
        "created_at": _ms_to_epoch_s(item.get("createTime")),
        "updated_at": _ms_to_epoch_s(item.get("updateTime")),
        "user_id": _int_or_zero(item.get("userId") or item.get("user_id")),
        "special_type": _int_or_zero(item.get("specialType") or item.get("special_type")),
    }


def user_playlists(limit: int = 100) -> "dict[str, Any] | None":
    """当前登录账号的歌单列表（含自建与收藏，自建判定交给调用方按 user_id 过滤）。

    返回 {"uid": 账号id, "playlists": [摘要行]}；未登录/接口失败返回 None。
    """
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
            uid = 0
            for src in (info.get("account"), info.get("profile")):
                if isinstance(src, dict):
                    uid = _int_or_zero(src.get("id") or src.get("userId"))
                    if uid:
                        break
            if not uid:
                return None
            rows = api.user_playlist(uid, offset=0, limit=limit)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    playlists = []
    for it in rows:
        if isinstance(it, dict):
            mapped = _map_playlist_summary(it)
            if mapped["playlist_id"] > 0:
                playlists.append(mapped)
    return {"uid": uid, "playlists": playlists}


def playlist_track_ids(playlist_id: int) -> "list[int] | None":
    """歌单曲目 ID 列表（保持网易歌单内顺序）；未登录返回 None，空歌单返回 []。"""
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            raw = api.playlist_songlist(int(playlist_id))
    except Exception:
        return None
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for it in raw:
        sid = it if isinstance(it, int) else (it.get("id") if isinstance(it, dict) else None)
        sid = _int_or_zero(sid)
        if sid > 0:
            ids.append(sid)
    return ids


# ---------------------------------------------------------------------------
# 公共歌单数据层（#55，上游 javycoder/fnos_music_ext v2.9.0 整段移植）
#
# 只依赖 NEMbox 内部 API，全部四件已在生产容器实证（NEMbox 0.5.3）：
#   NEMbox.api.BASE_URL（https://music.163.com）
#   NetEase._raw_request(method, endpoint, data)  —— endpoint 收全 URL，返回 response
#   NetEase.request(method, path, params)        —— 内部拼 BASE_URL + weapi 加密，返回 dict
#   NetEase.eapi_request(path, params)           —— /api/ 前缀原样，返回 dict
#
# 与我方版本的差异（有意保留）：
#   - 不移植上游 vip_entitled()/_mark_needs_vip()：我方 [VIP] 展示在 proxy 层
#     （_vip_tag_prefix，纯 fee 判定），此处不重复造一套权益探测。
#   - _map_song_detail 增加 fee 字段（与上游一致），供 proxy 展示层使用。
# ---------------------------------------------------------------------------

def _public_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from NEMbox.api import BASE_URL

    with _api_lock:
        api = _get_api_locked()
        resp = api._raw_request("GET", f"{BASE_URL}{path}", params or {})
        resp.raise_for_status()
        return _validate_public_response(resp.json())


def _validate_public_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("code", 200) != 200:
        raise RuntimeError(f"netease public request failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    return data


def _public_post(path: str, params: dict[str, Any]) -> dict[str, Any]:
    with _api_lock:
        api = _get_api_locked()
        return _validate_public_response(api.request("POST", path, params))


def public_playlist_categories(kind: str = "playlist") -> dict[str, Any]:
    highquality_tags: list[dict[str, str]] = []
    if kind == "highquality":
        data = _public_get("/api/playlist/highquality/tags")
        tags = data.get("tags") if isinstance(data, dict) else []
        rows = []
        for item in tags if isinstance(tags, list) else []:
            tag = (item.get("playlistTag") or item) if isinstance(item, dict) else None
            if isinstance(tag, dict) and tag.get("name"):
                rows.append({"id": str(tag.get("name")), "name": str(tag.get("name"))})
        return {"ok": True, "categories": [{"id": "", "name": "全部"}], "tags": rows}

    data = _public_post("/weapi/playlist/catalogue", {})
    if not isinstance(data, dict) or data.get("code") != 200:
        raise RuntimeError(f"netease catalogue failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    names = data.get("categories") or {}
    grouped: dict[str, list[dict]] = {}
    for raw in data.get("sub") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        group = str(names.get(str(raw.get("category")), "其他"))
        grouped.setdefault(group, []).append({"id": str(raw["name"]), "name": str(raw["name"])})
    categories = [{"id": "", "name": "全部"}]
    categories.extend({"id": name, "name": name} for name in grouped)
    try:
        high_rows = public_playlist_categories("highquality").get("tags") or []
        highquality_tags.extend(high_rows)
    except Exception:
        pass
    return {"ok": True, "categories": categories, "tags": grouped, "highquality_tags": highquality_tags}


# ==== 排行榜分类（#55 后续修复） ====
# 上游 v2.9.0 的 charts 通道把分类写死成「全部」：/api/toplist 是 64 条扁平榜单，
# WebUI 的分类下拉因此只有一个选项、形同虚设（用户实测反馈）。
# 榜单行本身带两个可用字段：
#   ToplistType —— S/N/O/H 为官方四榜（飙升/新歌/原创/热歌），其余为空；
#   tags        —— 特色榜的分类标签（华语/欧美/流行/电子/说唱/古典…）。
# 按这两者归类即可给出真实分类，且分类筛选在前端只影响列表过滤，不改任何上游调用。
_CHART_OFFICIAL_TYPES = frozenset(("S", "N", "O", "H"))

# 标签展示名映射：网易给一批特色榜打的 tag 就是「榜单」（实测 16 个），
# 直接当分类名会出现「分类→榜单」这种歧义，展示为「特色榜」。
_CHART_TAG_LABELS = {"榜单": "特色榜"}


def _toplist_rows() -> list[dict[str, Any]]:
    """拉取网易榜单全集（/api/toplist 的 list 字段）。"""
    data = _public_get("/api/toplist")
    rows = data.get("list") if isinstance(data, dict) else []
    return [row for row in rows or [] if isinstance(row, dict) and row.get("id")]


def _chart_is_official(row: dict[str, Any]) -> bool:
    return str(row.get("ToplistType") or "").strip().upper() in _CHART_OFFICIAL_TYPES


def public_chart_categories() -> list[dict[str, Any]]:
    """排行榜分类分组（供 WebUI 分类下拉渲染）。

    返回 [{id: "", name: "榜单分类", list: [{id, name}, ...]}]，
    id 约定："official" = 官方榜；"tag:<标签>" = 该标签下的特色榜。
    标签按榜单数量降序，数量相同按名称排序，保证下拉顺序稳定。
    """
    counts: dict[str, int] = {}
    for row in _toplist_rows():
        if _chart_is_official(row):
            continue
        for tag in row.get("tags") or []:
            name = str(tag or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    options = [{"id": "official", "name": "官方榜"}]
    for name, _count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        options.append({"id": "tag:" + name, "name": _CHART_TAG_LABELS.get(name, name)})
    return [{"id": "", "name": "榜单分类", "list": options}]


def _chart_matches(row: dict[str, Any], category: str) -> bool:
    """榜单是否命中分类筛选。

    约定：空分类 = 全部；无法识别的分类值也按全部处理（避免旧书签/过期参数
    给出空列表）；"official" 精确匹配官方四榜；"tag:<标签>" 是精确筛选，
    标签不存在时返回 False（用户明确在按该标签过滤）。
    """
    cat = str(category or "").strip()
    if not cat:
        return True
    if cat == "official":
        return _chart_is_official(row)
    if cat.startswith("tag:"):
        want = cat[len("tag:"):].strip()
        tags = [str(tag or "").strip() for tag in (row.get("tags") or [])]
        return bool(want) and want in tags
    return True


def _public_summary(item: dict[str, Any], kind: str = "playlist") -> dict[str, Any]:
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    cover = str(
        item.get("coverImgUrl") or item.get("cover_url")
        or item.get("picUrl") or item.get("cover") or ""
    )
    try:
        updated = int(item.get("updateTime") or item.get("trackNumberUpdateTime") or 0) // 1000
    except (TypeError, ValueError):
        updated = 0
    return {
        "id": str(item.get("id") or ""),
        "kind": kind,
        "source": "netease",
        "name": str(item.get("name") or ""),
        "cover_url": cover,
        "track_count": _int_or_zero(item.get("trackCount") or item.get("trackcount")),
        "author": str(item.get("nickname") or creator.get("nickname") or ""),
        "description": str(item.get("description") or item.get("copywriter") or ""),
        "updated_at": updated,
    }


def public_playlists(
    channel: str,
    category: str = "",
    sort: str = "hot",
    page: int = 1,
    limit: int = 30,
    cursor: str = "",
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 50))
    if channel == "charts":
        # 分类筛选：官方榜 / tag:<标签> / 空=全部（见 _chart_matches）
        rows = [row for row in _toplist_rows() if _chart_matches(row, category)]
        items = [_public_summary(row, "chart") for row in rows]
        return {"ok": True, "items": items, "total": len(items), "has_more": False}

    if channel == "recommend":
        data = _public_get("/api/personalized/playlist", {"limit": min(limit, 30)})
        rows = data.get("result") if isinstance(data, dict) else []
        return {"ok": True, "items": [_public_summary(row) for row in rows or [] if isinstance(row, dict) and row.get("id")], "has_more": False}

    if channel == "account_recommend":
        with _api_lock:
            api = _get_api_locked()
            raw = api.request("POST", "/weapi/v1/discovery/recommend/resource", {})
            if isinstance(raw, dict) and raw.get("code") == 301:
                return {"ok": False, "error": "not_logged_in", "logged_in": False}
            _validate_public_response(raw)
            rows = raw.get("recommend") or []
        if not isinstance(rows, list):
            raise RuntimeError("netease account recommend returned invalid payload")
        return {"ok": True, "items": [_public_summary(row) for row in rows if isinstance(row, dict) and row.get("id")], "has_more": False}

    if channel == "highquality":
        params: dict[str, Any] = {"limit": limit}
        if category:
            params["cat"] = category
        if cursor:
            params["before"] = cursor
        data = _public_get("/api/playlist/highquality/list", params)
        rows = data.get("playlists") if isinstance(data, dict) else []
        next_cursor = ""
        if rows:
            last = rows[-1] if isinstance(rows[-1], dict) else {}
            next_cursor = str(last.get("updateTime") or "")
        return {
            "ok": True,
            "items": [_public_summary(row) for row in rows or [] if isinstance(row, dict) and row.get("id")],
            "total": _int_or_zero(data.get("total")),
            "has_more": bool(data.get("more")) if isinstance(data, dict) else bool(rows),
            "next_cursor": next_cursor,
        }

    data = _public_post("/weapi/playlist/list", {
        "cat": category or "全部",
        "order": sort if sort in ("hot", "new") else "hot",
        "offset": (page - 1) * limit,
        "total": "true",
        "limit": limit,
    })
    if not isinstance(data, dict) or data.get("code") != 200:
        raise RuntimeError(f"netease playlists failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    rows = data.get("playlists") or []
    total = _int_or_zero(data.get("total"))
    return {
        "ok": True,
        "items": [_public_summary(row) for row in rows if isinstance(row, dict) and row.get("id")],
        "total": total,
        "has_more": bool(data.get("more")) or (page * limit < total if total else bool(rows)),
    }


def public_playlist(playlist_id: int, kind: str = "playlist") -> dict[str, Any]:
    if kind == "chart":
        rows = public_playlists("charts").get("items") or []
        for row in rows:
            if row.get("id") == str(playlist_id):
                return {"ok": True, "data": row}
    with _api_lock:
        api = _get_api_locked()
        raw = api.eapi_request("/api/v6/playlist/detail", {
            "id": int(playlist_id), "n": 0, "s": 0,
        })
    playlist = raw.get("playlist") if isinstance(raw, dict) else None
    if not isinstance(playlist, dict) or not playlist.get("id"):
        raise RuntimeError(f"playlist {playlist_id} not found")
    return {"ok": True, "data": _public_summary(playlist, "chart" if kind == "chart" else "playlist")}


def public_playlist_tracks(playlist_id: int, kind: str = "playlist") -> dict[str, Any]:
    with _api_lock:
        api = _get_api_locked()
        raw = api.eapi_request("/api/v6/playlist/detail", {
            "id": int(playlist_id), "n": 1000, "s": 0,
        })
    playlist = raw.get("playlist") if isinstance(raw, dict) else None
    if not isinstance(playlist, dict) or not isinstance(playlist.get("trackIds"), list):
        raise RuntimeError(f"playlist {playlist_id} tracks unavailable")
    track_ids_raw = playlist["trackIds"]
    ids: list[int] = []
    seen: set[int] = set()
    truncated = False
    for item in track_ids_raw or []:
        sid = item if isinstance(item, int) else (item.get("id") if isinstance(item, dict) else 0)
        sid = _int_or_zero(sid)
        if sid <= 0 or sid in seen:
            continue
        if len(ids) >= 1000:
            truncated = True
            break
        seen.add(sid)
        ids.append(sid)
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), 100):
        # playable_only=False（我方适配）：上游此函数不过滤可播（v2.8.0 解耦），
        # 而我方 batch_song_details 默认可播过滤会给公共歌单榜单丢歌，
        # 导致下面的完整性校验必然失败。其余调用方行为不变。
        rows.extend(batch_song_details(ids[start:start + 100], playable_only=False))
    by_id = {int(row["song_id"]): row for row in rows}
    if any(sid not in by_id for sid in ids):
        raise RuntimeError("netease playlist song details incomplete")
    total = max(len(track_ids_raw), _int_or_zero(playlist.get("trackCount")))
    return {"ok": True, "data": [by_id[sid] for sid in ids], "total": total, "truncated": truncated or total > 1000}
