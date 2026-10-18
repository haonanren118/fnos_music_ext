"""搜索聚合等待助手（★ 2026-10-18）

`/search/track` 的设计是「有在线条目就立刻回复」（本地在前、在线在后），首个响应
常常只带**最先返回的那个源**。各源都是 MockTransport 时谁先返回取决于调度，直接
断言首个响应会随机缺源 —— 表现为「明明在聚合里取到了，却在合并结果里消失」的假失败。

放在独立模块而不是 conftest：仓库里另有 `lxmusic-service/conftest.py`，`from
conftest import ...` 会解析到那个文件而报 ImportError。
"""


def wait_search_complete(client, path: str, params: dict, min_items: int = 0,
                         headers=None, tries: int = 40, interval: float = 0.15) -> dict:
    """轮询搜索接口直到各源聚合完成，返回响应 JSON。

    等「条数连续两次不再变化」再返回；调用方需 `prefetch_enabled=False`，
    否则预热探活会把探不通的条目标记不可播并在后续请求剔除，反而等到了降级结果。
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
