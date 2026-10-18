"""测试全局守卫：默认禁用真 ffmpeg，防止用例意外转码真实在线源。

需要验证转码路径的用例自行 monkeypatch transcode.FFMPEG_BIN 指向假实现
（tests 内置 fake_ffmpeg 脚本），其余用例一律走无 ffmpeg 的回落分支。
"""
import pytest

from proxy import transcode as tc


@pytest.fixture(autouse=True)
def _no_real_ffmpeg(monkeypatch):
    monkeypatch.setattr(tc, "FFMPEG_BIN", None)
    monkeypatch.setattr(tc, "FFPROBE_BIN", None)
    # 隔离会话注册表与并发槽，避免跨用例串扰
    monkeypatch.setattr(tc, "_SESSIONS", {})
    monkeypatch.setattr(tc, "_ACTIVE", 0)
    monkeypatch.setattr(tc, "_SLOT_COND", None)


def wait_search_complete(client, path: str, params: dict, min_items: int = 0,
                         headers: dict | None = None, tries: int = 40,
                         interval: float = 0.15) -> dict:
    """轮询搜索接口直到各源聚合完成，返回响应 JSON。

    `/search/track` 的设计是「有在线条目就立刻回复」（本地在前、在线在后），
    首个响应常常只带**最先返回的那个源** —— 各源都是 MockTransport 时谁先返回取决于
    调度，直接断言首个响应会随机缺源（表现为「明明取到了却在合并结果里消失」的假失败）。

    这里等「条数连续两次不再变化」再返回；配合用例里 `prefetch_enabled=False`
    （预热探活会把探不通的条目标记不可播并在后续请求剔除，那会干扰排序断言）。
    """
    import time as _time

    last_body: dict = {}
    prev_count = -1
    stable = 0
    for _ in range(tries):
        resp = client.get(path, params=params, headers=headers or {})
        last_body = resp.json()
        count = len(((last_body.get("data") or {}).get("list")) or [])
        if count >= min_items and count == prev_count:
            stable += 1
            if stable >= 2:
                return last_body
        else:
            stable = 0
        prev_count = count
        _time.sleep(interval)
    return last_body
