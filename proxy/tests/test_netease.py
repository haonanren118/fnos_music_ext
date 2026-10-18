"""Tests for fnmusic-ext netease (musicbox) integration, fast search, and pagination."""
import asyncio
import os
import time
import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import (
    fake_official_guid,
    app,
    CONF,
    _SEARCH_CACHE,
    _set_search_cache,
    _clean_search_cache,
    fetch_musicbox_search,
    _online_info,
    resolve_netease_url,
    resolve_online_lyric,
    find_cache_file,
)


@pytest.fixture(autouse=True)
def setup_netease_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    cache_dir = str(tmp_path / "cache")
    library_dir = str(tmp_path / "library")
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(library_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", library_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "netease_wait_s", 2.5)
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    monkeypatch.setitem(CONF, "search_cache_ttl", 300.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)


# =========================================================================
# 1. netease 搜索映射 (quality SQ→flac、LD→mp3; 字段映射正确)
# =========================================================================
@pytest.mark.anyio
async def test_fetch_musicbox_search_mapping():
    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            assert request.url.params.get("keyword") == "七里香"
            assert request.url.params.get("limit") == "10"
            assert request.url.params.get("type") == "song"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": "186016",
                            "song_name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 299,
                            "quality": "SQ 2.4M",
                        },
                        {
                            "song_id": "186017",
                            "song_name": "借口",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 258,
                            "quality": "LD 128k",
                        },
                        {
                            "song_id": "186018",
                            "song_name": "搁浅",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 200,
                            "quality": "HR 24bit",
                        },
                        {
                            "song_id": "186019",
                            "song_name": "园游会",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 240,
                            "quality": "无损",
                        },
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            assert request.url.params.get("ids") == "186016,186017,186018,186019"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": 186016,
                            "name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "album_pic_url": "https://img.test/qlx.jpg",
                            "duration_ms": 299000,
                            "has_sq": True,
                            "has_hr": False,
                        },
                        {
                            "song_id": 186017,
                            "name": "借口",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "album_pic_url": "https://img.test/jk.jpg",
                            "duration_ms": 258000,
                            "has_sq": False,
                            "has_hr": False,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_musicbox_search(client, "七里香", 10)
    assert items is not None
    assert len(items) == 4

    # SQ -> flac, cover_url 补全
    assert items[0]["id"] == "netease:186016"
    assert items[0]["source"] == "netease"
    assert items[0]["title"] == "七里香"
    assert items[0]["artist"] == "周杰伦"
    assert items[0]["album"] == "七里香"
    assert items[0]["duration_s"] == 299.0
    assert items[0]["ext"] == "flac"
    assert items[0]["cover_url"] == "https://img.test/qlx.jpg"
    assert items[0]["lyric"] == ""

    # LD -> mp3, cover_url 补全
    assert items[1]["id"] == "netease:186017"
    assert items[1]["ext"] == "mp3"
    assert items[1]["cover_url"] == "https://img.test/jk.jpg"

    # HR -> flac
    assert items[2]["id"] == "netease:186018"
    assert items[2]["ext"] == "flac"
    assert items[2]["cover_url"] == ""

    # 无损 -> flac
    assert items[3]["id"] == "netease:186019"
    assert items[3]["ext"] == "flac"
    assert items[3]["cover_url"] == ""


@pytest.mark.anyio
async def test_fetch_musicbox_search_error_handling():
    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(err_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_musicbox_search(client, "fail", 10)
    assert items is None
    assert await fetch_musicbox_search(client, "", 10) is None


@pytest.mark.anyio
async def test_fetch_musicbox_search_detail_failure_fallback():
    """songs/detail 挂掉（500）时 cover_url 留空，主结果仍在。"""
    def search_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": "186016",
                            "song_name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 299,
                            "quality": "LD 128k",
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(search_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_musicbox_search(client, "七里香", 10)
    assert items is not None
    assert len(items) == 1
    assert items[0]["id"] == "netease:186016"
    assert items[0]["title"] == "七里香"
    assert items[0]["cover_url"] == ""
    assert items[0]["ext"] == "mp3"


# =========================================================================
# 2. netease info 映射 (ar 多歌手 join、sq null → mp3、dt 毫秒→秒)
# =========================================================================
@pytest.mark.anyio
async def test_online_info_netease_mapping():
    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "说好不哭",
                        "ar": [{"name": "周杰伦"}, {"name": "阿信"}],
                        "al": {"name": "说好不哭", "picUrl": "http://img.test/shbk.jpg"},
                        "dt": 222000,
                        "sq": {"size": 25000000, "br": 999000},
                        "h": {"size": 9000000, "br": 320000},
                        "hr": None,
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/lyric":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "lyric": "[00:00.00]说好不哭\n[00:10.00]周杰伦",
                        "tlyric": "",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186017/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "普通音质曲",
                        "ar": [{"name": "单歌手"}],
                        "al": {"name": "专辑名", "picUrl": "http://img.test/pt.jpg"},
                        "dt": 180000,
                        "sq": None,
                        "hr": None,
                        "h": {"size": 4500000, "br": 320000},
                    },
                },
            )
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    req = httpx.Request("GET", "http://testserver")
    # 构造 Request 模拟
    from starlette.requests import Request as StarletteRequest
    scope = {"type": "http", "app": app}
    req_obj = StarletteRequest(scope)

    info_sq = await _online_info(req_obj, "online:netease:186016")
    assert info_sq is not None
    assert info_sq["id"] == "netease:186016"
    assert info_sq["source"] == "netease"
    assert info_sq["title"] == "说好不哭"
    assert info_sq["artist"] == "周杰伦 / 阿信"
    assert info_sq["album"] == "说好不哭"
    assert info_sq["cover_url"] == "http://img.test/shbk.jpg"
    assert info_sq["duration_s"] == 222.0
    assert info_sq["ext"] == "flac"
    assert info_sq["file_size"] == 25000000
    assert info_sq["lyric"] == "[00:00.00]说好不哭\n[00:10.00]周杰伦"

    # sq is None -> ext=mp3
    info_mp3 = await _online_info(req_obj, "online:netease:186017")
    assert info_mp3 is not None
    assert info_mp3["artist"] == "单歌手"
    assert info_mp3["duration_s"] == 180.0
    assert info_mp3["ext"] == "mp3"
    assert info_mp3["file_size"] == 4500000


# =========================================================================
# 3. resolve_netease_url 降级链 (lossless 404/code!=200 → exhigh 成功；全失败 None)
# =========================================================================
@pytest.mark.anyio
async def test_resolve_netease_url_downgrade():
    def downgrade_handler(request: httpx.Request) -> httpx.Response:
        quality = request.url.params.get("quality")
        if quality == "lossless":
            # lossless 返回 code 404 或无版权
            return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
        if quality == "exhigh":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"code": 200, "url": "http://audio.test/exhigh.mp3"}},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(downgrade_handler), base_url="http://127.0.0.1:8770"
    )
    url = await resolve_netease_url(client, "186016")
    assert url == "http://audio.test/exhigh.mp3"


@pytest.mark.anyio
async def test_resolve_netease_url_all_fail():
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8770"
    )
    url = await resolve_netease_url(client, "186016")
    assert url is None


# =========================================================================
# 4. stream_track netease 分支 (直链 206 透传/tee 落盘，缓存命中直接 206 不打上游)
# =========================================================================
def test_stream_track_netease_direct_stream_and_cache(tmp_path, monkeypatch):
    audio_content = b"FLAC_MAGIC_HEADER_TEST_AUDIO_CONTENT" * 40
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not hit upstream")

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    # Mock httpx.AsyncClient for direct stream
    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:186016")
        assert resp.status_code == 200
        assert resp.content == audio_content
        assert resp.headers.get("content-type") == "audio/flac"

        # 检查曲库落盘
        saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        # 缓存命中测试：第二次播放直接读本地文件，不请求上游
        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:netease:186016",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == audio_content[:10]


def test_stream_track_early_disconnect_background_tee(tmp_path, monkeypatch):
    """Bug1 回归测试：客户端提前断开（流式只读少量 chunk 后 break），后台 task 仍能完整落盘转正，且无 .part 残留。"""
    audio_content = b"FLAC_STREAM_TEST_CHUNK_PAYLOAD_PADDING_" * 128  # 5120 字节
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/early_disconnect.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")

    with TestClient(app) as client:
        # 客户端只消费少量 chunk 后提前 break 断开连接
        with client.stream("GET", "/music/api/v1/track/stream?guid=online:netease:186016") as r:
            assert r.status_code == 200
            for chunk in r.iter_bytes(chunk_size=128):
                break

        # 轮询等待后台 task 完成，最多 5s
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if os.path.exists(saved_file):
                break
            time.sleep(0.05)

        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        # 断言无 .part 残留
        parts = [f for f in os.listdir(CONF["library_dir"]) if f.endswith(".part")]
        assert len(parts) == 0


def test_stream_track_netease_mpeg_content_type_override_to_flac(tmp_path, monkeypatch):
    """Bug2 回归测试：网易 CDN 返回 content-type audio/mpeg 但 info 有 sq → 落盘为 .flac 且响应 content-type 为 audio/flac。"""
    audio_content = b"FLAC_AUDIO_CONTENT_WITH_MPEG_HEADER" * 40
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            # 网易 CDN 误返回 audio/mpeg
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:186016")
        assert resp.status_code == 200
        assert resp.content == audio_content
        # 响应头 content-type 被修正为 audio/flac
        assert resp.headers.get("content-type") == "audio/flac"

        # 检查曲库落盘文件为 .flac，而不是 .mp3
        saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        assert not os.path.exists(os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3"))


def test_stream_track_netease_range_no_cache_no_coroutine_warning(tmp_path, monkeypatch):
    """带 Range (bytes=100-200) 时 should_cache 为 False，不应触发 'coroutine was never awaited' RuntimeWarning。"""
    audio_content = b"FLAC_MAGIC_HEADER_TEST_AUDIO_CONTENT" * 40
    range_slice = audio_content[100:201]

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            assert request.headers.get("range") == "bytes=100-200"
            return httpx.Response(
                206,
                content=range_slice,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Range": f"bytes 100-200/{len(audio_content)}",
                    "Content-Length": str(len(range_slice)),
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    import warnings
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        with TestClient(app) as client:
            resp = client.get(
                "/music/api/v1/track/stream?guid=online:netease:186016",
                headers={"Range": "bytes=100-200"},
            )
            assert resp.status_code == 206
            assert resp.content == range_slice

    # 确认没有 coroutine was never awaited 警告
    runtime_warnings = [
        w for w in record if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert len(runtime_warnings) == 0


def test_stream_track_netease_unavailable_404():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:999999")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": 404,
            "msg": "online source unavailable",
            "data": None,
        }


# =========================================================================
# 5. search_track 分页：page=1 快返回、total 抬升、page=2 合并缓存且不重复、TTL 过期
# =========================================================================
def test_search_track_pagination_and_cache_ttl(monkeypatch):
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "OK",
                    "data": {
                        "list": [
                            {
                                "guid": "local:101",
                                "title": "夜曲",
                                "artist": "周杰伦",
                                "album": "十一月的萧邦",
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        return httpx.Response(
            200,
            json={"code": 0, "msg": "OK", "data": {"list": [], "total": 1}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": f"mb_{i}",
                        "song_name": f"网易歌曲_{i}",
                        "artist": "歌手A",
                        "album_name": "专辑A",
                        "duration": 200,
                        "quality": "SQ",
                    }
                    for i in range(1, 13)  # 12 首
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": f"kuwo:kw_{i}",
                        "source": "kuwo",
                        "title": f"酷我歌曲_{i}",
                        "artist": "歌手B",
                        "album": "专辑B",
                        "duration_s": 200,
                        "ext": "flac",
                    }
                    for i in range(1, 4)  # 3 首
                ],
            },
        )

    monkeypatch.setitem(CONF, "online_limit", 10)
    monkeypatch.setitem(CONF, "search_cache_ttl", 2.0)  # 短 TTL 测试过期

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # page=1 请求（本地优先布局：本地条目后紧跟全部在线条目，直到装满 size）
        resp1 = client.get("/music/api/v1/search/track?q=周杰伦&page=1&size=50")
        assert resp1.status_code == 200
        data1 = resp1.json()["data"]
        # total 抬升：本地 1 + 在线 15 (12 mb + 3 mdl) = 16
        assert data1["total"] == 16
        # page=1 = 本地 1 条 + 全部 15 条在线
        list1 = data1["list"]
        assert len(list1) == 16
        assert list1[0]["guid"] == "local:101"
        # 在线段内按源轮转（_spread_sources），不锁定下标顺序；只校验集合齐全
        online_guids = {it["guid"] for it in list1[1:]}
        expected_online = {
            fake_official_guid("online:netease:mb_%d" % i) for i in range(1, 13)
        } | {fake_official_guid("online:kuwo:kw_%d" % i) for i in range(1, 4)}
        assert online_guids == expected_online

        # page=2 请求（在线条目已在 page=1 装满，page=2 在线切片为空）
        resp2 = client.get("/music/api/v1/search/track?q=周杰伦&page=2&size=50")
        assert resp2.status_code == 200
        data2 = resp2.json()["data"]
        assert data2["total"] == 16
        list2 = data2["list"]
        assert len(list2) == 0

        # 断言 page=2 与 page=1 的在线条目无重叠
        guids1 = {it["guid"] for it in list1[1:]}
        guids2 = {it["guid"] for it in list2}
        assert guids1.isdisjoint(guids2)

        # page=3 请求（同为空切片）
        resp3_page = client.get("/music/api/v1/search/track?q=周杰伦&page=3&size=50")
        assert resp3_page.status_code == 200
        data3 = resp3_page.json()["data"]
        assert data3["total"] == 16
        list3 = data3["list"]
        assert len(list3) == 0
        guids3 = {it["guid"] for it in list3}
        assert guids2.isdisjoint(guids3)

        # 等待 TTL 过期
        time.sleep(2.1)
        entry = next(e for e in _SEARCH_CACHE.values() if e.get("keyword") == "周杰伦")
        assert time.time() - entry["ts"] >= 2.0
        # Revalidate this credential/config session without replacing its shown prefix.
        resp3 = client.get("/music/api/v1/search/track?q=周杰伦&page=1&size=50")
        assert resp3.status_code == 200
        assert resp3.json()["data"]["total"] == 16


# =========================================================================
# 6. 缓存淘汰 (>200 清理)
# =========================================================================
def test_search_cache_eviction():
    _SEARCH_CACHE.clear()
    now = time.time()

    # 填充 2005 条缓存
    for i in range(2005):
        _set_search_cache(f"kw_{i}", {"items": [f"item_{i}"], "ts": now + i, "task": None})

    # 断言超过 2000 时，清理掉最旧的一半
    assert len(_SEARCH_CACHE) <= 2000
    assert "kw_0" not in _SEARCH_CACHE
    assert "kw_500" not in _SEARCH_CACHE
    assert "kw_2004" in _SEARCH_CACHE


# =========================================================================
# 7. healthz 探测
# =========================================================================
def test_healthz_netease_probe(monkeypatch):
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        # musicbox 正常
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["musicbox"] == "ok"

    # musicbox 异常 (不影响整体 ok)
    def musicbox_fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_fail_handler), base_url="http://127.0.0.1:8770"
    )
    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["musicbox"] == "fail"

    # netease_enabled = False -> disabled
    monkeypatch.setitem(CONF, "netease_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["musicbox"] == "disabled"


# =========================================================================
# 8. TASK4: 歌词 + 封面 + 搜索量测试
# =========================================================================
@pytest.mark.anyio
async def test_resolve_online_lyric_netease(tmp_path, monkeypatch):
    """resolve_online_lyric netease：mock lyric 端点返回 LRC，断言写入缓存文件且二次调用不再请求远端；空歌词不写缓存返回 ""。"""
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))

    lyric_calls = {"n": 0}

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/lyric":
            lyric_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "lyric": "[00:01.00]七里香歌词第一行\n[00:05.00]第二行\n",
                        "tlyric": "[00:01.00]翻译行\n",
                    },
                },
            )
        if request.url.path == "/api/v1/song/empty_song/lyric":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"lyric": "", "tlyric": ""}},
            )
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    from starlette.requests import Request as StarletteRequest
    scope = {"type": "http", "app": app}
    req_obj = StarletteRequest(scope)

    # 首次调用：请求远端端点，拿到 lyric（保持原文不合并 tlyric），写入缓存
    text1 = await resolve_online_lyric(req_obj, "online:netease:186016")
    assert text1 == "[00:01.00]七里香歌词第一行\n[00:05.00]第二行"
    assert lyric_calls["n"] == 1

    # 二次调用：命中本地缓存，不再请求远端
    text2 = await resolve_online_lyric(req_obj, "online:netease:186016")
    assert text2 == "[00:01.00]七里香歌词第一行\n[00:05.00]第二行"
    assert lyric_calls["n"] == 1

    # 空歌词：不写缓存返回 ""
    text_empty = await resolve_online_lyric(req_obj, "online:netease:empty_song")
    assert text_empty == ""
    cache_file_empty = os.path.join(cache_dir, "online_netease_empty_song.lrc")
    assert not os.path.exists(cache_file_empty)


def test_search_volume_and_default_limits(monkeypatch):
    """搜索量：断言 fetch_musicbox_search 请求参数 limit=50（netease_search_limit），page=1 在线条目最多 30 条。"""
    captured_limits = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            limit = request.url.params.get("limit")
            captured_limits.append(limit)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": f"mb_{i}",
                            "song_name": f"网易歌曲_{i}",
                            "artist": "歌手A",
                            "album_name": "专辑A",
                            "duration": 200,
                            "quality": "SQ",
                        }
                        for i in range(1, 41)  # 返回 40 首
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"list": []}})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=测试&page=1&size=50")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # netease_search_limit = 50
        assert "50" in captured_limits
        # 本地优先布局：本地 0 条，首页装满 size=50；musicbox 返回 40 首全部展示
        assert len(data["list"]) == 40
        assert data["list"][0]["guid"] == fake_official_guid("online:netease:mb_1")
        assert data["list"][39]["guid"] == fake_official_guid("online:netease:mb_40")
        # total 为 40
        assert data["total"] == 40


def test_healthz_musicdl_only(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()
        assert rj["ok"] is True
        assert rj["musicdl"] == "ok"
        assert rj["musicbox"] == "disabled"


def test_healthz_musicbox_only(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "netease_enabled", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()
        assert rj["ok"] is True
        assert rj["musicdl"] == "disabled"
        assert rj["musicbox"] == "ok"


def test_healthz_both_sources_down_is_unhealthy(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()
        assert rj["ok"] is False
        assert rj["upstream"] == "ok"
        assert rj["musicdl"] == "fail"
        assert rj["musicbox"] == "fail"


def test_search_musicdl_only_skips_musicbox(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", False)
    called = {"musicbox": 0}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        called["musicbox"] += 1
        return httpx.Response(200, json={"ok": True, "data": []})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "duration_s": 269,
                        "ext": "mp3",
                    }
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        items = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == fake_official_guid("online:migu:1")
        assert called["musicbox"] == 0


def test_search_musicbox_only_skips_musicdl(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    called = {"musicdl": 0}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        called["musicdl"] += 1
        return httpx.Response(200, json={"ok": True, "items": []})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": "228908",
                            "song_name": "晴天",
                            "artist": "周杰伦",
                            "album_name": "叶惠美",
                            "duration": 269,
                            "quality": "SQ",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "data": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        items = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == fake_official_guid("online:netease:228908")
        assert called["musicdl"] == 0
