"""Tests for daily recommend, play-history merge, and LLM config gating."""
import asyncio
import json
import os
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import recommend as dailyrec
from proxy.app import CONF, _DAILY_TASKS, _SEARCH_CACHE, app, _conf_log_value, fake_official_guid
from proxy.app import resolve_real_guid


@pytest.fixture(autouse=True)
def setup_recommend_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _DAILY_TASKS.clear()
    dailyrec._LAST_DAILY_INFO.clear()
    rec_dir = str(tmp_path / "recommend_cache")
    hist_dir = str(tmp_path / "play_history")
    fav_dir = str(tmp_path / "online_favorites")
    cache_dir = str(tmp_path / "cache")
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", rec_dir)
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", hist_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.delenv("FNMUSIC_LLM_API_KEY", raising=False)
    monkeypatch.delenv("FNMUSIC_LLM_BASE_URL", raising=False)
    # lxmusic 客户端默认 mock（404），防止每日推荐链路在测试中触达真实 127.0.0.1:8772
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )


def _auth_user(guid="user-rec-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1}], "total": 1}},
            )
        if path.endswith("/playlist/batch-detail"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "trackCount": 3}]}},
            )
        if path.endswith("/play-history/list"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "local-track-1", "title": "本地"}], "total": 1}},
            )
        if path.endswith("/event/report"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})
        if "search/track" in path:
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})
    return handler


def test_infer_language_and_guid():
    assert dailyrec.infer_language("晴天", "周杰伦") == "中文"
    assert dailyrec.infer_language("夜に駆ける", "YOASOBI") == "日语"
    assert dailyrec.infer_language("Dynamite", "BTS 방탄소년단") == "韩语"
    assert dailyrec.infer_language("Shape of You", "Ed Sheeran") == "英语"
    guid = dailyrec.daily_playlist_guid("20260831", "user-1")
    assert dailyrec.is_daily_playlist_guid(guid)
    assert "20260831" in guid


def test_parse_llm_json_fenced_and_object():
    raw = """```json
    [{"title": "七里香", "artist": "周杰伦", "dimension": "artist", "reason": "同歌手"}]
    ```"""
    items = dailyrec.parse_llm_recommendations(raw)
    assert items[0]["title"] == "七里香"
    wrapped = json.dumps({"songs": [{"title": "海阔天空", "artist": "Beyond", "dimension": "genre"}]})
    items2 = dailyrec.parse_llm_recommendations(wrapped)
    assert items2[0]["artist"] == "Beyond"


def test_conf_log_redacts_secrets():
    assert _conf_log_value("llm_api_key", "sk-secret") == "***"
    assert _conf_log_value("musicdl_url", "http://127.0.0.1:8768") == "http://127.0.0.1:8768"


def test_playlist_list_injects_fallback_when_llm_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            # 可播校验探针（issue #23）：Range 头部探测需 200/206 音频响应
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        names = [x.get("name") for x in resp.json()["data"]["list"]]
        assert "牛一" in names
        assert any("每日推荐" in str(n) for n in names)
        first = resp.json()["data"]["list"][0]
        assert first["isDaily"] is True
        assert dailyrec.is_daily_playlist_guid(first["guid"])
        assert dailyrec.today_key() in first["guid"]


def test_playlist_list_drops_yesterday_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    yesterday = dailyrec.daily_playlist_guid("20200101", "user-rec-1")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-rec-1"}})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"guid": yesterday, "name": "每日推荐 01-01", "coverId": "old", "createdAt": 1, "updatedAt": 1},
                            {"guid": "localpl", "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1},
                        ],
                        "total": 2,
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": None})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        names = [x.get("name") for x in resp.json()["data"]["list"]]
        guids = [x.get("guid") for x in resp.json()["data"]["list"]]
        assert yesterday not in guids
        assert sum(1 for n in names if "每日推荐" in str(n)) == 1
        assert dailyrec.today_key() in str(guids[0])


def test_playlist_list_injects_daily_when_enabled(monkeypatch, tmp_path):
    # 大模型链路仅当网易音源未启用时生效（新策略），此处关闭网易以覆盖 LLM 注入端到端流程
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": json.dumps([
                            {"title": "晴天", "artist": "周杰伦", "dimension": "artist", "genre": "流行", "language": "中文", "type": "抒情", "reason": "同歌手"},
                            {"title": "七里香", "artist": "周杰伦", "dimension": "genre", "genre": "流行", "language": "中文", "type": "流行", "reason": "曲风"},
                        ])
                    }
                }]
            },
        )

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.llm_client = httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9")

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["total"] >= 2
        first = body["data"]["list"][0]
        assert first["isDaily"] is True
        assert dailyrec.is_daily_playlist_guid(first["guid"])
        assert first["trackCount"] >= 1

        detail = client.get(f"/music/api/v1/playlist/detail?guid={first['guid']}")
        assert detail.json()["data"]["guid"] == first["guid"]

        tracks = client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={first['guid']}&page=1&size=50")
        tj = tracks.json()
        assert tj["code"] == 0
        assert tj["data"]["total"] >= 1
        assert resolve_real_guid(tj["data"]["list"][0]["guid"]).startswith("online:")
        assert isinstance(tj["data"]["list"][0]["artists"], list)

        batch = client.get(f"/music/api/v1/playlist/batch-detail?guids={first['guid']},localpl")
        bj = batch.json()
        assert any(dailyrec.is_daily_playlist_guid(str(x.get("guid"))) for x in bj["data"]["list"])


def test_playlist_unauth_passthrough(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.json()["code"] == 99999


def test_event_report_records_online_play(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/event/report",
            json={"events": [{"eventType": "track_play", "occurredAt": 1, "payload": {"trackGUID": "online:migu:1"}}]},
        )
        assert resp.json()["code"] == 0
        items = dailyrec.load_online_play_history("user-rec-1")
        assert items[-1]["guid"] == "online:migu:1"


def test_play_history_merges_online(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("user-rec-1", "online:migu:99", {"title": "在线歌", "artist": "歌手"})
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/play-history/list")
        body = resp.json()
        guids = [x["guid"] for x in body["data"]["list"]]
        assert fake_official_guid("online:migu:99") in guids
        assert "local-track-1" in guids
        assert body["data"]["total"] == 2


def test_play_history_delete_online_and_official(tmp_path, monkeypatch):
    """删除闭环：官方契约 trackGUIDs 数组，online 条目（客户端持有伪装假 id）删本地
    存储；官方条目原样转发。"""
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("user-rec-1", "online:migu:99", {"title": "在线歌", "artist": "歌手"})
    forwarded = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-rec-1"}})
        forwarded.append((request.method, request.url.path))
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    with TestClient(app) as client:
        # 官方真实契约：trackGUIDs 数组（前端单条移除/"编辑"多选移除共用）
        fake = fake_official_guid("online:migu:99")
        resp = client.post("/music/api/v1/play-history/delete", json={"trackGUIDs": [fake]})
        assert resp.json()["code"] == 0
        assert all(it["guid"] != "online:migu:99" for it in dailyrec.load_online_play_history("user-rec-1"))

        # 兼容单数 trackGUID（TestClient.delete 不收 json，DELETE 用 request 发）
        dailyrec.record_online_play("user-rec-1", "online:migu:99", {"title": "在线歌"})
        resp2 = client.post("/music/api/v1/play-history/delete", json={"trackGUID": fake})
        assert resp2.json()["code"] == 0
        assert all(it["guid"] != "online:migu:99" for it in dailyrec.load_online_play_history("user-rec-1"))

        dailyrec.record_online_play("user-rec-1", "online:migu:99", {"title": "在线歌"})
        resp3 = client.request("DELETE", "/music/api/v1/play-history/delete", json={"guid": fake})
        assert resp3.json()["code"] == 0
        assert dailyrec.load_online_play_history("user-rec-1") == []

        # 官方条目：原样转发上游，不打官方之外的接口
        resp4 = client.post("/music/api/v1/play-history/delete", json={"trackGUIDs": ["official-guid-1"]})
        assert resp4.json()["code"] == 0
    assert forwarded == [("POST", "/music/api/v1/play-history/delete")]


def test_play_history_delete_mixed_batch_forwards_official_first(tmp_path, monkeypatch):
    """混合批次（在线+官方同批）：官方部分改写为 trackGUIDs 先行转发，成功后本地删除。"""
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("user-rec-1", "online:migu:7", {"title": "在线歌"})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-rec-1"}})
        body = json.loads(request.content)
        seen.append((body, len(dailyrec.load_online_play_history("user-rec-1"))))
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    fake = fake_official_guid("online:migu:7")
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/play-history/delete",
            json={"trackGUIDs": [fake, "official-guid-1", "official-guid-2"]},
        )
        assert resp.json()["code"] == 0
        assert dailyrec.load_online_play_history("user-rec-1") == []

    # 官方部分整体改写转发，且转发时本地在线条目尚未删除（官方先行）
    assert seen == [({"trackGUIDs": ["official-guid-1", "official-guid-2"]}, 1)]


def test_play_history_delete_official_failure_keeps_local(tmp_path, monkeypatch):
    """混合批次官方部分失败：透传官方错误，本地在线条目不动。"""
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("user-rec-1", "online:migu:7", {"title": "在线歌"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-rec-1"}})
        return httpx.Response(200, json={"code": 100005, "msg": "not found", "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    fake = fake_official_guid("online:migu:7")
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/play-history/delete",
            json={"trackGUIDs": [fake, "official-guid-1"]},
        )
        assert resp.json()["code"] == 100005
        assert [it["guid"] for it in dailyrec.load_online_play_history("user-rec-1")] == ["online:migu:7"]


def test_playlist_detail_size_minus_one_returns_all(tmp_path, monkeypatch):
    """size=-1 激活"返回全部"约定；0/乱值仍回退默认 50。"""
    tracks = [{"guid": f"online:migu:{i}", "title": f"歌{i}", "artist": "歌手A"} for i in range(60)]

    async def fake_bundle(request, user_guid, kind="daily"):
        return {"playlist": {"guid": "dp-test", "name": "每日"}, "tracks": [dict(t) for t in tracks]}

    monkeypatch.setattr("proxy.app._load_daily_bundle", fake_bundle)
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    with TestClient(app) as client:
        guid = dailyrec.daily_playlist_guid(user_guid="user-rec-1")
        base = "/music/api/v1/track/playlist-detail/list"
        all_resp = client.get(base, params={"playlistGUID": guid, "page": 1, "size": -1})
        assert all_resp.json()["data"]["total"] == 60
        assert len(all_resp.json()["data"]["list"]) == 60
        page_resp = client.get(base, params={"playlistGUID": guid, "page": 1, "size": 50})
        assert len(page_resp.json()["data"]["list"]) == 50
        zero_resp = client.get(base, params={"playlistGUID": guid, "page": 1, "size": 0})
        assert len(zero_resp.json()["data"]["list"]) == 50


def test_read_local_recent_tracks(tmp_path):
    db = tmp_path / "music.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT, year INTEGER, album_id INTEGER);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_genre (track_id INTEGER, genre_id INTEGER);
        CREATE TABLE play_history (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, play_count INTEGER, updated_at TEXT);
        INSERT INTO user VALUES (1, 'u1');
        INSERT INTO album VALUES (1, '叶惠美');
        INSERT INTO track VALUES (1, 'tg1', '晴天', 2003, 1);
        INSERT INTO artist VALUES (1, '周杰伦');
        INSERT INTO track_artist VALUES (1, 1);
        INSERT INTO genre VALUES (1, '流行');
        INSERT INTO track_genre VALUES (1, 1);
        INSERT INTO play_history VALUES (1, 1, 1, 3, '2026-08-31 09:00:00+08:00');
        """
    )
    con.commit()
    con.close()
    rows = dailyrec.read_local_recent_tracks(str(db), "u1", 20)
    assert rows[0]["title"] == "晴天"
    assert "周杰伦" in rows[0]["artist"]
    assert rows[0]["language"] == "中文"


def _make_local_db(path, n_tracks=30, played_by_user=None):
    """建最小可用的官方 music.db（local-random 层 / 按账户隔离测试用）。

    用户 u1/u2；曲目 lt1..ltN（audio_file 真实存在）；played_by_user 形如
    {"u1": [("lt1", "2026-09-28 10:00:00+08:00"), ...]} 写入各账户播放历史。
    """
    played_by_user = played_by_user or {}
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE album (id INTEGER PRIMARY KEY, guid TEXT, name TEXT, release_date TEXT);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT);
        CREATE TABLE track (
            id INTEGER PRIMARY KEY, guid TEXT, title TEXT, year INTEGER,
            album_id INTEGER, cover_guid TEXT, disc_no INTEGER, track_no INTEGER,
            isrc TEXT, duration_ms INTEGER, is_cue INTEGER,
            audio_file_id INTEGER,
            is_audio_file_deleted INTEGER DEFAULT 0,
            is_admin_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE audio_file (
            id INTEGER PRIMARY KEY, path TEXT, suffix TEXT, size INTEGER,
            bitrate INTEGER, sample_rate INTEGER, bit_depth INTEGER, channel INTEGER,
            container TEXT, codec TEXT, is_physical_file_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_genre (track_id INTEGER, genre_id INTEGER);
        CREATE TABLE favorite_track (
            id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, updated_at TEXT
        );
        CREATE TABLE play_history (
            id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER,
            play_count INTEGER, updated_at TEXT
        );
        INSERT INTO user VALUES (1, 'u1');
        INSERT INTO user VALUES (2, 'u2');
        """
    )
    track_ids = {}
    for i in range(1, n_tracks + 1):
        con.execute("INSERT INTO album VALUES (?, ?, ?, ?)", (i, f"alg{i}", f"专辑{i}", "2020-01-01"))
        con.execute("INSERT INTO artist VALUES (?, ?, ?)", (i, f"arg{i}", f"歌手{i}"))
        con.execute(
            "INSERT INTO audio_file VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (i, f"/vol1/music/t{i}.flac", "flac", 1000000 + i, 900, 44100, 16, 2, "flac", "flac"),
        )
        con.execute(
            "INSERT INTO track VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0)",
            (i, f"lt{i}", f"本地歌{i}", 2020, i, f"cover{i}", 1, i, f"ISRC{i}", 200000 + i * 1000, i),
        )
        con.execute("INSERT INTO track_artist VALUES (?, ?)", (i, i))
        track_ids[f"lt{i}"] = i
    uid_by_guid = {"u1": 1, "u2": 2}
    for user_guid, plays in played_by_user.items():
        for guid, updated_at in plays:
            con.execute(
                "INSERT INTO play_history (user_id, track_id, play_count, updated_at) VALUES (?, ?, 1, ?)",
                (uid_by_guid[user_guid], track_ids[guid], updated_at),
            )
    con.commit()
    con.close()
    return track_ids


def test_local_random_never_played_first_and_filters_deleted(tmp_path):
    """local-random：从未听过优先随机、不足补最久未听；三级删除标记的曲目一律不选。"""
    db = tmp_path / "music.db"
    # u1：lt8 很久前听过、lt9 最近听过、其余从未听；lt10 物理文件已删、lt7 管理员已删
    _make_local_db(
        db, n_tracks=10,
        played_by_user={"u1": [
            ("lt8", "2026-01-01 08:00:00+08:00"),
            ("lt9", "2026-09-27 22:00:00+08:00"),
        ]},
    )
    con = sqlite3.connect(db)
    con.execute("UPDATE audio_file SET is_physical_file_deleted = 1 WHERE id = 10")
    con.execute("UPDATE track SET is_admin_deleted = 1 WHERE id = 7")
    con.commit()
    con.close()

    rows = dailyrec.read_local_random_tracks(str(db), "u1", 10)
    guids = [r["guid"] for r in rows]
    assert "lt10" not in guids and "lt7" not in guids  # 已删文件不入池
    never_played = {f"lt{i}" for i in range(1, 7)}
    assert never_played <= set(guids[:6])  # 从未听过的 6 首排在最前
    assert guids.index("lt8") < guids.index("lt9")     # 不足时最久未听（lt8）先于最近听过（lt9）
    # 排除集生效：把最近听过的 lt9 排除后不再出现
    rows2 = dailyrec.read_local_random_tracks(str(db), "u1", 10, exclude_guids={"lt9"})
    assert "lt9" not in [r["guid"] for r in rows2]
    # 行形状可直接构建官方形态 track
    track = dailyrec.build_local_track(rows[0])
    assert track["guid"] == rows[0]["guid"] and not track["guid"].startswith("online:")
    assert track["artists"] and track["artists"][0]["name"]
    assert track["album"]["name"] and isinstance(track["audioSpec"], dict)
    assert track["audioSpec"]["path"].endswith(".flac")
    assert track["duration"] == rows[0]["duration_ms"]


def test_purge_stale_daily_cache_keeps_today_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    user = "user-rec-1"
    folder = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name(user))
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "20260830.json"), "w").write("{}")            # 旧格式（无前缀）
    open(os.path.join(folder, "daily-20260831.json"), "w").write("{}")
    open(os.path.join(folder, "hot-20260831.json"), "w").write("{}")
    open(os.path.join(folder, "daily-20260901.json"), "w").write("{}")
    dailyrec.purge_stale_daily_cache(user, "20260831")
    assert not os.path.exists(os.path.join(folder, "20260830.json"))        # 旧格式一并清理
    assert not os.path.exists(os.path.join(folder, "daily-20260901.json"))
    assert os.path.exists(os.path.join(folder, "daily-20260831.json"))
    assert os.path.exists(os.path.join(folder, "hot-20260831.json"))


def test_collect_exclude_sets_from_favorites():
    guids, tas = dailyrec.collect_exclude_sets(
        [{"guid": "online:migu:1", "track": {"title": "晴天", "artist": "周杰伦"}}],
        [{"guid": "local-1", "title": "七里香", "artist": "周杰伦"}],
    )
    assert "online:migu:1" in guids
    assert dailyrec.identity_key("晴天", "周杰伦") in tas
    assert dailyrec.identity_key("七里香", "周杰伦") in tas


@pytest.mark.anyio
async def test_daily_fills_twenty_online_and_skips_favorites(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    recs = [
        {"title": "已收藏", "artist": "收藏歌手", "dimension": "artist", "reason": "should skip"},
    ] + [
        {"title": f"新歌{i}", "artist": f"歌手{i}", "dimension": "genre", "reason": "n"}
        for i in range(30)
    ]

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(recs)}}]},
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        if request.url.path != "/search":
            return httpx.Response(404)
        kw = request.url.params.get("keyword") or ""
        if "已收藏" in kw or "收藏歌手" in kw:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:fav1",
                        "source": "migu",
                        "title": "已收藏",
                        "artist": "收藏歌手",
                        "duration_s": 200,
                        "ext": "mp3",
                    }],
                },
            )
        import re
        m = re.search(r"(\d+)", kw)
        i = int(m.group(1)) if m else 99
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [{
                    "id": f"migu:{i}",
                    "source": "migu",
                    "title": f"新歌{i}",
                    "artist": f"歌手{i}",
                    "duration_s": 200,
                    "ext": "mp3",
                }],
            },
        )

    from proxy.app import build_online_track

    yesterday = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name("u-fill"), "20200101.json")
    os.makedirs(os.path.dirname(yesterday), exist_ok=True)
    with open(yesterday, "w") as f:
        f.write("{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
                base_url="http://127.0.0.1:8770",
            ) as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-fill",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
            favorite_items=[{
                "guid": "online:migu:fav1",
                "track": {"title": "已收藏", "artist": "收藏歌手"},
            }],
        )
    assert payload["status"] == "ready"
    assert len(payload["tracks"]) == 20
    titles = [str(t.get("title")) for t in payload["tracks"]]
    guids = [str(t.get("guid")) for t in payload["tracks"]]
    assert "已收藏" not in titles
    assert "online:migu:fav1" not in guids
    assert not os.path.exists(yesterday)
    cached = dailyrec.load_daily_cache("u-fill", dailyrec.today_key())
    assert cached is not None
    assert len(cached["tracks"]) == 20


def test_healthz_includes_llm_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"code": 99999})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert "llm" in resp.json()
        assert resp.json()["llm"] in ("disabled", "enabled")
        rec = resp.json()["recommend"]
        assert rec["mode"] == "per-user: source-slot -> llm -> local-random"
        assert rec["netease"] is True
        assert isinstance(rec["source_slot_claimed"], bool)
        assert isinstance(rec["recent"], dict)


@pytest.mark.anyio
async def test_get_or_build_daily_uses_llm_when_netease_disabled(monkeypatch):
    """网易音源未启用且配置了 LLM 时走大模型，不能被 fallback 竞速取消。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "gpt-test")
    llm_calls = {"n": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        content = json.dumps([
            {"title": f"LLM歌{i}", "artist": f"LLM歌手{i}", "dimension": "llm", "reason": "test"}
            for i in range(30)
        ])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        kw = request.url.params.get("keyword") or ""
        title = kw.split()[-1] if kw else "x"
        return httpx.Response(
            200,
            json={"items": [{"id": f"migu:{abs(hash(kw)) % 100000}", "source": "migu", "title": title, "artist": "A", "duration_s": 180, "ext": "mp3"}]},
        )

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-llm-first",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
        )
    assert llm_calls["n"] >= 1
    assert len(payload["tracks"]) >= 1
    assert "llm" in payload["tiers"]
    # 至少部分曲目应来自 LLM 候选标题
    titles = " ".join(str(t.get("title") or "") for t in payload["tracks"])
    assert "LLM歌" in titles


def _mb_detail_rows(n: int, prefix: str, sid_base: int = 90000) -> list[dict]:
    return [
        {
            "song_id": sid_base + i,
            "name": f"{prefix}{i}",
            "artist": f"网易歌手{i % 5}",
            "album_name": f"专辑{i}",
            "album_pic_url": f"http://img/{sid_base + i}.jpg",
            "duration_ms": 210000,
            "has_sq": i % 4 == 0,
            "has_hr": False,
        }
        for i in range(n)
    ]


@pytest.mark.anyio
async def test_daily_prefers_netease_daily_and_skips_llm(tmp_path, monkeypatch):
    """网易启用时：真·每日推荐直接命中（平台 ID 直连），LLM 完全不调用。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    llm_calls = {"n": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        return httpx.Response(500)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            assert request.url.params.get("limit") == str(dailyrec.NETEASE_DAILY_LIMIT)
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推"), "logged_in": True})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-netease-daily",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=True,
            favorite_items=[{"guid": "online:netease:90000", "track": {"title": "网易日推0", "artist": "网易歌手0"}}],
        )
    assert payload["tiers"] == ["netease-daily"]
    assert payload["status"] == "ready"
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:netease:") for t in payload["tracks"])
    titles = [str(t.get("title")) for t in payload["tracks"]]
    assert "网易日推0" not in titles  # 已收藏跳过
    assert "网易日推1" in titles
    assert llm_calls["n"] == 0  # 网易启用时绝不调 LLM
    summary = dailyrec.last_recommend_summary().get("u-netease-daily:daily")
    assert summary and summary["tiers"] == ["netease-daily"]


@pytest.mark.anyio
async def test_hot_playlist_uses_netease_charts(tmp_path, monkeypatch):
    """热门推荐歌单：网易热歌榜免登录可用，封面取第一首有封面的歌。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"type": "not_logged_in", "message": "未登录或登录已过期"}},
            )
        if request.url.path == "/api/v1/toplist":
            assert request.url.params.get("index") == str(dailyrec.NETEASE_TOPLIST_INDEX)
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-netease-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            kind="hot",
        )
    assert payload["kind"] == "hot"
    assert payload["tiers"] == ["netease-charts"]
    assert payload["guid"].startswith("online:playlist:hot:")
    assert payload["playlist"]["name"] == "热门推荐"
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:netease:") for t in payload["tracks"])
    # 封面 = 第一个有 cover_url 的歌（第一首在列）；coverId 为其独立盐伪装 id，可反解
    assert payload["playlist"]["coverId"] == payload["tracks"][0]["coverId"]
    assert resolve_real_guid(payload["playlist"]["coverId"]) == payload["tracks"][0]["guid"]
    assert str(payload["tracks"][0].get("cover_url") or "").startswith("http://img/70000")


@pytest.mark.anyio
async def test_daily_skips_charts_when_not_logged_in(tmp_path, monkeypatch):
    """未登录时每日推荐不再借用榜单（榜单归热门歌单），无 LLM/音源则为空。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    paths = []

    def mb_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"type": "not_logged_in", "message": "未登录或登录已过期"}},
            )
        if request.url.path == "/api/v1/toplist":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-daily-no-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
        )
    assert "/api/v1/toplist" not in paths  # daily 链不再请求榜单
    assert payload["kind"] == "daily"
    assert payload["tracks"] == []
    assert payload["tiers"] == []


@pytest.mark.anyio
async def test_hot_uses_lx_charts_when_musicbox_unavailable(tmp_path, monkeypatch):
    """musicbox 全挂时热门歌单降级 lxmusic 免登录榜单；全无封面则 coverId 指向歌单自身。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    def lx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend":
            items = [
                {
                    "id": f"lx:kg:HASH{i}",
                    "lx_source": "kg",
                    "title": f"榜单歌{i}",
                    "artist": f"酷狗歌手{i}",
                    "album": "",
                    "duration_s": 200,
                    "ext": "mp3",
                    "cover_url": "",
                }
                for i in range(25)
            ]
            return httpx.Response(200, json={"ok": True, "items": items, "errors": {}})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770") as mb, \
            httpx.AsyncClient(transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772") as lx:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-lx-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            lx_client=lx,
            lx_enabled=True,
            kind="hot",
        )
    assert payload["kind"] == "hot"
    assert payload["tiers"] == ["lx-charts"]
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:lx:kg:") for t in payload["tracks"])
    # lx 榜单曲目 cover_url 全为空：v2.6.12 起在线曲目恒有 coverId（独立盐伪装），
    # 封面端点可按源直构/网易兜底出图，歌单 coverId 取第一首的 coverId（可反解）
    assert payload["playlist"]["coverId"] == payload["tracks"][0]["coverId"]
    assert resolve_real_guid(payload["playlist"]["coverId"]) == payload["tracks"][0]["guid"]


# ------------------------------------------------ 推荐歌单封面取曲

def test_pick_playlist_cover_track():
    tracks = [
        {"guid": "online:a:1", "cover_url": ""},
        {"guid": "online:a:2", "cover_url": f"https://{dailyrec.KW_TEXT_COVER_HOST}/pic?rid=1"},  # 酷我文本页假封面
        {"guid": "online:a:3", "cover_url": "https://img.example/3.jpg"},
        {"guid": "online:a:4", "cover_url": "https://img.example/4.jpg"},
    ]
    assert dailyrec.pick_playlist_cover_track(tracks)["guid"] == "online:a:3"
    assert dailyrec.pick_playlist_cover_track([]) is None
    assert dailyrec.pick_playlist_cover_track(None) is None
    assert dailyrec.pick_playlist_cover_track([{"guid": "x", "cover_url": ""}]) is None
    assert dailyrec.pick_playlist_cover_track(
        [{"guid": "x", "cover_url": ""}, {"guid": "y", "cover_url": "https://a/1.jpg"}]
    )["guid"] == "y"


def test_recommend_playlist_guid_kinds():
    day = "20260921"
    daily = dailyrec.recommend_playlist_guid("daily", day, "user-x")
    hot = dailyrec.recommend_playlist_guid("hot", day, "user-x")
    assert daily.startswith("online:playlist:daily:20260921:")
    assert hot.startswith("online:playlist:hot:20260921:")
    assert dailyrec.online_playlist_kind(daily) == "daily"
    assert dailyrec.online_playlist_kind(hot) == "hot"
    assert dailyrec.online_playlist_kind("online:migu:1") == ""
    assert dailyrec.is_recommend_playlist_guid(daily) and dailyrec.is_recommend_playlist_guid(hot)
    assert not dailyrec.is_recommend_playlist_guid("online:migu:1")


@pytest.mark.anyio
async def test_daily_cover_skips_coverless_tracks(tmp_path, monkeypatch):
    """构建出的歌单封面 = 第一个有封面直链的歌（第一首无封面时跳到第二首）。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    rows = [
        {
            "song_id": 51000, "name": "无封面歌", "artist": "歌手A", "album_name": "专辑A",
            "album_pic_url": "", "duration_ms": 210000, "has_sq": False, "has_hr": False,
        },
    ] + _mb_detail_rows(24, "有封面歌", sid_base=51001)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/recommend/songs"
        return httpx.Response(200, json={"ok": True, "data": rows, "logged_in": True})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-cover-pick",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
        )
    assert payload["tracks"][0]["title"] == "无封面歌"
    assert not payload["tracks"][0].get("cover_url")
    # v2.6.16：歌单 coverId 取第一个有封面歌的 coverId（独立盐纯 32-hex），可反解回其真实 guid
    assert payload["playlist"]["coverId"] == payload["tracks"][1]["coverId"]
    assert resolve_real_guid(payload["playlist"]["coverId"]) == payload["tracks"][1]["guid"]
    assert payload["tracks"][1].get("cover_url")


def test_playlist_list_injects_both_playlists_with_disguised_cover(tmp_path, monkeypatch):
    """双开关开启：每日推荐与热门推荐两条歌单都注入，coverId 伪装为官方 track_+32hex。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", True)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推"), "logged_in": True})
        if request.url.path == "/api/v1/toplist":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        recs = [it for it in data["list"] if dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))]
        assert len(recs) == 2
        assert data["total"] == len(data["list"])
        daily = next(it for it in recs if dailyrec.online_playlist_kind(str(it["guid"])) == "daily")
        hot = next(it for it in recs if dailyrec.online_playlist_kind(str(it["guid"])) == "hot")
        assert "每日推荐" in daily["name"]
        assert hot["name"] == "热门推荐"
        # coverId 伪装为纯 32-hex（v2.6.16 独立盐，列表行可渲染），且可反解回第一个有封面的歌
        import re as _re
        for rec in (daily, hot):
            cover = str(rec["coverId"])
            assert _re.fullmatch(r"[0-9a-f]{32}", cover)
            resolved = resolve_real_guid(cover)
            assert str(resolved).startswith("online:netease:")


@pytest.mark.anyio
async def test_resolve_recommendations_concurrency_and_throttle(monkeypatch):
    active = 0
    max_active = 0
    search_timestamps = []

    async def mock_search_keyword(keyword, *args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        search_timestamps.append(time.monotonic())
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1
            return [{
                "id": f"netease:{keyword}",
                "source": "netease",
                "title": keyword,
                "artist": "Artist",
                "album": "Album",
                "duration_s": 200,
                "ext": "mp3",
            }]

    monkeypatch.setattr(dailyrec, "_search_keyword", mock_search_keyword)
    monkeypatch.setattr(dailyrec, "RECOMMEND_SEARCH_CONCURRENCY", 2)
    monkeypatch.setattr(dailyrec, "RECOMMEND_SEARCH_INTERVAL", 0.05)

    recs = [{"title": f"Song {i}", "artist": "Artist"} for i in range(6)]
    t0 = time.monotonic()
    results = await dailyrec.resolve_recommendations(
        recs=recs,
        musicdl_client=None,
        musicbox_client=None,
        netease_enabled=True,
        build_track=lambda pick: {"guid": f"online:{pick['id']}", "title": pick["title"], "artist": pick["artist"]},
        limit=6,
    )

    assert max_active <= 2
    assert len(results) == 6
    assert len(search_timestamps) >= 6
    assert time.monotonic() - t0 >= 0.12


# -------------------------------- 多用户推荐隔离（issue #25）

def _seed_recommend_cache(user: str, kind: str, titles: list[str]) -> str:
    """直接落一份今日推荐缓存并返回歌单 guid（绕开构建链，聚焦注入隔离）。"""
    day = dailyrec.today_key()
    guid = dailyrec.recommend_playlist_guid(kind, day, user)
    safe = dailyrec._safe_user_name(user)
    tracks = [
        {
            "guid": f"online:migu:{safe}-{i}",
            "title": t,
            "artist": "歌手甲",
            "album": f"专辑{kind}",
        }
        for i, t in enumerate(titles)
    ]
    payload = {
        "day": day,
        "kind": kind,
        "guid": guid,
        "status": "ready",
        "playlist": dailyrec.build_playlist_record(
            guid, dailyrec.playlist_display_name(kind, day), guid, len(tracks)
        ),
        "tracks": dailyrec.stamp_playlist_tracks(tracks),
        "tiers": ["netease-daily"],
        "seedCount": 1,
        "favoriteCount": 0,
        "builtAt": 1,
    }
    dailyrec.save_daily_cache(user, day, payload, kind)
    return guid


def _switchable_auth(current: dict) -> httpx.MockTransport:
    """可切换当前用户 guid 的上游 mock（/user/me 缺 guid 时回落 shared）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            data = {"guid": current["guid"]} if current.get("guid") else {}
            return httpx.Response(200, json={"code": 0, "data": data})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1}], "total": 1}},
            )
        return httpx.Response(200, json={"code": 0, "data": None})

    return httpx.MockTransport(handler)


def test_playlist_list_daily_isolated_between_users(tmp_path, monkeypatch):
    """a) 两个真实 guid 用户各自注入各自的 daily（不同缓存不同歌单）。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", False)
    guid_a = _seed_recommend_cache("user-a", "daily", ["甲歌1", "甲歌2"])
    guid_b = _seed_recommend_cache("user-b", "daily", ["乙歌1", "乙歌2"])
    assert guid_a != guid_b

    current = {"guid": "user-a"}
    app.state.upstream_client = httpx.AsyncClient(transport=_switchable_auth(current), base_url="http://unix")
    with TestClient(app) as client:
        resp_a = client.get("/music/api/v1/playlist/list").json()
        recs_a = [it for it in resp_a["data"]["list"] if dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))]
        assert [it["guid"] for it in recs_a] == [guid_a]
        assert recs_a[0]["trackCount"] == 2

        tracks_a = client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": guid_a, "page": 1, "size": 50}).json()
        assert {t["title"] for t in tracks_a["data"]["list"]} == {"甲歌1", "甲歌2"}

        current["guid"] = "user-b"
        resp_b = client.get("/music/api/v1/playlist/list").json()
        recs_b = [it for it in resp_b["data"]["list"] if dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))]
        assert [it["guid"] for it in recs_b] == [guid_b]

        tracks_b = client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": guid_b, "page": 1, "size": 50}).json()
        assert {t["title"] for t in tracks_b["data"]["list"]} == {"乙歌1", "乙歌2"}


def test_playlist_list_shared_user_skips_daily_keeps_hot(tmp_path, monkeypatch):
    """b/c) 身份探测失败（shared）不注入个性化 daily；hot 为公共榜单允许注入。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", True)
    _seed_recommend_cache("user-a", "daily", ["甲歌1", "甲歌2"])  # 真实用户缓存存在也不得挂给 shared
    hot_guid = _seed_recommend_cache("shared", "hot", ["热歌1", "热歌2"])

    current = {"guid": ""}  # user/me 缺 data.guid → _probe_upstream_auth 回落 shared
    app.state.upstream_client = httpx.AsyncClient(transport=_switchable_auth(current), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        recs = [it for it in resp.json()["data"]["list"] if dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))]
        kinds = {dailyrec.online_playlist_kind(str(it["guid"])): it for it in recs}
        assert "daily" not in kinds  # shared 跳过个性化 daily
        assert kinds["hot"]["guid"] == hot_guid  # hot 公共榜单照常注入
        assert kinds["hot"]["trackCount"] == 2
        names = [it.get("name") for it in resp.json()["data"]["list"]]
        assert "牛一" in names  # 官方歌单不受影响


def test_shared_session_never_returns_others_daily_nor_builds_shared_daily(tmp_path, monkeypatch):
    """d) shared 场景绝不返回其他用户的 daily bundle，且不为 shared 构建个性化 daily。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", False)
    user_a_guid = _seed_recommend_cache("user-a", "daily", ["甲歌1", "甲歌2"])

    current = {"guid": ""}
    app.state.upstream_client = httpx.AsyncClient(transport=_switchable_auth(current), base_url="http://unix")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/playlist/detail", params={"guid": user_a_guid}).json()
        assert detail["code"] == 0
        assert detail["data"]["trackCount"] == 0
        assert detail["data"]["guid"] != user_a_guid

        tracks = client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": user_a_guid, "page": 1, "size": 50}).json()
        assert tracks["data"]["total"] == 0
        assert tracks["data"]["list"] == []

        batch = client.get("/music/api/v1/playlist/batch-detail", params={"guids": user_a_guid}).json()
        rec = batch["data"]["list"][0]
        assert dailyrec.online_playlist_kind(str(rec["guid"])) != "daily" or rec["trackCount"] == 0

    # 生成侧同样收口：shared 会话不触发 daily 构建（无 shared/daily-*.json 落盘）
    shared_folder = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name("shared"))
    assert not os.path.exists(os.path.join(shared_folder, f"daily-{dailyrec.today_key()}.json"))

    # shared 播放历史只落在 shared 自己的文件里，绝不进入真实用户的 daily 种子
    dailyrec.record_online_play("shared", "online:migu:shared-song", {"title": "共享歌", "artist": "某歌手"})
    assert dailyrec.load_online_play_history("user-a") == []
    assert all(s.get("guid") != "online:migu:shared-song" for s in dailyrec.seeds_from_online_history("user-a"))



# ====================================================================
# issue #23 / #29：逐首可播校验、渐进上架、tier 渐进 checkpoint、预算可配置
# ====================================================================


class _SlowTransport(httpx.AsyncBaseTransport):
    """延迟 N 秒后才响应的异步传输，用于模拟慢 LLM / 慢音源。"""

    def __init__(self, delay_s: float, response: httpx.Response):
        self.delay_s = delay_s
        self.response = response
        self.hits = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hits += 1
        await asyncio.sleep(self.delay_s)
        return self.response


def _playable_musicdl_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/stream":
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
    if request.url.path == "/search":
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦",
            "album": "叶惠美", "duration_s": 269, "ext": "mp3",
        }]})
    return httpx.Response(404)


def _dead_stream_musicdl_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/stream":
        return httpx.Response(404)
    if request.url.path == "/search":
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦",
            "album": "叶惠美", "duration_s": 269, "ext": "mp3",
        }]})
    return httpx.Response(404)


@pytest.mark.anyio
async def test_resolve_recommendations_drops_unplayable_track(monkeypatch):
    """issue #23：候选命中但可播校验失败（/stream 404）必须被丢弃。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    from proxy.app import build_online_track

    recs = [{"title": "晴天", "artist": "周杰伦", "dimension": "artist"}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_dead_stream_musicdl_handler), base_url="http://127.0.0.1:8768",
    ) as mdl, httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770",
    ) as mb:
        out = await dailyrec.resolve_recommendations(
            recs, mdl, mb, False, build_online_track,
        )
    assert out == []


@pytest.mark.anyio
async def test_resolve_recommendations_emits_progressively(monkeypatch):
    """issue #29：on_track 每入列一首即回调（渐进 checkpoint 的数据来源）。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    from proxy.app import build_online_track

    recs = [
        {"title": "晴天", "artist": "周杰伦", "dimension": "artist"},
        {"title": "七里香", "artist": "周杰伦", "dimension": "genre"},
    ]
    emitted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        kw = request.url.params.get("keyword") or "歌"
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": f"migu:{abs(hash(kw)) % 9999}", "source": "migu",
            "title": kw.split()[-1], "artist": "A", "duration_s": 200, "ext": "mp3",
        }]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8768",
    ) as mdl, httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770",
    ) as mb:
        out = await dailyrec.resolve_recommendations(
            recs, mdl, mb, False, build_online_track, on_track=emitted.append,
        )
    assert len(out) == 2
    assert emitted == out  # 回调与最终列表一一对应


@pytest.mark.anyio
async def test_llm_timeout_falls_back_to_local_random(tmp_path, monkeypatch):
    """issue #29 延续：LLM 层超时不再静默——失败原因落盘 tierFailures，
    local-random 层兜底保证歌单不再为空（本地曲库随机补齐）。"""
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=25)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    monkeypatch.setattr(dailyrec, "build_budget_s", lambda: 3.0)
    from proxy.app import build_online_track

    slow_llm = _SlowTransport(30.0, httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]}))
    async with httpx.AsyncClient(transport=slow_llm, base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(
                transport=httpx.MockTransport(_playable_musicdl_handler), base_url="http://127.0.0.1:8768",
            ) as mdl, httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770",
            ) as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u1",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
        )
    assert any(f.startswith("llm:") for f in payload.get("tierFailures") or [])
    assert "local-random" in payload["tiers"]
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(not str(t["guid"]).startswith("online:") for t in payload["tracks"])
    # healthz 摘要同样带上失败原因
    info = dailyrec.last_recommend_summary().get("u1:daily") or {}
    assert info.get("tierFailures")


@pytest.mark.anyio
async def test_daily_case1_no_source_no_llm_local_random(tmp_path, monkeypatch):
    """情况1：音源不支持每日推荐且未配置 LLM -> 本地曲库随机，各账户内容不同。"""
    db = tmp_path / "music.db"
    _make_local_db(
        db, n_tracks=30,
        played_by_user={
            "u1": [("lt1", "2026-09-27 21:00:00+08:00")],
            "u2": [("lt2", "2026-09-27 21:00:00+08:00")],
        },
    )
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    monkeypatch.delenv("FNMUSIC_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("FNMUSIC_LLM_API_KEY", raising=False)
    from proxy.app import build_online_track

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    ) as mb:
        payload_a = await dailyrec.get_or_build_daily(
            user_guid="u1", musicdl_client=None, musicbox_client=mb, llm_http=None,
            build_track=build_online_track, netease_enabled=True,
        )
        payload_b = await dailyrec.get_or_build_daily(
            user_guid="u2", musicdl_client=None, musicbox_client=mb, llm_http=None,
            build_track=build_online_track, netease_enabled=True,
        )
    for payload in (payload_a, payload_b):
        assert payload["tiers"] == ["local-random"]
        assert payload["status"] == "ready"
        assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
        assert all(not str(t["guid"]).startswith("online:") for t in payload["tracks"])
    guids_a = {t["guid"] for t in payload_a["tracks"]}
    guids_b = {t["guid"] for t in payload_b["tracks"]}
    assert guids_a != guids_b  # 各账户不同
    assert "lt1" not in guids_a  # 最近听过被排除
    assert "lt2" not in guids_b


@pytest.mark.anyio
async def test_daily_case2_source_slot_first_netease_others_local(tmp_path, monkeypatch):
    """情况2：网易可用但无 LLM -> 第一个用户用网易日推并占名额，
    第二个用户不再调网易、改走本地曲库随机，两份歌单不同。"""
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=30)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    mb_calls = {"n": 0}

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            mb_calls["n"] += 1
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推")})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        first = await dailyrec.get_or_build_daily(
            user_guid="u1", musicdl_client=None, musicbox_client=mb, llm_http=None,
            build_track=build_online_track, netease_enabled=True,
        )
        assert first["tiers"] == ["netease-daily"]
        assert len(first["tracks"]) == dailyrec.PLAYLIST_SIZE
        assert dailyrec.source_slot_claimed()  # 名额已被第一个用户占用

        second = await dailyrec.get_or_build_daily(
            user_guid="u2", musicdl_client=None, musicbox_client=mb, llm_http=None,
            build_track=build_online_track, netease_enabled=True,
        )
    assert mb_calls["n"] == 1  # 第二个用户没有再调网易日推
    assert second["tiers"] == ["local-random"]
    assert all(not str(t["guid"]).startswith("online:") for t in second["tracks"])
    first_guids = {t["guid"] for t in first["tracks"]}
    second_guids = {t["guid"] for t in second["tracks"]}
    assert first_guids and second_guids and first_guids != second_guids

    # 音源集合变化后名额释放：清缓存后下一个用户可重新用网易日推
    dailyrec.invalidate_today_cache_all_users()
    assert not dailyrec.source_slot_claimed()


@pytest.mark.anyio
async def test_daily_case3_source_slot_first_netease_others_llm(tmp_path, monkeypatch):
    """情况3：网易可用且配置了 LLM -> 第一个用户用网易日推，其余用户走 LLM。"""
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=30)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "gpt-test")
    mb_calls = {"n": 0}
    llm_calls = {"n": 0}

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            mb_calls["n"] += 1
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推")})
        return httpx.Response(404, json={"ok": False})

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        content = json.dumps([
            {"title": f"LLM歌{i}", "artist": f"LLM歌手{i}", "dimension": "llm", "reason": "test"}
            for i in range(30)
        ])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        # 按关键词变化：每个 LLM 候选都能解析到不同可播曲目（身份去重后仍凑得满 20）
        if request.url.path == "/stream":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        kw = request.url.params.get("keyword") or ""
        title = kw.split()[-1] if kw else "x"
        return httpx.Response(200, json={"items": [{
            "id": f"migu:{abs(hash(kw)) % 100000}", "source": "migu",
            "title": title, "artist": "A", "duration_s": 180, "ext": "mp3",
        }]})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb, \
            httpx.AsyncClient(
                transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768",
            ) as mdl:
        first = await dailyrec.get_or_build_daily(
            user_guid="u1", musicdl_client=mdl, musicbox_client=mb, llm_http=llm_client,
            build_track=build_online_track, netease_enabled=True,
        )
        llm_after_first = llm_calls["n"]
        second = await dailyrec.get_or_build_daily(
            user_guid="u2", musicdl_client=mdl, musicbox_client=mb, llm_http=llm_client,
            build_track=build_online_track, netease_enabled=True,
        )
    assert first["tiers"] == ["netease-daily"] and llm_after_first == 0
    assert mb_calls["n"] == 1
    assert second["tiers"] == ["llm"]
    assert llm_calls["n"] >= 1
    assert all(str(t["guid"]).startswith("online:") for t in second["tracks"])


def test_playlist_serves_local_random_tracks_and_cover_passthrough(tmp_path, monkeypatch):
    """本地随机歌单下发兼容：真实官方 guid 原样穿透（不做 online 伪装），
    歌单封面取本地曲目官方 coverId 并透传官方静态封面端点。"""
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=25)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setitem(CONF, "netease_enabled", False)

    cover_calls = {"n": 0, "id": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "u1"}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        if path == "/music/api/v1/static/cover":
            cover_calls["n"] += 1
            cover_calls["id"] = request.url.params.get("coverId")
            return httpx.Response(200, content=b"\x89PNG-fake", headers={"content-type": "image/png"})
        return httpx.Response(200, json={"code": 0, "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})), base_url="http://127.0.0.1:8770"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        names = [x.get("name") for x in resp.json()["data"]["list"]]
        assert any("每日推荐" in str(n) for n in names)
        daily = resp.json()["data"]["list"][0]
        assert daily["trackCount"] == dailyrec.PLAYLIST_SIZE
        assert not str(daily["coverId"]).startswith("track_")  # 官方封面 guid 原样下发
        assert str(daily["coverId"]).startswith("cover")

        tracks_resp = client.get(
            "/music/api/v1/track/playlist-detail/list",
            params={"playlistGUID": daily["guid"], "page": 1, "size": 50},
        )
        tracks = tracks_resp.json()["data"]["list"]
        assert len(tracks) == dailyrec.PLAYLIST_SIZE
        # 真实官方 guid 不做伪装，反解即自身；audioSpec 带真实路径与时长
        for t in tracks:
            g = str(t["guid"])
            assert g.startswith("lt") and resolve_real_guid(g) == g
            assert t["audioSpec"]["path"].endswith(".flac")
            assert t["duration"] > 0
            assert t["artists"] and t["artists"][0]["name"]
            assert t["album"]["name"]

        cover = client.get("/music/api/v1/static/cover", params={"coverId": daily["coverId"]})
        assert cover.status_code == 200
        assert cover.content[:8] == b"\x89PNG\r\n\x1a\n"  # 本地解析兜底占位图
        # v2.6.8 起：官方封面 guid 不再透传官方 /static/cover（对本地 guid 固定 400），
        # 改为服务端本地解析（内嵌图/网易兜底/占位图），上游封面端点零调用
        assert cover_calls["n"] == 0


def test_invalidate_today_cache_all_users(tmp_path):
    """issue #22：音源集合变化清掉所有用户当日缓存，隔日缓存不受影响。"""
    day = dailyrec.today_key()
    for user in ("u1", "u2"):
        folder = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name(user))
        os.makedirs(folder, exist_ok=True)
        for name in (f"daily-{day}.json", f"hot-{day}.json", "daily-20200101.json"):
            with open(os.path.join(folder, name), "w") as f:
                f.write('{"tracks": [1]}')

    removed = dailyrec.invalidate_today_cache_all_users()
    assert removed == 4
    for user in ("u1", "u2"):
        folder = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name(user))
        assert sorted(os.listdir(folder)) == ["daily-20200101.json"]


def test_playlist_list_serves_partial_checkpoint_progressively(tmp_path, monkeypatch):
    """issue #23：构建任务尚未完成但已有 1 首 checkpoint 时，歌单列表立即以
    当前进度上架（trackCount>=1），不再空等整链构建。"""
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")

    from proxy.app import build_online_track

    day = dailyrec.today_key()
    full_bundle = {"hits": 0}

    async def fake_build(**kwargs):
        # 0.6s 后先落 1 首 checkpoint，再拖到 1.5s 才完整结束——peek 的 2s 窗口
        # 应在 ~0.6-1.0s 之间看到 checkpoint 并立即返回，而不是等 full bundle
        full_bundle["hits"] += 1
        user_guid = kwargs.get("user_guid")
        kind = kwargs.get("kind") or "daily"
        track = build_online_track({
            "id": "migu:1", "source": "migu", "title": "晴天", "artist": "周杰伦",
            "album": "叶惠美", "duration_s": 269, "ext": "mp3",
        })
        first_cp = {
            "day": day, "kind": kind, "guid": dailyrec.daily_playlist_guid(day, user_guid),
            "status": "partial",
            "playlist": dailyrec.build_playlist_record(
                dailyrec.daily_playlist_guid(day, user_guid), "每日推荐", track.get("guid"), 1,
            ),
            "tracks": [track], "tiers": ["llm"], "seedCount": 0, "favoriteCount": 0,
            "builtAt": int(time.time()),
        }
        await asyncio.sleep(0.6)
        dailyrec.save_daily_cache(user_guid, day, first_cp, kind)
        await asyncio.sleep(1.5)
        full = dict(first_cp)
        full["tracks"] = [track] * 5
        return full

    monkeypatch.setattr(dailyrec, "get_or_build_daily", fake_build)
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_playable_musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.llm_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})
    ), base_url="http://127.0.0.1:9")

    with TestClient(app) as client:
        started = time.monotonic()
        resp = client.get("/music/api/v1/playlist/list")
        elapsed = time.monotonic() - started
        body = resp.json()
        first = body["data"]["list"][0]
        assert first["isDaily"] is True
        assert first["trackCount"] >= 1
        # 未等完整构建（完整结束需 ~2.1s，peek 窗口 2s；命中 checkpoint 应明显更早）
        assert elapsed < 1.8, f"应命中渐进 checkpoint 提前返回，实际等了 {elapsed:.2f}s"
