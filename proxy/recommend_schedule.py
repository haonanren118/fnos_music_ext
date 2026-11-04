"""每日推荐 / 热门推荐的「静默定时预构建」调度器。

背景：2.7.7 及以前，两份推荐歌单都是懒构建 —— 用户当天第一次打开歌单列表
才触发 get_or_build_daily()，最坏要等渐进 checkpoint 超时（列表 2s / 详情 20s），
后台构建还会继续跑满预算。用户诉求是「每天 00:00 静默更新」：到点先把当日
daily / hot 两份缓存构建好落盘，用户任何时刻打开都直接命中缓存，零等待。

职责边界（只做两件事，避免与在线请求逻辑分叉）：
  1. 决定「何时跑」：下一个刷新时刻（默认本地 06:00）+ 启动补做；
  2. 决定「为谁跑」：从既有缓存目录 / 播放历史里枚举已知用户。
实际构建完全复用 app._ensure_daily_task_for_app()，因此去重（_DAILY_TASKS）、
空结果冷却、渐进 checkpoint、轮换历史等行为与在线请求完全一致。

静默约束：
  - 仅后台 asyncio task，不产生任何 UI 提示、不改动前端；
  - 启动宽限期后才跑，不拖慢服务启动；
  - 单次失败只写日志，循环继续，不影响在线请求；
  - 分钟级重算下一次触发，避免长 sleep 遇系统时钟跳变 / 休眠而错过。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

try:
    from . import recommend as dailyrec  # type: ignore
except ImportError:  # 以脚本方式直接导入 proxy/ 目录时
    import recommend as dailyrec  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_HOUR = 0
STARTUP_GRACE_S = 45.0
_TICK_S = 60.0


def refresh_hour() -> int:
    """刷新时刻（默认 00:00）与 recommend.today_key() 共用同一定义，避免两处不一致。"""
    getter = getattr(dailyrec, "refresh_hour", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_REFRESH_HOUR


def seconds_until_next_refresh(now: datetime | None = None) -> float:
    """距下一个刷新时刻的秒数；落在刷新那一分钟内返回 0（立即触发）。

    留 1 分钟窗口而不是「恰好 00:00:00 才算到点」：长 sleep 被系统时钟跳变、
    NTP 校正或休眠打断时，仍能在窗口内补上，不至于整天错过。
    """
    now = now or datetime.now()
    target = now.replace(hour=refresh_hour(), minute=0, second=0, microsecond=0)
    if now < target:
        return (target - now).total_seconds()
    if now < target + timedelta(minutes=1):
        return 0.0
    return (target + timedelta(days=1) - now).total_seconds()


def known_users() -> list[str]:
    """已知用户列表（已是 _safe_user_name 之后的名字，幂等，可直接当 user_guid 用）。

    来源 1：recommend_cache/<user>/ —— 曾经成功生成过推荐的用户；
    来源 2：play_history/<user>.json —— 有在线播放记录、但缓存可能被清过的用户；
    另固定包含 shared：身份探测失败时 hot 是公共榜单，也应有当日缓存。
    读不到目录时返回 ["shared"]，不抛异常。
    """
    users = {"shared"}
    try:
        cache_root = dailyrec.recommend_cache_dir()
        for name in sorted(os.listdir(cache_root)):
            if os.path.isdir(os.path.join(cache_root, name)) and not name.startswith("."):
                users.add(name)
    except OSError:
        pass
    try:
        history_root = dailyrec.play_history_dir()
        for name in sorted(os.listdir(history_root)):
            if name.endswith(".json"):
                users.add(name[:-5])
    except OSError:
        pass
    return sorted(users)


def needs_build(user_guid: str, kind: str, day: str) -> bool:
    """当日该类型的缓存是否还缺（空 / 不存在 / 内容与日期不符）。"""
    try:
        cached = dailyrec.load_daily_cache(user_guid, day, kind)
    except Exception:
        return True
    return not (isinstance(cached, dict) and cached.get("tracks"))


async def prebuild_all(app, build_one, enabled_kinds) -> int:
    """把所有已知用户当日缺的 daily / hot 依次补齐，返回实际触发的构建数。

    顺序执行而非并发：推荐链本身要打音源 / LLM，并发会互相挤占配额，
    且「网易每日推荐」当天只有第一个用户能占名额，顺序可保证归属确定。
    build_one(app, user, kind) 由 app 提供，内部复用 _DAILY_TASKS 去重。
    """
    day = dailyrec.today_key()
    triggered = 0
    for user in known_users():
        try:
            kinds = enabled_kinds(user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommend refresh: enabled_kinds failed for %s: %s", user, exc)
            continue
        for kind in ("daily", "hot"):
            if kind not in kinds:
                continue
            if not needs_build(user, kind, day):
                continue
            try:
                await build_one(app, user, kind)
                triggered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单个失败不能中断整轮
                logger.warning("recommend refresh: %s/%s build failed: %s", user, kind, exc)
    if triggered:
        logger.info("recommend refresh: day=%s prebuilt %d bundle(s)", day, triggered)
    return triggered


async def run(app, build_one, enabled_kinds) -> None:
    """常驻循环：启动补做一次，此后每天刷新时刻静默补做。

    启动补做的必要性：服务若在刷新时刻之后才启动（NAS 重启 / 服务重启）且当日
    缓存缺失，不补做就会退化回「用户首次打开才构建」，静默效果当天失效。
    """
    await asyncio.sleep(STARTUP_GRACE_S)
    try:
        await prebuild_all(app, build_one, enabled_kinds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("recommend refresh: startup prebuild failed: %s", exc)

    while True:
        delay = seconds_until_next_refresh()
        if delay > _TICK_S:
            await asyncio.sleep(_TICK_S)
            continue
        if delay > 0:
            await asyncio.sleep(delay)
        # 到点再缓一拍，确保 today_key() 已经跨过逻辑日边界，不会构建到「昨天」
        await asyncio.sleep(0.5)
        try:
            await prebuild_all(app, build_one, enabled_kinds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommend refresh: scheduled prebuild failed: %s", exc)
        # 让开整个刷新窗口，避免同一分钟内反复进入（prebuild_all 本身是幂等的）
        await asyncio.sleep(61.0)
