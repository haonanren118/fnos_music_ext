"""v2.0.0 新特性回归：音质模式 / .env 热重载 / 推荐双开关 / 封面兜底链。"""
import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import recommend as dailyrec
from proxy.app import (
    CONF,
    _COVER_CDN_CACHE,
    _COVER_CDN_TRANSPORT,
    _LX_QUALITY_LADDER,
    _NETEASE_QUALITY_LADDER,
    _SEARCH_CACHE,
    _reset_search_cache,
    _TINY_GRAY_PNG,
    app,
    apply_env_hot_reload,
    ordered_stream_alternatives,
    quality_order,
    resolve_lx_url,
    resolve_netease_url,
    _aggregate_search,
)


@pytest.fixture(autouse=True)
def _clean_cover_caches():
    _COVER_CDN_CACHE.clear()
    yield
    _COVER_CDN_CACHE.clear()


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------- 音质模式

@pytest.mark.parametrize("mode,primary,expected", [
    # high：锚定 primary 向下（现网默认 lossless → 三档全走）
    ("high", "lossless", ["lossless", "high", "standard"]),
    ("high", "high", ["high", "standard"]),
    ("high", None, ["lossless", "high", "standard"]),
    # balanced：3 档取中间 high，失败先向下（standard）再向上（lossless）
    ("balanced", "lossless", ["high", "standard", "lossless"]),
    # smooth：从低到高
    ("smooth", "lossless", ["standard", "high", "lossless"]),
])
def test_quality_order_lx_ladder(mode, primary, expected):
    assert quality_order(_LX_QUALITY_LADDER, mode, primary) == expected


def test_quality_order_netease_ladder_modes():
    ladder = _NETEASE_QUALITY_LADDER
    # high 默认：lossless 向下降档（比旧的 lossless→exhigh 多两级兜底）
    assert quality_order(ladder, "high", "lossless") == ["lossless", "exhigh", "higher", "standard"]
    # balanced：6 档偶数取中间偏高（lossless），先向下再向上
    assert quality_order(ladder, "balanced", "lossless") == [
        "lossless", "exhigh", "higher", "standard", "hires", "jymaster",
    ]
    # smooth：从 standard 一路向上
    assert quality_order(ladder, "smooth", "lossless") == list(ladder)
    # primary 不在梯内 → 从最高档向下
    assert quality_order(ladder, "high", "ultra") == list(reversed(ladder))


@pytest.mark.anyio
@pytest.mark.parametrize("mode,expected", [
    ("high", ["lossless", "high", "standard"]),
    ("balanced", ["high", "standard", "lossless"]),
    ("smooth", ["standard", "high", "lossless"]),
])
async def test_resolve_lx_url_follows_quality_mode(monkeypatch, mode, expected):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params.get("quality")))
        return httpx.Response(404)  # 全部失败才会逐档尝试

    monkeypatch.setitem(CONF, "quality_mode", mode)
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772") as client:
        assert await resolve_lx_url(client, "lx:kg:HASH1") is None
    assert seen == expected


@pytest.mark.anyio
async def test_resolve_netease_url_default_high_ladder(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params.get("quality")))
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": ""}})

    monkeypatch.setitem(CONF, "quality_mode", "high")
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770") as client:
        assert await resolve_netease_url(client, "186016") is None
    # 6 档梯在 lossless 锚点向下：lossless → exhigh → higher → standard
    assert seen == ["lossless", "exhigh", "higher", "standard"]


def _alt(sid: str, kbps: float, dur: float = 200.0) -> dict:
    return {
        "id": f"x:{sid}", "source": "x", "title": "T", "artist": "A",
        "duration_s": dur, "file_size": int(kbps * 1000 * dur / 8),
    }


@pytest.mark.parametrize("mode,expected_ids", [
    ("high", ["3", "1", "2"]),       # 码率高者优先
    ("smooth", ["2", "1", "3"]),    # 码率低者优先
    ("balanced", ["1", "2", "3"]),  # 中间 320 先，向下 128，向上 900
])
def test_ordered_stream_alternatives_by_mode(monkeypatch, mode, expected_ids):
    monkeypatch.setitem(CONF, "quality_mode", mode)
    item = {
        "id": "x:0", "source": "x", "title": "T", "artist": "A", "duration_s": 200,
        "_alternatives": [_alt("1", 320), _alt("2", 128), _alt("3", 900)],
    }
    assert [a["id"].split(":")[1] for a in ordered_stream_alternatives(item)] == expected_ids


def test_ordered_stream_alternatives_unknown_bitrate_keep_tail(monkeypatch):
    monkeypatch.setitem(CONF, "quality_mode", "high")
    unknown = {"id": "x:u", "source": "x", "title": "T", "artist": "A", "duration_s": 200}
    item = {"id": "x:0", "source": "x", "title": "T", "artist": "A", "duration_s": 200,
            "_alternatives": [unknown, _alt("1", 320)]}
    ordered = ordered_stream_alternatives(item)
    # 已知码率排前，未知码率保持原相对顺序排最后
    assert [a["id"].split(":")[1] for a in ordered] == ["1", "u"]


# ---------------------------------------------------------------- .env 热重载

_HOT_ENV_KEYS = [
    "musicdl_enabled", "netease_enabled", "lx_enabled", "online_sources", "lx_sources",
    "quality_mode", "tee_save_enabled", "tee_save_dir", "tee_cache_max",
    "recommend_hot", "recommend_daily", "cover_enrich", "llm_base_url", "llm_model",
    "search_timeout",
]


@pytest.fixture()
def conf_guard():
    """快照并还原热重载可动的 CONF 键与白名单环境变量。

    还原必须直接赋值：若走 monkeypatch.setitem，其自身 teardown 会把
    污染值再次写回（monkeypatch 在本 fixture 之后才终结）。
    """
    snapshot = {k: CONF[k] for k in _HOT_ENV_KEYS if k in CONF}
    env_keys = [
        "FNMUSIC_MUSICDL_ENABLED", "FNMUSIC_NETEASE_ENABLED", "FNMUSIC_LX_ENABLED",
        "FNMUSIC_ONLINE_SOURCES", "LX_SOURCES", "FNMUSIC_QUALITY_MODE",
        "FNMUSIC_TEE_SAVE_ENABLED", "FNMUSIC_TEE_SAVE_DIR", "FNMUSIC_TEE_CACHE_MAX",
        "FNMUSIC_RECOMMEND_HOT", "FNMUSIC_RECOMMEND_DAILY", "FNMUSIC_COVER_ENRICH",
        "FNMUSIC_LLM_BASE_URL", "FNMUSIC_LLM_API_KEY", "FNMUSIC_LLM_MODEL",
        "FNMUSIC_SEARCH_TIMEOUT",
    ]
    env_snapshot = {k: os.environ.get(k) for k in env_keys}
    yield
    for k, v in snapshot.items():
        CONF[k] = v
    for k, v in env_snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_env_hot_reload_whitelist_updates_conf_and_environ(tmp_path, conf_guard):
    CONF["musicdl_enabled"] = True
    CONF["lx_enabled"] = False
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FNMUSIC_MUSICDL_ENABLED=false\n"
        "FNMUSIC_LX_ENABLED=true\n"
        "FNMUSIC_QUALITY_MODE=balanced\n"
        "FNMUSIC_TEE_SAVE_ENABLED=false\n"
        "FNMUSIC_TEE_CACHE_MAX=999\n"
        "FNMUSIC_RECOMMEND_HOT=false\n"
        "LX_SOURCES=kw,kg\n"
        "FNMUSIC_LLM_API_KEY='sk-test'\n"
        "FNMUSIC_LLM_BASE_URL=https://llm.example.com/v1/\n"
        "FNMUSIC_SEARCH_TIMEOUT=20\n"
        "FNMUSIC_MUSIC_DB=/should/not/apply.db\n",
        encoding="utf-8",
    )
    changed = apply_env_hot_reload(str(env_file))
    assert "musicdl_enabled" in changed and CONF["musicdl_enabled"] is False
    assert CONF["lx_enabled"] is True
    assert CONF["quality_mode"] == "balanced"
    assert CONF["tee_save_enabled"] is False
    assert CONF["tee_cache_max"] == 100  # 钳制到 1-100
    assert CONF["recommend_hot"] is False
    assert CONF["lx_sources"] == ["kw", "kg"]
    # LLM 密钥只进环境变量（recommend 直接读 env，绝不进 CONF）
    assert os.environ.get("FNMUSIC_LLM_API_KEY") == "sk-test"
    assert CONF["llm_base_url"] == "https://llm.example.com/v1"
    assert CONF["search_timeout"] == 20.0
    # 非白名单键不动
    assert CONF["music_db"] != "/should/not/apply.db"
    # 幂等：再跑一次无变化
    assert apply_env_hot_reload(str(env_file)) == []


def test_env_hot_reload_ignores_invalid_values(tmp_path, conf_guard):
    CONF["lx_enabled"] = True
    before = dict(CONF)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FNMUSIC_QUALITY_MODE=ultra\n"      # 非法枚举忽略
        "FNMUSIC_TEE_CACHE_MAX=abc\n"       # 非法整数忽略
        "FNMUSIC_LX_ENABLED=maybe\n",       # 非法布尔 → false 语义
        encoding="utf-8",
    )
    changed = apply_env_hot_reload(str(env_file))
    assert CONF["quality_mode"] == before["quality_mode"]
    assert CONF["tee_cache_max"] == before["tee_cache_max"]
    assert "lx_enabled" in changed and CONF["lx_enabled"] is False


def test_env_hot_reload_missing_file_noop(tmp_path, conf_guard):
    assert apply_env_hot_reload(str(tmp_path / "nope.env")) == []


def test_reset_search_cache_cancels_pending_tasks():
    async def scenario():
        async def forever():
            await asyncio.sleep(60)

        pending = asyncio.ensure_future(forever())
        _SEARCH_CACHE["k"] = {"items": [], "task": pending, "ts": 0, "pages": {}, "cursor": 0}
        _reset_search_cache()
        await asyncio.sleep(0)  # 让事件循环处理取消
        assert _SEARCH_CACHE == {}
        return pending

    pending = asyncio.run(scenario())
    assert pending.cancelled()


# ---------------------------------------------------------------- 推荐双开关

@pytest.mark.anyio
async def test_daily_disabled_skips_all_tiers(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    calls = {"n": 0}

    def mb_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "data": []})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-daily-off",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            recommend_hot=False,
            recommend_daily=False,
        )
    assert calls["n"] == 0  # 每日+榜单全关：不向 musicbox 打任何推荐请求
    assert payload["tracks"] == []
    assert payload["tiers"] == []


@pytest.mark.anyio
async def test_daily_off_keeps_hot_charts(tmp_path, monkeypatch):
    """仅关“每日”（开着榜单）：热门歌单正常打 toplist，daily 链不请求。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    paths = []

    def mb_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(200, json={"ok": True, "data": []})
        if request.url.path == "/api/v1/toplist":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-daily-off-hot-on",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            recommend_daily=False,
            kind="hot",
        )
    assert "/api/v1/recommend/songs" not in paths
    assert "/api/v1/toplist" in paths
    assert payload["kind"] == "hot"
    assert payload["tracks"] == []


@pytest.mark.anyio
async def test_recommend_hot_disabled_skips_chart_tiers(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    paths = []

    def mb_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"type": "not_logged_in", "message": "未登录"}},
            )
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-hot-off",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            recommend_hot=False,
        )
    assert "/api/v1/toplist" not in paths  # 榜单开关关闭：toplist 不请求
    assert payload["tracks"] == []
    assert payload["tiers"] == []


def test_playlist_inject_hidden_when_daily_disabled(monkeypatch):
    """FNMUSIC_RECOMMEND_DAILY=false：歌单列表不注入每日推荐占位。"""
    monkeypatch.setitem(CONF, "recommend_daily", False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-cover"}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert all(not str(it.get("guid") or "").startswith("online:playlist:daily:") for it in data["list"])


# ---------------------------------------------------------------- 封面兜底链

def _setup_clients(**handlers):
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="no upstream")),
        base_url="http://unix",
    )
    defaults = {
        "musicdl": lambda r: httpx.Response(404),
        "musicbox": lambda r: httpx.Response(404),
        "lx": lambda r: httpx.Response(404),
    }
    for name, handler in handlers.items():
        port = {"musicdl": 8768, "musicbox": 8770, "lx": 8772}[name]
        setattr(app.state, f"{name}_client", httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=f"http://127.0.0.1:{port}",
        ))
        defaults.pop(name, None)
    for name, handler in defaults.items():
        port = {"musicdl": 8768, "musicbox": 8770, "lx": 8772}[name]
        setattr(app.state, f"{name}_client", httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=f"http://127.0.0.1:{port}",
        ))


def test_cover_kw_text_url_resolved_via_rid(monkeypatch):
    """lx 酷我条目返回 artistpicserver 文本封面 → 解析 rid 拿真图直链 → 服务端回源图片字节（200）。"""
    monkeypatch.setitem(CONF, "lx_enabled", True)
    text_cover = "https://artistpicserver.kuwo.cn/pic?corp=kuwo&type=rid_pic&pictype=500&size=500&rid=123"
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-kw-real"

    def lx_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/track/info"
        return httpx.Response(200, json={"ok": True, "data": {
            "id": "lx:kw:123", "title": "歌", "artist": "手", "cover_url": text_cover,
        }})

    cdn_calls = {"n": 0}

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        cdn_calls["n"] += 1
        url = str(request.url)
        if "artistpicserver.kuwo.cn" in url:
            assert "rid=123" in url
            return httpx.Response(200, text="http://img.kuwo.cn/abc/real.jpg extra")
        assert url == "http://img.kuwo.cn/abc/real.jpg"
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    _setup_clients(lx=lx_handler)
    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:lx:kw:123", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")
        # 第二次命中图片字节缓存，rid 解析亦有缓存：不再打 CDN
        client.get("/music/api/v1/static/cover?coverId=online:lx:kw:123", follow_redirects=False)
    assert cdn_calls["n"] == 2  # 1 次 rid 解析 + 1 次图片回源


def test_cover_qq_albummid_gtimg_proxied(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "online_sources", "QqMusicClient,KuwoMusicClient,MiguMusicClient")
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-qq-gtimg"

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True,
            "id": "qq:003x", "source": "qq", "title": "歌", "artist": "手",
            "cover_url": "", "albummid": "003x9b8s1v8ZxT",
        })

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://y.gtimg.cn/music/photo_new/T002R800x800M000003x9b8s1v8ZxT")
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    _setup_clients(musicdl=musicdl_handler)
    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:qq:003x", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")


def test_cover_enrich_via_netease_search(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "online_sources", "KugouMusicClient,QqMusicClient,KuwoMusicClient,MiguMusicClient")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True,
            "id": "kugou:xyz", "source": "kugou", "title": "晴天", "artist": "周杰伦",
            "cover_url": "",
        })

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            assert request.url.params.get("keyword") == "周杰伦 晴天"
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "1", "song_name": "晴天", "artist": "周杰伦"},
                {"song_id": "2", "song_name": "晴天 (Live)", "artist": "别人"},
            ]})
        if request.url.path == "/api/v1/songs/detail":
            assert request.url.params.get("ids") == "1"
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "1", "album_pic_url": "https://p1.music.126.net/cover.jpg"},
            ]})
        return httpx.Response(404)

    _setup_clients(musicdl=musicdl_handler, musicbox=musicbox_handler)
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-netease-album"

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://p1.music.126.net/cover.jpg"
        # 网易图床按 Referer 鉴权，回源必须带官方域名 Referer
        assert request.headers.get("referer") == "https://music.163.com/"
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:kugou:xyz", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")


def test_cover_enrich_disabled_falls_to_placeholder(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "cover_enrich", False)
    monkeypatch.setitem(CONF, "online_sources", "KugouMusicClient,QqMusicClient,KuwoMusicClient,MiguMusicClient")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True,
            "id": "kugou:xyz", "source": "kugou", "title": "晴天", "artist": "周杰伦",
            "cover_url": "",
        })

    box_calls = {"n": 0}

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        box_calls["n"] += 1
        return httpx.Response(404)

    _setup_clients(musicdl=musicdl_handler, musicbox=musicbox_handler)
    # 网易直连兜底与所有外部 CDN 一律失败 → 确定性落占位图（否则测试依赖真实外网）
    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(503)))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:kugou:xyz", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    # 占位图绝不可被客户端长缓存（旧 max-age=86400 会把占位图冻住一整天）
    assert resp.headers.get("cache-control") == "no-cache"
    assert box_calls["n"] == 0  # 补全关闭不打 musicbox


def test_cover_placeholder_never_404_and_deterministic(monkeypatch):
    # monkeypatch 而非裸赋值：裸写 CONF 会泄漏到后续用例（曾让顺序依赖被掩盖）
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "id": "migu:9", "source": "migu", "title": "t", "artist": "a", "cover_url": "",
        })

    _setup_clients(musicdl=musicdl_handler)
    # 阻断网易直连兜底等真实外网：全部 503 → 确定性落占位图
    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(503)))
    guid = "online:migu:9"
    with TestClient(app) as client:
        r1 = client.get(f"/music/api/v1/static/cover?coverId={guid}", follow_redirects=False)
        r2 = client.get(f"/music/api/v1/static/cover?coverId={guid}", follow_redirects=False)
        head = client.head(f"/music/api/v1/static/cover?coverId={guid}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert head.status_code == 200
    assert r1.content == r2.content  # 同 guid 确定性选取
    assert r1.content != _TINY_GRAY_PNG  # 用的是仓库占位图池而非 1x1 兜底
    assert r1.content[:8] == b"\x89PNG\r\n\x1a\n"
    # 占位图 no-cache：每次回源确认，服务端负缓存兜性能，绝不把占位图冻在客户端
    assert r1.headers.get("cache-control") == "no-cache"


def test_cover_placeholder_files_exist_and_cover_pool():
    from proxy.app import _PLACEHOLDER_COUNT, _PLACEHOLDER_DIR
    for i in range(_PLACEHOLDER_COUNT):
        path = os.path.join(_PLACEHOLDER_DIR, f"placeholder-{i}.png")
        assert os.path.isfile(path) and os.path.getsize(path) > 1000, path
        with open(path, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_cover_kw_rid_guid_parsing():
    from proxy.app import _kw_rid_from_guid
    assert _kw_rid_from_guid("online:lx:kw:123456") == "123456"
    assert _kw_rid_from_guid("online:kuwo:654321") == "654321"
    assert _kw_rid_from_guid("online:lx:kg:HASH") == ""
    assert _kw_rid_from_guid("online:netease:1") == ""
    assert _kw_rid_from_guid("") == ""


@pytest.mark.anyio
@pytest.mark.parametrize("enabled,expect", [
    ({"netease_enabled": True, "musicdl_enabled": False, "lx_enabled": False}, ["musicbox"]),
    ({"netease_enabled": False, "musicdl_enabled": True, "lx_enabled": False}, ["musicdl"]),
    ({"netease_enabled": False, "musicdl_enabled": False, "lx_enabled": True}, ["lx"]),
    ({"netease_enabled": False, "musicdl_enabled": False, "lx_enabled": False}, []),
])
async def test_aggregate_search_dispatches_single_provider(monkeypatch, enabled, expect):
    called: list[str] = []

    async def fake_mb(*_a, **_k):
        called.append("musicbox")
        return {"items": [{"id": "netease:1", "title": "t", "artist": "a"}]}

    async def fake_mdl(*_a, **_k):
        called.append("musicdl")
        return {"items": [{"id": "kuwo:1", "title": "t", "artist": "a"}]}

    async def fake_lx(*_a, **_k):
        called.append("lx")
        return {"items": [{"id": "lx:kw:1", "title": "t", "artist": "a"}]}

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)
    for key, value in enabled.items():
        monkeypatch.setitem(CONF, key, value)
    monkeypatch.setitem(CONF, "search_debounce_s", 0)

    class _Req:
        app = app

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0, "credentials": "test"}
    await _aggregate_search(_Req(), "晴天", entry)
    assert called == expect


# ------------------------------------------------ 推荐歌单封面端点

def _write_recommend_bundle(rec_dir: str, user_guid: str, tracks: list, kind: str = "daily") -> str:
    day = dailyrec.today_key()
    guid = dailyrec.recommend_playlist_guid(kind, day, user_guid)
    folder = os.path.join(rec_dir, dailyrec._safe_user_name(user_guid))
    os.makedirs(folder, exist_ok=True)
    picked = dailyrec.pick_playlist_cover_track(tracks) or {}
    cover = str(picked.get("coverId") or picked.get("guid") or guid)
    payload = {
        "day": day, "kind": kind, "guid": guid, "status": "ready",
        "playlist": {"guid": guid, "name": "test", "coverId": cover,
                     "createdAt": 1, "updatedAt": 1, "trackCount": len(tracks), "isDaily": True},
        "tracks": tracks, "tiers": [], "seedCount": 0, "favoriteCount": 0, "builtAt": 1,
    }
    with open(os.path.join(folder, f"{kind}-{day}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return guid


def _cover_env(tmp_path, monkeypatch, user_guid="user-cover", musicdl_handler=None):
    """封面端点测试环境：upstream 提供登录态，三个音源默认 404（可传 musicdl 覆盖）。

    注意不能复用 _setup_clients——它会无条件把 upstream 覆盖成 400，导致鉴权探测失败。
    """
    rec_dir = str(tmp_path / "rc")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", rec_dir)
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "hist"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "favs"))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": user_guid}})
        return httpx.Response(400, text="no upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler or (lambda r: httpx.Response(404))),
        base_url="http://127.0.0.1:8768",
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8772"
    )
    return rec_dir


def test_static_cover_playlist_skips_coverless_tracks(tmp_path, monkeypatch):
    """歌单封面请求：第一首无封面 → 跳到第二首（musicdl 只收到第二首的 info）。"""
    # musicdl 封面直链需要音源开启（自给自足，不依赖前置用例的 CONF 泄漏）
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/info"
        assert request.url.params.get("id") == "migu:2"
        return httpx.Response(200, json={"ok": True, "id": "migu:2", "title": "歌", "artist": "手",
                                          "cover_url": "http://img.music.migu.cn/2.jpg"})

    rec_dir = _cover_env(tmp_path, monkeypatch, musicdl_handler=musicdl_handler)
    # 网易兜底返回空（不打真实外网）；migu 图床回源给确定性字节
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-migu2"

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        if "music.163.com" in str(request.url):
            return httpx.Response(200, json={"result": {"songs": []}})
        assert str(request.url) == "http://img.music.migu.cn/2.jpg"
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    tracks = [
        {"guid": "online:migu:1", "coverId": "online:migu:1", "cover_url": ""},
        {"guid": "online:migu:2", "coverId": "online:migu:2", "cover_url": "http://img.music.migu.cn/2.jpg"},
    ]
    pl_guid = _write_recommend_bundle(rec_dir, "user-cover", tracks)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={pl_guid}&size=120", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.content == fake_jpeg
    assert resp.headers.get("content-type").startswith("image/")


def test_static_cover_playlist_without_covers_returns_404(tmp_path, monkeypatch):
    """歌单内全部曲目无封面 → 404（客户端回落自带默认样式，不再给占位图）。"""
    rec_dir = _cover_env(tmp_path, monkeypatch)
    tracks = [
        {"guid": "online:migu:1", "coverId": "online:migu:1", "cover_url": ""},
        {"guid": "online:migu:2", "coverId": "online:migu:2", "cover_url": ""},
    ]
    pl_guid = _write_recommend_bundle(rec_dir, "user-cover", tracks, kind="hot")

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={pl_guid}&size=120", follow_redirects=False)
    assert resp.status_code == 404


def test_static_cover_disguised_playlist_cover_survives_restart(tmp_path, monkeypatch):
    """重启后内存注册表为空：伪装 coverId（track_+32hex）经 warm 从推荐缓存反解仍能出图。"""
    import hashlib

    from proxy.app import _FAKE_GUID_REVERSE, _REGISTRY_WARMED

    # _online_info 按 CONF 门禁调用 musicdl：本用例走 musicdl 封面直链，
    # 必须自给自足开启（此前隐式依赖前置用例泄漏的 musicdl_enabled=True，
    # 单跑/换文件顺序即挂）
    monkeypatch.setitem(CONF, "musicdl_enabled", True)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("id") == "migu:2"
        return httpx.Response(200, json={"ok": True, "id": "migu:2", "title": "歌", "artist": "手",
                                          "cover_url": "http://img.music.migu.cn/2.jpg"})

    rec_dir = _cover_env(tmp_path, monkeypatch, musicdl_handler=musicdl_handler)
    # 网易兜底返回空（不打真实外网）；migu 图床回源给确定性字节
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF-migu2"

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        if "music.163.com" in str(request.url):
            return httpx.Response(200, json={"result": {"songs": []}})
        assert str(request.url) == "http://img.music.migu.cn/2.jpg"
        return httpx.Response(200, content=fake_jpeg, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("proxy.app._COVER_CDN_TRANSPORT", httpx.MockTransport(cdn_handler))
    tracks = [
        {"guid": "online:migu:1", "coverId": "online:migu:1", "cover_url": ""},
        {"guid": "online:migu:2", "coverId": "online:migu:2", "cover_url": "http://img.music.migu.cn/2.jpg"},
    ]
    pl_guid = _write_recommend_bundle(rec_dir, "user-cover", tracks)
    fake_cover = "track_" + hashlib.md5(f"fnmusic-ext::{pl_guid}".encode()).hexdigest()

    # 模拟重启：内存反查表清空、warm 标记复位。必须改模块属性——
    # from import 后直接赋值只重绑本地名，模块级 _REGISTRY_WARMED 原样保留
    # （此前单跑碰巧为 False 才通过；任何前置用例触发过 warm 即挂）。
    backup = dict(_FAKE_GUID_REVERSE)
    warmed = _REGISTRY_WARMED
    _FAKE_GUID_REVERSE.clear()
    monkeypatch.setattr("proxy.app._REGISTRY_WARMED", False)
    try:
        with TestClient(app) as client:
            resp = client.get(f"/music/api/v1/static/cover?coverId={fake_cover}&size=120", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.content == fake_jpeg
        assert resp.headers.get("content-type").startswith("image/")
    finally:
        _FAKE_GUID_REVERSE.clear()
        _FAKE_GUID_REVERSE.update(backup)
        import proxy.app as _appmod
    _appmod._REGISTRY_WARMED = warmed
