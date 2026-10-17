"""封面回归用例（2026-10-17）

锁定两个曾经回归的点：
1. 在线曲目封面直链登记表（`_track_cover_remember/_track_cover_lookup`）——
   下发曲目行时登记，封面端点据此零上游出图；必须可持久化（重启后仍能反查）。
2. `_online_info` 在「源已关闭」时必须回落到播放历史快照的 title/artist ——
   否则「最近播放」里的旧源曲目（如切到网易后残留的 lx 条目）整片无封面。
"""
import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as P  # noqa: E402


def _request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/music/api/v1/static/cover",
        "headers": [], "query_string": b"", "app": P.app, "scheme": "http",
        "server": ("proxy", 80), "client": ("test", 1), "root_path": "",
    })


# ---------------------------------------------------------------------------
# 1) 封面直链登记表
# ---------------------------------------------------------------------------
@pytest.fixture()
def cover_map(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_TRACK_COVER_PATH", str(tmp_path / "track_cover_map.json"))
    monkeypatch.setattr(P, "_TRACK_COVER_URLS", {})
    monkeypatch.setattr(P, "_TRACK_COVER_LOADED", False)
    monkeypatch.setattr(P, "_TRACK_COVER_PERSIST_LAST", 0.0)
    monkeypatch.setattr(P, "_TRACK_COVER_PERSIST_MIN_INTERVAL_S", 0.0)
    return tmp_path / "track_cover_map.json"


def test_track_cover_remember_and_lookup(cover_map):
    guid = "online:netease:3448337966"
    P._track_cover_remember(guid, "https://p1.music.126.net/a.jpg")
    assert P._track_cover_lookup(guid) == "https://p1.music.126.net/a.jpg"


def test_track_cover_registry_persists_across_reload(cover_map):
    guid = "online:netease:509932349"
    P._track_cover_remember(guid, "https://p2.music.126.net/b.jpg")
    assert cover_map.exists()
    payload = json.loads(cover_map.read_text(encoding="utf-8"))
    assert payload["entries"][guid] == "https://p2.music.126.net/b.jpg"

    # 模拟重启：清空内存 + 允许懒加载 → 从磁盘恢复
    P._TRACK_COVER_URLS.clear()
    P._TRACK_COVER_LOADED = False
    assert P._track_cover_lookup(guid) == "https://p2.music.126.net/b.jpg"


def test_track_cover_registry_ignores_non_online_and_relative(cover_map):
    P._track_cover_remember("a" * 32, "https://p1.music.126.net/a.jpg")   # 本地 guid
    P._track_cover_remember("online:netease:1", "/music/api/v1/static/cover?coverId=x")  # 相对路径
    P._track_cover_remember("online:netease:2", "")
    assert P._TRACK_COVER_URLS == {}


# ---------------------------------------------------------------------------
# 2) 源已关闭时的历史快照兜底
# ---------------------------------------------------------------------------
def test_online_info_falls_back_to_history_when_source_disabled(monkeypatch):
    guid = "online:lx:kg:5b44a396be84f42aca21e9d6e72db78d"
    monkeypatch.setattr(P, "_source_enabled", lambda _g: False)
    monkeypatch.setattr(P, "_retained_track", lambda _r, _g: (None, None))
    monkeypatch.setattr(P, "_history_meta", lambda _g: {
        "id": guid, "source": "lx", "lx_source": "kg",
        "title": "慢慢喜欢你", "artist": "莫文蔚", "album": "", "cover_url": "",
    })

    data = asyncio.run(P._online_info(_request(), guid))
    assert isinstance(data, dict)
    assert data["title"] == "慢慢喜欢你"
    assert data["artist"] == "莫文蔚"


def test_online_info_keeps_retained_when_history_missing(monkeypatch):
    monkeypatch.setattr(P, "_source_enabled", lambda _g: False)
    monkeypatch.setattr(P, "_retained_track", lambda _r, _g: ({"title": "t"}, None))
    monkeypatch.setattr(P, "_history_meta", lambda _g: None)
    data = asyncio.run(P._online_info(_request(), "online:lx:kw:1"))
    assert data == {"title": "t"}
