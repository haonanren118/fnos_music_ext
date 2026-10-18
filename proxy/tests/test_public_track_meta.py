"""回归：公共歌单（榜单）注入的曲目元数据兜底 —— ★ 2026-10-18

背景：洛雪榜单/歌单曲目从不进音源服务的 songInfo 缓存（只有搜索会填），
`_online_info` 因此拿不到歌名与时长 → 客户端 audio-info 为空、判为不可播，
歌词也一并为空。修复方式：注入时登记元数据，_online_info 取不到时回填。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))

import app as P  # noqa: E402
import publicplaylists as pubpl  # noqa: E402


def test_index_and_lookup_roundtrip():
    tracks = [{
        "guid": "online:lx:kw:658247400",
        "title": "NEON", "artist": "A2O LITE", "album": "NEON",
        "cover_url": "http://img1.kuwo.cn/x.jpg", "duration_s": 208.0,
        "ext": "mp3", "file_size": 0, "audioSpec": {"bitrate": 320000},
    }]
    pubpl._TRACK_META_INDEX.clear()
    pubpl._index_track_meta(tracks)
    meta = pubpl.track_meta("online:lx:kw:658247400")
    assert meta and meta["title"] == "NEON"
    assert meta["artist"] == "A2O LITE"
    assert meta["cover_url"].endswith("x.jpg")
    # 不该把客户端视图专用字段塞进索引（保持索引精简）
    assert "audioSpec" not in meta


def test_lookup_missing_guid_returns_none():
    assert pubpl.track_meta("online:lx:kw:does-not-exist") is None
    assert pubpl.track_meta("") is None


def test_merge_fills_empty_data():
    pubpl._TRACK_META_INDEX.clear()
    pubpl._index_track_meta([{
        "guid": "online:lx:kg:abc", "title": "隔岸", "artist": "姚六一",
        "album": "", "duration_s": 273.0, "cover_url": "http://x/y.jpg",
    }])
    # 上游返回空 stub（洛雪未缓存时的典型形态）
    merged = P._merge_public_track_meta("online:lx:kg:abc", {"title": "", "artist": ""})
    assert merged["title"] == "隔岸"
    assert merged["artist"] == "姚六一"
    assert merged["duration_s"] == 273.0
    # 上游完全没有数据时也要能给出元数据
    only_meta = P._merge_public_track_meta("online:lx:kg:abc", None)
    assert only_meta and only_meta["title"] == "隔岸"


def test_merge_never_overwrites_existing():
    pubpl._TRACK_META_INDEX.clear()
    pubpl._index_track_meta([{"guid": "online:lx:tx:m1", "title": "旧歌名", "artist": "旧歌手"}])
    merged = P._merge_public_track_meta(
        "online:lx:tx:m1", {"title": "真实歌名", "artist": "真实歌手", "duration_s": 100.0})
    assert merged["title"] == "真实歌名"
    assert merged["artist"] == "真实歌手"
    assert merged["duration_s"] == 100.0


def test_index_bounded():
    pubpl._TRACK_META_INDEX.clear()
    pubpl._TRACK_META_INDEX_MAX = 10
    try:
        pubpl._index_track_meta([
            {"guid": "online:lx:kw:%d" % i, "title": "t%d" % i} for i in range(30)
        ])
        assert len(pubpl._TRACK_META_INDEX) <= 10
        assert pubpl.track_meta("online:lx:kw:29") is not None
    finally:
        pubpl._TRACK_META_INDEX_MAX = 8000
        pubpl._TRACK_META_INDEX.clear()
