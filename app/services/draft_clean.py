"""草稿箱周清理服务:契约 execute()(browser_jobs kind=draft_clean)+ 每周调度器。

本系统不用草稿功能,所有草稿都是垃圾(draft-only 历史遗留 + 发布编辑器自动存的空草稿,
每次发布类自动化跑过都可能新增)。DraftCleanScheduler 每号每 7 天 enqueue 一条清理任务,
执行走统一台账 → supervisor 派发 → account_worker 子进程拟人清理——与发布/检测/导出
同一条同号串行通道,**绝不与定时发布抢 profile**。

范围:所有已接入 cookie 的账号(cookie_status=invalid 的号 creator 会话可能仍活,
实测 acc5/6 能进草稿箱;真不可达时以 draftbox_not_found 收口,下周再试)。
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.draft_cleaner import DraftCleanError, clean_drafts
from app.browser.sync_client import SyncClient
from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services.cookie_check import load_account_cookies

# 系统直调 operator 约定(同 note_metrics_scheduler)
_SYSTEM_OPERATOR_ID = 0
# 每号清理周期(天)
_CLEAN_EVERY_DAYS = 7


def _clean_sync(account_id: int, cookies: list[dict]) -> dict:
    """同一线程内:建 client → start → clean_drafts → stop。"""
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"error": f"浏览器启动失败:{start.get('error')}"}
        # 不拦主站登录态:creator 会话可能仍活,以草稿箱实际可达性为准(browser 层判)
        return clean_drafts(client.page, account_id)
    except DraftCleanError as e:
        return {"error": e.reason}
    except Exception as e:  # noqa: BLE001
        return {"error": f"草稿清理异常:{e}"}
    finally:
        client.stop()


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次草稿清理(契约函数):同号锁 + 浏览器闸内跑同步清理。

    返回 {"deleted","remaining"} 或 {"error"};任何意外兜底为 error,绝不抛出。
    """
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "该账号未接入 cookie,无法打开草稿箱"}
        async with account_locks.get(account_id):
            async with browser_slot():
                return await asyncio.to_thread(_clean_sync, account_id, cookies)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"草稿清理任务异常 account_id={account_id}")
        return {"error": f"草稿清理任务异常:{exc}"}


class DraftCleanScheduler:
    """周清理调度:每 interval 秒醒来,对「7 天内没有 draft_clean 台账行」的账号 enqueue。

    结构套 CookieChecker/NoteMetricsScheduler 模板。判据用「7 天内任意状态台账行」:
    失败(如 creator 真不可达)也等下周期再试,不高频重试;在途行天然去重。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                enqueued = await self.scan_once()
                if enqueued:
                    logger.info(f"[draft_clean_scheduler] 本轮 enqueue {enqueued} 个账号的草稿清理")
            except Exception:
                logger.exception("[draft_clean_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮:已接入 cookie 且 7 天内无 draft_clean 台账行的账号,各 enqueue 一条。"""
        week_ago = datetime.utcnow() - timedelta(days=_CLEAN_EVERY_DAYS)
        async with self._session_factory() as session:
            account_ids = list((await session.execute(
                select(XhsAccount.id)
                .where(
                    XhsAccount.login_cookies.isnot(None),
                    XhsAccount.login_cookies != "",
                )
                .order_by(XhsAccount.id)
            )).scalars().all())
            recent = set((await session.execute(
                select(BrowserJob.account_id)
                .where(
                    BrowserJob.kind == "draft_clean",
                    BrowserJob.created_at >= week_ago,
                )
                .distinct()
            )).scalars().all())

            enqueued = 0
            for account_id in account_ids:
                if self._is_stopping():
                    break
                if account_id in recent:
                    continue
                # 直插台账行(走本组件 session_factory,理由同 ensure_baseline)
                session.add(BrowserJob(
                    id=uuid.uuid4().hex,
                    kind="draft_clean",
                    account_id=account_id,
                    operator_id=_SYSTEM_OPERATOR_ID,
                    payload="{}",
                    status="queued",
                ))
                logger.info(f"[draft_clean_scheduler] 账号 {account_id} 距上次清理超 "
                            f"{_CLEAN_EVERY_DAYS} 天,已 enqueue")
                enqueued += 1
            if enqueued:
                await session.commit()
        return enqueued

    def _is_stopping(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, timeout: float) -> None:
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
