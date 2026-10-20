"""lxmusic-service API 测试：搜索/解析管线走用户自定义源替身，内置平台接口走 MockTransport。

锁定行为：
- 统一曲目 ID 契约 "lx:<source>:<identifier>"
- 播放解析唯一通道 = 用户源 musicUrl（quality 档位映射 + 降级）
- capabilities 门控（无源/平台未声明/熔断打开）
- /api/v1/source 管理端点、healthz、榜单、歌词
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import sys
import time
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeRuntime, lxapp, mock_client
from source_runtime import SourceError

# ------------------------------------------------------------------ 搜索固定响应 ---

_KW_RS_BODY = (
    "{'abslist':["
    "{'MUSICRID':'MUSIC_228908','SONGNAME':'晴天','ARTIST':'周杰伦','ALBUM':'叶惠美',"
    "'DURATION':269,'PAY':1,'web_albumpic_short':'120/85/1/4091887608.jpg',"
    "'payInfo':{'cannotOnlinePlay':'0','cannotDownload':'1'}},"
    "{'MUSICRID':'MUSIC_111222','SONGNAME':'晴天 (DJ版)','ARTIST':'路人','DURATION':130,'PAY':0,"
    "'payInfo':{'cannotOnlinePlay':'1'}}"
    "]}"
)


def _media_handler(total=38210000, ext="flac"):
    def handler(request: httpx.Request) -> httpx.Response:
        if "media.test" in str(request.url):
            return httpx.Response(
                206,
                headers={"Content-Type": f"audio/x-{ext}", "Content-Range": f"bytes 0-1/{total}"},
                content=b"fLaC" if ext == "flac" else b"ID3",
            )
        return httpx.Response(404)

    return handler


def _kw_media_handler(media_total=38210000):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            assert request.url.params.get("all") == "晴天"
            return httpx.Response(200, text=_KW_RS_BODY)
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": f"bytes 0-1/{media_total}"},
                content=b"fLaC",
            )
        return httpx.Response(404)

    return handler


# ------------------------------------------------------------------ ID 契约 ---

def test_normalize_sources_startup():
    """LX_SOURCES 启动归一：别名→规范代码、去重、非法丢弃、全非法回退默认。"""
    assert lxapp._normalize_sources(["kugou", "kuwo", "kugou"]) == ["kg", "kw"]
    assert lxapp._normalize_sources(["qq", "tx"]) == ["tx"]
    assert lxapp._normalize_sources(["bogus", ""]) == ["kg", "wy", "mg", "kw"]
    assert lxapp._normalize_sources([]) == ["kg", "wy", "mg", "kw"]
    # 模块加载时 CONF["sources"] 必然是归一结果（别名若不归一会被 _SEARCHERS 静默跳过）
    assert lxapp.CONF["sources"] == lxapp._normalize_sources(lxapp.CONF["sources"])
    assert all(src in lxapp._SEARCHERS for src in lxapp.CONF["sources"])


def test_parse_track_id():
    assert lxapp.parse_track_id("lx:kg:ABC123") == ("kg", "ABC123")
    assert lxapp.parse_track_id("lx:wy:186016") == ("wy", "186016")
    assert lxapp.parse_track_id("lx:mg:600902") == ("mg", "600902")
    assert lxapp.parse_track_id("lx:tx:0039MnYb0qxYhV") == ("tx", "0039MnYb0qxYhV")
    assert lxapp.parse_track_id("lx:kw:228908") == ("kw", "228908")
    assert lxapp.parse_track_id("kg:HASH2") == ("kg", "HASH2")
    assert lxapp.parse_track_id("lx:xx:1") == ("", "")
    assert lxapp.parse_track_id("garbage") == ("", "")
    assert lxapp.parse_track_id("") == ("", "")
    # 冒号在 identifier 内不算分隔符
    assert lxapp.parse_track_id("lx:wy:a:b:c") == ("wy", "a:b:c")


def test_quality_tiers_mapping():
    assert lxapp._quality_tiers("lossless") == ["lossless", "high", "standard"]
    assert lxapp._quality_tiers("flac") == ["lossless", "high", "standard"]
    assert lxapp._quality_tiers("high") == ["high", "standard"]
    assert lxapp._quality_tiers("320") == ["high", "standard"]
    assert lxapp._quality_tiers("standard") == ["standard"]
    assert lxapp._quality_tiers("") == ["standard"]


# ------------------------------------------------------------------ capabilities ---

def test_capabilities_without_source(isolated):
    caps = lxapp.source_capabilities()
    assert set(caps) == {"kg", "wy", "mg", "tx", "kw"}
    assert all(not c["playback_available"] for c in caps.values())
    assert all(not c["search_available"] for c in caps.values())
    assert all(c["reason"] == "no_source_configured" for c in caps.values())
    assert all(c["qualitys"] == [] for c in caps.values())


def test_capabilities_init_failed_when_configured(isolated):
    isolated.last_error = "init: boom"
    isolated.active_url = "https://src.test/1.js"
    caps = lxapp.source_capabilities()
    assert all(c["reason"] == "source_init_failed" for c in caps.values())


def test_capabilities_declared_platforms_only(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k", "320k", "flac"], "tx": ["128k"]})
    caps = lxapp.source_capabilities()
    assert caps["kw"]["playback_available"] is True
    assert caps["kw"]["search_available"] is True
    assert caps["kw"]["qualitys"] == ["128k", "320k", "flac"]
    assert caps["tx"]["playback_available"] is True
    for src in ("kg", "wy", "mg"):
        assert caps[src]["playback_available"] is False
        assert caps[src]["search_available"] is False
        assert caps[src]["reason"] == "platform_not_supported"


def test_capabilities_circuit_open(isolated):
    isolated._runtime = FakeRuntime()  # 默认声明 kw
    # 刚打开（距 open 不到 30s 试探窗口）：已声明平台被拦截
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 5, "breaks": 1,
    }
    caps = lxapp.source_capabilities()
    # 已声明平台被熔断拦截；未声明平台仍报 platform_not_supported
    assert caps["kw"]["reason"] == "source_circuit_open"
    assert caps["kw"]["playback_available"] is False
    assert caps["kw"]["search_available"] is False
    assert caps["kg"]["reason"] == "platform_not_supported"


def test_circuit_retry_window_allows_half_open_probe(isolated):
    """熔断打开 30s 后放行一次试探：不能让搜索因第三方源偶发抽风空转整整 10 分钟冷却。"""
    # 打开 31s：进入试探窗口，允许 half-open 一次
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 31, "breaks": 1,
    }
    assert lxapp._chain_available("user_source") is True
    assert lxapp._chain_acquire("user_source") is True
    assert lxapp._CHAIN_HEALTH["user_source"].get("half_open") is True
    lxapp._chain_report("user_source", True)
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["state"] == "closed"

    # 刚打开（5s 内）：不放行
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 5, "breaks": 1,
    }
    assert lxapp._chain_available("user_source") is False

    # half_open 已被占用：其他请求不放行（单一试探者）
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 31, "breaks": 1,
        "half_open": True,
    }
    assert lxapp._chain_available("user_source") is False


def test_circuit_half_open_single_concurrent_probe(isolated):
    """半开窗口内并发请求只允许一个获准试探：试探打到故障源的流量必须收敛到 1。

    换算成场景：源刚熔断 30 秒后恢复窗口打开，此时用户连续点搜索/播放，
    10 个并发解析请求里只能有 1 个真的去请求源，其余立即走熔断短路返回，
    否则"冷却"形同虚设。claim 在 _chain_acquire 内同步完成（无 await 点），
    顺序连续调用即可锁定该语义——若中间出现让出点，第 2 个调用也会通过。
    """
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 31, "breaks": 1,
    }

    results = [lxapp._chain_acquire("user_source") for _ in range(10)]
    assert sum(results) == 1, "连续 10 个 acquire 必须只有 1 个获准"
    h = lxapp._CHAIN_HEALTH["user_source"]
    assert h.get("half_open") is True, "获准者必须同步占住 half_open 名额"

    # 试探失败：顺延一个完整冷却窗口，且清掉 half_open 让下一窗口可再试探
    before_breaks = h.get("breaks", 0)
    lxapp._chain_report("user_source", False)
    h = lxapp._CHAIN_HEALTH["user_source"]
    assert "half_open" not in h
    assert h["breaks"] == before_breaks + 1
    assert h["open_until"] > time.time() + lxapp._CHAIN_OPEN_SECONDS - 5


def test_circuit_successful_probe_closes_and_next_acquire_free(isolated):
    """试探成功立即闭合：后续请求无需再过窗口判断，直接放行。"""
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 0, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS - 31, "breaks": 1,
    }
    assert lxapp._chain_acquire("user_source") is True
    lxapp._chain_report("user_source", True)
    h = lxapp._CHAIN_HEALTH["user_source"]
    assert h["open_until"] == 0.0 and "half_open" not in h
    assert lxapp._chain_available("user_source") is True
    assert lxapp._chain_acquire("user_source") is True  # 不再设置 half_open
    assert "half_open" not in lxapp._CHAIN_HEALTH["user_source"]


def test_source_activation_resets_stale_circuit(isolated):
    """换源必须清零熔断：旧源（坏源）攒下的 open 状态不能连带拦截新源。"""
    lxapp._CHAIN_HEALTH["user_source"] = {
        "fails": 2, "open_until": time.time() + lxapp._CHAIN_OPEN_SECONDS, "breaks": 1,
    }
    with TestClient(lxapp.app) as client:
        r = client.post("/api/v1/source", json={"url": "https://src.test/good.js"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
    assert "user_source" not in lxapp._CHAIN_HEALTH
    assert isolated.activated == ["https://src.test/good.js"]


def test_runtime_override_does_not_replace_manager(isolated):
    original = lxapp.SOURCE_MANAGER
    fake = FakeRuntime(platforms={"tx": ["128k"]})
    token = lxapp._RUNTIME_OVERRIDE.set(fake)
    try:
        assert lxapp.SOURCE_MANAGER is original
        assert lxapp.current_runtime() is fake
    finally:
        lxapp._RUNTIME_OVERRIDE.reset(token)
    assert lxapp._RUNTIME_OVERRIDE.get() is None


# ------------------------------------------------------------------ 搜索：用户源管线 ---

def test_search_kw_verified_via_user_source(isolated):
    rt = FakeRuntime()  # 默认 kw + flac 直链
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        rj = resp.json()
        # cannotOnlinePlay=1 剔除；VIP 曲经用户源解析+探活通过后保留
        ids = [it["id"] for it in rj["items"]]
        assert ids == ["lx:kw:228908"]
        item = rj["items"][0]
        assert item["verified"] is True
        assert item["ext"] == "flac"
        assert item["file_size"] == 38210000
        assert item["cover_url"].startswith("https://img1.kuwo.cn/star/albumcover/")
        assert len(rt.calls) == 1
        assert rt.calls[0]["quality"] == "128k"  # standard 档 → 脚本 128k
        assert rt.calls[0]["info"]["rid"] == "228908"

        # track/url 复用探活缓存（15 分钟内不再回源）
        resp2 = client.get("/api/v1/track/url", params={"id": "lx:kw:228908", "quality": "lossless"})
        assert resp2.status_code == 200
        data = resp2.json()["data"]
        assert data["url"].endswith(".flac")
        assert data["ext"] == "flac"
        assert len(rt.calls) == 1  # 缓存命中，未再调脚本


def test_search_kw_quality_downgrade_when_flac_undeclared(isolated):
    rt = FakeRuntime(platforms={"kw": ["128k"]})
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908", "quality": "lossless"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 脚本只声明 128k：lossless/high 档无匹配质量，降档到 standard
        assert [c["quality"] for c in rt.calls] == ["128k"]
        assert data["ext"] == "flac"  # 探活实证的实际容器
    cached = lxapp._cache_get("lx:kw:228908")
    assert cached["_probe"]["attempted_tiers"] == ["lossless", "high", "standard"]
    # 降档完成后 lossless 请求可复用
    assert lxapp._fresh_probe(cached, "lossless") is not None


def test_search_wy_vip_excluded_when_source_rejects(isolated):
    rt = FakeRuntime(platforms={"wy": ["128k"]}, resolver=SourceError("resolve", "no url"))
    isolated._runtime = rt
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "music.163.com/api/search" in url:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "result": {
                        "songs": [
                            {"id": 777888, "name": "晴天", "artists": [{"name": "周杰伦"}],
                             "duration": 269000, "fee": 1},
                            {"id": 186016, "name": "晴天", "artists": [{"name": "周杰伦"}],
                             "duration": 269000, "fee": 0},
                        ]
                    }
                },
            )
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "wy"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [it["id"] for it in items] == ["lx:wy:186016"]  # VIP 曲未混入
    assert calls["n"] == 1
    # 单次失败不应熔断
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["fails"] == 1 and snap["user_source"]["open"] is False


def test_search_platform_not_declared_zero_requests(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k"]})  # tx 未声明
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "tx"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["items"] == []
        assert rj["errors"]["tx"] == "platform_not_supported"
    assert hits["n"] == 0  # 能力门控在发起搜索前拦截


def test_search_without_source_returns_reason(isolated):
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["items"] == []
        assert rj["errors"]["kw"] == "no_source_configured"


def test_search_kw_partial_media_rejected(isolated):
    """声称 400KB 的试听片段（269s 的歌）：空实体无媒体签名，探活剔除。"""
    isolated._runtime = FakeRuntime()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            return httpx.Response(200, text=_KW_RS_BODY)
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/400000"},
            )
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ------------------------------------------------------------------ track/url ---

def test_track_url_invalid_id():
    with TestClient(lxapp.app) as client:
        assert client.get("/api/v1/track/url", params={"id": "bogus"}).status_code == 400
        assert client.get("/api/v1/track/url", params={"id": "lx:xx:1"}).status_code == 400


def test_track_url_no_source_404_with_reason(isolated):
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "no_source_configured"


def test_track_url_dead_media_link_404_without_circuit_trip(isolated):
    """脚本返回了直链但媒体 404：解析"干净失败"，不算源故障。"""
    isolated._runtime = FakeRuntime(resolver=lambda i, q, p: "https://dead.test/x.flac")
    lxapp.app.state.http = mock_client(_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 404
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["fails"] == 0


def test_track_url_source_error_returns_502(isolated):
    isolated._runtime = FakeRuntime(resolver=SourceError("resolve", "script exploded"))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 502
        assert "resolve failed" in resp.json()["error"]


def test_track_url_music_info_carries_platform_keys(isolated):
    rt = FakeRuntime(platforms={"kg": ["128k", "320k", "flac"], "kw": ["128k"]})
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kg:KGHASH1", "quality": "standard"})
        assert resp.status_code == 200
    info = rt.calls[0]["info"]
    assert info["songmid"] == "KGHASH1"
    assert info["hash"] == "KGHASH1"
    assert info["source"] == "kg"


# ------------------------------------------------------------------ 熔断 ---

def test_circuit_opens_after_consecutive_failures(isolated):
    rt = FakeRuntime(resolver=SourceError("resolve", "boom"))
    isolated._runtime = rt

    async def run():
        http = mock_client(lambda r: httpx.Response(404))
        # 阈值与冷却时长是运维参数（2026-10-20 从 3/600s 放宽到 6/120s），
        # 断言跟随常量而非硬编码，这样调参不必改测试。
        n = lxapp._CHAIN_FAIL_THRESHOLD + 2
        try:
            for _ in range(n):
                try:
                    await lxapp.resolve_and_probe(
                        http, "kw", {"id": "lx:kw:1", "title": "t", "duration_s": 200}
                    )
                except lxapp.ChainTransportError:
                    pass
        finally:
            await http.aclose()
        return n

    import asyncio

    n = asyncio.run(run())
    # 达阈值后打开熔断，其后 2 次被熔断跳过（不打上游）
    assert len(rt.calls) == lxapp._CHAIN_FAIL_THRESHOLD
    assert n == lxapp._CHAIN_FAIL_THRESHOLD + 2
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["open"] is True
    assert snap["user_source"]["breaks"] == 1
    assert snap["user_source"]["state"] == "open"


def test_circuit_recovery_on_open_expiry(isolated):
    lxapp._CHAIN_HEALTH["user_source"] = {"fails": 0, "open_until": time.time() - 1, "breaks": 1}
    isolated._runtime = FakeRuntime()

    async def run():
        http = mock_client(_media_handler())
        try:
            return await lxapp.resolve_and_probe(
                http, "kw", {"id": "lx:kw:9", "title": "t", "duration_s": 200}
            )
        finally:
            await http.aclose()

    import asyncio

    result = asyncio.run(run())
    assert result is not None and result["resolver"] == "user_source"
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["state"] == "closed"  # half_open 试探成功后闭合


# ------------------------------------------------------------------ healthz / source 端点 ---

def test_healthz_reports_user_source(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k", "320k", "flac"]})
    with TestClient(lxapp.app) as client:
        rj = client.get("/healthz").json()
    assert rj["ok"] is True
    assert rj["version"] == "2.0.0"
    assert rj["user_source"]["initialized"] is True
    assert rj["user_source"]["source"]["platforms"]["kw"]["qualitys"] == ["128k", "320k", "flac"]
    assert rj["capabilities"]["kw"]["playback_available"] is True
    assert "user_source" in rj["circuit"]
    assert rj["charts"] == ["kg", "kw", "wy"]


def test_source_endpoints_manage(isolated, tmp_path):
    isolated.state_path = tmp_path / "state.json"
    isolated.script_cache = tmp_path / "source.js"

    with TestClient(lxapp.app) as client:
        r = client.get("/api/v1/source")
        assert r.status_code == 200
        assert r.json()["data"]["configured"] is False
        assert "circuit" in r.json()

        # 非法协议
        r = client.post("/api/v1/source", json={"url": "ftp://bad"})
        assert r.status_code == 400

        # 激活失败：分类错误返回 400
        r = client.post("/api/v1/source", json={"url": "https://bad-source/1.js"})
        assert r.status_code == 400
        body = r.json()
        assert body["ok"] is False
        assert body["category"] == "download"

        # 激活成功
        r = client.post("/api/v1/source", json={"url": "https://src.test/good.js"})
        assert r.status_code == 200
        assert r.json()["data"]["url"] == "https://src.test/good.js"

        # 停用并清除持久化状态
        isolated.state_path.write_text("{}", encoding="utf-8")
        isolated.script_cache.write_text("/*x*/", encoding="utf-8")
        r = client.delete("/api/v1/source")
        assert r.status_code == 200
        assert not isolated.state_path.exists()
        assert not isolated.script_cache.exists()
        assert isolated.shut_down == 1


def test_source_verify_endpoint(isolated, monkeypatch):
    calls = {}

    async def fake_verify(url):
        calls["url"] = url
        return {"ok": True, "url": url, "category": "", "message": "",
                "meta": {"name": "x"}, "platforms": ["kw"]}

    stub = types.ModuleType("verify_source")
    stub.verify_url = fake_verify
    monkeypatch.setitem(sys.modules, "verify_source", stub)

    with TestClient(lxapp.app) as client:
        r = client.post("/api/v1/source/verify", json={"url": "https://s/1.js"})
        assert r.status_code == 200
        rj = r.json()
        assert rj["ok"] is True
        assert rj["data"]["platforms"] == ["kw"]
        assert calls["url"] == "https://s/1.js"

        # file:// 上传地址同样允许进入校验链路
        r1 = client.post("/api/v1/source/verify", json={"url": "file:///data/lxmusic/uploads/x.js"})
        assert r1.status_code == 200
        assert calls["url"] == "file:///data/lxmusic/uploads/x.js"

        r2 = client.post("/api/v1/source/verify", json={"url": "notaurl"})
        assert r2.status_code == 400


_VALID_SCRIPT = (
    "/*\n * @name 上传源\n * @version 1.0.0\n * @author tester\n */\n"
    "console.log('boot')\n"
)


def test_source_upload_and_file_url_activate(isolated, tmp_path):
    """upload 落盘 → file:// URL → 以 file:// 激活（state.json 持久化）。"""
    isolated.state_dir = tmp_path
    with TestClient(lxapp.app) as client:
        # 无效脚本被拒
        bad = client.post("/api/v1/source/upload", json={"filename": "bad.js", "script": "var x=1"})
        assert bad.status_code == 400
        assert bad.json()["category"] == "invalid"

        # 合法脚本落盘
        r = client.post("/api/v1/source/upload",
                        json={"filename": "../../我的源.js", "script": _VALID_SCRIPT})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["url"].startswith(f"file://{tmp_path}/uploads/")
        assert data["url"].endswith(".js")
        assert "我的源" in data["url"]
        assert data["meta"]["name"] == "上传源"
        assert Path(data["path"]).is_file()

        # 以 file:// URL 激活
        activated = client.post("/api/v1/source", json={"url": data["url"]})
        assert activated.status_code == 200
        assert activated.json()["data"]["url"] == data["url"]

def test_source_verify_endpoint_never_500s_on_verify_exception(isolated, monkeypatch):
    """校验链路任何异常（SourceError 或未预期错误）都必须返回结构化 JSON，
    裸 500 纯文本会让 WebUI 反代解析崩溃，用户只看到 "HTTP 500"。"""
    from source_runtime import SourceError

    async def boom(url):
        raise SourceError("resolve", "console.group is not a function")

    async def crash(url):
        raise RuntimeError("unexpected")

    stub = types.ModuleType("verify_source")
    stub.verify_url = boom
    monkeypatch.setitem(sys.modules, "verify_source", stub)

    with TestClient(lxapp.app) as client:
        r = client.post("/api/v1/source/verify", json={"url": "https://s/1.js"})
        assert r.status_code == 200
        rj = r.json()
        assert rj["ok"] is False
        assert rj["data"]["category"] == "resolve"
        assert "console.group" in rj["data"]["message"]

        stub.verify_url = crash
        r2 = client.post("/api/v1/source/verify", json={"url": "https://s/1.js"})
        assert r2.status_code == 200
        rj2 = r2.json()
        assert rj2["ok"] is False
        assert rj2["data"]["category"] == "internal"
        assert "unexpected" in rj2["data"]["message"]


# ------------------------------------------------------------------ 歌词 ---

def test_track_lyric_tx_base64_decode():
    lrc = "[00:01.00]晴天 - 周杰伦\n[00:05.30]故事的小黄花&#58;"
    encoded = _b64.b64encode(lrc.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("songmid") == "0039MnYb0qxYhV"
        return httpx.Response(200, json={"retcode": 0, "lyric": encoded})

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:tx:0039MnYb0qxYhV"})
        assert resp.status_code == 200
        text = resp.json()["data"]["lyric"]
        assert text.startswith("[00:01.00]晴天 - 周杰伦")
        assert text.endswith("故事的小黄花:")  # &#58; → :


def test_track_lyric_kw_returns_empty():
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:kw:228908"})
        assert resp.status_code == 200
        assert resp.json()["data"]["lyric"] == ""


# ------------------------------------------------------------------ 榜单推荐 ---

def test_kg_chart_free_direct_and_vip_via_user_source(isolated):
    isolated._runtime = FakeRuntime(platforms={"kg": ["128k", "320k", "flac"]})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "m.kugou.com/rank/info/" in url:
            assert request.url.params.get("page") == "1", "rank/info 不带 page 时 songs.list 为空"
            assert request.url.params.get("rankid") == "8888"
            return httpx.Response(
                200,
                json={
                    "songs": {
                        "list": [
                            {
                                "hash": "KGFREE1", "sqhash": "KGSQ1", "320hash": "KGHQ1",
                                "songname": "榜单歌A", "authors": [{"author_name": "歌手A", "author_id": 1}],
                                "duration": 210, "pay_type": 0, "price": 0, "pkg_price": 0,
                                "album_sizable_cover": "http://imge.kugou.com/stdmusic/{size}/a.jpg",
                                "sqfilesize": 25000000,
                            },
                            {
                                "hash": "VIPHASH", "songname": "榜单歌VIP",
                                "authors": [{"author_name": "歌手V", "author_id": 2}],
                                "duration": 200, "pay_type": 3,
                            },
                        ]
                    }
                },
            )
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/28936190"},
                content=b"fLaC",
            )
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 5, "sources": "kg"})
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["ok"] is True
    assert rj["errors"] == {}
    assert [it["id"] for it in rj["items"]] == ["lx:kg:KGFREE1", "lx:kg:VIPHASH"]
    free, vip = rj["items"]
    assert free["duration_s"] == 210  # rank 接口秒制
    assert free["ext"] == "flac" and "{size}" not in free["cover_url"]
    assert vip["verified"] is True  # VIP 经用户源解析+探活
    assert vip["pay_type"] == 3


def test_wy_chart_vip_routes_to_probe(isolated):
    isolated._runtime = FakeRuntime(platforms={"wy": ["128k"]}, resolver=SourceError("resolve", "reject"))

    def handler(request: httpx.Request) -> httpx.Response:
        if "personalized/newsong" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 1,
                            "song": {"id": "3425638996", "name": "新歌免费",
                                     "artists": [{"name": "歌手D", "id": 9}],
                                     "album": {"name": "专辑D", "picUrl": "http://img/d.jpg"},
                                     "duration": 211686, "fee": 0},
                        },
                        {
                            "id": 2,
                            "song": {"id": "3425638997", "name": "新歌VIP",
                                     "artists": [{"name": "歌手E", "id": 10}],
                                     "album": {"name": "专辑E", "picUrl": ""},
                                     "duration": 200000, "fee": 1},
                        },
                    ]
                },
            )
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 5, "sources": "wy"})
    assert resp.status_code == 200
    rj = resp.json()
    assert [it["id"] for it in rj["items"]] == ["lx:wy:3425638996"]
    assert rj["items"][0]["duration_s"] == 211.686


def test_recommend_aggregates_with_early_stop(isolated):
    isolated._runtime = FakeRuntime(platforms={"kg": ["128k"], "wy": ["128k"]})
    calls = {"kg": 0, "wy": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "m.kugou.com/rank/info/" in url:
            calls["kg"] += 1
            rows = [
                {"hash": f"KGN{i}", "songname": f"聚合歌{i}",
                 "authors": [{"author_name": f"聚合歌手{i}", "author_id": i}],
                 "duration": 200, "pay_type": 0}
                for i in range(3)
            ]
            return httpx.Response(200, json={"songs": {"list": rows}})
        if "personalized/newsong" in url:
            calls["wy"] += 1
            return httpx.Response(200, json={"result": []})
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 3, "sources": "kg,wy"})
    assert resp.status_code == 200
    rj = resp.json()
    assert len(rj["items"]) == 3
    assert calls == {"kg": 1, "wy": 0}  # kg 凑满后未再触达 wy


# ------------------------------------------------------------------ 探活器/纯函数 ---

def test_probe_url_rejects_html():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "html.test" in url:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html>404</html>")
        if "audio.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "application/octet-stream", "Content-Range": "bytes 0-1/1000"},
                content=b"ID3",
            )
        if "redir.test" in url:
            return httpx.Response(302, headers={"Location": "https://audio.test/x.mp3"})
        return httpx.Response(500)

    import asyncio

    async def run():
        http = mock_client(handler)
        try:
            ok_html, _, _, _ = await lxapp.probe_url(http, "https://html.test/x")
            ok_audio, final, ct, size = await lxapp.probe_url(http, "https://audio.test/x.mp3")
            ok_redir, final_r, _, _ = await lxapp.probe_url(http, "https://redir.test/x")
            return ok_html, ok_audio, final, ct, size, ok_redir, final_r
        finally:
            await http.aclose()

    ok_html, ok_audio, final, ct, size, ok_redir, final_r = asyncio.run(run())
    assert ok_html is False
    assert ok_audio is True and size == 1000
    assert ok_redir is True and final_r.endswith("x.mp3")


def test_fresh_probe_tier_and_expiry():
    base = {"url": "https://cdn.test/a.flac", "ext": "flac", "headers": {},
            "probed": True, "validation_status": "media_verified"}
    # standard 缓存 → lossless 请求拒绝（需重新解析高音质）
    item = {"_probe": dict(base, ts=time.time(), actual_tier="standard")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # 降档完成后可复用
    item = {"_probe": dict(base, ts=time.time(), actual_tier="standard",
                           attempted_tiers=["lossless", "high", "standard"])}
    assert lxapp._fresh_probe(item, "lossless") is not None
    # standard 缓存 → standard 请求复用
    got = lxapp._fresh_probe({"_probe": dict(base, ts=time.time(), actual_tier="standard")}, "standard")
    assert got and got["url"].endswith(".flac") and "ts" not in got
    # lossless 缓存 → standard 请求也可复用（音质只高不低）
    item = {"_probe": dict(base, ts=time.time(), actual_tier="lossless")}
    assert lxapp._fresh_probe(item, "standard") is not None
    # 过期缓存拒绝
    item = {"_probe": dict(base, ts=time.time() - lxapp.CONF["probe_fresh_s"] - 1, actual_tier="lossless")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # 无缓存 / 非 dict / 试听
    assert lxapp._fresh_probe({}, "standard") is None
    assert lxapp._fresh_probe(None, "standard") is None
    assert lxapp._fresh_probe({"_probe": dict(base, ts=time.time(), actual_tier="lossless"),
                               "trial": "1"}, "standard") is None


def test_same_scope_cancels_search_other_scope_waits(monkeypatch):
    """同范围的新搜索取消还在跑的平台任务；另一个范围排队，不取消当前搜索。"""
    import asyncio

    started = []
    cancelled = []
    release = {"one": asyncio.Event(), "two": asyncio.Event(), "three": asyncio.Event()}

    async def slow(client, keyword, limit):
        started.append(keyword)
        try:
            await release[keyword].wait()
        except asyncio.CancelledError:
            cancelled.append(keyword)
            raise
        return []

    monkeypatch.setitem(lxapp._SEARCHERS, "kw", slow)
    monkeypatch.setattr(lxapp, "source_capabilities", lambda: {"kw": {"playback_available": True, "reason": ""}})
    monkeypatch.setitem(lxapp.CONF, "sources", ["kw"])

    async def scenario():
        transport = httpx.ASGITransport(app=lxapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://lx") as client:
            async def hit(keyword, scope):
                return await client.get(
                    "/api/v1/search",
                    params={"keyword": keyword, "sources": "kw"},
                    headers={"X-Fnmusic-Scope": scope},
                )

            first = asyncio.create_task(hit("one", "user-a"))
            for _ in range(50):
                if "one" in started:
                    break
                await asyncio.sleep(0.01)
            assert started == ["one"]
            second = asyncio.create_task(hit("two", "user-a"))
            for _ in range(50):
                if "two" in started and "one" in cancelled:
                    break
                await asyncio.sleep(0.01)
            assert cancelled == ["one"]
            assert started == ["one", "two"]
            third = asyncio.create_task(hit("three", "user-b"))
            await asyncio.sleep(0.05)
            assert started == ["one", "two"]
            release["two"].set()
            second_resp = await second
            for _ in range(50):
                if "three" in started:
                    break
                await asyncio.sleep(0.01)
            assert "three" not in cancelled
            assert started == ["one", "two", "three"]
            release["three"].set()
            third_resp = await third
            first_resp = await first
            assert first_resp.json()["superseded"] is True
            assert second_resp.status_code == 200
            assert third_resp.status_code == 200
            assert third_resp.json().get("superseded") is not True

    asyncio.run(scenario())


def test_title_relevance_ranking():
    r = lxapp._title_relevance
    assert r("晴天", "晴天 周杰伦") == 1  # 原版
    assert r("晴天 (KTV版伴奏)", "晴天 周杰伦") == 1
    assert r("晴天周杰伦串烧版", "晴天 周杰伦") == 1
    assert r("超好听晴天周杰伦remix", "晴天 周杰伦") == 2
    assert r("志明与春娇+晴天+双截棍", "晴天 周杰伦") == 3
    assert r("花海", "晴天 周杰伦") == 3
    assert r("晴天", "晴天") == 0
    assert r("任意", "") == 3


def test_lenient_pydict_parses_python_literal():
    class _FakeResp:
        text = "{'abslist':[{'MUSICRID':'MUSIC_1','SONGNAME':'A'}]}"

        def json(self):
            raise ValueError("not json")

    parsed = lxapp._lenient_pydict(_FakeResp(), "kw")
    assert parsed == {"abslist": [{"MUSICRID": "MUSIC_1", "SONGNAME": "A"}]}


def test_search_probe_disabled_returns_unverified_without_probing(isolated, monkeypatch):
    """当 search_probe 为 False 时，kw/tx 等搜索跳过 _probe_candidates，直接返回未验证候选。"""
    monkeypatch.setitem(lxapp.CONF, "search_probe", False)
    kw_raw = {
        "abslist": [
            {"MUSICRID": "MUSIC_123", "SONGNAME": "晴天", "ARTIST": "周杰伦", "ALBUM": "叶惠美", "DURATION": "269", "web_albumpic_short": "c.jpg"}
        ]
    }
    called_probe = []

    async def fake_probe(*args, **kwargs):
        called_probe.append(True)
        return []

    monkeypatch.setattr(lxapp, "_probe_candidates", fake_probe)

    def handler(request: httpx.Request):
        return httpx.Response(200, json=kw_raw)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            items = await lxapp.kw_search(client, "晴天", 10)
            assert len(items) == 1
            assert items[0]["id"] == "lx:kw:123"
            assert items[0]["verified"] is False
            assert items[0]["validation_status"] == "unverified"
            assert not called_probe, "不应触发逐曲探活"
        finally:
            await client.aclose()

    asyncio.run(scenario())

