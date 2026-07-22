"""占位废账号 TTL 兜底回收:周期删超龄仍未回填 user_id 的 xhs_account_ 占位行及其授权。

服务端自愈(cookie_service.import_cookies 真登录成功即清同 operator 近窗占位)覆盖"失败后
当天重试成功"的主路径;本 reaper 兜底覆盖"操作者失败后一直没重试"的残留——占位行超过
``PLACEHOLDER_TTL_HOURS`` 仍是 user_id 空的 xhs_account_ 前缀行,周期清掉防长期堆积污染
可授权账号列表。判据(与需求 §7.2 一致):``user_id IS NULL AND name LIKE 'xhs_account_%'
AND created_at < utcnow - TTL``;删账号行 + 其全部授权行(应用层级联,与 delete_account 一致)。

设计对齐 ``BrowserReaper`` / ``CookieChecker``:``PLACEHOLDER_REAP_INTERVAL=0`` 时
lifespan 不起该循环;循环**先睡后扫**,故默认间隔(3600s)下,毫秒级进出 lifespan 的单测
永不真正触发回收。``reap_placeholders_once`` 抽出为纯任务便于单测,自开短事务会话(不复用
调用方会话),``stop()`` 优雅取消(可打断 interval 休眠)。
"""

import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select

from app.core.config import settings
from app.models.operator import OperatorAccountAccess
from app.models.xhs_account import XhsAccount


async def reap_placeholders_once(session_factory) -> int:
    """扫一轮:删超 TTL 的占位废账号(user_id 空 + xhs_account_ 前缀)及其授权行,返回删除数。

    仅删满足全部三条件的行:user_id 为空、name 以 xhs_account_ 开头、created_at 早于
    utcnow - PLACEHOLDER_TTL_HOURS。带 user_id 的号(即便超龄)与未超龄的占位都不动。
    """
    cutoff = datetime.utcnow() - timedelta(hours=settings.PLACEHOLDER_TTL_HOURS)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(XhsAccount.id, XhsAccount.name).where(
                    XhsAccount.user_id.is_(None),
                    XhsAccount.name.like("xhs_account_%"),
                    XhsAccount.created_at < cutoff,
                )
            )
        ).all()
        if not rows:
            return 0
        ids = [row.id for row in rows]
        await session.execute(
            delete(OperatorAccountAccess).where(
                OperatorAccountAccess.xhs_account_id.in_(ids)
            )
        )
        await session.execute(delete(XhsAccount).where(XhsAccount.id.in_(ids)))
        await session.commit()
    logger.info(
        f"[placeholder_reaper] TTL 回收占位废账号 删除 {len(ids)} 行: "
        + ", ".join(f"{row.id}:{row.name}" for row in rows)
    )
    return len(ids)


class PlaceholderReaper:
    """周期占位废账号回收后台循环:每 ``interval`` 秒跑一轮 ``reap_placeholders_once``。

    **先睡后扫**(对齐 BrowserReaper):默认间隔下,毫秒级进出 lifespan 的单测不触发真实
    回收。单轮异常仅记录不崩循环;``stop()`` 优雅取消(可打断 interval 休眠)。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台回收循环。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台循环:先睡 interval 再跑一轮回收;单轮异常仅记录不崩循环。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            await self._sleep(self._interval)
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                await reap_placeholders_once(self._session_factory)
            except Exception:
                logger.exception("[placeholder_reaper] 回收轮次异常")

    async def _sleep(self, timeout: float) -> None:
        """可被 stop() 立即打断的休眠;未 start 时退化为普通 sleep。"""
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        """优雅停:置停止信号 → 等后台循环退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
