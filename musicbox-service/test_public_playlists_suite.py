"""Offline tests for musicbox public-playlists port (#55).

Stubs NEMbox.api (the four internals the port relies on were verified against
the production container: BASE_URL / _raw_request / request / eapi_request),
so the real netease_ext data layer + app endpoints run with zero network.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# XDG 重定向到临时目录：app.py 导入时会跑 ensure_xdg_dirs()，别污染真实 home
# ---------------------------------------------------------------------------
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_xdg")
for _var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
    os.environ.setdefault(_var, os.path.join(_TMP_ROOT, _var.split("_", 1)[1].lower()))

# ---------------------------------------------------------------------------
# NEMbox 桩（必须在 import netease_ext 之前注入 sys.modules）
# ---------------------------------------------------------------------------
ROUTES: dict = {}


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def close(self):
        pass


class FakeNetEase:
    """按 ROUTES 路由表应答；结构与生产容器 NEMbox 0.5.3 对齐。"""

    def __init__(self):
        self.session = _FakeSession()

    # --- 端口依赖的四个内部 API ---
    def _raw_request(self, method, endpoint, data=None):
        path = endpoint.replace("https://music.163.com", "")
        hit = ROUTES.get(("GET", path))
        if isinstance(hit, Exception):
            raise hit
        if hit is None:
            return _FakeResp({"code": 404})
        if isinstance(hit, tuple):  # (payload, status_code)
            return _FakeResp(hit[0], hit[1])
        return _FakeResp(hit)

    def request(self, method, path, params=None, default=None, custom_cookies=None):
        hit = ROUTES.get((method, path))
        if isinstance(hit, Exception):
            return {"code": -1}  # 真实 NEMbox 吞异常回落 default
        return hit if hit is not None else {"code": -1}

    def eapi_request(self, path, params=None, default=None):
        hit = ROUTES.get(("EAPI", path))
        if isinstance(hit, Exception):
            return {"code": -1}
        return hit if hit is not None else {"code": -1}

    # --- netease_ext 既有链路依赖 ---
    def get_account_info(self):
        return ROUTES.get(("ACCOUNT",), {"profile": {"userId": 1, "vipType": 0}})

    def songs_url(self, ids):
        return ROUTES.get(
            ("SONGS_URL",),
            [{"id": i, "url": f"http://dummy/{i}.mp3", "code": 200, "fee": 0} for i in ids],
        )

    def songs_detail(self, ids):
        # SONGS_DETAIL 钩子（#55 playable_only 适配测试用）：Exception 则抛、
        # list 则原样返回，用于构造"详情接口本身缺数据"的场景
        hit = ROUTES.get(("SONGS_DETAIL",))
        if isinstance(hit, Exception):
            raise hit
        if isinstance(hit, list):
            return hit
        rows = []
        for i in ids:
            rows.append({
                "id": i,
                "name": f"song-{i}",
                "ar": [{"name": "artist"}],
                "al": {"name": "album", "picUrl": f"http://pic/{i}"},
                "dt": 180000,
                "fee": 1 if i == 424242 else 0,
            })
        return rows

    def playlist_songlist(self, pid):
        return [1, 2, 3]


_fake_api = types.ModuleType("NEMbox.api")
_fake_api.BASE_URL = "https://music.163.com"
_fake_api.NetEase = FakeNetEase
_nembox = types.ModuleType("NEMbox")
_nembox.__path__ = []
_nembox.api = _fake_api
sys.modules.setdefault("NEMbox", _nembox)
sys.modules.setdefault("NEMbox.api", _fake_api)

_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

import netease_ext  # noqa: E402
import app as mbapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    ROUTES.clear()
    netease_ext.reset_api("test")
    with netease_ext._login_state_lock:
        netease_ext._login_state = None
    yield
    ROUTES.clear()


@pytest.fixture
def client():
    return TestClient(mbapp.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_capabilities_shape(client):
    r = client.get("/api/v1/public-playlists/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "musicbox"
    src = body["sources"][0]
    assert src["id"] == "netease"
    assert src["playback_available"] is True
    assert [c["id"] for c in src["channels"]] == [
        "recommend", "top", "highquality", "charts", "account_recommend",
    ]
    assert src["channels"][-1]["login_required"] is True
    assert isinstance(src["logged_in"], bool)


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------

def test_categories_grouped(client):
    ROUTES[("POST", "/weapi/playlist/catalogue")] = {
        "code": 200,
        "categories": {"0": "语种", "1": "风格"},
        "sub": [
            {"name": "华语", "category": 0},
            {"name": "流行", "category": 1},
            {"name": "", "category": 0},  # 无名字段应被跳过
        ],
    }
    ROUTES[("GET", "/api/playlist/highquality/tags")] = {
        "code": 200,
        "tags": [{"playlistTag": {"name": "精品"}}],
    }
    r = client.get("/api/v1/public-playlists/categories")
    assert r.status_code == 200
    body = r.json()
    assert body["categories"][0] == {"id": "", "name": "全部"}
    # categories 是「分组名」（语种/风格），tags 在每组的 list 里
    ids = [c["id"] for c in body["categories"][1:]]
    assert set(ids) == {"语种", "风格"}
    by_id = {c["id"]: c for c in body["categories"][1:]}
    # 无名字段的 sub 条目被跳过，语种组只剩华语
    assert by_id["语种"]["list"] == [{"id": "华语", "name": "华语"}]
    assert by_id["风格"]["list"] == [{"id": "流行", "name": "流行"}]
    # 原始 tags 分组结构也随行下发
    assert body["tags"] == {
        "语种": [{"id": "华语", "name": "华语"}],
        "风格": [{"id": "流行", "name": "流行"}],
    }
    # highquality_tags 尽力而为聚合成功
    assert {"id": "精品", "name": "精品"} in body["highquality_tags"]


def test_categories_chart_kind_uses_toplist_tags(client):
    """排行榜分类：官方四榜（ToplistType）+ 特色榜（榜单自带 tags）。

    上游 v2.9.0 的 charts 分类写死为「全部」，WebUI 分类下拉形同虚设；
    这里锁定修复后的契约：官方榜 + 按 tag 归类的特色榜，且不调用榜单歌曲接口。
    """
    ROUTES[("GET", "/api/toplist")] = {"code": 200, "list": [
        {"id": 3778678, "name": "热歌榜", "ToplistType": "H", "tags": []},
        {"id": 71384707, "name": "网易云古典榜", "tags": ["古典"]},
        {"id": 1978921795, "name": "网易云电音榜", "tags": ["电子", "榜单"]},
    ]}
    r = client.get("/api/v1/public-playlists/categories", params={"kind": "chart"})
    assert r.status_code == 200
    body = r.json()
    assert body["tags"] == []
    options = body["categories"][0]["list"]
    assert [opt["id"] for opt in options] == ["official", "tag:古典", "tag:榜单", "tag:电子"]
    assert [opt["name"] for opt in options] == ["官方榜", "古典", "特色榜", "电子"]


def test_chart_category_filter_applied_to_list(client):
    """分类筛选必须真正作用到榜单列表（不止是下拉选项）。"""
    ROUTES[("GET", "/api/toplist")] = {"code": 200, "list": [
        {"id": 3778678, "name": "热歌榜", "ToplistType": "H", "tags": []},
        {"id": 71384707, "name": "网易云古典榜", "tags": ["古典"]},
    ]}
    r = client.get("/api/v1/public-playlists", params={"channel": "charts", "category": "tag:古典"})
    assert r.status_code == 200
    assert [it["id"] for it in r.json()["items"]] == ["71384707"]
    r = client.get("/api/v1/public-playlists", params={"channel": "charts", "category": "official"})
    assert [it["id"] for it in r.json()["items"]] == ["3778678"]


def test_chart_empty_and_unknown_category_return_all(client):
    """空分类 = 全部；无法识别的分类值按全部处理（分类参数过期时不该给用户空列表）。

    注意 tag:<标签> 是精确筛选：标签不存在时返回空列表（用户在明确按某标签过滤）。
    """
    ROUTES[("GET", "/api/toplist")] = {"code": 200, "list": [
        {"id": 3778678, "name": "热歌榜", "ToplistType": "H", "tags": []},
        {"id": 71384707, "name": "网易云古典榜", "tags": ["古典"]},
    ]}
    for category in ("", "bogus"):
        r = client.get("/api/v1/public-playlists", params={"channel": "charts", "category": category})
        assert r.status_code == 200
        assert len(r.json()["items"]) == 2, category
    r = client.get("/api/v1/public-playlists", params={"channel": "charts", "category": "tag:不存在的标签"})
    assert r.json()["items"] == []


def test_categories_bad_source(client):
    r = client.get("/api/v1/public-playlists/categories", params={"source": "qq"})
    assert r.status_code == 400


def test_categories_upstream_error_502(client):
    ROUTES[("POST", "/weapi/playlist/catalogue")] = {"code": 500}
    r = client.get("/api/v1/public-playlists/categories")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_charts(client):
    ROUTES[("GET", "/api/toplist")] = {
        "code": 200,
        "list": [
            {"id": 1, "name": "飙升榜", "coverImgUrl": "http://c/1", "trackCount": 100,
             "updateTime": 1700000000000, "creator": {"nickname": "官方"}},
            {"id": 2, "name": "新歌榜"},
        ],
    }
    r = client.get("/api/v1/public-playlists", params={"channel": "charts"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["has_more"] is False
    assert body["total"] == 2
    first = body["items"][0]
    assert first["kind"] == "chart" and first["source"] == "netease"
    assert first["updated_at"] == 1700000000  # ms -> s
    assert first["author"] == "官方"


def test_list_recommend(client):
    ROUTES[("GET", "/api/personalized/playlist")] = {
        "code": 200,
        "result": [{"id": 10, "name": "每日推荐", "picUrl": "http://c/10", "trackCount": 30}],
    }
    r = client.get("/api/v1/public-playlists", params={"channel": "recommend"})
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["id"] == "10"
    assert body["items"][0]["cover_url"] == "http://c/10"


def test_list_highquality_cursor(client):
    ROUTES[("GET", "/api/playlist/highquality/list")] = {
        "code": 200,
        "playlists": [{"id": 20, "name": "精品", "updateTime": 1699999999000}],
        "total": 87,
        "more": True,
    }
    r = client.get(
        "/api/v1/public-playlists",
        params={"channel": "highquality", "category": "华语", "cursor": "1699999999000", "limit": 30},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is True
    assert body["next_cursor"] == "1699999999000"
    assert body["total"] == 87


def test_list_top_default_params(client):
    ROUTES[("POST", "/weapi/playlist/list")] = {
        "code": 200, "playlists": [{"id": 30, "name": "热门"}], "total": 30, "more": True,
    }
    r = client.get(
        "/api/v1/public-playlists",
        params={"channel": "top", "sort": "new", "page": 3, "limit": 20},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["id"] == "30"
    assert body["has_more"] is True


def test_list_top_offset_and_order_sent():
    """数据层直测：page->offset、非法 sort 回落 hot、cat 缺省全部。"""
    captured = {}

    class _Api:
        def request(self, method, path, params=None, default=None, custom_cookies=None):
            captured["path"] = path
            captured["params"] = dict(params or {})
            return {"code": 200, "playlists": [], "total": 0, "more": False}

    orig = netease_ext._get_api_locked
    netease_ext._get_api_locked = lambda: _Api()
    try:
        netease_ext.public_playlists("top", category="", sort="bogus", page=4, limit=20)
    finally:
        netease_ext._get_api_locked = orig
    assert captured["path"] == "/weapi/playlist/list"
    p = captured["params"]
    assert p["cat"] == "全部"
    assert p["order"] == "hot"  # 非法 sort 回落
    assert p["offset"] == 60    # (4-1)*20
    assert p["total"] == "true"
    assert p["limit"] == 20


def test_list_account_recommend_not_logged_in(client):
    ROUTES[("POST", "/weapi/v1/discovery/recommend/resource")] = {"code": 301}
    r = client.get("/api/v1/public-playlists", params={"channel": "account_recommend"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "not_logged_in"
    assert body["logged_in"] is False


def test_list_bad_channel_400(client):
    r = client.get("/api/v1/public-playlists", params={"channel": "nope"})
    assert r.status_code == 400


def test_list_bad_source_400(client):
    r = client.get("/api/v1/public-playlists", params={"source": "kugou"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------

def test_detail_playlist(client):
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {
        "code": 200,
        "playlist": {
            "id": 500, "name": "测试歌单", "coverImgUrl": "http://c/500",
            "trackCount": 42, "updateTime": 1699999999000,
            "description": "desc", "creator": {"nickname": "someone"},
        },
    }
    r = client.get("/api/v1/public-playlists/500")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["id"] == "500"
    assert data["kind"] == "playlist"
    assert data["track_count"] == 42
    assert data["author"] == "someone"
    assert data["description"] == "desc"
    assert data["updated_at"] == 1699999999


def test_detail_chart_hits_charts_list(client):
    ROUTES[("GET", "/api/toplist")] = {
        "code": 200,
        "list": [{"id": 7, "name": "云音乐新歌榜", "trackCount": 88}],
    }
    r = client.get("/api/v1/public-playlists/7", params={"kind": "chart"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["id"] == "7" and data["kind"] == "chart"


def test_detail_chart_miss_falls_through_to_eapi(client):
    ROUTES[("GET", "/api/toplist")] = {"code": 200, "list": [{"id": 8, "name": "other"}]}
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {
        "code": 200, "playlist": {"id": 7, "name": "fallback"},
    }
    r = client.get("/api/v1/public-playlists/7", params={"kind": "chart"})
    assert r.status_code == 200
    assert r.json()["data"]["id"] == "7"
    assert r.json()["data"]["kind"] == "chart"


def test_detail_not_found_502(client):
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {"code": 200, "playlist": None}
    r = client.get("/api/v1/public-playlists/999")
    assert r.status_code == 502


def test_detail_bad_source_400(client):
    r = client.get("/api/v1/public-playlists/500", params={"source": "migu"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# tracks
# ---------------------------------------------------------------------------

def test_tracks_full(client):
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {
        "code": 200,
        "playlist": {
            "id": 600,
            "trackCount": 4,
            "trackIds": [{"id": 424242}, {"id": 1}, {"id": 424242}, {"id": 2}, 3],
        },
    }
    r = client.get("/api/v1/public-playlists/600/tracks")
    assert r.status_code == 200
    body = r.json()
    ids = [row["song_id"] for row in body["data"]]
    assert ids == [424242, 1, 2, 3]  # 去重且保持 trackIds 顺序
    # total = max(len(trackIds), trackCount)：原始 trackIds 5 条（含 1 条重复）
    # 压过 trackCount=4——上游语义，去重只影响下发列表，不回收 total
    assert body["total"] == 5
    assert body["truncated"] is False
    # fee 随行下发（proxy _vip_tag_prefix 的输入；本层不做 needs_vip 标记）
    fee_map = {row["song_id"]: row["fee"] for row in body["data"]}
    assert fee_map[424242] == 1
    assert fee_map[1] == 0


def test_tracks_truncated_over_1000(client):
    track_ids = [{"id": i} for i in range(1, 1201)]
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {
        "code": 200, "playlist": {"id": 601, "trackCount": 1200, "trackIds": track_ids},
    }
    r = client.get("/api/v1/public-playlists/601/tracks")
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1000
    assert body["truncated"] is True
    assert body["total"] == 1200


def test_tracks_incomplete_details_502(client):
    """songs_detail 缺数据 -> 完整性校验失败 -> 502（不伪装空列表）。

    我方适配（playable_only=False 后）：songs_url 不再参与本路径（上游 v2.8.0
    封面与可播性解耦），incomplete 只能由详情接口自身缺数据触发——这里让
    歌单 2 首歌只回来 1 首的详情。
    """
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {
        "code": 200, "playlist": {"id": 602, "trackCount": 2, "trackIds": [{"id": 1}, {"id": 2}]},
    }
    ROUTES[("SONGS_DETAIL",)] = [{
        "id": 1,
        "name": "song-1",
        "ar": [{"name": "artist"}],
        "al": {"name": "album", "picUrl": "http://pic/1"},
        "dt": 180000,
        "fee": 0,
    }]
    r = client.get("/api/v1/public-playlists/602/tracks")
    assert r.status_code == 502


def test_tracks_bad_source_400(client):
    r = client.get("/api/v1/public-playlists/600/tracks", params={"source": "kuwo"})
    assert r.status_code == 400


def test_tracks_eapi_error_502(client):
    ROUTES[("EAPI", "/api/v6/playlist/detail")] = {"code": 401}
    r = client.get("/api/v1/public-playlists/600/tracks")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# 数据层：_public_get 的 HTTP 层错误传播
# ---------------------------------------------------------------------------

def test_public_get_http_error_raises():
    ROUTES[("GET", "/api/toplist")] = ({"code": 500}, 503)
    with pytest.raises(Exception):
        netease_ext.public_playlists("charts")


# ---------------------------------------------------------------------------
# batch_song_details 的 playable_only 契约（#55 加法式改动的回归护栏）
# ---------------------------------------------------------------------------

def test_batch_song_details_default_filters_unplayable():
    """默认（playable_only=True，既有行为勿改）：过 songs_url 可播探测，
    无 url 的曲目整行剔除。搜索/推荐入口依赖该过滤。"""
    ROUTES[("SONGS_URL",)] = [
        {"id": 1, "url": "http://dummy/1.mp3", "code": 200, "fee": 0},
        {"id": 2, "url": None, "code": 404, "fee": 0},  # 取不到链 -> 过滤
    ]
    rows = netease_ext.batch_song_details([1, 2])
    assert [row["song_id"] for row in rows] == [1]


def test_batch_song_details_playable_only_false_keeps_all():
    """playable_only=False（#55 公共歌单专用）：不探测可播，全量返回，
    与上游 v2.8.0「封面与可播性解耦」同语义。"""
    ROUTES[("SONGS_URL",)] = [
        {"id": 1, "url": "http://dummy/1.mp3", "code": 200, "fee": 0},
        {"id": 2, "url": None, "code": 404, "fee": 0},
    ]
    rows = netease_ext.batch_song_details([1, 2], playable_only=False)
    assert [row["song_id"] for row in rows] == [1, 2]
