"""pytest 收集配置（musicbox-service）。

`test_public_playlists.py` 在本机被 ACL 锁死（读、写、删除、takeown 全部拒绝，
Windows 侧疑似遗留独占句柄），其内容还是 #55 期间的旧断言：
  - 排行榜分类期望只有「全部」（已改为 官方榜 + tag 特色榜）；
  - tracks 502 用 songs_url 全挂触发（playable_only=False 后改为详情缺数据触发）。
保留该文件会让 `pytest musicbox-service/` 持续报红，因此这里显式忽略；
权威用例已迁到 `test_public_playlists_suite.py`（旧文件全部用例 + 排行榜分类新增用例）。

锁定解除后（重启机器即可）可以删除本文件，并把 suite 改回原名。
"""
collect_ignore = ["test_public_playlists.py"]
