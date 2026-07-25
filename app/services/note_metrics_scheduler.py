"""笔记数据定时采集调度器:每天每号自动 enqueue 一条 note_export 台账任务。

动机:note_metrics_daily 按 snapshot_date 存趋势快照,但采集此前全靠 skill 侧/人工
手动触发 —— 没人触发趋势就断更。本组件补上定时器,只做「今天该不该采」的决策 +
enqueue,执行仍走统一台账(supervisor 派发 → account_worker 子进程拟人导出),与手动
触发完全同路,不新增执行域。

节流铁律(防对真账号高频重试):**每号每天最多自动采 1 次** —— 今天已有快照、或今天
已存在任意状态的 note_export 台账行(含手动触发的、含失败的)都跳过。失败不当天补采,
次日自然重试;手动 refresh 不受影响(REST 直 enqueue,不经本组件)。

日界口径:与 note_export.execute 的 snapshot_date 一致,取 UTC 日期。
"""
from datetime import datetime, timezone

import asyncio

from loguru import logger
from sqlalchemy import select

from app.models.browser_job import BrowserJob
from app.models.note_metric import NoteMetricDaily
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo

# 非请求上下文的进程内直调,台账 operator_id 约定用 0(见 BrowserJob.operator_id 注释)
_SYSTEM_OPERATOR_ID = 0


class NoteMetricsScheduler:
    """周期扫描:对 cookie_status='valid' 账号,今天没采过就 enqueue 一条 note_export。

    结构套 CookieChecker 模板(start/_run_loop/stop,interval>0 才注册)。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台调度循环。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台循环:每 interval 秒跑一轮 scan_once,单轮异常不打断循环。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                enqueued = await self.scan_once()
                if enqueued:
                    logger.info(f"[note_metrics_scheduler] 本轮补采 enqueue {enqueued} 个账号")
            except Exception:
                logger.exception("[note_metrics_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮:valid 账号中「今天没快照且今天没采集过」的,各 enqueue 一条;返回条数。"""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")  # 与 note_export snapshot_date 同口径
        day_start = datetime(now.year, now.month, now.day)  # naive UTC,对齐 created_at 存储

        async with self._session_factory() as session:
            account_ids = list((await session.execute(
                select(XhsAccount.id)
                .where(XhsAccount.cookie_status == "valid")
                .order_by(XhsAccount.id)
            )).scalars().all())
            # 今天已有快照的账号
            snapped = set((await session.execute(
                select(NoteMetricDaily.account_id)
                .where(NoteMetricDaily.snapshot_date == today)
                .distinct()
            )).scalars().all())
            # 今天已存在 note_export 台账行的账号(任意状态:queued/running/done/error 都算已尝试)
            attempted = set((await session.execute(
                select(BrowserJob.account_id)
                .where(
                    BrowserJob.kind == "note_export",
                    BrowserJob.created_at >= day_start,
                )
                .distinct()
            )).scalars().all())

        enqueued = 0
        for account_id in account_ids:
            if self._is_stopping():
                break
            if account_id in snapped or account_id in attempted:
                continue
            await browser_jobs_repo.enqueue(
                "note_export", {}, operator_id=_SYSTEM_OPERATOR_ID, account_id=account_id
            )
            logger.info(f"[note_metrics_scheduler] 账号 {account_id} 今日未采,已 enqueue note_export")
            enqueued += 1
        return enqueued

    def _is_stopping(self) -> bool:
        """是否已收到停止信号(未 start 时视为不停止,便于直接调 scan_once 测试)。"""
        return self._stop_event is not None and self._stop_event.is_set()

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
