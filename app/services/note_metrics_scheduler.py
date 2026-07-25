"""笔记数据定时采集调度器:每天每号自动 enqueue note_export,失败等比退避补采。

动机:note_metrics_daily 按 snapshot_date 存趋势快照,但采集此前全靠 skill 侧/人工
手动触发 —— 没人触发趋势就断更。本组件补上定时器,只做「现在该不该采」的决策 +
enqueue,执行仍走统一台账(supervisor 派发 → account_worker 子进程拟人导出),与手动
触发完全同路,不新增执行域。

采集范围:**所有已接入 cookie 的账号**(不筛 cookie_status —— 失效号也尝试,失败留
台账痕迹供运营发现;未接入 cookie 的账号必然失败,跳过直到扫码接入)。

节流(防对真账号高频重试):**每号每天最多自动采 3 次,失败后等比退避**——
第 1 次失败后隔 ≥1h 补采,第 2 次失败后隔 ≥2h(等比 ×2),3 次仍败当天放弃次日重来。
今天已有快照 / 已有 done 台账行 / 在途(queued|running)都跳过;手动触发的台账行同样
计入当日次数(手动成功后不再自动采)。

日界口径:与 note_export.execute 的 snapshot_date 一致,取 UTC 日期。
"""
from datetime import datetime, timedelta, timezone

import asyncio

from loguru import logger
from sqlalchemy import select

from app.models.browser_job import BrowserJob
from app.models.note_metric import NoteMetricDaily
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo

# 非请求上下文的进程内直调,台账 operator_id 约定用 0(见 BrowserJob.operator_id 注释)
_SYSTEM_OPERATOR_ID = 0
# 每号每天最多自动尝试次数(含手动触发的台账行)
_MAX_ATTEMPTS_PER_DAY = 3
# 等比退避:第 n 次失败后,距该次尝试 ≥ 下表[n-1] 秒才补采(1h → 2h,比值 2)
_RETRY_BACKOFF_S = [3600, 7200]


async def ensure_baseline(session_factory, account_id: int) -> bool:
    """新账号首次基底采集:cookie 确认 valid 后调用,从未有快照则立即 enqueue 一条 note_export。

    幂等:已有任意日快照(基底在)或今天已有 note_export 台账行(在途/已试)都不再发。
    全程走传入的 session_factory(不用 repo 全局会话——cookie 检测的单测只 patch 组件
    工厂,走全局会话会把测试数据打进生产库)。返回是否真的 enqueue 了。
    调用方(cookie 检测写回路径)须 try/except 包裹:基底采集失败绝不打断检测主流程。
    """
    import uuid

    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    async with session_factory() as session:
        has_snapshot = (await session.execute(
            select(NoteMetricDaily.id)
            .where(NoteMetricDaily.account_id == account_id)
            .limit(1)
        )).scalar() is not None
        if has_snapshot:
            return False
        attempted_today = (await session.execute(
            select(BrowserJob.id)
            .where(
                BrowserJob.kind == "note_export",
                BrowserJob.account_id == account_id,
                BrowserJob.created_at >= day_start,
            )
            .limit(1)
        )).scalar() is not None
        if attempted_today:
            return False
        # 直插台账行(与 browser_jobs_repo.enqueue 同构,但走本函数的 session_factory)
        session.add(BrowserJob(
            id=uuid.uuid4().hex,
            kind="note_export",
            account_id=account_id,
            operator_id=_SYSTEM_OPERATOR_ID,
            payload="{}",
            status="queued",
        ))
        await session.commit()
    logger.info(f"[note_metrics_scheduler] 新账号 {account_id} cookie 转 valid,基底采集已 enqueue")
    return True


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
        """跑一轮:全部已接入 cookie 的账号,按「快照/次数/在途/退避」决策补采;返回 enqueue 条数。"""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")  # 与 note_export snapshot_date 同口径
        day_start = datetime(now.year, now.month, now.day)  # naive UTC,对齐 created_at 存储
        now_naive = now.replace(tzinfo=None)  # 与 created_at(naive UTC)可比

        async with self._session_factory() as session:
            # 所有已接入 cookie 的账号(不筛 cookie_status;无 cookie 必然失败,跳过)
            account_ids = list((await session.execute(
                select(XhsAccount.id)
                .where(
                    XhsAccount.login_cookies.isnot(None),
                    XhsAccount.login_cookies != "",
                )
                .order_by(XhsAccount.id)
            )).scalars().all())
            # 今天已有快照的账号
            snapped = set((await session.execute(
                select(NoteMetricDaily.account_id)
                .where(NoteMetricDaily.snapshot_date == today)
                .distinct()
            )).scalars().all())
            # 今天各号的 note_export 台账行(含手动触发的),按账号聚合供次数/在途/退避判定
            rows = (await session.execute(
                select(BrowserJob.account_id, BrowserJob.status, BrowserJob.created_at)
                .where(
                    BrowserJob.kind == "note_export",
                    BrowserJob.created_at >= day_start,
                )
                .order_by(BrowserJob.created_at)
            )).all()
        jobs_today: dict[int, list] = {}
        for acc, status, created_at in rows:
            jobs_today.setdefault(acc, []).append((status, created_at))

        enqueued = 0
        for account_id in account_ids:
            if self._is_stopping():
                break
            if account_id in snapped:
                continue
            jobs = jobs_today.get(account_id, [])
            if any(s == "done" for s, _ in jobs):
                continue  # 今天已成功(快照可能还在写入途中),不重采
            attempts = len(jobs)
            if attempts >= _MAX_ATTEMPTS_PER_DAY:
                continue  # 当日次数用尽,次日重来
            if attempts > 0:
                last_status, last_at = jobs[-1]
                if last_status in ("queued", "running"):
                    continue  # 在途,等它出结果
                # 上次失败(error):等比退避,距上次尝试满足间隔才补采
                backoff = _RETRY_BACKOFF_S[min(attempts - 1, len(_RETRY_BACKOFF_S) - 1)]
                if now_naive < last_at + timedelta(seconds=backoff):
                    continue
            await browser_jobs_repo.enqueue(
                "note_export", {}, operator_id=_SYSTEM_OPERATOR_ID, account_id=account_id
            )
            logger.info(
                f"[note_metrics_scheduler] 账号 {account_id} 今日第 {attempts + 1} 次采集已 enqueue"
                + (f"(上次失败退避 {_RETRY_BACKOFF_S[min(attempts - 1, len(_RETRY_BACKOFF_S) - 1)]}s 已过)" if attempts else "")
            )
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
