"""Tests for netease account playlists injection (nmplaylists, FNMUSIC_NETEASE_MY_PLAYLISTS)."""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import nmplaylists as nmpl
from proxy.app import CONF, app, ensure_registry_warm, fake_official_guid, resolve_real_guid
import proxy.app as proxy_app


OWN_UID = 365
PLAYLIST_ROWS = [
    {"playlist_id": 111, "name": "我喜欢的音乐", "cover_url": "http://img/111.jpg",
     "track_count": 9, "created_at": 1700000000, "updated_at": 1700000100,
     "user_id": OWN_UID, "special_type": 5},
    {"playlist_id": 222, "name": "收藏的他人歌单", "cover_url": "http://img/222.jpg",
     "track_count": 3, "created_at": 1700000000, "updated_at": 1700000000,
     "user_id": 999, "special_type": 0},
    {"playlist_id": 333, "name": "我的私歌单", "cover_url": "",
     "track_count": 2, "created_at": 1700000000, "updated_at": 1700000200,
     "user_id": OWN_UID, "special_type": 0},
]
TRACK_ROWS = [
    {"song_id": 101, "name": "晴天", "artist": "周杰伦", "album_name": "叶惠美",
     "album_pic_url": "http://img/a1.jpg", "duration_ms": 269000, "has_sq": True},
    {"song_id": 102, "name": "七里香", "artist": "周杰伦", "album_name": "七里香",
     "album_pic_url": "", "duration_ms": 299000, "has_sq": False},
]


def _musicbox_handler(rows=None, tracks=None, mode="ok"):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/user/playlists":
            state["calls"] += 1
            if mode == "down":
                return httpx.Response(500)
            if mode == "logged_out":
                return httpx.Response(200, json={"ok": False, "error": "not_logged_in", "logged_in": False})
            return httpx.Response(200, json={
                "ok": True, "logged_in": True, "account_uid": OWN_UID, "data": rows if rows is not None else [],
            })
        if path.startswith("/api/v1/user/playlists/") and path.endswith("/tracks"):
            if mode in ("down", "logged_out"):
                return httpx.Response(200, json={"ok": False, "error": "not_logged_in"})
            return httpx.Response(200, json={"ok": True, "data": tracks or []})
        return httpx.Response(404)

    handler.state = state
    return handler


def _auth_upstream(guid="user-nm-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"list": [{"guid": "localpl", "name": "官方歌单", "coverId": "c1",
                                   "createdAt": 1, "updatedAt": 1, "trackCount": 3}],
                         "total": 1},
            })
        if path.endswith("/playlist/batch-detail"):
            return httpx.Response(200, json={
                "code": 0, "data": {"list": [{"guid": "localpl", "trackCount": 3}]},
            })
        if path.endswith("/event/report"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})
        return httpx.Response(200, json={"code": 0, "data": None})
    return handler


@pytest.fixture(autouse=True)
def setup_nm_env(tmp_path, monkeypatch):
    nmpl.reset_for_test()
    monkeypatch.setenv("FNMUSIC_NM_PLAYLISTS_DIR", str(tmp_path / "nm_playlists_cache"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend_cache"))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "play_history"))
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "online_favorites"))
    monkeypatch.setitem(CONF, "plt_dir", str(tmp_path / "playlist_tracks"))
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "netease_my_playlists", True)
    # 关闭推荐注入，聚焦 nm 歌单行为（注入位置断言不依赖推荐构建链）
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setitem(CONF, "recommend_daily", False)
    yield
    nmpl.reset_for_test()


def _wire(musicbox_handler):
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_auth_upstream()), base_url="http://unix"
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicbox_client = client
    return musicbox_handler


def test_playlist_list_injects_own_playlists_before_official():
    mb = _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        lst = resp.json()["data"]["list"]
    guids = [it["guid"] for it in lst]
    # 仅自建歌单注入（收藏的他人歌单 user_id != 账号 uid 不显示），插在官方歌单之前
    assert guids == ["online:playlist:nm:111", "online:playlist:nm:333", "localpl"]
    card = lst[0]
    assert card["name"] == "我喜欢的音乐"
    assert card["isDaily"] is True
    assert card["trackCount"] == 9
    assert card["createdAt"] == 1700000000
    # coverId 伪装为官方 track_+32hex 形态，且假 id 可反解回 nm guid
    assert card["coverId"].startswith("track_") and len(card["coverId"]) == 38
    assert fake_official_guid(card["guid"]) == card["coverId"][6:]
    assert mb.state["calls"] == 1


def test_toggle_off_no_injection():
    mb = _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    CONF["netease_my_playlists"] = False
    try:
        with TestClient(app) as client:
            resp = client.get("/music/api/v1/playlist/list")
            lst = resp.json()["data"]["list"]
        assert [it["guid"] for it in lst] == ["localpl"]
        assert mb.state["calls"] == 0
    finally:
        CONF["netease_my_playlists"] = True


def test_source_disabled_no_injection():
    mb = _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    CONF["netease_enabled"] = False
    try:
        with TestClient(app) as client:
            resp = client.get("/music/api/v1/playlist/list")
            lst = resp.json()["data"]["list"]
        assert [it["guid"] for it in lst] == ["localpl"]
        assert mb.state["calls"] == 0
    finally:
        CONF["netease_enabled"] = True


def test_musicbox_down_silent_and_cooldown():
    mb = _wire(_musicbox_handler(mode="down"))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        lst = resp.json()["data"]["list"]
        assert [it["guid"] for it in lst] == ["localpl"]
        # 失败不影响 total（只有官方 1 条）
        assert resp.json()["data"]["total"] == 1
        # 冷却期内第二次列表不再敲 musicbox
        client.get("/music/api/v1/playlist/list")
    assert mb.state["calls"] == 1


def test_not_logged_in_skips_injection():
    mb = _wire(_musicbox_handler(mode="logged_out"))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        lst = resp.json()["data"]["list"]
        client.get("/music/api/v1/playlist/list")
    assert [it["guid"] for it in lst] == ["localpl"]
    assert mb.state["calls"] == 1  # 未登录同样进入冷却


def test_detail_batch_and_track_list_flow():
    _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        # 详情：卡片字段完整
        resp = client.get("/music/api/v1/playlist/detail", params={"guid": "online:playlist:nm:111"})
        assert resp.status_code == 200
        detail = resp.json()["data"]
        assert detail["guid"] == "online:playlist:nm:111"
        assert detail["name"] == "我喜欢的音乐"
        # 未知歌单：结构化错误
        resp = client.get("/music/api/v1/playlist/detail", params={"guid": "online:playlist:nm:999"})
        assert resp.json()["code"] == -1
        # 批量详情：nm 卡片与官方歌单合并返回
        resp = client.get("/music/api/v1/playlist/batch-detail",
                          params={"guids": "online:playlist:nm:111,localpl"})
        assert resp.status_code == 200
        blist = resp.json()["data"]["list"]
        assert [it["guid"] for it in blist] == ["online:playlist:nm:111", "localpl"]
        # 曲目列表：分页 + 伪装下发（guid 不带 online: 前缀）
        resp = client.get("/music/api/v1/track/playlist-detail/list",
                          params={"playlistGUID": "online:playlist:nm:111", "page": 1, "size": 1})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert len(data["list"]) == 1
        assert not str(data["list"][0]["guid"]).startswith("online:")
        assert data["list"][0]["title"] == "晴天"
        assert isinstance(data["list"][0]["createdAt"], int)
        assert isinstance(data["list"][0]["album"]["createdAt"], int)
        # size=-1 全量
        resp = client.get("/music/api/v1/track/playlist-detail/list",
                          params={"playlistGUID": "online:playlist:nm:111", "page": 1, "size": -1})
        assert len(resp.json()["data"]["list"]) == 2


def test_cover_redirect(monkeypatch):
    _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    # 封面直链改同源回源（200 字节），不再 302 跳外部图床
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-nm-cover"
    monkeypatch.setattr(
        "proxy.app._COVER_CDN_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})),
    )
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        fake = fake_official_guid("online:playlist:nm:111")
        resp = client.get("/music/api/v1/static/cover", params={"coverId": f"track_{fake}"}, follow_redirects=False)
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")
        # 无封面直链的私歌单：404（客户端回落默认样式）
        fake333 = fake_official_guid("online:playlist:nm:333")
        resp = client.get("/music/api/v1/static/cover", params={"coverId": f"track_{fake333}"}, follow_redirects=False)
        assert resp.status_code == 404


def test_readonly_guards_absorb_writes():
    _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    with TestClient(app) as client:
        for path in ("/music/api/v1/playlist/add-track", "/music/api/v1/playlist/remove-track"):
            resp = client.post(path, json={"guid": "online:playlist:nm:111", "trackGUIDs": ["whatever"]})
            assert resp.status_code == 200
            assert resp.json()["code"] == 0
        resp = client.post("/music/api/v1/playlist/delete", json={"guid": "online:playlist:nm:111"})
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


def test_disk_cache_restart_recovery(tmp_path):
    _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        client.get("/music/api/v1/track/playlist-detail/list",
                   params={"playlistGUID": "online:playlist:nm:111", "size": -1})
        track_fake = fake_official_guid("online:netease:101")
    # 模拟重启：内存清空，磁盘缓存仍在；musicbox 已不可用
    nmpl.reset_for_test()
    down = _wire(_musicbox_handler(mode="down"))
    assert nmpl.card_for(111) is not None
    assert nmpl.card_for(111)["name"] == "我喜欢的音乐"
    assert nmpl.cover_url_for(111) == "http://img/111.jpg"
    # 裸假 id（反查表未重建）也能定位歌单
    assert nmpl.playlist_id_from_cover_request("", "track_" + fake_official_guid("online:playlist:nm:111")) == 111
    # musicbox 不可用时曲目回落磁盘
    assert [t["guid"] for t in nmpl.cached_tracks(111)] == ["online:netease:101", "online:netease:102"]
    # ensure_registry_warm 扫描 nm 缓存重建曲目假 id 反查
    proxy_app._REGISTRY_WARMED = False
    try:
        ensure_registry_warm()
        assert resolve_real_guid(track_fake) == "online:netease:101"
    finally:
        proxy_app._REGISTRY_WARMED = True
    assert down.state["calls"] == 0  # 全程未触达已挂的 musicbox


def test_summaries_cached_in_memory():
    mb = _wire(_musicbox_handler(rows=PLAYLIST_ROWS, tracks=TRACK_ROWS))
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        client.get("/music/api/v1/playlist/list")
    assert mb.state["calls"] == 1  # 5 分钟 TTL 内命中内存缓存
