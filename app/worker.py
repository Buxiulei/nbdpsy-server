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
from sqlalchemy import update, or_, select

import app.core.db as db_module
from app.browser.browser_reaper import BrowserReaper
from app.browser.cookie_checker import CookieChecker
from app.core.config import settings
from app.models.publish_job import PublishJob
from app.services import op_images as op_images_service
from app.services.content_archive import ArchiveReaper
from app.services.draft_clean import DraftCleanScheduler
from app.services.note_metrics_scheduler import NoteMetricsScheduler
from app.services.placeholder_reaper import PlaceholderReaper

# browser_jobs 台账 repo(P1 产物):集成前分支上可能尚不存在,容缺导入 —— repo 为 None 时
# Supervisor 跳过 browser_jobs 相关扫描(仅调度 publish_jobs),集成后自动生效;
# 测试注入假 repo 按契约签名(recover_stale/list_dispatchable/claim_job_sync/finish_job_sync)验证。
try:
    from app.services import browser_jobs_repo as _default_browser_jobs_repo
except ImportError:  # pragma: no cover - 集成前的过渡态
    _default_browser_jobs_repo = None

# 仓库根:account_worker 子进程的 cwd 与 .venv python 解释器路径基准。
_REPO_ROOT = Path(__file__).resolve().parents[1]


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
    - ``scan_interval`` / ``batch_per_account`` / ``proc_timeout`` / ``child_grace`` /
      ``max_procs``:分别兜底 ``WORKER_SCAN_INTERVAL``(5s)/ ``WORKER_BATCH_PER_ACCOUNT``
      (3)/ ``ACCOUNT_PROC_TIMEOUT``(1800s)/ 停机宽限(10s)/ ``BROWSER_CONCURRENCY``。
    """

    def __init__(
        self,
        session_factory,
        *,
        repo=None,
        include_video: bool = False,
        scan_interval: float | None = None,
        batch_per_account: int | None = None,
        proc_timeout: float | None = None,
        child_grace: float | None = None,
        max_procs: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repo = repo if repo is not None else _default_browser_jobs_repo
        self._include_video = include_video
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
        self._max_procs = max_procs if max_procs is not None else settings.BROWSER_CONCURRENCY
        self._worker_tag = f"supervisor-{os.getpid()}"
        # account_id → 存活子进程(派生登记,子进程退出即回收移除)
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        # 账号公平轮转表:本轮派过的账号移到队尾,单账号大批量不饿死其他账号
        self._rotation: list[int] = []
        # 进程内在途 op_images job id(防同一 job 重复起执行任务;真正互斥靠乐观认领)
        self._op_inflight: set[str] = set()
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
        self._video_scheduler = None

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
        if settings.DRAFT_CLEAN_INTERVAL > 0:
            self._draft_clean_scheduler = DraftCleanScheduler(
                self._session_factory, settings.DRAFT_CLEAN_INTERVAL
            )
            self._draft_clean_scheduler.start()
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

    async def _stop_components(self) -> None:
        """停后台组件(与启动相反顺序)。"""
        if self._video_scheduler is not None:
            await self._video_scheduler.stop()
            self._video_scheduler = None
        if self._draft_clean_scheduler is not None:
            await self._draft_clean_scheduler.stop()
            self._draft_clean_scheduler = None
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

    async def _due_publish_jobs(self) -> list[tuple[int, int, datetime | None]]:
        """SQL 直查到期 pending 发布任务:(id, account_id, created_at),按 id 升序。

        到期语义与 PublishScheduler.scan_once 一致:schedule_time 与 next_retry_at
        均为空或已到。冷却/日上限判定在 account_worker 侧执行(见 CLI 契约),此处不做门。
        """
        now = datetime.utcnow()
        async with self._session_factory() as session:
            stmt = (
                select(PublishJob.id, PublishJob.account_id, PublishJob.created_at)
                .where(PublishJob.status == "pending")
                .where(
                    or_(
                        PublishJob.schedule_time.is_(None),
                        PublishJob.schedule_time <= now,
                    )
                )
                .where(
                    or_(
                        PublishJob.next_retry_at.is_(None),
                        PublishJob.next_retry_at <= now,
                    )
                )
                .order_by(PublishJob.id)
            )
            result = await session.execute(stmt)
            return [tuple(row) for row in result.all()]

    @staticmethod
    def _norm_created(value) -> datetime:
        """把 created_at 归一成可排序 datetime(repo 侧可能给 ISO 字符串;缺失当最旧)。"""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return datetime.min

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
        - 派过的账号移到轮转表尾:单账号灌大批量不饿死其他账号。
        """
        work: dict[int, list[tuple[datetime, str, object]]] = {}
        for job_id, account_id, created in publish_rows:
            work.setdefault(account_id, []).append(
                (self._norm_created(created), "publish", job_id)
            )
        for row in account_rows:
            acc = row.get("account_id")
            if acc is None:
                # 契约上仅 op_images 允许无账号(已在 scan_once 分流),防御性跳过
                continue
            work.setdefault(acc, []).append(
                (self._norm_created(row.get("created_at")), "browser", row["id"])
            )
        if not work:
            return

        dispatched: list[int] = []
        for acc in self._fair_order(list(work.keys())):
            if acc in self._procs:
                continue  # 同账号严格串行:已有存活子进程,本轮不再派
            if len(self._procs) >= self._max_procs:
                break  # 全局子进程封顶,余下账号排队等下轮
            items = sorted(work[acc])[: self._batch]
            publish_ids = sorted(jid for _c, kind, jid in items if kind == "publish")
            browser_ids = [jid for _c, kind, jid in items if kind == "browser"]
            await self._spawn_account_worker(acc, publish_ids, browser_ids)
            dispatched.append(acc)
        # 本轮派过的账号移到轮转表尾(公平:下轮优先照顾没派到的账号)
        for acc in dispatched:
            self._rotation.remove(acc)
            self._rotation.append(acc)

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
        self._track(self._reap_child(account_id, proc))
        logger.info(
            "已派生账号 {} 子进程 pid={}(publish={}, browser={})",
            account_id,
            proc.pid,
            publish_ids,
            browser_ids,
        )

    async def _reap_child(self, account_id: int, proc) -> None:
        """等子进程退出并出表;超硬超时(proc_timeout)SIGKILL 进程组防僵死占坑。"""
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._proc_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "账号 {} 子进程超硬超时 {}s,SIGKILL 进程组",
                    account_id,
                    self._proc_timeout,
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
    """worker 进程入口:建表 → 起 Supervisor(含视频调度)→ 信号驱动优雅停机。"""
    await db_module.init_db()
    supervisor = Supervisor(db_module.async_session, include_video=True)
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
