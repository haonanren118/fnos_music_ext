"""lxmusic-service/lxserver_client.py
lxserver (XCQ0607/lxserver) HTTP API 客户端与数据映射层。

职责：
1. 与容器内本地运行的 lxserver (默认 http://127.0.0.1:8005) 通信；
2. 带 x-frontend-auth 头部鉴权访问管理端点；
3. 将 lxserver 的音乐对象映射为 fnmusic-ext 契约格式 (lx:<src>:<id>)；
4. 维护 songInfo 双向映射缓存，确保 track/url 与 track/info 解析高命中率；
5. 封装自定义音源管理接口 (import/upload/delete/toggle/list/validate)。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
from typing import Any

import httpx

logger = logging.getLogger("fnmusic.lxserver")

DEFAULT_LXSERVER_URL = os.environ.get("LXSERVER_URL", "http://127.0.0.1:8005").rstrip("/")
DEFAULT_ADMIN_PASSWORD = os.environ.get("LXSERVER_ADMIN_PASSWORD", "123456")

# 平台别名归一
_SOURCE_ALIASES = {
    "kugou": "kg",
    "kg": "kg",
    "netease": "wy",
    "163": "wy",
    "wy": "wy",
    "migu": "mg",
    "mg": "mg",
    "qq": "tx",
    "tencent": "tx",
    "tx": "tx",
    "kuwo": "kw",
    "kw": "kw",
}

# 支持的平台集合
SUPPORTED_PLATFORMS = ("kw", "kg", "tx", "wy", "mg")


def normalize_source(src: str | None) -> str:
    if not src:
        return ""
    return _SOURCE_ALIASES.get(src.strip().lower(), "")


def parse_interval_to_seconds(interval: str | int | float | None) -> int:
    """解析 "05:24" 或 324 -> 324 秒。"""
    if interval is None:
        return 0
    if isinstance(interval, (int, float)):
        return max(0, int(interval))
    text = str(interval).strip()
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def extract_track_identifier(song: dict, source: str) -> str:
    """提取平台主键 identifier：保持与现有契约一致。
    kg: hash (或 songmid)
    kw: songmid (即 rid)
    wy: songmid (即 id)
    tx: songmid
    mg: copyrightId (或 songmid)
    """
    source = normalize_source(source or song.get("source"))
    # 酷狗优先用 hash
    if source == "kg":
        # 寻找最高音质的 hash 或顶层 hash
        for key in ("hash", "hash_flac", "hash_320k", "hash_128k"):
            val = song.get(key)
            if val:
                return str(val).lower()
        types = song.get("types") or []
        for t in types:
            if isinstance(t, dict) and t.get("hash"):
                return str(t["hash"]).lower()
    # 咪咕优先用 copyrightId
    if source == "mg":
        cid = song.get("copyrightId")
        if cid:
            return str(cid)
    # 默认用 songmid 或 id
    mid = song.get("songmid") or song.get("id") or song.get("rid")
    if mid:
        return str(mid)
    return ""


def map_lxserver_song(song: dict, fallback_source: str = "") -> dict:
    """将 lxserver song 对象映射为 fnmusic-ext 标准条目。"""
    source = normalize_source(song.get("source") or fallback_source)
    identifier = extract_track_identifier(song, source)
    if not identifier:
        identifier = str(song.get("songmid") or song.get("id") or int(time.time() * 1000))

    track_id = f"lx:{source}:{identifier}"
    duration_s = parse_interval_to_seconds(song.get("interval"))
    title = str(song.get("name") or song.get("title") or "未知曲目").strip()
    artist = str(song.get("singer") or song.get("artist") or "未知歌手").strip()
    album = str(song.get("albumName") or song.get("album") or "").strip()
    cover_url = str(song.get("img") or song.get("pic") or song.get("picUrl") or "").strip()

    # 提取音质与文件扩展名
    types_list = []
    types_raw = song.get("types")
    if isinstance(types_raw, list):
        for t in types_raw:
            if isinstance(t, dict) and t.get("type"):
                types_list.append(str(t["type"]))
            elif isinstance(t, str):
                types_list.append(t)
    elif isinstance(types_raw, dict):
        types_list = list(types_raw.keys())

    ext = "mp3"
    if "flac" in types_list or "flac24bit" in types_list or "hires" in types_list:
        ext = "flac"

    item: dict[str, Any] = {
        "id": track_id,
        "lx_source": source,
        "title": title,
        "artist": artist,
        "album": album,
        "duration_s": duration_s,
        "ext": ext,
        "cover_url": cover_url,
        "types": types_list,
        # 兼容旧搜索器字段
        "songmid": str(song.get("songmid") or identifier),
        "album_id": str(song.get("albumId") or ""),
    }

    # 针对酷狗保留 hash 映射
    if source == "kg":
        item["hash"] = identifier
        if isinstance(song.get("types"), list):
            for t in song["types"]:
                if isinstance(t, dict) and t.get("hash"):
                    q = t.get("type", "")
                    if q == "128k":
                        item["hash_128k"] = t["hash"]
                    elif q == "320k":
                        item["hash_320k"] = t["hash"]
                    elif "flac" in q:
                        item["hash_flac"] = t["hash"]

    # 针对咪咕保留 copyrightId
    if source == "mg":
        item["copyrightId"] = str(song.get("copyrightId") or identifier)

    return item


class LxServerClient:
    """与本地 lxserver 通信的 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_LXSERVER_URL,
        admin_password: str = DEFAULT_ADMIN_PASSWORD,
        timeout: float = 20.0,
        store_path: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_password = admin_password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._mgmt_lock: asyncio.Lock | None = None
        # songInfo 反查缓存: track_id -> songInfo dict
        self._song_info_cache: dict[str, dict] = {}
        self._cache_max = 2000
        self._song_list_locks: dict[str, asyncio.Lock] = {}
        # songInfo 磁盘持久化（issue #49）：热门歌单按天落盘而 songInfo 只在内存，
        # 服务重启后榜单曲目反查只能合成"未知曲目/未知歌手"，关键词型源脚本按
        # 歌名歌手取链必然失败——榜单播不了、搜索后能播。存储启用时启动加载、
        # 写入脏标记 + 短延迟合并落盘，条目顺序即新旧顺序（淘汰与落盘共用）。
        self._store_path = str(store_path or "").strip()
        self._store_dirty = False
        self._store_flush_task: asyncio.Task | None = None
        self._store_load()

    # ---------------------------------------------------------- songInfo 存储 --
    def _store_load(self) -> None:
        """启动时把磁盘 songInfo 载入内存缓存（文件序 = 最旧在前）。损坏/缺失静默重置。"""
        if not self._store_path:
            return
        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, dict):
                return
            loaded = 0
            for track_id, raw in items.items():
                if not isinstance(track_id, str) or not isinstance(raw, dict) or loaded >= self._cache_max:
                    break
                self._song_info_cache[track_id] = raw
                loaded += 1
            if loaded:
                logger.info("songInfo store loaded %d entries from %s", loaded, self._store_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("songInfo store load failed (%s): reset to empty", exc)
            self._song_info_cache.clear()

    def _store_flush_now(self) -> None:
        """同步落盘：tmp + rename 原子替换；目录缺失自动创建；失败仅留脏标记。"""
        if not self._store_path:
            return
        path = self._store_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {"version": 1, "items": self._song_info_cache}
            fd, tmp = tempfile.mkstemp(prefix=".songinfo-", dir=os.path.dirname(path) or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._store_dirty = False
        except Exception as exc:
            logger.warning("songInfo store flush failed: %s", exc)

    def _store_schedule_flush(self) -> None:
        """脏标记 + 短延迟合并落盘：榜单/搜索单次批量写入几十条，避免逐条 fsync。
        无运行事件循环（同步上下文）时只留脏标记，下次写入或 close 时补写。"""
        if not self._store_path or self._store_flush_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _flush():
            try:
                await asyncio.sleep(2.0)
                self._store_flush_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._store_flush_task = None

        self._store_flush_task = loop.create_task(_flush())

    def _store_trim(self) -> None:
        if len(self._song_info_cache) >= self._cache_max:
            for k in list(self._song_info_cache.keys())[: self._cache_max // 4]:
                self._song_info_cache.pop(k, None)

    @property
    def mgmt_lock(self) -> asyncio.Lock:
        if self._mgmt_lock is None:
            self._mgmt_lock = asyncio.Lock()
        return self._mgmt_lock

    def song_list_lock(self, source: str) -> asyncio.Lock:
        src = normalize_source(source)
        if not src:
            raise ValueError("unsupported source")
        lock = self._song_list_locks.setdefault(src, asyncio.Lock())
        return lock

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={
                    "User-Agent": "fnmusic-lxserver-client/1.0",
                    "x-frontend-auth": self.admin_password,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._store_flush_task is not None:
            self._store_flush_task.cancel()
            self._store_flush_task = None
        self.flush_song_info_store()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def cache_song_info(self, track_id: str, song_info: dict) -> None:
        if not track_id or not song_info:
            return
        self._store_trim()
        self._song_info_cache[track_id] = song_info
        if self._store_path:
            self._store_dirty = True
            self._store_schedule_flush()

    def get_cached_song_info(self, track_id: str) -> dict | None:
        return self._song_info_cache.get(track_id)

    def flush_song_info_store(self) -> None:
        """对外同步落盘入口（close 与测试用）：有脏数据才写。"""
        if self._store_path and self._store_dirty:
            self._store_flush_now()

    def synthesize_song_info(self, track_id: str, fallback_meta: dict | None = None) -> dict:
        """从 track_id 及元数据合成一个合法的洛雪 songInfo 对象。"""
        # 优先读取缓存
        cached = self.get_cached_song_info(track_id)
        if cached:
            return cached

        parts = track_id.split(":", 2)
        if len(parts) == 3 and parts[0] == "lx":
            source = normalize_source(parts[1])
            identifier = parts[2]
        elif len(parts) == 2:
            source = normalize_source(parts[0])
            identifier = parts[1]
        else:
            source = "kw"
            identifier = track_id

        meta = fallback_meta or {}
        name = meta.get("title") or meta.get("name") or "未知曲目"
        singer = meta.get("artist") or meta.get("singer") or "未知歌手"
        album_name = meta.get("album") or meta.get("albumName") or ""

        info: dict[str, Any] = {
            "name": name,
            "singer": singer,
            "source": source,
            "songmid": identifier,
            "albumName": album_name,
            "interval": meta.get("duration_s") or 0,
            "img": meta.get("cover_url") or "",
        }

        if source == "kg":
            info["hash"] = identifier
            info["albumId"] = meta.get("album_id") or ""
        elif source == "mg":
            info["copyrightId"] = identifier
        elif source == "kw":
            info["rid"] = identifier
            info["albumId"] = meta.get("album_id") or ""

        return info

    # ------------------------------------------------------------- 状态端点 --
    async def get_status(self) -> dict | None:
        """获取 lxserver 状态信息。"""
        try:
            client = await self.get_client()
            resp = await client.get("/api/status", timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("lxserver /api/status failed: %s", exc)
        return None

    async def is_alive(self) -> bool:
        status = await self.get_status()
        return status is not None

    # ------------------------------------------------------------- 搜索端点 --
    async def search(
        self,
        keyword: str,
        source: str = "kw",
        page: int = 1,
        pages: int = 1,
        limit: int = 20,
    ) -> list[dict]:
        """单平台搜索。返回已映射的标准 item 列表。"""
        src = normalize_source(source)
        if not src:
            return []
        client = await self.get_client()
        params = {
            "name": keyword,
            "source": src,
            "page": page,
            "pages": pages,
            "limit": limit,
        }
        resp = await client.get("/api/music/search", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning("lxserver search error %s for %s (%s)", resp.status_code, keyword, src)
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []

        results = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            item = map_lxserver_song(raw, fallback_source=src)
            track_id = item["id"]
            # 记录完整原始 songInfo 供后续反查解析
            self.cache_song_info(track_id, raw)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------- 播放链接解析 --
    async def get_music_url(
        self,
        song_info: dict,
        quality: str = "128k",
        exclude_api_sources: list[str] | None = None,
    ) -> dict | None:
        """调用 lxserver /api/music/url 解析直链。

        返回格式:
        {"url": "...", "type": "128k", "sourceName": "...", "sourceId": "...", ...}
        200 但无 url（脚本"成功"却拿不出直链，如平台未激活的静默空返回）时同样
        返回完整响应（url 为空串/缺失，含 attempts 供换源排除）；非 200 或解析
        异常才返回 None。
        """
        client = await self.get_client()
        body = {
            "songInfo": song_info,
            "quality": quality,
            "enableAutoSwitchApiSource": True,
        }
        if exclude_api_sources:
            body["excludeApiSources"] = exclude_api_sources
        resp = await client.post("/api/music/url", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning("lxserver get_music_url error %s: %.150s", resp.status_code, resp.text)
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data
        return None

    # ------------------------------------------------------------- 歌词端点 --
    async def get_lyric(self, song_info: dict) -> dict | None:
        """调用 lxserver /api/music/lyric 获取歌词。"""
        client = await self.get_client()
        source = song_info.get("source") or ""
        songmid = song_info.get("songmid") or song_info.get("id") or ""
        params = {
            "source": source,
            "songmid": songmid,
            "name": song_info.get("name") or "",
            "singer": song_info.get("singer") or "",
            "hash": song_info.get("hash") or "",
            "interval": song_info.get("interval") or "",
            "copyrightId": song_info.get("copyrightId") or "",
        }
        try:
            resp = await client.get("/api/music/lyric", params=params, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and (data.get("lyric") or data.get("lrc")):
                    return data
        except Exception:
            pass

        # 降级尝试 POST 接口
        body = {"songInfo": song_info}
        resp = await client.post("/api/music/lyric", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            if "promise" in data and isinstance(data["promise"], dict):
                return data["promise"]
            return data
        return None

    # ------------------------------------------------------------- 榜单端点 --
    async def get_leaderboard_boards(self, source: str = "kg") -> list[dict]:
        """获取平台的可用榜单列表。"""
        src = normalize_source(source) or "kg"
        async with self.song_list_lock(src):
            client = await self.get_client()
            resp = await client.get("/api/music/leaderboard/boards", params={"source": src}, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning(
                "lxserver leaderboard boards error %s for %s: %.150s", resp.status_code, src, resp.text,
            )
            return []
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
        if isinstance(data, list):
            return data
        return []

    async def get_song_list_tags(self, source: str) -> dict:
        src = normalize_source(source)
        if not src:
            raise ValueError("unsupported source")
        async with self.song_list_lock(src):
            client = await self.get_client()
            resp = await client.get("/api/music/songList/tags", params={"source": src}, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"lxserver tags error {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("lxserver tags returned invalid payload")
        return data

    async def get_song_lists(
        self, source: str, tag_id: str = "", sort_id: str = "", page: int = 1
    ) -> dict:
        src = normalize_source(source)
        if not src:
            raise ValueError("unsupported source")
        params = {"source": src, "tagId": tag_id or "", "sortId": sort_id or "", "page": max(1, int(page or 1))}
        async with self.song_list_lock(src):
            client = await self.get_client()
            resp = await client.get("/api/music/songList/list", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"lxserver song lists error {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("lxserver song lists returned invalid payload")
        return data

    async def search_song_lists(self, source: str, text: str, page: int = 1) -> dict:
        """歌单搜索（lxserver `/api/music/songList/search`，参数名是 `text` 不是 keyword）。

        lx-music 的「歌单」板块除分类歌单/排行榜外还有搜索，此前 WebUI 只暴露了前两者，
        洛雪面板的「通道」下拉因此只有 2 项。各平台 search 返回体已归一化为
        `{list:[{id,name,author,total,img,desc,source}], total, limit, source}`。
        """
        src = normalize_source(source)
        if not src:
            raise ValueError("unsupported source")
        keyword = str(text or "").strip()
        if not keyword:
            raise ValueError("keyword cannot be empty")
        async with self.song_list_lock(src):
            client = await self.get_client()
            resp = await client.get(
                "/api/music/songList/search",
                params={"source": src, "text": keyword, "page": max(1, int(page or 1))},
                timeout=self.timeout,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"lxserver song list search error {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("lxserver song list search returned invalid payload")
        return data

    async def fetch_lyric(self, source: str, song_id: str) -> str:
        """按平台+曲目 id 取歌词（lxserver `/api/music/lyric`，免登录可用）。

        为什么需要它：lxmusic 自己的 `*_resolve_lyric` 依赖本地 songInfo 缓存，
        而该缓存**只由搜索填充**。公共歌单/榜单注入的曲目从未经过搜索，
        于是各平台解析器全部拿不到歌词（实测 kw 榜单曲目 lyric 恒为空）。
        lxserver 直接回源，不依赖缓存，是这条路上唯一可靠的数据来源。
        """
        src = normalize_source(source)
        raw_id = str(song_id or "").strip()
        if not src or not raw_id:
            return ""
        # lxserver 会去掉 `<source>_` 前缀，这里直接传裸 id 更稳
        if raw_id.startswith(f"{src}_"):
            raw_id = raw_id[len(src) + 1:]
        try:
            client = await self.get_client()
            resp = await client.get(
                "/api/music/lyric",
                params={"source": src, "id": raw_id},
                timeout=self.timeout,
            )
        except Exception:
            return ""
        if resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("lyric") or "").strip()

    async def get_leaderboard_meta(self, source: str, board_id: str) -> dict:
        """单条榜单的 {total, cover_url}（只取第 1 页）。

        lxserver 的 leaderboard/boards 只给 `{id,name,bangid}`：**既没有曲目数、
        也没有榜图**。而 leaderboard/list 第 1 页同时带 `total` 和每首曲目的 `img`
        （实测 0.15~0.5s/条），因此：
          - total      → 榜单曲目数；
          - cover_url  → 借首页第一首的专辑图当榜单图标（各平台均为静态榜表，
                         本来就没有官方榜图，这是唯一可得且真实相关的图）。
        供榜单列表并发补数/补图（见 app.py 的 _chart_meta_for）。

        注意：**不抢 song_list_lock**。那个锁是为「整榜分页拉全量」准备的串行锁，
        这里只是单页只读查询；一旦共用，43 条榜单会被串行成 8~12 秒（实测），
        并发补数就失去意义。靠调用方的信号量限流（默认 8 并发）。
        """
        src = normalize_source(source)
        raw_id = str(board_id or "").strip()
        if not src or not raw_id:
            return {}
        client = await self.get_client()
        resp = await client.get(
            "/api/music/leaderboard/list",
            params={"source": src, "bangid": raw_id, "page": 1},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total <= 0:
            total = len(rows) or None
        cover = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate = str(row.get("img") or row.get("picUrl") or row.get("cover") or "").strip()
            if candidate.startswith("http"):
                cover = candidate
                break
        return {"total": total, "cover_url": cover}

    async def get_song_list_detail(self, source: str, playlist_id: str, page: int = 1) -> dict:
        src = normalize_source(source)
        raw_id = str(playlist_id or "")
        if not src or not raw_id:
            raise ValueError("invalid source or playlist id")
        params = {"source": src, "id": raw_id, "page": max(1, int(page or 1))}
        async with self.song_list_lock(src):
            client = await self.get_client()
            resp = await client.get("/api/music/songList/detail", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"lxserver song detail error {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("lxserver song detail returned invalid payload")
        return data

    @staticmethod
    def normalize_public_playlist(row: dict, source: str, kind: str = "playlist") -> dict:
        src = normalize_source(source)
        raw_id = str(
            (row.get("bangid") if kind == "chart" else None) or row.get("id") or row.get("specialId")
            or row.get("dissid") or row.get("playlist_id") or ""
        ).strip()
        # 名称/作者/简介里可能带 HTML 实体（QQ 歌单搜索实测返回
        # 「华语&#124;&#32;修复心情」这种），统一次解一次，否则界面显示裸实体。
        name = html.unescape(str(row.get("name") or row.get("title") or "")).strip()
        cover = str(row.get("img") or row.get("image") or row.get("pic") or row.get("cover") or "").strip()
        # 曲目数：lxserver 的 leaderboard/boards 只返回 {id,name,bangid}，没有曲目数 ——
        # 榜单列表此时返回 None（未知），不要谎报 0（WebUI 会显示「· 0 首」，
        # 用户看到整页「0 首」以为数据坏了）。分类歌单/搜索结果都带 total/songnum。
        raw_count = row.get("total") or row.get("songnum") or row.get("song_count")
        track_count: "int | None"
        if raw_count in (None, "", 0, "0"):
            track_count = None if kind == "chart" else 0
        else:
            try:
                track_count = int(raw_count)
            except (TypeError, ValueError):
                track_count = None if kind == "chart" else 0
        return {
            "id": raw_id,
            "kind": kind,
            "source": src,
            "name": name,
            "cover_url": cover,
            "track_count": track_count,
            "author": html.unescape(str(row.get("author") or row.get("nickname") or "")).strip(),
            "description": html.unescape(str(row.get("desc") or row.get("description") or "")).strip(),
            "updated_at": 0,
        }

    async def get_leaderboard_list(self, source: str, board_id: str, page: int = 1, *, strict: bool = False) -> list[dict]:
        """获取指定榜单内的歌曲列表并做数据映射。"""
        src = normalize_source(source)
        async with self.song_list_lock(src):
            client = await self.get_client()
            # lxserver 契约：榜单歌曲接口的参数名是 bangid（不是 id），缺失直接 400
            params = {"source": src, "bangid": board_id, "page": page}
            resp = await client.get("/api/music/leaderboard/list", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            if strict:
                raise RuntimeError(f"lxserver leaderboard list error {resp.status_code}")
            logger.warning(
                "lxserver leaderboard list error %s for %s/%s: %.150s",
                resp.status_code, src, board_id, resp.text,
            )
            return []
        data = resp.json()
        raw_list = []
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            raw_list = data["list"]
        elif isinstance(data, list):
            raw_list = data
        elif strict:
            raise RuntimeError("lxserver leaderboard returned invalid payload")

        items = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            item = map_lxserver_song(raw, fallback_source=src)
            self.cache_song_info(item["id"], raw)
            items.append(item)
        return items

    # ------------------------------------------------------------- 自定义音源管理 --
    async def list_custom_sources(self) -> list[dict]:
        """获取所有已导入的自定义音源列表。"""
        client = await self.get_client()
        resp = await client.get("/api/custom-source/list", timeout=self.timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data["sources"]
        return []

    async def import_custom_source(self, url: str) -> dict:
        """从 URL 导入自定义源。lxserver 端点期望 JSON {url}，成功返回 {success,id,metadata,...}。

        allowUnsafeVM=True 代答 lxserver 的"原生 VM 模式"确认（pdone/lx-music-source
        等聚合类脚本需要原生 VM 才能运行；部署侧已由 entrypoint 将
        system.allowUnsafeVM 置 true，服务端全局开关仍兜底）。"""
        client = await self.get_client()
        resp = await client.post(
            "/api/custom-source/import",
            json={"url": url, "allowUnsafeVM": True},
            timeout=self.timeout,
        )
        data = self._parse_admin_resp(resp, "导入自定义源")
        return data

    async def upload_custom_source(self, filename: str, script_content: str) -> dict:
        """上传自定义源脚本文本。lxserver 端点期望 JSON {filename, content}，成功返回 {success,id,metadata,...}。

        allowUnsafeVM=True 代答"原生 VM 模式"确认，否则需要 VM 的脚本（如聚合
        API 类）上传永远收到 requireUnsafe 确认文案而被 500 挡下（2026-10-11
        pdone/lx-music-source 聚合API接口 实测）。"""
        client = await self.get_client()
        resp = await client.post(
            "/api/custom-source/upload",
            json={"filename": filename, "content": script_content, "allowUnsafeVM": True},
            timeout=self.timeout,
        )
        return self._parse_admin_resp(resp, "上传自定义源")

    async def toggle_custom_source(self, source_id: str, enable: bool) -> bool:
        """启用或禁用某音源（lxserver 端点键名为 enabled）。"""
        client = await self.get_client()
        resp = await client.post(
            "/api/custom-source/toggle",
            json={"id": source_id, "enabled": bool(enable)},
            timeout=self.timeout,
        )
        data = self._parse_admin_resp(resp, f"切换自定义源 {source_id}")
        return bool(data.get("success", True))

    async def delete_custom_source(self, source_id: str) -> bool:
        """删除某音源。"""
        client = await self.get_client()
        resp = await client.post("/api/custom-source/delete", json={"id": source_id}, timeout=self.timeout)
        data = self._parse_admin_resp(resp, f"删除自定义源 {source_id}")
        return bool(data.get("success", True))

    @staticmethod
    def _parse_admin_resp(resp: httpx.Response, action: str) -> dict:
        """lxserver 管理端点约定：HTTP 200 + {success: true} 才算成功，
        失败时（HTTP 500/403 或 success=false）抛 RuntimeError 带服务端错误文案。"""
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code == 200 and data.get("success") is not False:
            return data
        message = data.get("error") or data.get("message") or f"{action}失败 (HTTP {resp.status_code})"
        raise RuntimeError(message)

    async def set_source_enabled(self, target_id_or_name: str, enabled: bool) -> bool:
        """设置单个源的启用状态（多源可同时启用）。若 target 不存在则直接返回 False，不触碰任何源。"""
        async with self.mgmt_lock:
            sources = await self.list_custom_sources()
            target = next(
                (
                    s
                    for s in sources
                    if str(s.get("id") or "") == target_id_or_name
                    or str(s.get("name") or "") == target_id_or_name
                ),
                None,
            )
            if not target:
                return False

            target_id = str(target.get("id") or "")
            if not target_id:
                return False
            await self.toggle_custom_source(target_id, enabled)
            return True
