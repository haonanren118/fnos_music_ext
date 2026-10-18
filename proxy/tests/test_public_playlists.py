"""公共歌单注入、曲目缓存与只读保护测试。"""
import json

import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import publicplaylists as pubpl
from proxy.app import CONF, app, fake_official_guid, resolve_real_guid


SELECTION = [
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"},
]
SUMMARY = {
    "id": "3778678", "kind": "chart", "source": "netease", "name": "热歌榜",
    "cover_url": "http://img.test/hot.jpg", "track_count": 2, "author": "网易",
    "description": "", "updated_at": 1760000000,
}
TRACKS = [
    {"song_id": 101, "name": "晴天", "artist": "周杰伦", "album_name": "叶惠美",
     "album_pic_url": "http://img.test/a.jpg", "duration_ms": 269000, "fee": 1, "needs_vip": True},
    {"song_id": 102, "name": "七里香", "artist": "周杰伦", "album_name": "七里香",
     "album_pic_url": "", "duration_ms": 299000, "fee": 0},
]


def _musicbox_handler(mode="ok"):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        path = request.url.path
        if mode == "down":
            return httpx.Response(500)
        if path == "/api/v1/public-playlists/3778678":
            return httpx.Response(200, json={"ok": True, "data": SUMMARY})
        if path == "/api/v1/public-playlists/3778678/tracks":
            return httpx.Response(200, json={"ok": True, "data": TRACKS, "total": 2, "truncated": False})
        return httpx.Response(404)

    handler.state = state
    return handler


def _upstream(guid="user-public-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {"list": [{"guid": "official", "name": "官方", "trackCount": 3}], "total": 1},
            })
        return httpx.Response(200, json={"code": 0, "data": None})
    return handler


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    pubpl.reset_for_test()
    monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR", str(tmp_path / "public_cache"))
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    # 我方 fork 适配：lifespan 启动时会 apply_env_hot_reload() 读 .env 覆写 CONF
    # （生产正确行为），但会冲掉下面的 monkeypatch.setitem。指向不存在的文件
    # 隔离之，保证 recommend_* 开关以本 fixture 为准。
    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setitem(CONF, "recommend_daily", False)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "netease_my_playlists", False)
    monkeypatch.setitem(CONF, "netease_playlist_resident", False)
    monkeypatch.setitem(CONF, "public_playlists_enabled", True)
    monkeypatch.setitem(CONF, "public_playlists_selection", json.dumps(SELECTION))
    yield
    pubpl.reset_for_test()
    app.state.upstream_client = None
    app.state.musicbox_client = None
    app.state.lx_client = None


def _wire(mode="ok"):
    handler = _musicbox_handler(mode)
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream()), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://musicbox")
    return handler


def test_selection_guid_and_default_initialization():
    entry = pubpl.parse_selection("", {"netease_enabled": True, "lx_enabled": False})
    assert [item["id"] for item in entry] == ["3778678", "3779629", "19723756", "2884035"]
    guid = pubpl.playlist_guid(entry[0])
    assert guid == "online:playlist:public:musicbox:netease:chart:3778678"
    assert pubpl.entry_from_guid(guid)["id"] == "3778678"


def test_explicit_empty_selection_does_not_restore_defaults():
    assert pubpl.parse_selection("[]", {"netease_enabled": True}) == []


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["summary", "tracks"])
async def test_cancelled_waiter_keeps_shared_public_resolution(monkeypatch, kind):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(*args):
        calls.append(True)
        entered.set()
        await release.wait()
        return SUMMARY if kind == "summary" else []

    monkeypatch.setattr(pubpl, "_fetch_summary" if kind == "summary" else "_fetch_tracks", fetch)
    monkeypatch.setattr(pubpl, "_provider_client", lambda *args: None)
    load = (lambda: pubpl.load_summary(app.state, SELECTION[0])) if kind == "summary" else (
        lambda: pubpl.load_tracks(app.state, SELECTION[0], lambda row: row))
    first = asyncio.create_task(load())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(load())
    await asyncio.sleep(0)
    release.set()
    await second
    assert len(calls) == 1


@pytest.mark.anyio
async def test_public_shutdown_cancels_shared_work(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(pubpl, "_fetch_summary", fetch)
    monkeypatch.setattr(pubpl, "_provider_client", lambda *args: None)
    waiter = asyncio.create_task(pubpl.load_summary(app.state, SELECTION[0]))
    await entered.wait()
    await pubpl.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled.is_set() and not pubpl._summary_tasks


def test_public_detail_requires_finos_authentication():
    handler = _wire()
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"code": 401})),
        base_url="http://unix",
    )
    with TestClient(app) as client:
        response = client.get("/music/api/v1/playlist/detail", params={"guid": pubpl.playlist_guid(SELECTION[0])})
    assert response.status_code == 401
    assert handler.state["calls"] == 0


def test_netease_selection_deduplicates_chart_and_playlist():
    rows = [SELECTION[0], {**SELECTION[0], "kind": "playlist"}]
    assert pubpl.parse_selection(json.dumps(rows)) == SELECTION


def test_lx_without_playback_platforms_is_not_injected():
    entry = {"provider": "lx", "source": "kg", "kind": "playlist", "id": "id_1"}
    assert not pubpl._selection_runtime_usable(entry, {"lx_enabled": True, "lx_sources": []})


def test_cached_card_remains_visible_while_another_summary_loads(monkeypatch):
    entry = {**SELECTION[0], "id": "3779629"}
    monkeypatch.setitem(CONF, "public_playlists_selection", json.dumps([SELECTION[0], entry]))
    pubpl._summary_cache[pubpl.playlist_guid(SELECTION[0])] = (SUMMARY, float("inf"))

    async def run():
        gate = asyncio.Event()
        async def handler(request):
            await gate.wait()
            return httpx.Response(200, json={"ok": True, "data": {**SUMMARY, "id": entry["id"]}})
        state = type("State", (), {})()
        state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://musicbox")
        monkeypatch.setattr(pubpl, "schedule_prefetch", lambda *args: None)
        try:
            cards = await pubpl.peek_cards(state, CONF, lambda item: item)
            assert [card["guid"] for card in cards] == [pubpl.playlist_guid(SELECTION[0])]
            gate.set()
            await asyncio.gather(*list(pubpl._summary_tasks.values()))
        finally:
            await state.musicbox_client.aclose()
    asyncio.run(run())


def test_tracks_failure_cooldown_without_disk_snapshot():
    handler = _musicbox_handler("down")

    async def run() -> int:
        state = type("State", (), {})()
        state.musicbox_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://musicbox",
        )
        try:
            for _ in range(2):
                with pytest.raises(RuntimeError):
                    await pubpl.load_tracks(state, SELECTION[0], lambda item: {"guid": str(item["song_id"])})
            return handler.state["calls"]
        finally:
            await state.musicbox_client.aclose()

    assert asyncio.run(run()) == 1


def test_playlist_list_detail_tracks_and_readonly():
    _wire()
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        rows = resp.json()["data"]["list"]
        assert [row["guid"] for row in rows] == [pubpl.playlist_guid(SELECTION[0]), "official"]
        assert rows[0]["name"] == "[网易] 热歌榜"
        assert rows[0]["trackCount"] == 2
        # 卡片封面字段（2026-10-18）：客户端侧边栏按 coverUrl 取图，且 coverId 必须是
        # 纯 32-hex（track_ 前缀是行内曲目惯例，卡片不认）—— 与每日推荐同形态。
        cid = rows[0]["coverId"]
        assert len(cid) == 32 and all(ch in "0123456789abcdef" for ch in cid)
        assert rows[0]["coverUrl"].endswith("coverId=" + cid)

        resp = client.get("/music/api/v1/playlist/detail", params={"guid": pubpl.playlist_guid(SELECTION[0])})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "[网易] 热歌榜"

        resp = client.get("/music/api/v1/track/playlist-detail/list", params={
            "playlistGUID": pubpl.playlist_guid(SELECTION[0]), "size": -1,
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert data["list"][0]["title"].startswith("[VIP] 晴天")
        assert not str(data["list"][0]["guid"]).startswith("online:")

        for path in ("/music/api/v1/playlist/add-track", "/music/api/v1/playlist/remove-track"):
            assert client.post(path, json={"guid": pubpl.playlist_guid(SELECTION[0]), "trackGUIDs": ["x"]}).json()["code"] == 0
        assert client.post("/music/api/v1/playlist/delete", json={"guid": pubpl.playlist_guid(SELECTION[0])}).json()["code"] == 0


def test_upstream_failure_does_not_block_official_list():
    handler = _wire("down")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        assert [row["guid"] for row in resp.json()["data"]["list"]] == ["official"]
        assert resp.json()["data"]["total"] == 1
    assert handler.state["calls"] == 1


def test_disk_summary_and_tracks_survive_restart():
    _wire()
    with TestClient(app) as client:
        client.get("/music/api/v1/playlist/list")
        client.get("/music/api/v1/track/playlist-detail/list", params={
            "playlistGUID": pubpl.playlist_guid(SELECTION[0]), "size": -1,
        })
        track_fake = fake_official_guid("online:netease:101")
    pubpl.reset_for_test()
    _wire("down")
    assert pubpl.cached_card(pubpl.playlist_guid(SELECTION[0]))["name"] == "[网易] 热歌榜"
    assert len(pubpl.cached_tracks(pubpl.playlist_guid(SELECTION[0]))) == 2
    assert resolve_real_guid(track_fake) == "online:netease:101"
