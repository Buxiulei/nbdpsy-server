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

# 占位废账号 name 的字面前缀(插件 accountName 时间戳兜底 = f"xhs_account_{Date.now()}")。
PLACEHOLDER_NAME_PREFIX = "xhs_account_"


def placeholder_clauses():
    """占位废账号字面判据(需求 §7.2)的 SQL 子句:user_id 为空 + name 字面前缀。

    ``startswith(PREFIX, autoescape=True)`` 生成带 ESCAPE 的字面前缀 LIKE
    (``name LIKE 'xhs\\_account\\_%' ESCAPE ...``),把 name 里的下划线当普通字符而非 LIKE
    单字符通配符——否则昵称形如 ``xhsXaccountY`` 且 user_id 为空的真号会被误纳入清理集,
    违反需求 §5(不丢可能有效的 cookie)。方向 A(cookie_service 自愈)与方向 B(本模块 TTL
    回收)共用本判据,保证清理口径单一来源、不漂移。
    """
    return (
        XhsAccount.user_id.is_(None),
        XhsAccount.name.startswith(PLACEHOLDER_NAME_PREFIX, autoescape=True),
    )


async def delete_placeholder_rows(session, ids) -> int:
    """删除 ids 中"仍满足占位判据"的账号行及其授权行,返回实际删除的账号数。不自 commit。

    DELETE 内重申判据是并发防线:SELECT→DELETE 之间被并发 import 原地回填 user_id 升级成真号
    的行,DELETE 时因不再满足 ``user_id IS NULL`` 而豁免(账号行不删)。授权行只清"账号确已不存在
    (即刚被本次删掉)"的——按 ``xhs_account_id NOT IN (现存账号 id)`` 收窄,保证被升级成真号的
    行其授权行不被多删。
    """
    if not ids:
        return 0
    result = await session.execute(
        delete(XhsAccount).where(XhsAccount.id.in_(ids), *placeholder_clauses())
    )
    deleted = result.rowcount
    if deleted:
        await session.execute(
            delete(OperatorAccountAccess).where(
                OperatorAccountAccess.xhs_account_id.in_(ids),
                OperatorAccountAccess.xhs_account_id.not_in(select(XhsAccount.id)),
            )
        )
    return deleted


async def reap_placeholders_once(session_factory) -> int:
    """扫一轮:删超 TTL 的占位废账号(user_id 空 + xhs_account_ 字面前缀)及其授权行,返回删除数。

    仅删满足全部三条件的行:user_id 为空、name 以 xhs_account_ 字面开头、created_at 早于
    utcnow - PLACEHOLDER_TTL_HOURS。带 user_id 的号(即便超龄)与未超龄的占位都不动。删除走
    delete_placeholder_rows(DELETE 内重申判据 + 授权行按实际删除收窄)。
    """
    cutoff = datetime.utcnow() - timedelta(hours=settings.PLACEHOLDER_TTL_HOURS)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(XhsAccount.id, XhsAccount.name).where(
                    *placeholder_clauses(),
                    XhsAccount.created_at < cutoff,
                )
            )
        ).all()
        if not rows:
            return 0
        ids = [row.id for row in rows]
        deleted = await delete_placeholder_rows(session, ids)
        await session.commit()
    if deleted:
        logger.info(
            f"[placeholder_reaper] TTL 回收占位废账号 删除 {deleted}/{len(ids)} 候选: "
            + ", ".join(f"{row.id}:{row.name}" for row in rows)
        )
    return deleted


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
