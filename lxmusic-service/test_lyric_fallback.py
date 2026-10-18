"""洛雪歌词回退到 lxserver 的回归测试（★ 2026-10-18）

背景：公共歌单/榜单注入的曲目没进过搜索 → lxmusic 本地 songInfo 缓存为空 →
`kw_resolve_lyric` 等平台解析器全部落空 → 这类曲目歌词恒为空（实测 kw 榜单 300 首全 0）。
修复：`/api/v1/track/lyric` 在平台解析器返回空时，回退到 lxserver `/api/music/lyric`
（免登录、直接回源，实测可用）。这里锁定客户端侧的参数与解析。
"""
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lxserver_client import LxServerClient  # noqa: E402


def _client(handler):
    client = LxServerClient(base_url="http://127.0.0.1:8005")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8005"
    )
    client._ready = True
    return client


def test_fetch_lyric_parses_lrc():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/music/lyric"
        assert request.url.params["source"] == "kw"
        assert request.url.params["id"] == "658247400"
        return httpx.Response(200, json={"lyric": "[00:00.00]NEON"})

    client = _client(handler)
    try:
        text = asyncio.run(client.fetch_lyric("kw", "658247400"))
    finally:
        asyncio.run(client._client.aclose())
    assert text == "[00:00.00]NEON"


def test_fetch_lyric_strips_source_prefix():
    """lxserver 侧会剥前缀，但带前缀传过去也能自愈；这里锁定我们不重复发送前缀。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.url.params["id"]
        return httpx.Response(200, json={"lyric": "x"})

    client = _client(handler)
    try:
        asyncio.run(client.fetch_lyric("kw", "kw_658247400"))
    finally:
        asyncio.run(client._client.aclose())
    assert seen["id"] == "658247400"


def test_fetch_lyric_empty_inputs_and_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("lxserver 未启动")

    client = _client(boom)
    try:
        assert asyncio.run(client.fetch_lyric("", "1")) == ""
        assert asyncio.run(client.fetch_lyric("kw", "")) == ""
        assert asyncio.run(client.fetch_lyric("kw", "1")) == ""
    finally:
        asyncio.run(client._client.aclose())


def test_fetch_lyric_blank_response_is_empty_string():
    client = _client(lambda request: httpx.Response(200, json={"lyric": "   "}))
    try:
        assert asyncio.run(client.fetch_lyric("kw", "1")) == ""
    finally:
        asyncio.run(client._client.aclose())
