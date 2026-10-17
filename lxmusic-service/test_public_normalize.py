"""洛雪 public playlist 归一化回归（2026-10-17）

锁定三点：
1. 排行榜 board（lxserver 只给 {id,name,bangid}）的曲目数是「未知」而不是 0 ——
   否则 WebUI 整页显示「· 0 首」，用户以为数据坏了。
2. 分类歌单/搜索结果的曲目数照常取 total/songnum。
3. 名称/作者里的 HTML 实体要解码（QQ 歌单搜索实测返回「华语&#124;&#32;修复心情」）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lxserver_client import LxServerClient  # noqa: E402

normalize = LxServerClient.normalize_public_playlist


def test_chart_board_without_count_is_unknown():
    row = {"id": "kw__93", "name": "飙升榜", "bangid": "93"}
    out = normalize(row, "kw", "chart")
    assert out["track_count"] is None
    assert out["id"] == "93"
    assert out["kind"] == "chart"


def test_playlist_keeps_real_count():
    row = {"id": "2287245933", "name": "华语", "total": "667", "img": "http://img/x.jpg"}
    out = normalize(row, "kw")
    assert out["track_count"] == 667
    assert out["cover_url"] == "http://img/x.jpg"


def test_playlist_without_count_stays_zero():
    # 分类歌单缺 total 时维持 0（与历史行为一致，只有榜单才改成未知）
    out = normalize({"id": "1", "name": "x"}, "kw")
    assert out["track_count"] == 0


def test_html_entities_decoded():
    row = {"id": "7217", "name": "华语&#124;&#32;修复心情", "author": "A&amp;B"}
    out = normalize(row, "tx")
    assert out["name"] == "华语| 修复心情"
    assert out["author"] == "A&B"


def test_chart_id_prefers_bangid():
    out = normalize({"id": "kw__17", "bangid": "17", "name": "新歌榜"}, "kw", "chart")
    assert out["id"] == "17"
