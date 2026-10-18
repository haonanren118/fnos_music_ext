"""回归：musicInfo 主键约定（★ 2026-10-18）

洛雪脚本按 `hash ?? songmid ?? id` 取主键，而各平台的「songmid」含义不同：
- kg：hash
- tx：strMediaMid（songmid 与 file.media_mid 不同值）
- **mg：数字 songmid（如 1142695325），不是 copyrightId（如 6005754MDZN）**
- kw：rid
把 copyrightId 填进 mg 的 songmid 会让所有解析后端报「无法解析到有效歌曲」，
咪咕全军覆没（实测同一首歌：传数字 songmid 出直链，传 copyrightId 必失败）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source_runtime import build_music_info  # noqa: E402


def test_mg_songmid_is_numeric_not_copyright_id():
    item = {
        "id": "lx:mg:6005754MDZN", "_identifier": "6005754MDZN",
        "title": "玻璃", "artist": "Gareth.T", "duration_s": 185,
        "songmid": "1142695325", "copyrightId": "6005754MDZN",
    }
    info = build_music_info(item, "mg")
    assert info["songmid"] == "1142695325", "mg 的 songmid 必须是数字 songmid"
    assert info["copyrightId"] == "6005754MDZN", "copyrightId 要原样保留"
    # 脚本取 songId = hash ?? songmid ?? id → 必须拿到数字 songmid
    song_id = info.get("hash") or info.get("songmid") or info.get("id")
    assert song_id == "1142695325"


def test_mg_without_songmid_falls_back_to_identifier():
    item = {"id": "lx:mg:6005754MDZN", "_identifier": "6005754MDZN", "title": "x"}
    info = build_music_info(item, "mg")
    assert info["songmid"] == "6005754MDZN"  # 没有数字 songmid 时保持原行为，不崩


def test_other_platforms_unchanged():
    kg = build_music_info({"id": "lx:kg:abc", "_identifier": "abc", "hash": "HASH"}, "kg")
    assert kg["hash"] == "HASH" and kg["songmid"] == "HASH"

    tx = build_music_info({"id": "lx:tx:MID", "_identifier": "MID", "str_media_mid": "MEDIA"}, "tx")
    assert tx["songmid"] == "MID" and tx["strMediaMid"] == "MEDIA"

    kw = build_music_info({"id": "lx:kw:658247400", "_identifier": "658247400", "rid": "RID"}, "kw")
    assert kw["rid"] == "RID"

    wy = build_music_info({"id": "lx:wy:123", "_identifier": "123", "song_id": "123"}, "wy")
    assert wy["songId"] == "123"
