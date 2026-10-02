"""issue #22 专辑支持测试：三源 VO 专辑结构、专辑详情合成、专辑曲目列表、在线专辑搜索合并。

覆盖 2.5.0 新增能力：
  - build_online_track 对三个音源统一登记伪装专辑 guid（点击专辑可反解）
  - /music/api/v1/album/detail 分源合成（netease 真实接口 / musicdl 聚合 / 官方透传）
  - /music/api/v1/track/album-detail/list 分页下发合成曲目
  - /music/api/v1/search/album 官方结果后合并在线专辑（netease 真实 + musicdl/lx 聚合）
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import recommend as dailyrec
from proxy.app import (
    CONF,
    _FAKE_ALBUM_REGISTRY,
    _SEARCH_CACHE,
    _track_cover_id,
    app,
    album_entry_from_real_guid,
    build_online_track,
    fake_official_guid,
    resolve_fake_album,
)


@pytest.fixture(autouse=True)
def clean_album_state(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _FAKE_ALBUM_REGISTRY.clear()
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend_cache"))
    monkeypatch.delenv("FNMUSIC_LLM_API_KEY", raising=False)
    monkeypatch.delenv("FNMUSIC_LLM_BASE_URL", raising=False)
    # persist 落盘路径与节流指针指向临时目录：任何 persist 登记都不得写到仓库/用户目录
    from proxy import app as app_mod
    monkeypatch.setattr(app_mod, "_ALBUM_REGISTRY_PATH", str(tmp_path / "album_registry.json"))
    monkeypatch.setattr(app_mod, "_ALBUM_PERSIST_LAST", 0.0)
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )
    yield
    _SEARCH_CACHE.clear()
    _FAKE_ALBUM_REGISTRY.clear()


# ---------------------------------------------------------------- WS4.1 三源 VO 结构


@pytest.mark.parametrize("item", [
    {"id": "netease:123", "source": "netease", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "flac"},
    {"id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "mp3"},
    {"id": "lx:kg:abc123", "source": "lx", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "flac"},
])
def test_build_online_track_registers_album_for_all_sources(item):
    """三个音源的曲目 VO 必须统一产出 album 对象并登记伪装专辑 guid（可点击进详情）。"""
    vo = build_online_track(item)
    album = vo.get("album")
    assert isinstance(album, dict) and album.get("name") == "叶惠美"
    assert album.get("guid") == f"{vo['guid']}:album"
    assert album.get("coverId") == _track_cover_id(vo["guid"])
    assert vo.get("albumName") == "叶惠美"
    # 伪装登记可反解：fake 形态与 track_ 前缀形态都能命中
    fake = fake_official_guid(album["guid"])
    entry = resolve_fake_album(fake)
    assert entry is not None and entry["album"] == "叶惠美"
    assert entry["source"] == str(item["source"])
    assert resolve_fake_album(f"track_{fake}") is not None
    # 真实形态 guid 也能定位登记条目（封面直链钩子用）
    assert album_entry_from_real_guid(album["guid"]) is not None


# ---------------------------------------------------------------- 专辑详情 + 曲目列表


def _musicbox_album_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/song/123/info"):
        return httpx.Response(200, json={"ok": True, "data": {
            "song_id": 123, "name": "晴天",
            "al": {"id": 789, "name": "叶惠美", "picUrl": "https://p.music.126.net/cover.jpg"},
        }})
    if path.endswith("/album/789"):
        return httpx.Response(200, json={"ok": True, "data": [
            {"song_id": 123, "song_name": "晴天", "artist": "周杰伦", "duration": 269000, "has_sq": True},
            {"song_id": 124, "song_name": "懦夫", "artist": "周杰伦", "duration": 251000, "sq": True},
        ]})
    return httpx.Response(404, json={"ok": False})


def _upstream_official_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-album-1"}})
    if path.endswith("/album/detail") or path.startswith("/music/api/v1/album"):
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {
            "guid": "official-album-1", "name": "官方专辑", "trackCount": 3,
        }})
    if path.endswith("/search/album"):
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {
            "list": [{"guid": "official-album-1", "name": "官方专辑", "trackCount": 3, "artists": []}],
            "total": 1,
        }})
    if path.endswith("/track/album-detail/list"):
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {
            "list": [{"guid": "official-track-1", "title": "本地歌"}], "total": 1, "sort": "",
        }})
    return httpx.Response(200, json={"code": 0, "data": None})


def test_netease_album_detail_real_api_and_track_list(monkeypatch):
    """netease 专辑详情走 musicbox 真实接口；/track/album-detail/list 复用合成结果分页。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    item = {"id": "netease:123", "source": "netease", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "flac"}
    vo = build_online_track(item)
    fake = fake_official_guid(f"{vo['guid']}:album")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_official_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox_album_handler), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/album/detail", params={"guid": fake}).json()
        assert detail["code"] == 0
        data = detail["data"]
        assert data["name"] == "叶惠美"
        assert data["trackCount"] == 2
        assert data["artists"] and data["artists"][0]["name"] == "周杰伦"
        assert len(data["tracks"]) == 2

        tracks = client.get("/music/api/v1/track/album-detail/list", params={"albumGUID": fake, "page": 1, "size": 1}).json()
        assert tracks["code"] == 0
        assert tracks["data"]["total"] == 2
        assert len(tracks["data"]["list"]) == 1  # 分页生效
        # 下发的条目 id 是伪装 32-hex（客户端只认官方形态）
        assert "online:" not in str(tracks["data"]["list"][0].get("guid"))


def test_official_album_forwards_untouched():
    """官方专辑（非伪装 guid）的详情与曲目列表原样透传，不受拦截影响。"""
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_official_handler), base_url="http://unix")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/album/detail", params={"guid": "official-album-1"}).json()
        assert detail["data"]["guid"] == "official-album-1"
        tracks = client.get("/music/api/v1/track/album-detail/list", params={"albumGUID": "official-album-1"}).json()
        assert tracks["data"]["list"][0]["guid"] == "official-track-1"


def test_musicdl_album_aggregate_detail(monkeypatch):
    """musicdl 专辑：从内存搜索缓存聚合同名同源曲目合成详情。"""
    monkeypatch.setitem(CONF, "netease_enabled", False)
    item = {"id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "mp3"}
    vo = build_online_track(item)
    fake = fake_official_guid(f"{vo['guid']}:album")
    # 搜索缓存里再放两首同专辑曲目（一首同源同名专辑、一首不同专辑作对照）；
    # ts 必须新鲜，否则 _clean_search_cache 会按 TTL 清掉
    import time as _time
    _SEARCH_CACHE["scope:叶惠美"] = {"items": [
        {"id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "mp3"},
        {"id": "migu:10", "source": "migu", "title": "懦夫", "artist": "周杰伦", "album": "叶惠美", "duration_s": 251, "ext": "mp3"},
        {"id": "migu:11", "source": "migu", "title": "七里香", "artist": "周杰伦", "album": "七里香", "duration_s": 299, "ext": "mp3"},
        {"id": "netease:77", "source": "netease", "title": "跨源歌", "artist": "X", "album": "叶惠美", "duration_s": 200, "ext": "mp3"},
    ], "ts": _time.time(), "keyword": "叶惠美", "scope": "scope", "credentials": "", "config": {}, "task": None}

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_official_handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/album", params={"guid": fake}).json()
        assert detail["code"] == 0
        titles = sorted(t["title"] for t in detail["data"]["tracks"])
        assert titles == ["懦夫", "晴天"]  # 同专辑同源聚合，跨源/异专辑剔除
        assert detail["data"]["trackCount"] == 2


def test_album_detail_not_synthesizable_returns_business_error(monkeypatch):
    """无法合成（无专辑名且无缓存）返回业务错误，绝不透传官方"无此专辑"。"""
    monkeypatch.setitem(CONF, "netease_enabled", False)
    item = {"id": "migu:404", "source": "migu", "title": "无专辑歌", "artist": "X", "duration_s": 100, "ext": "mp3"}
    vo = build_online_track(item)
    fake = fake_official_guid(f"{vo['guid']}:album")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_official_handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/album/detail", params={"guid": fake}).json()
        assert detail["code"] != 0
        assert "专辑" in detail["msg"]


# ---------------------------------------------------------------- /search/album 合并


def test_search_album_merges_online_albums(monkeypatch):
    """/search/album：官方本地专辑在前，netease 真实专辑与 musicdl 聚合专辑合并，
    与官方同名去重；netease 专辑登记真实 album_id 可直达详情。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search" and request.url.params.get("type") == "album":
            assert request.url.params.get("keyword") == "叶惠美"
            return httpx.Response(200, json={"ok": True, "data": [
                {"album_id": 789, "album_name": "叶惠美", "artist": "周杰伦", "pic_url": "https://p.music.126.net/yhm.jpg"},
                {"album_id": 790, "album_name": "官方专辑", "artist": "周某某"},  # 与官方撞名 → 去重
            ]})
        if request.url.path == "/api/v1/album/789":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": 123, "song_name": "晴天", "artist": "周杰伦", "duration": 269000, "has_sq": True},
                {"song_id": 124, "song_name": "懦夫", "artist": "周杰伦", "duration": 251000, "has_sq": False},
            ]})
        return httpx.Response(404, json={"ok": False})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json={"items": [
                {"id": "migu:9", "source": "migu", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "mp3"},
                {"id": "migu:10", "source": "migu", "title": "懦夫", "artist": "周杰伦", "album": "叶惠美", "duration_s": 251, "ext": "mp3"},
            ]})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_official_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768")
    with TestClient(app) as client:
        body = client.get("/music/api/v1/search/album", params={"keyword": "叶惠美"}).json()
        assert body["code"] == 0
        names = [x["name"] for x in body["data"]["list"]]
        # 官方在前；netease 真实"叶惠美"与 musicdl 聚合"叶惠美"同名只保留一个；撞名的 790 被去重
        assert names[0] == "官方专辑"
        assert names.count("叶惠美") == 1
        assert "叶惠美" in names
        assert body["data"]["total"] >= 2
        # 在线专辑条目 id 为伪装 32-hex
        online = [x for x in body["data"]["list"] if x["name"] == "叶惠美"][0]
        assert len(str(online["guid"])) == 32

        # netease 专辑已登记真实 album_id：详情无需 song/info 二跳直达
        entry = resolve_fake_album(online["guid"])
        assert entry is not None
        assert entry["album_id"] == "789"
        detail = client.get("/music/api/v1/album/detail", params={"guid": online["guid"]}).json()
        assert detail["code"] == 0
        assert detail["data"]["name"] == "叶惠美"
        assert detail["data"]["trackCount"] == 2


def test_search_album_official_error_passthrough(monkeypatch):
    """官方信封非 code=0（如未登录）时原样透传，不做合并。"""
    def deny_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 401, "msg": "unauthorized", "data": None})

    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(deny_handler), base_url="http://unix")
    with TestClient(app) as client:
        body = client.get("/music/api/v1/search/album", params={"keyword": "x"}).json()
        assert body["code"] == 401


def test_register_fake_album_richer_snapshot_overrides_stub():
    """issue #28 真机复现的合并 bug：首次解析失败的 stub（仅 id/source）先占位后，
    更全的后到信息必须能覆盖它，否则专辑合成/命名永远拿到残缺条目。"""
    from proxy.app import _item_meta_richness

    stub = {"id": "kuwo:228908", "source": "kuwo"}
    full = {"id": "kuwo:228908", "source": "kuwo", "title": "晴天", "artist": "周杰伦",
            "album": "叶惠美", "duration_s": 269, "ext": "flac"}
    assert _item_meta_richness(stub) == 0 and _item_meta_richness(full) == 2

    build_online_track({"id": "kuwo:228908", "source": "kuwo", "title": "无专辑歌", "artist": "X"})  # 无关条目隔离
    # 第一次：stub 先登记（残缺）
    register = __import__("proxy.app", fromlist=["register_fake_album"]).register_fake_album
    register("online:kuwo:228908", "", stub)
    fake = fake_official_guid("online:kuwo:228908:album")
    entry = resolve_fake_album(fake)
    assert entry["album"] == ""
    # 第二次：全量信息应覆盖 stub，专辑名补齐
    register("online:kuwo:228908", "叶惠美", full)
    entry = resolve_fake_album(fake)
    assert entry["album"] == "叶惠美"
    assert entry["item"].get("title") == "晴天"
    # 反向：stub 不得降级已全量的条目
    register("online:kuwo:228908", "", stub)
    entry = resolve_fake_album(fake)
    assert entry["album"] == "叶惠美" and entry["item"].get("title") == "晴天"


# ---------------------------------------------------------------- /search/album 登记持久化（重启恢复）


def test_search_album_registry_survives_restart(monkeypatch, tmp_path):
    """/search/album 的在线专辑锚点必须跨重启可反解（原实现只存内存，重启后
    客户端停留在旧搜索结果点进专辑会回落官方"无此专辑"）。"""
    import time as _time

    from proxy import app as app_mod

    reg_path = tmp_path / "album_registry.json"
    monkeypatch.setattr(app_mod, "_ALBUM_REGISTRY_PATH", str(reg_path))
    monkeypatch.setattr(app_mod, "_ALBUM_PERSIST_LAST", 0.0)

    anchor = "online:netease:album:789"
    app_mod.register_fake_album(
        anchor, "叶惠美", {"cover_url": "https://p.music.126.net/yhm.jpg"}, album_id="789", persist=True,
    )
    assert reg_path.exists(), "persist=True 的登记必须落盘"
    fake = app_mod.fake_official_guid(f"{anchor}:album")

    # 模拟重启：清空内存注册表与 reverse 映射，warm 目录全部指向空临时目录
    app_mod._FAKE_ALBUM_REGISTRY.clear()
    app_mod._FAKE_GUID_REVERSE.clear()
    app_mod._REGISTRY_WARMED = False
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setitem(CONF, "fav_dir", str(empty))
    monkeypatch.setitem(CONF, "plt_dir", str(empty))
    monkeypatch.setattr(dailyrec, "play_history_dir", lambda: str(empty))
    monkeypatch.setattr(dailyrec, "recommend_cache_dir", lambda: str(empty))
    app_mod.ensure_registry_warm()

    entry = app_mod.resolve_fake_album(fake)
    assert entry is not None, "重启后 warm 必须从落盘登记恢复专辑锚点"
    assert entry["album"] == "叶惠美"
    assert entry["album_id"] == "789"
    # 反向映射也一并恢复（coverId 走 extract_guid → resolve_real_guid 依赖它）
    assert app_mod.resolve_real_guid(fake) == f"{anchor}:album"


def test_persist_flag_not_set_for_track_derived_albums(monkeypatch, tmp_path):
    """曲目衍生登记（build_online_track 路径）不得落盘——可由收藏/历史快照重建，
    落盘只会写放大。"""
    reg_path = tmp_path / "album_registry.json"
    from proxy import app as app_mod

    monkeypatch.setattr(app_mod, "_ALBUM_REGISTRY_PATH", str(reg_path))
    monkeypatch.setattr(app_mod, "_ALBUM_PERSIST_LAST", 0.0)
    build_online_track({"id": "netease:42", "source": "netease", "title": "晴天", "artist": "周杰伦",
                        "album": "叶惠美", "duration_s": 269, "ext": "flac"})
    assert not reg_path.exists(), "未标 persist 的登记不应触发写盘"
