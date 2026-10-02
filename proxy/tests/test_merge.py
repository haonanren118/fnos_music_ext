"""Tests for fnmusic-ext proxy (search merge, streaming tee cache, passthrough)."""
import os
import pytest
import httpx
from fastapi.testclient import TestClient

from proxy.app import (
    fake_official_guid,
    app,
    CONF,
    _SEARCH_CACHE,
    _track_cover_id,
    find_cache_file,
    library_basename,
    remember_media_path,
    write_audio_tags,
)


def _assert_playback_metadata_shape(data: dict, guid: str) -> None:
    """飞牛 _h()：data.track.genres.join / album 对象 / artists 列表，缺一即跳过播放。"""
    track = data["track"]
    assert track["guid"] in (guid, fake_official_guid(guid))
    assert isinstance(track["artists"], list)
    assert isinstance(track["genres"], list)
    " / ".join(track["genres"])
    assert isinstance(track["album"], dict)
    assert "name" in track["album"]
    spec = data["audioSpec"]
    assert spec.get("format")
    assert spec.get("channel") == 2
    assert "size" in spec


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
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
    monkeypatch.setitem(CONF, "netease_wait_s", 3.0)
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    monkeypatch.setitem(CONF, "search_cache_ttl", 604800.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)

    def default_musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "data": []})

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(default_musicbox_handler), base_url="http://127.0.0.1:8770"
    )


def test_library_basename_omits_source_id():
    assert library_basename("晴天", "周杰伦") == "周杰伦 - 晴天"
    assert library_basename("不再犹豫", "BEYOND") == "BEYOND - 不再犹豫"
    assert library_basename("晴天", "") == "晴天"
    assert "600902" not in library_basename("晴天", "周杰伦")


def test_find_cache_file_legacy_id_name_and_ref(tmp_path):
    guid = "online:migu:600902000006889366"
    legacy = os.path.join(CONF["library_dir"], "晴天 - 600902000006889366.mp3")
    with open(legacy, "wb") as f:
        f.write(b"x" * 2048)
    # Bare IDs collide across sources: only an explicit namespaced ref is safe.
    assert find_cache_file(guid) is None
    assert find_cache_file("online:kuwo:600902000006889366") is None

    renamed = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
    os.rename(legacy, renamed)
    remember_media_path(guid, renamed)
    assert find_cache_file(guid) == renamed


def test_write_audio_tags_id3(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    try:
        import mutagen
    except ImportError:
        pytest.skip("mutagen not available")
    path = str(tmp_path / "sample.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-q:a",
            "9",
            path,
        ],
        check=True,
        capture_output=True,
    )
    write_audio_tags(path, title="晴天", artist="周杰伦", album="叶惠美")
    from mutagen import File as MutagenFile

    tagged = MutagenFile(path, easy=True)
    assert tagged is not None
    assert tagged["title"] == ["晴天"]
    assert tagged["artist"] == ["周杰伦"]
    assert tagged["album"] == ["叶惠美"]


def test_search_track_merge_success():
    """用例 a: 上游 code 0 + 列表 → 合并追加 online 条目、guid 前缀正确。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "夜曲",
                        "artist": "周杰伦",
                        "album": "十一月的萧邦",
                        "duration": 226000,
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "SQ 2.4M",
                }
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

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
        resp = client.get("/music/api/v1/search/track?keyword=夜曲")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 0
        items = res_json["data"]["list"]
        assert len(items) == 2
        # 本地条目保持不变
        assert items[0]["guid"] == "local:101"
        assert items[0]["title"] == "夜曲"
        # 在线条目合并追加且格式正确
        assert items[1]["guid"] == fake_official_guid("online:netease:228908")
        assert items[1]["title"] == "晴天"
        assert items[1]["artist"] == "周杰伦"
        assert items[1]["albumName"] == "叶惠美" and items[1]["album"]["name"] == "叶惠美"
        assert items[1]["duration_ms"] == 269000
        assert items[1]["durationMs"] == 269000
        assert items[1]["codec"] == "flac"
        assert items[1]["format"] == "flac"
        assert items[1]["is_online"] is True
        assert items[1]["artists"][0]["name"] == "周杰伦"
        assert res_json["data"]["total"] == 2


def test_search_track_merge_with_q_param():
    """前端打包使用 q 而不是 keyword，在线合并仍要生效。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("keyword") == "晴天"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "晴天",
                        "artist": "周杰伦",
                        "album_name": "叶惠美",
                        "duration": 269,
                        "quality": "LD 128k",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

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
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == fake_official_guid("online:netease:1")


def test_search_track_preserves_lossless_and_common_formats():
    """在线结果按源站真实格式声明 audioSpec（flac/wav/m4a），不再一律伪装 mp3。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "", "data": {"list": [], "total": 0}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "flac1",
                        "song_name": "不再犹豫",
                        "artist": "Beyond",
                        "album_name": "犹豫",
                        "duration": 240,
                        "quality": "SQ 2.4M",
                    }
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
                        "id": "migu:m4a1",
                        "source": "migu",
                        "title": "海阔天空",
                        "artist": "Beyond",
                        "duration_s": 326,
                        "ext": "aac",
                    },
                    {
                        "id": "kuwo:wav1",
                        "source": "kuwo",
                        "title": "光辉岁月",
                        "artist": "Beyond",
                        "duration_s": 300,
                        "ext": "wav",
                    },
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
        resp = client.get("/music/api/v1/search/track?q=不再犹豫&page=1&size=50")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert resp.json()["data"]["total"] == 3
        by_guid = {it["guid"]: it for it in items}
        flac = by_guid[fake_official_guid("online:netease:flac1")]
        assert flac["format"] == "flac"
        assert flac["audioSpec"]["format"] == "flac"
        assert flac["audioSpec"]["path"].endswith(".flac")
        # v2.6.16：曲目 coverId 为独立盐伪装的纯 32-hex（列表行可渲染），可反解回原 guid
        assert flac["coverId"] == _track_cover_id("online:netease:flac1")
        assert flac["coverId"] != fake_official_guid("online:netease:flac1")  # 封面盐与曲目 guid 盐解耦
        assert by_guid[fake_official_guid("online:migu:m4a1")]["format"] == "m4a"
        assert by_guid[fake_official_guid("online:kuwo:wav1")]["format"] == "wav"
        assert by_guid[fake_official_guid("online:kuwo:wav1")]["audioSpec"]["bitDepth"] == 16


def test_search_track_merges_when_upstream_data_null_list_missing():
    """上游 code=0 但 data 为 null 时仍应合成 list 并合并在线结果。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "不再犹豫",
                        "artist": "Beyond",
                        "duration": 240,
                        "quality": "SQ",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

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
        resp = client.get("/music/api/v1/search/track?q=不再犹豫")
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == fake_official_guid("online:netease:1")
        assert items[0]["format"] == "flac"


def test_metadata_uses_source_ext_not_forced_mp3():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "id": "kuwo:flac1",
                "title": "不再犹豫",
                "artist": "Beyond",
                "duration_s": 240,
                "ext": "flac",
                "file_size": 28000000,
                "cover_url": "http://img.test/c.jpg",
                "source": "kuwo",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/metadata?guid=online:kuwo:flac1")
        spec = resp.json()["data"]["audioSpec"]
        assert spec["format"] == "flac"
        assert spec["codec"] == "flac"
        assert spec["path"].endswith(".flac")
        _assert_playback_metadata_shape(resp.json()["data"], guid="online:kuwo:flac1")


def test_search_track_upstream_unauthorized():
    """用例 b: 上游 99999 (未登录/INVALID TOKEN) → 原样透传不合并，且快速路径绝不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        # 并行预取可能被触发，但 401 路径必须立刻返回、不依赖该响应
        return httpx.Response(200, json={"ok": True, "items": [{"id": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 99999
        assert res_json["msg"] == "INVALID TOKEN"
        assert res_json["data"] is None


def test_search_track_upstream_http_401_fast_path():
    """上游 HTTP 401 非 200 响应 → 快速路径原样透传，不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 99999, "msg": "UNAUTHORIZED"})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 401
        assert resp.json()["code"] == 99999


def test_search_suggest_upstream_unauthorized_fast_path(monkeypatch):
    """开启 suggest 合并时，若上游返回 99999，快速路径直接返回，不调用 musicdl。"""
    monkeypatch.setitem(CONF, "merge_suggest", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{"title": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=test")
        assert resp.status_code == 200
        assert resp.json()["code"] == 99999


def test_search_track_deduplication():
    """用例 c: title+artist 与上游重复时去重。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "LD",
                },
                {
                    "song_id": "228909",
                    "song_name": "晴天 (Live)",
                    "artist": "周杰伦",
                    "album_name": "演唱会",
                    "duration": 300,
                    "quality": "LD",
                },
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

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
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[1]["guid"] == fake_official_guid("online:netease:228909")
        assert items[1]["title"] == "晴天 (Live)"


def test_stream_online_guid_range_and_tee_cache(monkeypatch):
    """用例 d: stream online guid Range 转发与落盘 (mock musicdl 返回带 Content-Length 的 200 流，断言 cache 文件生成且内容一致)。"""
    # 自动下载歌词开启场景：落库后同名 .lrc 一并写入（默认关，见 test_tee_cache.py）
    monkeypatch.setitem(CONF, "lyric_auto_dl", True)
    audio_content = b"RIFF....WAVEfmt....FAKE_MP3_STREAM_CONTENT" * 50
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not be called")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                },
            )
        assert request.url.path == "/stream"
        assert request.url.params.get("id") == "kuwo:228908"
        assert request.url.params.get("proxy") == "true"
        return httpx.Response(
            200,
            content=audio_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": content_len,
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert resp.status_code == 200
        assert resp.content == audio_content
        assert resp.headers.get("content-length") == content_len

        # 检查落盘到飞牛曲库目录：歌名 - id.ext + 同名 .lrc
        cache_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
        assert os.path.exists(cache_file)
        assert "228908" not in os.path.basename(cache_file)
        with open(cache_file, "rb") as f:
            saved = f.read()
        assert saved == audio_content

        lyric_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()


def test_stream_online_guid_range_0_1_safari_probe_no_cache():
    """Safari Range bytes=0-1 探测不产生任何缓存文件（tmp cache dir 断言为空）。"""
    probe_content = b"\x00\x01"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=0-1"
        return httpx.Response(
            206,
            content=probe_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": "bytes 0-1/5000",
                "Content-Length": "2",
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=0-1"},
        )
        assert resp.status_code == 206
        assert resp.content == probe_content

        # 断言临时曲库/缓存目录没有音频落盘
        for d in (CONF["cache_dir"], CONF["library_dir"]):
            if os.path.exists(d):
                assert not any(
                    f.endswith((".mp3", ".flac", ".lrc")) for f in os.listdir(d)
                )


def test_stream_online_guid_existing_cache_no_part():
    """已存在完整缓存文件时，再次在线播放不再生成 .part。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    cache_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.mp3")
    existing_content = b"EXISTING_CACHED_AUDIO_CONTENT" * 50
    with open(cache_file, "wb") as f:
        f.write(existing_content)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert resp.status_code == 200
        assert resp.content == existing_content

        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == existing_content[:10]

        files = os.listdir(CONF["cache_dir"])
        assert not any(f.endswith(".part") for f in files)
        assert "online_kuwo_228908.mp3" in files
        with open(cache_file, "rb") as f:
            assert f.read() == existing_content


def test_stream_online_guid_nonzero_range_no_cache():
    """Range 从非 0 开始时不落盘，只转发。"""
    partial_content = b"PARTIAL_STREAM_DATA"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=100-119"
        return httpx.Response(
            206,
            content=partial_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": "bytes 100-119/1000",
                "Content-Length": str(len(partial_content)),
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=100-119"},
        )
        assert resp.status_code == 206
        assert resp.content == partial_content
        assert resp.headers.get("content-range") == "bytes 100-119/1000"

        # 断言没有落盘
        cache_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.mp3")
        assert not os.path.exists(cache_file)
        assert not os.path.exists(os.path.join(CONF["library_dir"], "unknown.mp3"))
        assert not os.path.exists(os.path.join(CONF["library_dir"], "unknown - 228908.mp3"))


def test_stream_online_guid_unavailable_404():
    """musicdl 404/502 时返回飞牛格式 404 JSON。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "Source error"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:notfound")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": 404,
            "msg": "online source unavailable",
            "data": None,
        }


def test_stream_non_online_guid_passthrough():
    """用例 e: 非 online: 前缀的 guid → 透传到 unix socket。"""
    local_audio = b"LOCAL_UNIX_SOCKET_AUDIO_BYTES"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/track/stream"
        assert request.url.params.get("guid") == "local:9999"
        assert request.headers.get("range") == "bytes=0-100"
        return httpx.Response(
            206,
            content=local_audio,
            headers={
                "Content-Type": "audio/flac",
                "Content-Range": "bytes 0-100/5000",
                "Accept-Ranges": "bytes",
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=local:9999",
            headers={"Range": "bytes=0-100"},
        )
        assert resp.status_code == 206
        assert resp.content == local_audio
        assert resp.headers.get("content-range") == "bytes 0-100/5000"


def test_online_lyrics_and_metadata():
    """在线歌词与元数据合成。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            assert request.url.params.get("id") == "kuwo:228908"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "source": "kuwo",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration_s": 269,
                    "ext": "mp3",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # Lyrics (legacy path)
        resp = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:228908")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["guid"] == fake_official_guid("online:kuwo:228908")
        assert "[00:00.00]晴天" in rj["data"]["lyric"]

        # 飞牛播放器实际走 GET /lyric/list?trackGUID=
        resp_list = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp_list.status_code == 200
        lj = resp_list.json()
        assert lj["code"] == 0
        assert lj["data"]["preferred"] == fake_official_guid("online:kuwo:228908:lyric")
        assert len(lj["data"]["list"]) == 1
        item = lj["data"]["list"][0]
        assert item["guid"] == fake_official_guid("online:kuwo:228908:lyric")
        assert item["source"] == 2
        assert item["isLRC"] is True
        assert "[00:00.00]晴天" in item["content"]

        # Metadata
        resp2 = client.get("/music/api/v1/track/metadata?guid=online:kuwo:228908")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["code"] == 0
        assert rj2["data"]["title"] == "晴天"
        assert rj2["data"]["artist"] == "周杰伦"
        assert rj2["data"]["duration_ms"] == 269000
        assert rj2["data"]["audioSpec"]["format"] == "mp3"
        assert rj2["data"]["audioSpec"]["codec"] == "mp3"
        assert rj2["data"]["audioSpec"]["channel"] == 2
        _assert_playback_metadata_shape(rj2["data"], guid="online:kuwo:228908")
        assert rj2["data"]["track"]["hasLyric"] is True


def test_online_lyrics_and_metadata_musicdl_error():
    """在线歌词/元数据获取失败时，安全返回 code 0 和空 data，绝不 500。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "failed"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:err")
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "ok", "data": {}}

        resp_list = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:err")
        assert resp_list.status_code == 200
        assert resp_list.json()["code"] == 0
        assert resp_list.json()["data"]["list"] == []
        assert resp_list.json()["data"]["preferred"] == ""

        resp2 = client.get("/music/api/v1/track/metadata?guid=online:kuwo:err")
        assert resp2.status_code == 200
        # /info 失败也必须给出 _h() 可解构的 stub，否则播放器抛错后直接跳过、永不请求 stream
        _assert_playback_metadata_shape(resp2.json()["data"], guid="online:kuwo:err")


def test_lyric_cache_hit_skips_musicdl():
    """第一次拉歌词落盘后，再次播放只读 cache/*.lrc，不再请求 musicdl。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    lyric_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.lrc")
    with open(lyric_file, "w", encoding="utf-8") as f:
        f.write("[00:00.00]本地缓存的晴天\n[00:10.00]不再请求源站\n")

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local lyric cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp.status_code == 200
        item = resp.json()["data"]["list"][0]
        assert "本地缓存的晴天" in item["content"]

        resp2 = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:228908")
        assert "本地缓存的晴天" in resp2.json()["data"]["lyric"]


def test_lyric_list_persists_sidecar():
    """首次 /lyric/list 从 musicdl 取回后落 .lrc；无音频时只落 cache，不进曲库（孤儿歌词修复）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    info_calls = {"n": 0}

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            info_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp.status_code == 200
        assert info_calls["n"] == 1
        lyric_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()
        assert not any(f.endswith(".lrc") for f in os.listdir(CONF["library_dir"]))

        resp2 = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert "晴天" in resp2.json()["data"]["list"][0]["content"]
        assert info_calls["n"] == 1


def test_ext_healthz():
    """自身端点 /_ext/healthz 探测 upstream 和 musicdl。"""
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
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["upstream"] == "ok"
        assert rj["musicdl"] == "ok"
        assert rj["musicbox"] == "ok"


def test_search_track_late_wait_first_source_completed(monkeypatch):
    """等到各音源结束（上限 search_timeout）再回复，慢源的空结果不会丢掉先返回的歌曲。"""
    monkeypatch.setitem(CONF, "search_timeout", 2.0)
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "list": [
                        {
                            "guid": "local:101",
                            "title": "晴天",
                            "artist": "周杰伦",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async def musicbox_handler(request: httpx.Request) -> httpx.Response:
        import asyncio
        await asyncio.sleep(0.8)  # 极慢，不应该被本次首屏等待
        return httpx.Response(200, json={"ok": True, "data": []})

    async def musicdl_handler(request: httpx.Request) -> httpx.Response:
        import asyncio
        await asyncio.sleep(0.15)  # 率先在超时外阶段返回
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "kuwo:first_win",
                        "source": "kuwo",
                        "title": "晴天 (Live)",
                        "artist": "刘瑞琦",
                        "duration_s": 260,
                        "ext": "mp3",
                    }
                ],
            },
        )

    async def lx_handler(request: httpx.Request) -> httpx.Response:
        import asyncio
        await asyncio.sleep(0.5)  # 慢于 musicdl
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        # 本地保持在第一项
        assert items[0]["guid"] == "local:101"
        assert items[0]["title"] == "晴天"
        # 率先返回的 musicdl 被并入
        assert items[1]["guid"] == fake_official_guid("online:kuwo:first_win")
        assert items[1]["title"] == "晴天 (Live)"
        assert items[1]["artist"] == "刘瑞琦"


def test_search_cache_ttl_default_seven_days():
    """默认搜索缓存有效期为 7 天 (604800 秒)。"""
    assert CONF["search_cache_ttl"] == 604800.0


def test_search_track_within_budget_keeps_order(monkeypatch):
    """在阶段一预算内（3s），所有返回的源均保留，并按 本地 > 网易云 > musicdl > 洛雪 排序与去重。"""
    monkeypatch.setitem(CONF, "netease_wait_s", 1.0)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "list": [
                        {
                            "guid": "local:101",
                            "title": "晴天",
                            "artist": "周杰伦",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "mb_same",
                        "song_name": "晴天",
                        "artist": "周杰伦",  # 与本地重复，应被去重
                        "album_name": "叶惠美",
                    },
                    {
                        "song_id": "mb_unique",
                        "song_name": "晴天",
                        "artist": "网易翻唱歌手",
                        "album_name": "翻唱合辑",
                    },
                ],
            },
        )

    async def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "kuwo:mdl_same",
                        "source": "kuwo",
                        "title": "晴天",
                        "artist": "网易翻唱歌手",  # 与网易云重复，网易云优先
                        "duration_s": 200,
                        "ext": "mp3",
                    },
                    {
                        "id": "kuwo:mdl_unique",
                        "source": "kuwo",
                        "title": "晴天",
                        "artist": "Musicdl翻唱歌手",
                        "duration_s": 210,
                        "ext": "mp3",
                    },
                ],
            },
        )

    async def lx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "lx:kg:lx_unique",
                        "source": "lx",
                        "title": "晴天",
                        "artist": "洛雪翻唱歌手",
                        "duration_s": 220,
                        "ext": "mp3",
                    }
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        # 1. 本地
        assert items[0]["guid"] == "local:101"
        assert items[0]["artist"] == "周杰伦"
        # 2. 网易云
        assert items[1]["guid"] == fake_official_guid("online:netease:mb_unique")
        assert items[1]["artist"] == "网易翻唱歌手"
        # Unknown musicbox duration cannot prove this is the same recording.
        assert items[2]["guid"] == fake_official_guid("online:kuwo:mdl_same")
        assert items[3]["guid"] == fake_official_guid("online:kuwo:mdl_unique")
        assert items[3]["artist"] == "Musicdl翻唱歌手"
        assert items[4]["guid"] == fake_official_guid("online:lx:kg:lx_unique")
        assert items[4]["artist"] == "洛雪翻唱歌手"
        assert len(items) == 5



def test_search_track_strict_local_first_pagination():
    """多页严格本地优先：本地 60 条(size=20)占据前 3 页，第 4 页起为在线段。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("size", "20"))
        start = (page - 1) * size
        chunk = [
            {"guid": f"local:{i}", "title": f"本地歌{i}", "artist": f"歌手{i % 7}"}
            for i in range(start, min(start + size, 60))
        ]
        return httpx.Response(200, json={"code": 0, "data": {"list": chunk, "total": 60}})

    async def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {"song_id": f"mb_{i}", "song_name": f"在线歌{i}", "artist": f"在线歌手{i}", "duration": 200}
                    for i in range(50)
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "items": []})),
        base_url="http://127.0.0.1:8768",
    )

    with TestClient(app) as client:
        seen = []
        for page in range(1, 7):
            resp = client.get(f"/music/api/v1/search/track?q=歌&page={page}&size=20")
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 60 + 50
            items = data["list"]
            seen.extend(it["guid"] for it in items)
            if page <= 3:
                # 纯本地页：全部是本地 guid
                assert items and all(str(it["guid"]).startswith("local:") for it in items)
            elif page == 4:
                # 边界页：本地尾部 0 条（60 恰为 size 整数倍）+ 在线头部 20 条
                assert all(not str(it["guid"]).startswith("local:") for it in items)
        # 走完 6 页：60 本地 + 50 在线 = 110 条，无重复
        locals_seen = [g for g in seen if str(g).startswith("local:")]
        onlines_seen = [g for g in seen if not str(g).startswith("local:")]
        assert len(locals_seen) == 60 and len(onlines_seen) == 50
        assert len(set(seen)) == 110
        # 本地全部出现在线之前
        first_online_idx = seen.index(onlines_seen[0])
        assert all(str(g).startswith("local:") for g in seen[:first_online_idx])
        assert all(not str(g).startswith("local:") for g in seen[first_online_idx:])


def test_search_track_official_clamp_out_of_range_page():
    """官方搜索越界页钳制回第 1 页（total=4 时 page≥2 仍返回同样 4 条，
    收藏/歌单条目接口无此行为）。合并层必须丢弃该回声，否则官方条目
    拼上在线切片在每个后续页重复出现。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        chunk = [
            {"guid": f"local:{i}", "title": f"本地歌{i}", "artist": f"歌手{i}"}
            for i in range(4)
        ]
        return httpx.Response(200, json={"code": 0, "data": {"list": chunk, "total": 4}})

    async def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {"song_id": f"mb_{i}", "song_name": f"在线歌{i}", "artist": f"在线歌手{i}", "duration": 200}
                    for i in range(10)
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "items": []})),
        base_url="http://127.0.0.1:8768",
    )

    with TestClient(app) as client:
        seen = []
        for page in (1, 2, 3):
            resp = client.get(f"/music/api/v1/search/track?q=歌&page={page}&size=10")
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 4 + 10
            items = data["list"]
            if page == 1:
                # 边界页：4 官方 + 6 在线
                assert len(items) == 10
            if page == 3:
                # 在线段走完：纯空页（total 不变，客户端据此停页）
                assert items == []
            seen.extend(str(it["guid"]) for it in items)
        locals_seen = [g for g in seen if g.startswith("local:")]
        onlines_seen = [g for g in seen if not g.startswith("local:")]
        assert locals_seen == [f"local:{i}" for i in range(4)]
        assert len(onlines_seen) == 10
        assert len(set(seen)) == 14


def test_general_passthrough():
    """非拦截路径透传（如静态资源或登录接口）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/user/profile"
        assert request.headers.get("authorization") == "Bearer mytoken123"
        return httpx.Response(200, json={"code": 0, "data": {"username": "admin"}})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/user/profile",
            headers={"Authorization": "Bearer mytoken123"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "data": {"username": "admin"}}


def test_search_suggest_merge(monkeypatch):
    """测试 search/suggest 开启与关闭配置。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": ["本地周杰伦"]})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {"title": "周杰伦 晴天"},
                    {"title": "周杰伦 七里香"},
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    # 默认关闭
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦"]

    # 开启 suggest 合并
    monkeypatch.setitem(CONF, "merge_suggest", True)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦", "周杰伦 晴天", "周杰伦 七里香"]


def test_online_hls_playlist():
    """在线曲 HLS 兜底 playlist 指向 stream。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "duration_s": 269, "ext": "mp3", "title": "晴天"},
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/hls/online:kuwo:228908/preset.m3u8")
        assert resp.status_code == 200
        body = resp.text
        assert "#EXTM3U" in body
        assert "guid=online%3Akuwo%3A228908" in body
        assert "#EXT-X-ENDLIST" in body


def test_online_transcode_ready():
    """在线曲 transcode 直接 success，避免前端卡会话。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/track/transcode",
            json={"guid": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        hb = client.post(
            "/music/api/v1/track/transcode/heartbeat",
            json={"guid": "online:kuwo:228908"},
        )
        assert hb.status_code == 200
        assert hb.json()["code"] == 0


def test_favorite_track_create_online_authorized():
    """用例 1: create online: 上游 mock 已登录(user/me 200 code:0 guid:user-a)，本地返回 code:0，fav 文件落盘。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a", "name": "admin"}})
        return httpx.Response(500, text="Unexpected upstream call")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "source": "kuwo",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration_s": 269,
                    "ext": "mp3",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

        # 验证 fav 文件已落盘在 user-a.json
        user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
        assert os.path.exists(user_fav)
        import json
        with open(user_fav, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert len(saved["items"]) == 1
        item = saved["items"][0]
        assert item["guid"] == "online:kuwo:228908"
        track = item["track"]
        assert track["title"] == "晴天"
        assert track["artists"][0]["name"] == "周杰伦"
        assert track["album"]["name"] == "叶惠美"
        assert track["isFavorite"] is True
        assert track["duration"] == 269000
        assert track["audioSpec"]["format"] == "mp3"


def test_favorite_track_create_online_unauthorized():
    """用例 2: create online 未登录: 上游 mock INVALID TOKEN → 原样透传返回 99999。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        assert not os.path.exists(os.path.join(CONF["fav_dir"], "user-a.json"))


def test_favorite_track_create_local_passthrough():
    """用例 3: create 本地 guid: 透传上游 mock（验证不写本地）。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/create":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True
        assert len(os.listdir(CONF["fav_dir"])) == 0


def test_favorite_track_delete_online():
    """用例 4: delete online: 已登录 → code:0 且存储清空；幂等再删仍 code:0。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
    with TestClient(app) as client:
        # 先收藏
        resp1 = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp1.status_code == 200
        import json
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 1

        # 删除
        resp2 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp2.status_code == 200
        assert resp2.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0

        # 再次幂等删除
        resp3 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp3.status_code == 200
        assert resp3.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0


def test_favorite_track_list_merge():
    """用例 5: list 合并: 官方 mock 1 条本地 + 本地存 1 条在线 → total=2，list 里有在线条目且形状完整。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            data = {
                "code": 0,
                "msg": "",
                "data": {
                    "list": [
                        {
                            "guid": "local:101",
                            "title": "夜曲",
                            "artists": [{"name": "周杰伦", "guid": "local:artist:1"}],
                            "album": {"name": "十一月的萧邦", "guid": "local:album:1"},
                            "duration": 226000,
                            "isFavorite": True,
                        }
                    ],
                    "total": 1,
                    "sort": "favoriteAt,desc",
                },
            }
            return httpx.Response(200, json=data)
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "migu:600908",
                    "source": "migu",
                    "title": "稻香",
                    "artist": "周杰伦",
                    "album": "魔杰座",
                    "duration_s": 223,
                    "ext": "flac",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 存入一条在线收藏
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:migu:600908"},
        )

        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        data = rj["data"]
        assert data["total"] == 2
        items = data["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[0]["isFavorite"] is True
        
        online_item = items[1]
        assert online_item["guid"] == fake_official_guid("online:migu:600908")
        assert online_item["title"] == "稻香"
        assert online_item["duration"] == 223000
        assert online_item["isFavorite"] is True
        assert online_item["isCue"] is False
        assert isinstance(online_item["genres"], list)
        assert isinstance(online_item["artists"], list)
        assert online_item["artists"][0]["name"] == "周杰伦"
        assert isinstance(online_item["album"], dict)
        assert online_item["album"]["name"] == "魔杰座"
        assert isinstance(online_item["audioSpec"], dict)
        assert online_item["audioSpec"]["format"] == "flac"
        assert "createdAt" in online_item
        assert "updatedAt" in online_item


def test_favorite_track_create_unwritable_fav_dir_safe():
    """用例 6: create 时 fav 文件不可写（如路径指向不可写或非法路径）→ 不抛异常，返回仍 code:0（绝不能 500）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    # 将 fav_dir 设置为文件路径而非目录，使在其中创建文件失败
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    bad_file = os.path.join(CONF["cache_dir"], "not_a_dir")
    with open(bad_file, "w") as f:
        f.write("xxx")
    CONF["fav_dir"] = os.path.join(bad_file, "sub")

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


def test_favorite_track_list_upstream_unauthorized():
    """用例 7: list 上游 INVALID TOKEN → 原样返回。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}


def test_favorite_track_delete_local_passthrough():
    """用例 8: delete 本地 guid: 透传上游 mock。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/delete":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True


def test_user_isolation_create_and_list():
    """用户隔离用例 1: 用户 A create → 用户 B list 看不到 A 的在线条目；B 自己 create 后只看到自己的。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            gid = request.url.params.get("id")
            title = "A的歌曲" if "aaa" in str(gid) else "B的歌曲"
            return httpx.Response(200, json={"ok": True, "title": title})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目 A
        current_user["guid"] = "user-a"
        resp_a_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:aaa"},
        )
        assert resp_a_create.status_code == 200

        # B 查看列表，看不到 A 的收藏
        current_user["guid"] = "user-b"
        resp_b_list1 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list1.status_code == 200
        assert resp_b_list1.json()["data"]["total"] == 0
        assert len(resp_b_list1.json()["data"]["list"]) == 0

        # B 收藏曲目 B
        resp_b_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:bbb"},
        )
        assert resp_b_create.status_code == 200

        # B 再次查看列表，只有 B 的歌曲
        resp_b_list2 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list2.status_code == 200
        assert resp_b_list2.json()["data"]["total"] == 1
        assert resp_b_list2.json()["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:bbb")

        # A 查看列表，只有 A 的歌曲
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:aaa")


def test_user_isolation_delete():
    """用户隔离用例 2: 用户 A create、用户 B delete 同一 guid → A 的仍在（B 幂等 code:0），互不影响。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "公共在线曲目"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目
        current_user["guid"] = "user-a"
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:same_song"},
        )

        # B 尝试删除同一曲目（B 本身未收藏）
        current_user["guid"] = "user-b"
        resp_b_del = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:same_song"},
        )
        assert resp_b_del.status_code == 200
        assert resp_b_del.json()["code"] == 0

        # A 检查列表，收藏依然在
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:same_song")


def test_user_me_missing_guid_fallback_shared():
    """用户隔离用例 3: user/me 响应缺 guid → 落 'shared' 桶不报错。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            # 返回没有 guid 字段或者 data 非 dict
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"name": "someone"}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "兜底歌曲"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 创建收藏
        resp_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:fallback_track"},
        )
        assert resp_create.status_code == 200
        assert resp_create.json()["code"] == 0

        # 检查是否落入 shared.json
        shared_file = os.path.join(CONF["fav_dir"], "shared.json")
        assert os.path.exists(shared_file)
        import json
        with open(shared_file, "r", encoding="utf-8") as f:
            items = json.load(f)["items"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:kuwo:fallback_track"

        # 查询 list
        resp_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_list.status_code == 200
        assert resp_list.json()["data"]["total"] == 1
        assert resp_list.json()["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:fallback_track")


def test_user_guid_sanitization():
    """测试 user_guid 特殊字符文件名过滤安全逻辑。"""
    from proxy.app import sanitize_user_guid
    assert sanitize_user_guid("user-123_ABC") == "user-123_ABC"
    assert sanitize_user_guid("../../etc/passwd") == "______etc_passwd"
    assert sanitize_user_guid("user:name*?<>|") == "user_name_____"
    assert sanitize_user_guid("   ") == "shared"
    assert sanitize_user_guid("") == "shared"
    assert sanitize_user_guid(None) == "shared"


def test_static_cover_online_coverid_proxied(monkeypatch):
    """coverId 为在线 guid 且 /info 给出封面直链 → 服务端同源回源图床字节（200），
    不再 302 跳外部图床（远程 HTTPS 下混合内容/防盗链会拦掉直链）。"""
    cover_target = "http://img.music.migu.cn/cover123.jpg"
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-fake-migu-cover"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Should not reach upstream")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/info"
        assert request.url.params.get("id") == "migu:123"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "id": "migu:123",
                "title": "测试歌曲",
                "artist": "歌手",
                "cover_url": cover_target,
            },
        )

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == cover_target
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=online:migu:123&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")
        assert resp.headers.get("cache-control") == "public, max-age=86400"


def test_static_cover_local_coverid_local_resolution(monkeypatch):
    """coverId 为非 online: 本地 guid → 不再透传上游（飞牛对本地 guid 固定 400），
    改走本地解析：无内嵌图/元数据/网易兜底时落占位图（200 PNG + no-cache，绝不 404）。"""

    upstream_paths = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        upstream_paths.append(request.url.path)
        if request.url.path == "/music/api/v1/track/metadata":
            return httpx.Response(200, json={"code": 0, "data": None})
        return httpx.Response(400, text="upstream /static/cover must not be called")

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        # 网易兜底等外部请求一律失败，保证确定性落占位图
        return httpx.Response(503)

    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=local:track:999&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # 占位图池 PNG
        assert resp.headers.get("content-type") == "image/png"
        # 占位图绝不可被客户端长缓存（旧 max-age=86400 会把占位图冻住一整天）
        assert resp.headers.get("cache-control") == "no-cache"
        # 本地 guid 绝不透传官方 /static/cover（固定 400，透传必失败）
        assert "/music/api/v1/static/cover" not in upstream_paths


def test_favorite_track_list_official_items_populate_is_favorite_and_empty_handling():
    """测试 official_list 缺失 isFavorite 时补齐 True，且在官方列表为空或有条目时正确合并和计算 total。"""
    official_data_holder = {"list": []}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "",
                    "data": {
                        "list": official_data_holder["list"],
                        "total": len(official_data_holder["list"]),
                    },
                },
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-fav-test"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "netease:12345",
                    "source": "netease",
                    "title": "测试网易曲目",
                    "artist": "歌手A",
                    "album": "专辑A",
                    "duration_s": 180,
                    "ext": "mp3",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # Case 1: 官方列表为空，无在线收藏 -> total=0, list=[]
        resp = client.get("/music/api/v1/favorite-track/list")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["total"] == 0
        assert rj["data"]["list"] == []

        # 添加一条在线收藏
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:netease:12345"},
        )

        # Case 2: 官方列表为空，存在 1 条在线收藏 -> total=1
        resp2 = client.get("/music/api/v1/favorite-track/list")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["data"]["total"] == 1
        assert len(rj2["data"]["list"]) == 1
        assert rj2["data"]["list"][0]["guid"] == fake_official_guid("online:netease:12345")
        assert rj2["data"]["list"][0]["isFavorite"] is True

        # Case 3: 官方列表中包含未带 isFavorite 字段（或 isFavorite 为 False）的条目
        official_data_holder["list"] = [
            {"guid": "local:201", "title": "本地曲目1"},
            {"guid": "local:202", "title": "本地曲目2", "isFavorite": False},
        ]
        resp3 = client.get("/music/api/v1/favorite-track/list")
        assert resp3.status_code == 200
        rj3 = resp3.json()
        # 官方 2 条 + 在线 1 条 = 3 条
        assert rj3["data"]["total"] == 3
        items = rj3["data"]["list"]
        assert len(items) == 3
        assert items[0]["guid"] == "local:201"
        assert items[0]["isFavorite"] is True
        assert items[1]["guid"] == "local:202"
        assert items[1]["isFavorite"] is True
        assert items[2]["guid"] == fake_official_guid("online:netease:12345")
        assert items[2]["isFavorite"] is True


def test_is_playable_online_track_defense():
    from proxy.app import is_playable_online_track

    # 1. 试听标题过滤
    assert not is_playable_online_track({"id": "netease:1", "title": "夜曲 (试听版)"})
    assert not is_playable_online_track({"id": "netease:2", "title": "晴天（试听）"})
    assert not is_playable_online_track({"id": "kuwo:3", "title": "花海 - 试听片段"})

    # 2. 试听标记/不可播标记
    assert not is_playable_online_track({"id": "netease:4", "title": "稻香", "is_trial": True})
    assert not is_playable_online_track({"id": "netease:5", "title": "稻香", "freeTrialInfo": {"start": 0}})
    assert not is_playable_online_track({"id": "lx:kg:6", "title": "稻香", "is_free_part": 1})
    assert not is_playable_online_track({"id": "lx:kg:7", "title": "稻香", "fail_process": 4})
    assert not is_playable_online_track({"id": "lx:kg:8", "title": "稻香", "pay_type": 1})
    assert not is_playable_online_track({"id": "lx:kg:9", "title": "稻香", "playable": False})
    assert not is_playable_online_track({"id": "lx:kg:10", "title": "稻香", "has_stream": False})

    # 3. 直链无流/404过滤
    assert not is_playable_online_track({"id": "kuwo:11", "title": "七里香", "download_url": ""})
    assert not is_playable_online_track({"id": "kuwo:12", "title": "七里香", "download_url": "http://err.com/404/error.html"})
    assert not is_playable_online_track({"id": "kuwo:13", "title": "七里香", "download_url": "ftp://bad.com/1.mp3"})

    # 4. 正常有效可播歌曲
    assert is_playable_online_track({"id": "netease:100", "title": "晴天", "artist": "周杰伦"})
    assert is_playable_online_track({"id": "kuwo:101", "title": "晴天", "artist": "周杰伦", "download_url": "http://cdn.com/101.mp3"})


def test_merge_online_tracks_filters_unplayable_defense():
    from proxy.app import merge_online_tracks

    upstream_json = {"code": 0, "msg": "OK", "data": {"list": [], "total": 0}}
    online_data = [
        {"id": "netease:1", "title": "枫 (试听)", "artist": "周杰伦", "duration_s": 200},
        {"id": "kuwo:2", "title": "搁浅", "artist": "周杰伦", "download_url": "", "duration_s": 200},
        {"id": "kuwo:3", "title": "退后", "artist": "周杰伦", "download_url": "http://cdn.com/3.mp3", "duration_s": 250},
    ]
    merged = merge_online_tracks(upstream_json, online_data)
    items = merged["data"]["list"]
    # 只有有效且可播的退后 (id=3) 会被合并
    assert len(items) == 1
    assert items[0]["guid"] == "online:kuwo:3"
    assert items[0]["title"] == "退后"






def test_forward_to_upstream_keeps_content_length():
    """identity 响应透传精确 Content-Length（对齐官方直连行为）；204 不带。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/echo":
            return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
        if request.url.path == "/music/api/v1/nocontent":
            return httpx.Response(204)
        if request.url.path == "/music/api/v1/range":
            return httpx.Response(
                206, content=b"ab",
                headers={"content-range": "bytes 0-1/10"},
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    with TestClient(app) as client:
        r = client.get("/music/api/v1/echo")
        assert r.status_code == 200
        assert r.headers.get("content-length") == "5"
        assert r.content == b"hello"

        r204 = client.get("/music/api/v1/nocontent")
        assert r204.status_code == 204
        assert "content-length" not in r204.headers

        # Range 分段：Content-Length 与 Content-Range 同时到达客户端
        r206 = client.get("/music/api/v1/range")
        assert r206.headers.get("content-length") == "2"
        assert r206.headers.get("content-range") == "bytes 0-1/10"
        assert r206.content == b"ab"


def test_tee_finalize_metadata_fallback_avoids_unknown(tmp_path, monkeypatch):
    """issue #28：流式上下文元数据缺失时，从在线历史/收藏快照回查命名，
    绝不以 unknown (n) 形态进曲库。"""
    import json

    from proxy import recommend as dailyrec
    from proxy.app import _tee_finalize, _lookup_online_snapshot

    tee_dir = str(tmp_path / "library")
    os.makedirs(tee_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "tee_save_dir", tee_dir)
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(
        CONF, "fav_dir", str(tmp_path / "online_favorites"),
    )
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "play_history"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend_cache"))
    monkeypatch.setenv("FNMUSIC_NM_PLAYLISTS_DIR", str(tmp_path / "nm_playlists"))

    guid = "online:kuwo:228908"
    # 首播已上报 track_play：历史快照带完整元数据
    dailyrec.record_online_play("user-1", guid, {
        "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
    })
    assert _lookup_online_snapshot(guid)["title"] == "晴天"

    # tee 落盘时 info 解析失败（空 dict）——文件名仍应来自历史快照
    part = os.path.join(tee_dir, "cache_safe_guid.abc.part")
    with open(part, "wb") as f:
        f.write(b"RIFF....FAKE_AUDIO" * 64)
    _tee_finalize(part, guid, "mp3", None, tee_enabled=True)
    files = os.listdir(tee_dir)
    assert files == ["周杰伦 - 晴天.mp3"], f"应以快照元数据命名，实际: {files}"

    # 收藏快照同样可兜底：无历史但收藏里有元数据
    guid2 = "online:migu:9"
    with open(os.path.join(CONF["fav_dir"], "user-1.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{
            "guid": guid2, "addedAt": 1,
            "track": {"title": "七里香", "artist": "周杰伦", "album": "七里香"},
        }]}, f)
    part2 = os.path.join(tee_dir, "cache_safe_guid2.abc.part")
    with open(part2, "wb") as f:
        f.write(b"RIFF....FAKE_AUDIO2" * 64)
    _tee_finalize(part2, guid2, "mp3", {}, tee_enabled=True)
    assert "周杰伦 - 七里香.mp3" in os.listdir(tee_dir)


def _isolate_meta_dirs(tmp_path, monkeypatch):
    """推荐/网易歌单/历史目录指到临时目录，避免扫到本机缓存。"""
    rec = tmp_path / "recommend_cache"
    nm = tmp_path / "nm_playlists"
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(rec))
    monkeypatch.setenv("FNMUSIC_NM_PLAYLISTS_DIR", str(nm))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "play_history"))
    tee_dir = str(tmp_path / "library")
    os.makedirs(tee_dir, exist_ok=True)
    os.makedirs(CONF["fav_dir"], exist_ok=True)
    monkeypatch.setitem(CONF, "tee_save_dir", tee_dir)
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "auto_cover", False)
    return rec, nm, tee_dir


def _write_json(path, payload) -> None:
    import json

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _tiny_mp3(path: str) -> None:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    try:
        import mutagen  # noqa: F401
    except ImportError:
        pytest.skip("mutagen not available")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "0.2", "-q:a", "9", path,
        ],
        check=True,
        capture_output=True,
    )


def _album_of(path: str) -> str:
    from mutagen import File as MutagenFile

    tagged = MutagenFile(path, easy=True)
    assert tagged is not None
    return tagged["album"][0]


def test_tag_fields_unwraps_album_and_keeps_plain_strings():
    from proxy.app import _tag_fields

    title, artist, album = _tag_fields({
        "title": "晴天",
        "artists": [{"name": "周杰伦"}],
        "album": {"name": "叶惠美", "guid": "online:netease:1:album", "artists": [], "coverId": "x"},
        "albumName": "不该用这个",
    })
    assert (title, artist, album) == ("晴天", "周杰伦", "叶惠美")
    assert _tag_fields({
        "name": "七里香",
        "album": {"name": "", "guid": "g"},
        "albumName": "七里香",
    }) == ("七里香", "", "七里香")
    assert _tag_fields({"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}) == (
        "晴天", "周杰伦", "叶惠美",
    )


def test_tee_finalize_recommend_vo_names_file_and_plain_album(tmp_path, monkeypatch):
    """推荐缓存里的前端曲目（album 为对象）落盘：文件名用歌名，专辑标签只写 name。"""
    from proxy.app import _tee_finalize, _tee_metadata_fallback

    rec, nm, tee_dir = _isolate_meta_dirs(tmp_path, monkeypatch)
    guid = "online:netease:186016"
    vo = {
        "guid": guid,
        "title": "晴天",
        "artists": [{"name": "周杰伦", "guid": f"{guid}:artist"}],
        "album": {
            "name": "叶惠美",
            "guid": f"{guid}:album",
            "artists": [{"name": "周杰伦"}],
            "coverId": guid,
        },
        "albumName": "叶惠美",
        "cover_url": "https://example.invalid/cover.jpg",
    }
    _write_json(os.path.join(rec, "user-1", "daily-20200101.json"), {
        "day": "20200101", "status": "ready", "tracks": [vo],
    })
    holder: dict = {}
    title, artist, album = _tee_metadata_fallback(guid, "", "", "", holder)
    assert (title, artist, album) == ("晴天", "周杰伦", "叶惠美")
    assert holder.get("cover_url") == "https://example.invalid/cover.jpg"
    assert "{" not in album

    part = os.path.join(tee_dir, "part-rec.mp3")
    _tiny_mp3(part)
    _tee_finalize(part, guid, "mp3", None, tee_enabled=True)
    dest = os.path.join(tee_dir, "周杰伦 - 晴天.mp3")
    assert os.path.isfile(dest)
    assert _album_of(dest) == "叶惠美"

    # 网易账号歌单磁盘缓存是同一类 VO，不在搜索缓存里也要能定名
    guid_nm = "online:netease:42"
    _write_json(os.path.join(nm, "pl-7.json"), {"tracks": [{
        "guid": guid_nm,
        "title": "七里香",
        "artist": "周杰伦",
        "album": {"name": "七里香", "guid": f"{guid_nm}:album"},
    }]})
    part_nm = os.path.join(tee_dir, "part-nm.mp3")
    _tiny_mp3(part_nm)
    _tee_finalize(part_nm, guid_nm, "mp3", {}, tee_enabled=True)
    dest_nm = os.path.join(tee_dir, "周杰伦 - 七里香.mp3")
    assert os.path.isfile(dest_nm)
    assert _album_of(dest_nm) == "七里香"


def test_tee_finalize_favorite_album_object_and_keeps_string_album(tmp_path, monkeypatch):
    """收藏快照的专辑对象写成专辑名；调用方已经给了纯字符串专辑时不被缓存覆盖。"""
    import json

    from proxy.app import _tee_finalize

    _rec, _nm, tee_dir = _isolate_meta_dirs(tmp_path, monkeypatch)
    guid = "online:kuwo:9"
    with open(os.path.join(CONF["fav_dir"], "user-1.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{
            "guid": guid,
            "track": {
                "title": "七里香",
                "artists": [{"name": "周杰伦"}],
                "album": {"name": "七里香", "guid": f"{guid}:album", "coverId": guid},
            },
        }]}, f)
    part = os.path.join(tee_dir, "part-fav.mp3")
    _tiny_mp3(part)
    _tee_finalize(part, guid, "mp3", None, tee_enabled=True)
    dest = os.path.join(tee_dir, "周杰伦 - 七里香.mp3")
    assert os.path.isfile(dest)
    assert _album_of(dest) == "七里香"

    guid2 = "online:kuwo:10"
    _write_json(os.path.join(_rec, "user-1", "daily-20200101.json"), {"tracks": [{
        "guid": guid2,
        "title": "晴天",
        "artist": "周杰伦",
        "album": {"name": "不该覆盖", "guid": f"{guid2}:album"},
    }]})
    part2 = os.path.join(tee_dir, "part-keep.mp3")
    _tiny_mp3(part2)
    _tee_finalize(part2, guid2, "mp3", {
        "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
    }, tee_enabled=True)
    kept = os.path.join(tee_dir, "周杰伦 - 晴天.mp3")
    assert os.path.isfile(kept)
    assert _album_of(kept) == "叶惠美"


def test_write_audio_tags_unwraps_album_object(tmp_path):
    path = str(tmp_path / "sample.mp3")
    _tiny_mp3(path)
    write_audio_tags(path, title="晴天", artist="周杰伦", album={
        "name": "叶惠美", "guid": "online:netease:1:album", "artists": [], "coverId": "x",
    })
    assert _album_of(path) == "叶惠美"


def test_search_track_pagination_no_cross_page_duplication():
    """官方搜索忽略 size 全量返回结果集（2026-09-25 更新实测）时，本地段按请求
    窗口切片：page1 恰好 size 条、page2 为余量本地+在线切片，跨页零重复。"""
    local_tracks = [
        {"guid": f"local:{i}", "title": f"本地歌{i}", "artist": "周杰伦",
         "album": "专辑", "duration": 200000}
        for i in range(11)
    ]

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        # 无论 page/size 如何都返回同样 11 条（官方钳制 + 忽略 size 的真实行为）
        return httpx.Response(200, json={"code": 0, "data": {"list": local_tracks, "total": 11}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False})

    online_items = [
        {"id": f"kuwo:{900 + i}", "source": "kuwo", "title": f"在线歌{i}", "artist": "周杰伦",
         "album": "叶惠美", "duration_s": 250, "ext": "mp3"}
        for i in range(6)
    ]

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": online_items})

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
        p1 = client.get("/music/api/v1/search/track?keyword=周杰伦&page=1&size=10").json()
        p2 = client.get("/music/api/v1/search/track?keyword=周杰伦&page=2&size=10").json()
        p3 = client.get("/music/api/v1/search/track?keyword=周杰伦&page=3&size=10").json()
    l1 = [str(i["guid"]) for i in p1["data"]["list"]]
    l2 = [str(i["guid"]) for i in p2["data"]["list"]]
    l3 = [str(i["guid"]) for i in p3["data"]["list"]]
    # page1 = 本地前 10 条；page2 = 第 11 条本地 + 全部 6 条在线（窗口要 9 条只
    # 有 6 条可给）；page3 本地段越界清空、在线窗口 [9:19) 为空 → 空页
    assert len(l1) == 10
    assert len(l2) == 7
    assert l3 == []
    assert not (set(l1) & set(l2))
    assert len(set(l1) | set(l2)) == 17  # 11 本地 + 6 在线，零重复
    assert p1["data"]["total"] == p2["data"]["total"] == p3["data"]["total"] == 17


def test_search_track_pagination_respected_size_untouched():
    """官方尊重 size 时（返回条数 ≤ size）不切片，既有分页语义不变。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        # 尊重分页：page=2 返回第 11-20 条（这里模拟返回第 2 页内容）
        data = {"code": 0, "data": {"list": [
            {"guid": f"local:{i}", "title": f"歌{i}", "artist": "a", "album": "x", "duration": 1}
            for i in range(10, 20)
        ], "total": 30}}
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

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
        p2 = client.get("/music/api/v1/search/track?keyword=x&page=2&size=10").json()
    # 返回条数(10)不超 size(10)：原样透传第 2 页，不被切片清空
    assert [str(i["guid"]) for i in p2["data"]["list"]] == [f"local:{i}" for i in range(10, 20)]
