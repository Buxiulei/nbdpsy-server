"""worker 进程入口 + Supervisor 调度中枢(``python -m app.worker``)。

设计:``docs/design/2026-07-24-api-worker-split-design.md`` §二(进程模型)/ §四(多账号
并行调度)。本进程是调度中枢,自身不起账号浏览器:

- 5s 扫描循环:僵死恢复(browser_jobs)→ 取可派发工作(publish_jobs SQL 直查 +
  browser_jobs 台账)→ 按账号公平轮转派生 ``python -m app.account_worker`` 子进程。
- 同账号同一时刻至多 1 个子进程(顶替进程内 account_locks 的串行语义);
  全局子进程数封顶 ``settings.BROWSER_CONCURRENCY``。
- ``kind=op_images`` 无账号、纯 API 调用型,不派子进程,supervisor 进程内直接异步执行
  (乐观认领 ``claim_job_sync`` 经 to_thread → ``op_images.execute`` → 写回终态)。
- 后台组件(CookieChecker / BrowserReaper / PlaceholderReaper / 视频调度)从
  server.py lifespan 迁到本进程,随 Supervisor 起停;视频调度仅在
  ``include_video=True``(worker 进程入口)时启动,单进程 ``all`` 模式维持
  「视频 worker 独立进程部署」的既有行为不回归。
- SIGTERM/SIGINT:停止派发 → 给存活子进程 10s 收尾 → SIGKILL 进程组,15s 内退出。
"""

import asyncio
import json
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func, update, select

import app.core.db as db_module
from app.browser.browser_reaper import BrowserReaper
from app.browser.cookie_checker import CookieChecker
from app.browser.egress_guard import EgressGuard
from app.core.config import settings
from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob
from app.services import op_images as op_images_service
from app.services.audience_sync_scheduler import AudienceSyncScheduler
from app.services.content_archive import ArchiveReaper
from app.services.draft_clean import DraftCleanScheduler
from app.services.dreamina import ClipReaper, DreaminaScheduler
from app.services.interaction_backfill_scheduler import InteractionBackfillScheduler
from app.services.note_ledger_sync_scheduler import NoteLedgerSyncScheduler
from app.services.note_metrics_scheduler import NoteMetricsScheduler
from app.services.onboarding_scheduler import OnboardingScheduler
from app.services.placeholder_reaper import PlaceholderReaper
from app.services.retention_scheduler import RetentionScheduler

# 派发判据的唯一真源(见 app/services/queue_status.py 模块 docstring):什么算一次会话、
# 闸放不放行、批次怎么排序、帽值默认取值。轮询端点的 queue 段读的是同一批函数 ——
# 判据只此一份,派发层与可见性层不可能各说各话。
from app.services.queue_status import (
    LAYER_OPERATOR,
    SOURCE_BROWSER,
    SOURCE_PUBLISH,
    browser_session_filter,
    cap_allows,
    configured_max_procs,
    configured_operator_session_cap,
    configured_session_cap,
    layer_of,
    norm_created,
    publish_due_filter,
    publish_session_filter,
    queue_sort_key,
    session_window_cutoff,
)

# browser_jobs 台账 repo(P1 产物):集成前分支上可能尚不存在,容缺导入 —— repo 为 None 时
# Supervisor 跳过 browser_jobs 相关扫描(仅调度 publish_jobs),集成后自动生效;
# 测试注入假 repo 按契约签名(recover_stale/list_dispatchable/claim_job_sync/finish_job_sync)验证。
try:
    from app.services import browser_jobs_repo as _default_browser_jobs_repo
except ImportError:  # pragma: no cover - 集成前的过渡态
    _default_browser_jobs_repo = None

# 仓库根:account_worker 子进程的 cwd 与 .venv python 解释器路径基准。
_REPO_ROOT = Path(__file__).resolve().parents[1]

# 已到点的发布任务积压到几条就告警(只告警,绝不据此改任务状态,理由见 _warn_publish_backlog)
PUBLISH_BACKLOG_ALERT = 3


def _sqlite_db_path() -> str:
    """从 ``settings.DATABASE_URL`` 提取 sqlite 文件路径(供 sync 侧台账直连与子进程 --db)。"""
    url = settings.DATABASE_URL
    marker = ":///"
    if marker in url:
        return url.split(marker, 1)[1]
    return "data/nbdpsy.db"


class Supervisor:
    """调度中枢:扫描 DB 队列,按账号公平轮转派生 account_worker 子进程。

    参数均有 settings 兜底,构造入参仅供测试注入小值/假件:

    - ``repo``:browser_jobs 台账 repo(默认模块级容缺导入的真 repo,可能为 None)。
    - ``include_video``:是否内嵌视频调度(worker 进程 True;server ``all`` 模式 False)。
    - ``include_dreamina``:是否内嵌即梦片段调度 + 产物 TTL 清理(同上开关语义)。
    - ``scan_interval`` / ``batch_per_account`` / ``proc_timeout`` / ``child_grace`` /
      ``max_procs``:分别兜底 ``WORKER_SCAN_INTERVAL``(5s)/ ``WORKER_BATCH_PER_ACCOUNT``
      (3)/ ``ACCOUNT_PROC_TIMEOUT``(1800s)/ 停机宽限(10s)/ ``BROWSER_CONCURRENCY``;
    - ``session_cap``:同号一小时浏览器会话总闸,兜底 ``ACCOUNT_HOURLY_SESSION_CAP``(4);
    - ``operator_session_cap``:同号一小时运营触发会话帽(比系统那层宽),兜底
      ``ACCOUNT_HOURLY_OPERATOR_SESSION_CAP``(12)。
    """

    def __init__(
        self,
        session_factory,
        *,
        repo=None,
        include_video: bool = False,
        include_dreamina: bool = False,
        scan_interval: float | None = None,
        batch_per_account: int | None = None,
        proc_timeout: float | None = None,
        child_grace: float | None = None,
        max_procs: int | None = None,
        session_cap: int | None = None,
        operator_session_cap: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repo = repo if repo is not None else _default_browser_jobs_repo
        self._include_video = include_video
        self._include_dreamina = include_dreamina
        self._scan_interval = (
            scan_interval
            if scan_interval is not None
            else getattr(settings, "WORKER_SCAN_INTERVAL", 5)
        )
        self._batch = (
            batch_per_account
            if batch_per_account is not None
            else getattr(settings, "WORKER_BATCH_PER_ACCOUNT", 3)
        )
        self._proc_timeout = (
            proc_timeout
            if proc_timeout is not None
            else getattr(settings, "ACCOUNT_PROC_TIMEOUT", 1800)
        )
        self._child_grace = child_grace if child_grace is not None else 10.0
        self._max_procs = max_procs if max_procs is not None else configured_max_procs()
        self._session_cap = (
            session_cap if session_cap is not None else configured_session_cap()
        )
        self._operator_session_cap = (
            operator_session_cap
            if operator_session_cap is not None
            else configured_operator_session_cap()
        )
        self._worker_tag = f"supervisor-{os.getpid()}"
        # account_id → 存活子进程(派生登记,子进程退出即回收移除)
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        # 账号公平轮转表:本轮派过的账号移到队尾,单账号大批量不饿死其他账号
        self._rotation: list[int] = []
        # 进程内在途 op_images job id(防同一 job 重复起执行任务;真正互斥靠乐观认领)
        self._op_inflight: set[str] = set()
        # 已因会话总闸被拦过的账号(日志去重:5s 一轮,每轮都吵就没人看了)
        self._governed_accounts: set[int] = set()
        # 同上,运营那层单独一份:两层文案不同、超帽时机也不同,合用一份会互相吞日志
        self._operator_governed_accounts: set[int] = set()
        # 内部后台 task 登记(子进程回收 / op_images 执行),停机时统一收尾
        self._tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        # 后台组件(启动后持有引用,停机反向 stop)
        self._cookie_checker: CookieChecker | None = None
        self._browser_reaper: BrowserReaper | None = None
        self._placeholder_reaper: PlaceholderReaper | None = None
        self._archive_reaper: ArchiveReaper | None = None
        self._note_metrics_scheduler: NoteMetricsScheduler | None = None
        self._draft_clean_scheduler: DraftCleanScheduler | None = None
        self._retention_scheduler: RetentionScheduler | None = None
        self._interaction_backfill_scheduler: InteractionBackfillScheduler | None = None
        self._audience_sync_scheduler: AudienceSyncScheduler | None = None
        self._note_ledger_sync_scheduler: NoteLedgerSyncScheduler | None = None
        self._onboarding_scheduler: OnboardingScheduler | None = None
        self._egress_guard: EgressGuard | None = None
        self._video_scheduler = None
        self._dreamina_scheduler: DreaminaScheduler | None = None
        self._clip_reaper: ClipReaper | None = None

    # ---------------- 生命周期 ----------------

    def request_stop(self) -> None:
        """请求停机(SIGTERM/SIGINT 处理器、server lifespan shutdown 调用):停止派发。"""
        self._stop_event.set()

    async def run(self) -> None:
        """主循环:起后台组件 → 每 scan_interval 扫描派发,直到 request_stop → 优雅收尾。"""
        await self._start_components()
        try:
            while not self._stop_event.is_set():
                try:
                    await self.scan_once()
                except Exception:
                    logger.exception("supervisor 扫描循环异常(忽略,下轮重试)")
                # 可被 request_stop 立即唤醒的休眠
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._scan_interval
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_components()
            await self._terminate_children()
            await self._cancel_tasks()

    async def _start_components(self) -> None:
        """起后台组件(从 server.py lifespan 平移,开关语义逐项不变:interval>0 才起)。"""
        if settings.COOKIE_CHECK_INTERVAL > 0:
            self._cookie_checker = CookieChecker(
                self._session_factory, settings.COOKIE_CHECK_INTERVAL
            )
            self._cookie_checker.start()
        if settings.BROWSER_REAP_INTERVAL > 0:
            self._browser_reaper = BrowserReaper(settings.BROWSER_REAP_INTERVAL)
            self._browser_reaper.start()
        if settings.PLACEHOLDER_REAP_INTERVAL > 0:
            self._placeholder_reaper = PlaceholderReaper(
                self._session_factory, settings.PLACEHOLDER_REAP_INTERVAL
            )
            self._placeholder_reaper.start()
        if settings.ARCHIVE_REAP_INTERVAL > 0:
            self._archive_reaper = ArchiveReaper(
                self._session_factory, settings.ARCHIVE_REAP_INTERVAL
            )
            self._archive_reaper.start()
        if settings.NOTE_METRICS_INTERVAL > 0:
            self._note_metrics_scheduler = NoteMetricsScheduler(
                self._session_factory, settings.NOTE_METRICS_INTERVAL
            )
            self._note_metrics_scheduler.start()
        if settings.RETENTION_CHECK_INTERVAL > 0:
            # 代管账号笔记上限淘汰:挂在 note_metrics 之后(它只在"当日快照已存在"时才动手),
            # 故注册点与 NoteMetricsScheduler 同款、就挂在它旁边。
            self._retention_scheduler = RetentionScheduler(
                self._session_factory, settings.RETENTION_CHECK_INTERVAL
            )
            self._retention_scheduler.start()
        if settings.EGRESS_CHECK_INTERVAL > 0:
            self._egress_guard = EgressGuard(settings.EGRESS_CHECK_INTERVAL)
            self._egress_guard.start()
        if settings.DRAFT_CLEAN_INTERVAL > 0:
            self._draft_clean_scheduler = DraftCleanScheduler(
                self._session_factory, settings.DRAFT_CLEAN_INTERVAL
            )
            self._draft_clean_scheduler.start()
        if settings.INTERACTION_BACKFILL_INTERVAL > 0:
            self._interaction_backfill_scheduler = InteractionBackfillScheduler(
                self._session_factory, settings.INTERACTION_BACKFILL_INTERVAL
            )
            self._interaction_backfill_scheduler.start()
        if settings.AUDIENCE_SYNC_ENABLED and settings.AUDIENCE_SYNC_INTERVAL > 0:
            # 受众行为库采集:与补量调度同门(只插 queued 行,浏览器在 account_worker 子进程)。
            # ENABLED 是 kill switch —— 平台改版/撞墙频繁时先关它止血,已入库数据与分析端点不受影响。
            self._audience_sync_scheduler = AudienceSyncScheduler(
                self._session_factory, settings.AUDIENCE_SYNC_INTERVAL
            )
            self._audience_sync_scheduler.start()
        if (
            settings.LEDGER_SYNC_SCHEDULER_ENABLED
            and settings.LEDGER_SYNC_SCAN_INTERVAL > 0
        ):
            # 笔记台账保底同步:与上面两位同门(只插 queued 行,浏览器在 account_worker 子进程)。
            # 补的是"只有发过布的号才被同步"这个缺口 —— 纯手工运营的号原本永远等不到同步。
            self._note_ledger_sync_scheduler = NoteLedgerSyncScheduler(
                self._session_factory,
                settings.LEDGER_SYNC_SCAN_INTERVAL,
                settings.LEDGER_SYNC_MIN_INTERVAL,
            )
            self._note_ledger_sync_scheduler.start()
        if settings.ONBOARDING_CHECK_INTERVAL > 0:
            self._onboarding_scheduler = OnboardingScheduler(
                self._session_factory,
                settings.ONBOARDING_CHECK_INTERVAL,
                settings.ONBOARDING_CHECK_RETRY_HOURS,
            )
            self._onboarding_scheduler.start()
        if self._include_video:
            # 平移自 app/video/worker.py:必须先 import stages 注册七阶 handler
            # (原地 mutate STAGE_HANDLERS),否则自链首阶段即 KeyError。延迟导入:
            # 仅 worker 进程付视频依赖成本,server(all/api)不受影响。
            import app.video.stages  # noqa: F401
            from app.video.scheduler import VideoScheduler

            self._video_scheduler = VideoScheduler(
                self._session_factory,
                concurrency=int(settings.VIDEO_WORKER_CONCURRENCY or 1),
                heartbeat_interval=int(settings.VIDEO_HEARTBEAT_INTERVAL or 300),
                stale_timeout=int(settings.VIDEO_STALE_TIMEOUT or 900),
            )
            self._video_scheduler.start()
        if self._include_dreamina:
            # 即梦片段调度:每轮先提交 queued 再轮询在飞任务;产物 TTL 另起 reaper。
            # CLI 调用全在 services/dreamina 内经 create_subprocess_exec 串行执行,
            # 与本进程其它后台组件共享事件循环不阻塞。
            self._dreamina_scheduler = DreaminaScheduler(self._session_factory)
            self._dreamina_scheduler.start()
            if settings.CLIP_REAP_INTERVAL > 0:
                self._clip_reaper = ClipReaper(
                    self._session_factory, settings.CLIP_REAP_INTERVAL
                )
                self._clip_reaper.start()

    async def _stop_components(self) -> None:
        """停后台组件(与启动相反顺序)。"""
        if self._clip_reaper is not None:
            await self._clip_reaper.stop()
            self._clip_reaper = None
        if self._dreamina_scheduler is not None:
            await self._dreamina_scheduler.stop()
            self._dreamina_scheduler = None
        if self._video_scheduler is not None:
            await self._video_scheduler.stop()
            self._video_scheduler = None
        if self._egress_guard is not None:
            await self._egress_guard.stop()
            self._egress_guard = None
        if self._onboarding_scheduler is not None:
            await self._onboarding_scheduler.stop()
            self._onboarding_scheduler = None
        if self._note_ledger_sync_scheduler is not None:
            await self._note_ledger_sync_scheduler.stop()
            self._note_ledger_sync_scheduler = None
        if self._audience_sync_scheduler is not None:
            await self._audience_sync_scheduler.stop()
            self._audience_sync_scheduler = None
        if self._interaction_backfill_scheduler is not None:
            await self._interaction_backfill_scheduler.stop()
            self._interaction_backfill_scheduler = None
        if self._draft_clean_scheduler is not None:
            await self._draft_clean_scheduler.stop()
            self._draft_clean_scheduler = None
        if self._retention_scheduler is not None:
            await self._retention_scheduler.stop()
            self._retention_scheduler = None
        if self._note_metrics_scheduler is not None:
            await self._note_metrics_scheduler.stop()
            self._note_metrics_scheduler = None
        if self._archive_reaper is not None:
            await self._archive_reaper.stop()
            self._archive_reaper = None
        if self._placeholder_reaper is not None:
            await self._placeholder_reaper.stop()
            self._placeholder_reaper = None
        if self._browser_reaper is not None:
            await self._browser_reaper.stop()
            self._browser_reaper = None
        if self._cookie_checker is not None:
            await self._cookie_checker.stop()
            self._cookie_checker = None

    async def _terminate_children(self) -> None:
        """停机收尾:给存活子进程 child_grace 秒收尾机会,超时 SIGKILL 进程组并回收。"""
        procs = dict(self._procs)
        if not procs:
            return
        waiters = [asyncio.ensure_future(p.wait()) for p in procs.values()]
        _done, pending = await asyncio.wait(waiters, timeout=self._child_grace)
        for t in pending:
            t.cancel()
        for account_id, proc in procs.items():
            if proc.returncode is None:
                logger.warning(
                    "停机:账号 {} 子进程未在 {}s 内退出,SIGKILL 进程组",
                    account_id,
                    self._child_grace,
                )
                self._kill_process_group(proc)
        # SIGKILL 后 wait 立即返回;统一回收避免僵尸
        await asyncio.gather(*(p.wait() for p in procs.values()), return_exceptions=True)

    async def _cancel_tasks(self) -> None:
        """取消并回收全部内部后台 task(子进程已终止,回收 task 会自然结束)。"""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _track(self, coro) -> asyncio.Task:
        """登记内部后台 task(完成即自动出表)。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------- 扫描与派发 ----------------

    async def scan_once(self) -> None:
        """单轮扫描:僵死恢复 → 取可派发工作 → op_images 进程内执行 → 账号公平派发。"""
        # a. browser_jobs 僵死恢复(repo 缺席 = P1 未集成,跳过)
        if self._repo is not None:
            try:
                await self._repo.recover_stale()
            except Exception:
                logger.exception("browser_jobs 僵死恢复失败(忽略,下轮重试)")
        # a2. publish_jobs 僵死恢复(评审确认的 critical:account_worker 被 SIGKILL/
        #     断电后 publishing 永久悬挂——finally 兜底进程级击杀下不执行,必须由
        #     supervisor 巡回归位)。语义移植自 PublishScheduler.recover_stale:
        #     超 PUBLISH_JOB_TIMEOUT 的 publishing 复位 pending;排除仍有存活子进程的
        #     账号(在途真发布,墙钟超时≠僵死,复位会二次发布)。
        try:
            await self._recover_stale_publish()
        except Exception:
            logger.exception("publish_jobs 僵死恢复失败(忽略,下轮重试)")
        # b. 可派发工作:publish_jobs(SQL 直查到期 pending)+ browser_jobs(queued 全量)
        publish_rows = await self._due_publish_jobs()
        self._warn_publish_backlog(publish_rows)
        browser_rows: list[dict] = []
        if self._repo is not None:
            try:
                browser_rows = await self._repo.list_dispatchable()
            except Exception:
                logger.exception("browser_jobs 扫描失败(忽略,下轮重试)")
        # e. op_images:无账号、API 调用型,不派子进程,supervisor 进程内直接执行
        account_rows: list[dict] = []
        for row in browser_rows:
            if row.get("kind") == "op_images":
                self._launch_op_images(row)
            else:
                account_rows.append(row)
        # c+d. 账号公平轮转派发子进程
        await self._dispatch_accounts(publish_rows, account_rows)

    async def _recover_stale_publish(self) -> int:
        """publishing 且 started_at 超 PUBLISH_JOB_TIMEOUT 的僵死发布复位 pending。

        排除 self._procs 中仍有存活子进程的账号:该号 account_worker 可能仍在真发布,
        复位会触发重派 + 二次发布(重复笔记)。子进程消亡后下一轮即可命中复位。
        返回复位条数。
        """
        cutoff = datetime.utcnow() - timedelta(seconds=settings.PUBLISH_JOB_TIMEOUT)
        live_accounts = [
            acc for acc, proc in self._procs.items() if proc.returncode is None
        ]
        async with self._session_factory() as session:
            stmt = (
                update(PublishJob)
                .where(PublishJob.status == "publishing")
                .where(PublishJob.started_at.is_not(None))
                .where(PublishJob.started_at <= cutoff)
            )
            if live_accounts:
                stmt = stmt.where(PublishJob.account_id.not_in(live_accounts))
            stmt = stmt.values(status="pending", started_at=None)
            res = await session.execute(stmt)
            await session.commit()
        if res.rowcount:
            logger.warning(f"[supervisor] 复位僵死 publishing 发布任务 {res.rowcount} 条")
        return res.rowcount

    def _warn_publish_backlog(self, rows: list) -> None:
        """**已到点**却积压的发布任务超阈值就告警;绝不据此改任何任务状态。

        为什么只告警不动手(2026-08-03 运营误判事故):一批排到 08-08 的定时稿被当成
        "卡了 4 天的僵尸任务",提出"pending 超 30 分钟自动置 failed" —— 那会把定时发布
        整个功能杀死(每篇定时稿都在创建 30 分钟后、远早于发布时间被判失败)。

        所以判据只看**已到点的**(``_due_publish_jobs`` 已按 schedule_time 过滤掉未到点
        的),未到点的定时稿一条都不会进这里。真僵死另有 ``_recover_stale_publish`` 负责
        (那管的是 ``publishing`` 悬挂,有明确的超时语义)。

        告警不落库、不改状态 —— 它是给人看的信号,不是自动处置的依据。
        """
        if len(rows) < PUBLISH_BACKLOG_ALERT:
            return
        by_account: dict[int, int] = {}
        for row in rows:
            by_account[row[1]] = by_account.get(row[1], 0) + 1
        logger.warning(
            f"[supervisor] 已到点的发布任务积压 {len(rows)} 条(阈值 {PUBLISH_BACKLOG_ALERT}),"
            f"按账号: {by_account} —— 未到点的定时稿不计入,这里积压说明派发或执行侧有问题"
        )

    async def _due_publish_jobs(self) -> list[tuple[int, int, datetime | None, int | None]]:
        """SQL 直查到期 pending 发布任务:(id, account_id, created_at, created_by),按 id 升序。

        到期语义与 PublishScheduler.scan_once 一致:schedule_time 与 next_retry_at
        均为空或已到。冷却/日上限判定在 account_worker 侧执行(见 CLI 契约),此处不做门。
        """
        now = datetime.utcnow()
        async with self._session_factory() as session:
            stmt = (
                select(
                    PublishJob.id,
                    PublishJob.account_id,
                    PublishJob.created_at,
                    PublishJob.created_by,
                )
                .where(PublishJob.status == "pending")
                .where(publish_due_filter(now))
                .order_by(PublishJob.id)
            )
            result = await session.execute(stmt)
            return [tuple(row) for row in result.all()]

    # created_at 归一(排序口径的一部分)在 queue_status 里,读侧排位次时按同一口径
    _norm_created = staticmethod(norm_created)

    def _fair_order(self, accounts: list[int]) -> list[int]:
        """公平轮转序:新账号按账号号稳定补入轮转表尾,返回表序过滤出的有工作账号。"""
        for acc in sorted(accounts):
            if acc not in self._rotation:
                self._rotation.append(acc)
        present = set(accounts)
        return [acc for acc in self._rotation if acc in present]

    async def _dispatch_accounts(
        self, publish_rows: list[tuple], account_rows: list[dict]
    ) -> None:
        """按账号分组 + 轮转排序,每账号一批(oldest first,封顶 batch)派 1 个子进程。

        - 同账号已有存活子进程 → 跳过(同账号严格串行);
        - 全局子进程数达 ``max_procs`` → 本轮停派(排队等下轮);
        - 同号一小时会话总闸(``_apply_session_cap``)可能滤掉本批任务(系统层严、运营层宽);
        - 派过的账号移到轮转表尾:单账号灌大批量不饿死其他账号。
        """
        work: dict[int, list[tuple[datetime, str, object, int]]] = {}
        for job_id, account_id, created, created_by in publish_rows:
            work.setdefault(account_id, []).append(
                queue_sort_key(created, SOURCE_PUBLISH, job_id, created_by or 0)
            )
        for row in account_rows:
            acc = row.get("account_id")
            if acc is None:
                # 契约上仅 op_images 允许无账号(已在 scan_once 分流),防御性跳过
                continue
            work.setdefault(acc, []).append(
                queue_sort_key(
                    row.get("created_at"),
                    SOURCE_BROWSER,
                    row["id"],
                    row.get("operator_id") or 0,
                )
            )
        if not work:
            return

        recent = await self._recent_session_counts(list(work.keys()))
        dispatched: list[int] = []
        for acc in self._fair_order(list(work.keys())):
            if acc in self._procs:
                continue  # 同账号严格串行:已有存活子进程,本轮不再派
            if len(self._procs) >= self._max_procs:
                break  # 全局子进程封顶,余下账号排队等下轮
            items = self._apply_session_cap(
                acc, sorted(work[acc])[: self._batch], recent.get(acc, 0)
            )
            if not items:
                continue  # 全批被会话总闸拦下:任务留队列,下轮按新窗口重估
            publish_ids = sorted(jid for _c, src, jid, _op in items if src == SOURCE_PUBLISH)
            browser_ids = [jid for _c, src, jid, _op in items if src == SOURCE_BROWSER]
            await self._spawn_account_worker(acc, publish_ids, browser_ids)
            dispatched.append(acc)
        # 本轮派过的账号移到轮转表尾(公平:下轮优先照顾没派到的账号)
        for acc in dispatched:
            self._rotation.remove(acc)
            self._rotation.append(acc)

    # ---------------- 同号会话频次总闸 ----------------

    async def _recent_session_counts(self, account_ids: list[int]) -> dict[int, int]:
        """数各账号近 ``SESSION_WINDOW_SECONDS`` 内的浏览器会话数(含在飞),按号返回。

        一次会话 = 起一次 camoufox。计数**不分触发方**(系统的、运营的都算),因为风控
        看的是号本身的行为频次,不管是谁点的。

        两个来源缺一不可:

        - ``browser_jobs``:终态行(done/error)按 ``updated_at`` 落窗口 + 全部 running 行
          (在飞会话不看时间——它正占着一次);``queued`` 不算,还没起浏览器,数进去会自锁;
        - ``publish_jobs``:发布链在 account_worker 里直接调 ``sync_client.publish_once``,
          **不在 browser_jobs 留痕**,不单独数就会漏掉最重的那类会话。已发布行按
          ``started_at``(会话开始时刻)落窗口,``publishing`` 行按在飞计入。

        已知欠数:发布失败/排重试的行会把 ``started_at`` 清空(见 account_worker
        ``_apply_publish_decision``),那次真实发生过的会话事后无从计时,只能漏数。宁可
        少数不多数——多数会误伤正常派发,少数只是闸略松,业务侧自有节流兜底。

        **"算不算一次会话"的行判据不写在这里**,取自 queue_status 的两个 filter——轮询
        端点的 ``queue.detail.used`` 数的是同一批行,共用 filter 才不会两边各说一个数。
        这里只负责投影(聚合成计数),读侧另按同一 filter 取时刻算 window_resets_at。
        """
        if not account_ids:
            return {}
        cutoff = session_window_cutoff(datetime.utcnow())
        counts: dict[int, int] = {}
        async with self._session_factory() as session:
            browser_rows = await session.execute(
                select(BrowserJob.account_id, func.count())
                .where(BrowserJob.account_id.in_(account_ids))
                .where(browser_session_filter(cutoff))
                .group_by(BrowserJob.account_id)
            )
            for acc, n in browser_rows.all():
                counts[acc] = counts.get(acc, 0) + int(n or 0)
            publish_rows = await session.execute(
                select(PublishJob.account_id, func.count())
                .where(PublishJob.account_id.in_(account_ids))
                .where(publish_session_filter(cutoff))
                .group_by(PublishJob.account_id)
            )
            for acc, n in publish_rows.all():
                counts[acc] = counts.get(acc, 0) + int(n or 0)
        return counts

    def _apply_session_cap(
        self, account_id: int, items: list[tuple], recent: int
    ) -> list[tuple]:
        """按剩余会话额度过滤本批任务,返回可派的部分(可能为空)。

        风控红线:同号一小时 ≤4-5 次浏览器会话(2026-08-07 实测 5 次就把号弹上验证墙)。
        各业务模块只守自己的闸,谁也看不见别人——只有派发层看得见全部 kind,闸开在这。

        闸分**双层**,两层各有自己的帽值,共用同一份会话计数(计数不分触发方):

        - **系统自发任务**(operator_id 非正)按 ``session_cap`` 的剩余额度放行,超了就
          留在队列里,下轮扫描按滚动窗口重新估(不改状态、不失败、不排期,一小时后自然
          轮到);
        - **运营触发任务**(operator_id>0)按更宽的 ``operator_session_cap`` 放行——人工
          意图仍然优先,但不再无限直通;它照样吃掉一格系统额度,后面的系统任务据此收紧。
        - 帽值 ≤0 = 关掉对应那一层(运维逃生口,与本仓其它 "0=关闭" 的开关同款语义)。

        运营那层是补的漏:原先运营任务一律放行,假设"运营侧已有配额闸"。2026-08-07 被
        证伪——skill 拿运营 apikey 跑批量逐篇组件回读,一小时 192 条 note_components_read
        全豁免直通,单号最高 51 次会话/时,是红线的 10 倍。运营配额闸
        (``OPERATOR_PENDING_QUOTA``)限的是**并发未终态数**,不限速率,拦不住这种打法。

        分层与放行判据取自 queue_status(``layer_of`` / ``cap_allows``):轮询端点的
        ``queue.blocked_by`` 用的是同一个判据,运营看到的 "session_cap" 与这里拦不拦
        永远同进同出。
        """
        kept: list[tuple] = []
        budget = self._session_cap - recent  # 系统层剩余额度
        op_budget = self._operator_session_cap - recent  # 运营层剩余额度
        blocked = 0
        op_blocked = 0
        for item in items:
            layer = layer_of(item[3])
            if cap_allows(
                layer,
                budget=budget,
                op_budget=op_budget,
                session_cap=self._session_cap,
                operator_session_cap=self._operator_session_cap,
            ):
                kept.append(item)
                budget -= 1
                op_budget -= 1
            elif layer == LAYER_OPERATOR:
                op_blocked += 1
            else:
                blocked += 1
        # 去重:同号连续被闸只吵一次,恢复派发后再超线才会再吵。两层各记各的
        if blocked:
            if account_id not in self._governed_accounts:
                self._governed_accounts.add(account_id)
                logger.warning(
                    f"[supervisor] 会话总闸:账号 {account_id} 近一小时已 {recent} 次浏览器会话"
                    f"(帽值 {self._session_cap}),本轮 {blocked} 个系统任务延后"
                )
        else:
            self._governed_accounts.discard(account_id)
        if op_blocked:
            if account_id not in self._operator_governed_accounts:
                self._operator_governed_accounts.add(account_id)
                logger.warning(
                    f"[supervisor] 会话总闸:运营任务也已延后:账号 {account_id} 近一小时 "
                    f"{recent} 次会话,超运营帽值 {self._operator_session_cap}"
                    f"——批量操作请改用批量端点或自行限速(本轮 {op_blocked} 个)"
                )
        else:
            self._operator_governed_accounts.discard(account_id)
        return kept

    @staticmethod
    def _spawn_timeout_for(base_timeout: float, video_sizes: list) -> float:
        """本次 spawn 的进程硬超时:基准 + 载荷里**最大**那个视频该给的时间。

        为什么必须伸缩(用户会传 15-30 分钟的 GB 级视频):基准 1800s 是给普通浏览器任务
        定的,一条 GB 级视频光是上传到小红书 + 平台转码就可能到 10-30 分钟级,发布会话
        必然被 supervisor 在半路 SIGKILL,而且杀在**已经传完、正在发布**的时刻最亏。

        - 没有视频载荷 → 原样返回基准,**普通任务行为逐字节不变**(这是硬要求);
        - 有视频 → 基准 + ``media_timeout_s``(与 step3v 共用同一公式,不另造);
        - 一批多条时按**最大**那个算 —— 按最小算等于给大的那条判死刑。
        """
        sizes = [s for s in (video_sizes or []) if s]
        if not sizes:
            return base_timeout
        from app.publish.policy import media_timeout_s

        return base_timeout + media_timeout_s(
            max(sizes),
            base_s=settings.VIDEO_UPLOAD_TIMEOUT_BASE_S,
            per_100mb_s=settings.VIDEO_UPLOAD_TIMEOUT_PER_100MB_S,
            cap_s=settings.VIDEO_UPLOAD_TIMEOUT_CAP_S,
        )

    async def _spawn_account_worker(
        self, account_id: int, publish_ids: list, browser_ids: list
    ) -> None:
        """派生账号子进程(独立进程组,cwd=仓库根),登记并挂回收 task。"""
        cmd = [
            str(_REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "app.account_worker",
            "--account-id",
            str(account_id),
            "--db",
            _sqlite_db_path(),
        ]
        if publish_ids:
            cmd += ["--publish-job-ids", ",".join(str(i) for i in publish_ids)]
        if browser_ids:
            cmd += ["--browser-job-ids", ",".join(str(i) for i in browser_ids)]
        try:
            # start_new_session=True:子进程独立进程组,硬超时/停机可 SIGKILL 整组
            # (含其派生的 camoufox),不误伤 supervisor 自身。
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(_REPO_ROOT), start_new_session=True
            )
        except Exception:
            logger.exception("账号 {} 子进程派生失败(下轮重试)", account_id)
            return
        self._procs[account_id] = proc
        # 本次载荷带大视频就给这一个子进程加时(普通任务恒等于基准,见 _spawn_timeout_for)
        timeout = self._spawn_timeout_for(
            self._proc_timeout, self._publish_video_sizes(publish_ids)
        )
        self._track(self._reap_child(account_id, proc, timeout))
        logger.info(
            "已派生账号 {} 子进程 pid={}(publish={}, browser={}, 硬超时={}s)",
            account_id,
            proc.pid,
            publish_ids,
            browser_ids,
            int(timeout),
        )

    @staticmethod
    def _publish_video_sizes(publish_ids: list) -> list:
        """读这批 publish job 各自**大媒体**文件的字节数(没有媒体/读不到的位置为 None)。

        媒体 = 视频(``video_path``)或播客音频(``audio_path``)—— 两者互斥,同一行
        最多只有一个非空,故按行取"哪个有取哪个"。1GB 音频与 GB 级视频在上传耗时上
        同量级,凭什么给视频加时不给音频加,就没有理由。

        直接查库文件而不是把大小塞进队列:队列那层与媒体无关(调研结论),不为这一个
        用途污染它。读失败一律 None → 伸缩公式退回基准,最坏退化成改动前的行为。
        (函数名沿用 ``_publish_video_sizes`` 不改:worker 的既有回归测试按这个名字锁着,
        为字面准确改名要连带动测试,收益仅美观。)
        """
        if not publish_ids:
            return []
        import sqlite3

        from app.publish.policy import media_file_size

        try:
            conn = sqlite3.connect(_sqlite_db_path())
            try:
                rows = conn.execute(
                    "SELECT video_path, audio_path FROM publish_jobs WHERE id IN (%s)"
                    % ",".join("?" * len(publish_ids)),
                    [int(i) for i in publish_ids],
                ).fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — 取大小是优化,失败就退回基准超时
            logger.warning("读 publish job 媒体体积失败(退回基准硬超时)")
            return []
        return [media_file_size(r[0] or r[1]) for r in rows]

    async def _reap_child(self, account_id: int, proc, timeout: float | None = None) -> None:
        """等子进程退出并出表;超硬超时 SIGKILL 进程组防僵死占坑。

        ``timeout`` 由 spawn 按本次载荷算好传进来(带大视频的会更长);不传退回基准。
        """
        proc_timeout = self._proc_timeout if timeout is None else timeout
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=proc_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "账号 {} 子进程超硬超时 {}s,SIGKILL 进程组",
                    account_id,
                    proc_timeout,
                )
                self._kill_process_group(proc)
                await proc.wait()
        finally:
            if self._procs.get(account_id) is proc:
                self._procs.pop(account_id, None)

    @staticmethod
    def _kill_process_group(proc) -> None:
        """SIGKILL 子进程整个进程组;进程组已消亡则回退杀单进程,均容错。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

    # ---------------- op_images 进程内执行 ----------------

    def _launch_op_images(self, row: dict) -> None:
        """把 queued 的 op_images 行交给进程内异步执行(去重:同 id 在途不重复起)。"""
        job_id = row.get("id")
        if not job_id or job_id in self._op_inflight:
            return
        self._op_inflight.add(job_id)
        self._track(self._run_op_images(job_id))

    async def _run_op_images(self, job_id: str) -> None:
        """乐观认领(claim_job_sync 经 to_thread)→ execute → 写回终态。

        契约无 async claim,sync 侧 sqlite3 直连经 to_thread 使用;认领失败(已被领走/
        非 queued)静默退。结果含 "error" 键 → status=error,否则 done;execute 抛异常
        兜底转 error,绝不留 running 悬挂(finish 自身失败则交僵死恢复按 unknown 处置)。
        """
        db_path = _sqlite_db_path()
        try:
            claimed = await asyncio.to_thread(
                self._repo.claim_job_sync, db_path, job_id, self._worker_tag
            )
            if claimed is None:
                return
            payload = claimed.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload or "{}")
            hb = self._track(self._repo.heartbeat_loop(job_id))  # 执行期心跳防误判僵死
            try:
                try:
                    result = await op_images_service.execute(payload)
                    if not isinstance(result, dict):
                        result = {"error": f"op_images 返回非 dict:{type(result).__name__}"}
                except Exception as exc:
                    logger.exception("op_images job {} 执行异常", job_id)
                    result = {"error": str(exc)}
                status = "error" if result.get("error") else "done"
                await asyncio.to_thread(
                    self._repo.finish_job_sync, db_path, job_id, status, result,
                    self._worker_tag,
                )
            finally:
                hb.cancel()
        except Exception:
            logger.exception("op_images job {} 台账收尾异常(交僵死恢复处置)", job_id)
        finally:
            self._op_inflight.discard(job_id)


async def main() -> None:
    """worker 进程入口:建表 → 起 Supervisor(含视频调度 + 即梦片段调度)→ 信号驱动优雅停机。"""
    await db_module.init_db()
    supervisor = Supervisor(
        db_module.async_session, include_video=True, include_dreamina=True
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, supervisor.request_stop)
        except NotImplementedError:  # pragma: no cover - 个别平台不支持
            pass
    logger.info(
        "worker supervisor 启动(scan_interval={}s, max_procs={})",
        supervisor._scan_interval,
        supervisor._max_procs,
    )
    await supervisor.run()
    logger.info("worker supervisor 已退出")


if __name__ == "__main__":
    asyncio.run(main())
