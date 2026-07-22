"""视频调度器（方案 C）：把运营工具 celery 版视频搬运/再制作调度语义 1:1 翻译成 asyncio。

源语义基准（逐字保真，只读参考）：
``小红书运营工具/backend/app/tasks/video_transport_tasks.py``（阶段编排 / _HeartbeatPump 300s
心跳泵 / recovery_scan 15min 僵死判定 + 重排前 touch + _MAX_RETRIES 判死 / STAGE_BUDGET_SECONDS
预算表 / _slim 落库）与 ``services/video_transport/job_store.py`` 全家（状态机函数族）。

celery→asyncio 的关键换面：
- 每阶段一个 celery 任务 + apply_async 尾部自链  →  单 worker 进程内 ``_run_stages`` 逐阶段
  自链（一个协程跑完整条链，仍每阶段独立会话，避免长 handler 占住事务）。
- beat 定时 recovery_scan  →  ``_run_loop`` 每 poll 周期先 ``recover_stale`` 再 ``scan_queued``
  （扩展宿主 ``app/publish/scheduler.py`` 的「循环内每轮先回收再扫表」范式）。
- celery 队列天然去重  →  ``mark_running`` 原子占用（``UPDATE ... WHERE status='queued'``，
  rowcount 判是否真占到）防「扫表 + 恢复」双重处理同一 job（源无此步，asyncio 单进程轮询需要）。
- _HeartbeatPump 守护线程 + 独立 SessionLocal  →  ``_heartbeat_pump`` 异步任务 + 独立会话，
  ``async with`` 包住 handler，无论成败 __aexit__ 都停泵（源 try/finally 语义）。

job_store 函数族一并落在本模块（设计 §2：job_store 语义并入 scheduler/models），全部改 async
并收调用方传入的 ``AsyncSession``（宿主 services 惯例）。stages JSON 更新沿用源
「读→deepcopy→改→整体赋值」，否则 SQLAlchemy 不感知 JSON 列 dirty。

时间统一 ``datetime.utcnow()``（naive UTC），与宿主 publish 调度器 / 模型 created_at 一致。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select, update

# 视频 job 模型由 Track M1（app/models/video_job.py）产出——M1/M2 并行且文件零交集，
# M2 独立开发期该模块尚不存在，故容错导入：缺失时置 None，测试在测试文件内建同构临时模型
# 并 monkeypatch 本模块的 ``VideoJob`` 全局（报告注明，合流以 M1 为准）。生产（合流后）
# 该 import 正常成功，job_store 函数族 / 调度器查询全部经模块全局 ``VideoJob`` 引用它。
try:  # pragma: no cover - 合流后恒成功
    from app.models.video_job import VideoJob
except ImportError:  # pragma: no cover - M2 独立开发期
    # 合流后删除本 try/except 容错，直接 `from app.models.video_job import VideoJob`。
    VideoJob = None


# ── 阶段序（与源 job_store 一致，逐字保真）───────────────────────────────
# transport（纯搬运）：download→...→mux→deliver。
STAGE_ORDER = ["download", "transcript", "resegment", "translate", "dub", "mux", "deliver"]
# remake（分镜级再制作）：analyze/rewrite/storyboard/render/compose 替换 mux，保留
# download/transcript/resegment/translate/dub/deliver 复用 transport 阶段体。
# wave5 弹性时间轴：dub 提到 storyboard 之前——先测每句自然时长（rate=1.0 合成），再由
# storyboard 阶段的 timeline.relayout 按语音优先重排时间轴，最后 compose 建配音轨。
REMAKE_STAGE_ORDER = ["download", "analyze", "transcript", "resegment", "translate",
                      "rewrite", "dub", "storyboard", "render", "compose", "deliver"]

# 阶段预算表（monotonic deadline 透传 handler）——源照搬，含各波实测调整后的值。
STAGE_BUDGET_SECONDS = {
    "download": 1800, "transcript": 1800, "resegment": 1200,
    "translate": 3600, "dub": 3600, "mux": 1800, "deliver": 300,
    # analyze：VL 内容细分 + 密集抽帧真实耗时实测 42min（生产 job12），故 3600。
    "analyze": 3600, "rewrite": 1800, "storyboard": 600,
    # wave5 120fps 编码变慢：render 5400、compose 3600。
    "render": 5400, "compose": 3600,
}

# 僵死判定阈值（分钟）：status=running 且心跳超此时长 → recovery 判僵死续跑。
_STALE_MINUTES = 15
# 单个 job 恢复重排上限：超此次数直接判死，避免坏 job 无限重排。
_MAX_RETRIES = 2
# 阶段内心跳泵间隔（秒）：handler 执行期间每这么多秒 touch 一次心跳，让长阶段（analyze
# 实测 42min）阶段内持续刷心跳，不被 recovery 按 15min 无心跳误判僵死。
_HEARTBEAT_PUMP_SECONDS = 300

# ── 阶段 handler 注册表 ────────────────────────────────────────────────
# 签名 ``async (job, session, ctx) -> stats dict``，ctx={"deadline": monotonic秒}。
# 本 track（M2 调度骨架）留空；Track M3（pipeline 平移）就地填充：
#   ``from app.video.scheduler import STAGE_HANDLERS; STAGE_HANDLERS.update({...})``
# 必须原地 mutate（勿 rebind），保持调度器与本模块引用同一 dict 对象，运行时可见。
#
# 【硬契约·M3 平移红线：handler 全程不得阻塞事件循环】
# 本调度器是单进程 asyncio worker——心跳泵、主循环（recover/scan）、并发的其它 job 全部跑在
# **同一个事件循环**上。任何 handler 里的同步阻塞调用都会冻住整个循环：心跳泵停刷 →
# recover_stale 误判本 job 僵死、其它 job 排不上、主循环扫不了表。故 M3 平移 ffmpeg/pydub/
# dashscope-SDK 等同步代码时：
#   - CPU 密集 / 同步阻塞 I/O（ffmpeg 子进程、pydub 解码、requests、文件大读写）：
#     必须 ``await asyncio.to_thread(sync_fn, ...)`` 下沉到线程；
#   - 外部子进程（ffmpeg/ffprobe）：必须 ``await asyncio.create_subprocess_exec(...)``（异步子进程），
#     不得用 ``subprocess.run`` / ``os.system``。
# 范式出处：宿主 ``app/publish/scheduler.py`` —— ``sync_client.publish_once`` 与
# ``materialize_images`` 均经 ``asyncio.to_thread`` 下沉，不阻塞发布调度循环。
#
# 【M3 填充后必须补回全量注册 assert（源 tasks.py:413）】
#   assert set(STAGE_HANDLERS) == set(STAGE_ORDER) | set(REMAKE_STAGE_ORDER), \
#       "STAGE_HANDLERS 与两种 mode 的阶段集合不一致"
# 覆盖两种 mode 全部阶段，漏一个自链会在该阶段 KeyError → fail_job 断链。
STAGE_HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {}


# ── stages JSON / 落库小工具 ────────────────────────────────────────────
def _slim(stats: dict) -> dict:
    """stats 只留可 JSON 化的小字段（路径/计数/来源），列表型大数据必须已由 handler 落盘。

    源铁律照搬：阶段间大数据（segments/translated 等）落盘 raw/*.json，stages_json 里只存
    路径 + 计数，避免整表 JSON 膨胀。"""
    return {k: v for k, v in stats.items()
            if isinstance(v, (str, int, float, bool, type(None)))
            or (isinstance(v, dict) and len(str(v)) < 2000)}


# ── 阶段序推导（纯函数，无会话）─────────────────────────────────────────
def stage_order(job) -> list[str]:
    """按 job.mode 取阶段顺序（mode 列缺省/为空按 transport 兜底）。"""
    return REMAKE_STAGE_ORDER if getattr(job, "mode", None) == "remake" else STAGE_ORDER


def next_stage(job, stage: str) -> str | None:
    """自链用：返回 stage 的下一阶段；已是末阶段返回 None（→ finish_job）。"""
    order = stage_order(job)
    idx = order.index(stage)
    return order[idx + 1] if idx + 1 < len(order) else None


def first_incomplete_stage(job) -> str:
    """恢复用：返回首个非 done 阶段（崩溃处续跑锚点）；全 done 时返回末阶段。"""
    stages = job.stages or {}
    order = stage_order(job)
    for name in order:
        if (stages.get(name) or {}).get("status") != "done":
            return name
    return order[-1]


# ── async job_store 函数族（收 AsyncSession）─────────────────────────────
async def create_job(session, *, url: str, options: dict, user_id: int | None,
                     mode: str = "transport"):
    """建一条视频 job（初始 status=queued，worker 轮询取）。"""
    job = VideoJob(url=url, options=options or {}, created_by=user_id, mode=mode)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def create_revision_job(session, parent, *, instructions: str, edit_plan: list):
    """派生 revision job（成片修订，源 create_revision_job 语义平移）。

    新行 mode=remake、parent_job_id 指向被修订父 job；options 深拷贝父配置（voice/burn 等
    pipeline 参数，revision 重跑才与父同参）并加 revision 块（原始意见 instructions + 解析出
    的编辑清单 edit_plan）。term_sheet 继承父 job——translate 阶段被跳过（继承），不继承则
    deliver 术语表为空。修订链不限层数——parent 传上一层 job 即可（revision 的 revision 允许）。"""
    options = deepcopy(parent.options or {})
    options["revision"] = {"instructions": instructions, "edit_plan": edit_plan}
    job = VideoJob(
        url=parent.url,
        video_id=parent.video_id,
        title=parent.title,
        duration_seconds=parent.duration_seconds,
        mode="remake",
        options=options,
        parent_job_id=parent.id,
        created_by=parent.created_by,
        term_sheet=deepcopy(parent.term_sheet) if parent.term_sheet else parent.term_sheet,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def mark_stages_inherited(session, job, stages: list[str], parent_id: int) -> None:
    """把继承自父 job 的阶段逐个标 done，stats 记 inherited_from（B4 接线用）。

    revision job 不重跑 download/analyze/transcript/resegment/translate（最贵段），产物由
    inherit_artifacts 拷贝到位后调用本函数标记，管线从 rewrite 起跑（first_incomplete=rewrite）。"""
    for name in stages:
        await update_stage(session, job, name, status="done",
                           stats={"inherited_from": parent_id})


async def get_job(session, job_id: int):
    """按主键取 job；不存在返回 None。"""
    return await session.get(VideoJob, job_id)


async def update_stage(session, job, stage: str, *, status: str,
                       error: str | None = None, stats: dict | None = None) -> None:
    """更新单阶段状态到 stages_json（源 update_stage 语义平移）。

    stages JSON 走「读→deepcopy→改→整体赋值」，否则 SQLAlchemy 不感知 JSON 列 dirty
    （样板：源 op_llm_jobs._set_job）。status=running 首次记 started_at；done/error 记
    finished_at；error 同步冒泡到 job.error；job.status==queued 时顺带转 running。"""
    stages = deepcopy(job.stages or {})
    entry = stages.get(stage) or {}
    now = datetime.utcnow().isoformat()
    if status == "running" and not entry.get("started_at"):
        entry["started_at"] = now
    if status in ("done", "error"):
        entry["finished_at"] = now
    entry["status"] = status
    if error is not None:
        entry["error"] = error
    if stats is not None:
        entry["stats"] = stats
    stages[stage] = entry
    job.stages = stages
    job.stage = stage
    if status == "error":
        job.error = error
    if job.status == "queued":
        job.status = "running"
    job.updated_at = datetime.utcnow()
    await session.commit()


async def touch_heartbeat(session, job_id: int) -> None:
    """刷新 job 心跳时间戳（recovery 据此判僵死）。用 UPDATE 语句而非载对象，独立会话可安全并发调。"""
    await session.execute(
        update(VideoJob).where(VideoJob.id == job_id).values(heartbeat_at=datetime.utcnow())
    )
    await session.commit()


async def finish_job(session, job, products: dict) -> None:
    """全阶段完成：落 products、status=completed、清 error。"""
    job.products = products
    job.status = "completed"
    job.error = None
    job.updated_at = datetime.utcnow()
    await session.commit()


async def fail_job(session, job, error: str) -> None:
    """终态失败：status=failed、写 error（截断 2000）。"""
    job.status = "failed"
    job.error = (error or "")[:2000]
    job.updated_at = datetime.utcnow()
    await session.commit()


class VideoScheduler:
    """视频调度器：DB 状态机 + 原子占用 + 阶段自链 + 心跳泵 + 僵死恢复（方案 C 单 worker）。

    生命周期（``start``/``stop``，与宿主 publish 调度器同构）：起 concurrency 个内部队列 worker，
    后台协程每 poll 周期先 ``recover_stale``（僵死 running 回 queued）再 ``scan_queued`` → 投入
    内部队列；worker 取 job_id → ``_process``（原子占用 → ``_run_stages`` 逐阶段自链）。

    并发上限缺省 1（单机 CPU 编码，同视频链路 CPU 密集，排队语义与源一致，可调）。
    handler 注册表缺省引用模块级 ``STAGE_HANDLERS``（M3 填充）；测试注入 mock handler。
    """

    def __init__(
        self,
        session_factory,
        *,
        concurrency: int = 1,
        poll_interval: float = 5.0,
        heartbeat_interval: float = _HEARTBEAT_PUMP_SECONDS,
        handlers: dict | None = None,
    ) -> None:
        self._session_factory = session_factory
        # 至少 1 个 worker，防 concurrency 配 0 时队列永不被消费。
        self._concurrency = max(1, concurrency)
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        # handlers=None 时引用模块级 STAGE_HANDLERS（同一 dict 对象，M3 原地 mutate 可见）。
        self._handlers = handlers if handlers is not None else STAGE_HANDLERS
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        # 本进程当前真处理中的 job id（占用后登记、finally 撤销）。recover_stale 据此排除在途
        # job，防「阶段墙钟长于阈值误判僵死 → 复位 → 重投 → 双链竞态」。与阶段内心跳泵配合后
        # 误判概率趋零。进程重启后天然为空 → 所有 running 均可回收，崩溃恢复语义不变。
        self._in_flight: set[int] = set()

    # ── DB 扫描 / 原子占用 / 僵死恢复 ──────────────────────────────────
    async def scan_queued(self) -> list[int]:
        """选待处理 job id：status=queued，按 id 升序（新 job 与 recover 复位回 queued 的均在此）。"""
        async with self._session_factory() as session:
            stmt = (select(VideoJob.id).where(VideoJob.status == "queued")
                    .order_by(VideoJob.id))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def mark_running(self, job_id: int) -> bool:
        """原子占用：``UPDATE ... SET status='running',heartbeat_at=now WHERE id=? AND status='queued'``。

        返回是否真占到（rowcount==1）。同一 queued job 两处并发调用只一处返回 True，防「扫表 +
        恢复」双重处理。占用同时刷心跳，闭合「占到 → _in_flight 登记」之间的僵死误判窗口。"""
        now = datetime.utcnow()
        async with self._session_factory() as session:
            stmt = (
                update(VideoJob)
                .where(VideoJob.id == job_id)
                .where(VideoJob.status == "queued")
                .values(status="running", heartbeat_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount == 1

    async def recover_stale(self) -> int:
        """回收僵死 job：status=running 且心跳超 _STALE_MINUTES → 复位回 queued 让主循环重占续跑。

        返回复位条数。源语义逐条保真：
        - 只碰「心跳超时」的僵死 job（worker 崩溃 / OOM / 平滑重启），不碰显式 fail 的（已 failed 被过滤）；
        - 超 _MAX_RETRIES 直接 fail_job（判死），避免坏 job 无限重排；
        - 复位前先 ``touch_heartbeat``（重排前先 touch，把「再次误判窗口」推后一个 _STALE_MINUTES）；
        - retry_count 递增。

        与源差异（celery→asyncio）：源 recovery 直接 enqueue_stage 且保持 status=running；此处复位
        回 queued，由主循环 scan → mark_running 原子重占（统一「占用防双发」入口），避免两轮 recovery
        重复入队。排除本进程 _in_flight 在途 job（阶段墙钟长不等于僵死）。"""
        cutoff = datetime.utcnow() - timedelta(minutes=_STALE_MINUTES)
        recovered = 0
        async with self._session_factory() as session:
            stmt = (select(VideoJob).where(VideoJob.status == "running")
                    .where(VideoJob.heartbeat_at < cutoff))
            stale = list((await session.execute(stmt)).scalars().all())
            for job in stale:
                if job.id in self._in_flight:
                    continue  # 本进程真处理中，墙钟超时不算僵死
                if (job.retry_count or 0) >= _MAX_RETRIES:
                    await fail_job(session, job, "恢复扫描：超过最大重试次数")
                    continue
                job.retry_count = (job.retry_count or 0) + 1
                job.status = "queued"
                job.updated_at = datetime.utcnow()
                await session.commit()
                # 重排前立即刷心跳（防御加固）：即便本次判僵死有误（原链其实还活着），刷心跳也能
                # 避免下一轮扫描再抓同一 job 重复入队。
                await touch_heartbeat(session, job.id)
                recovered += 1
        return recovered

    # ── 阶段内心跳泵 ──────────────────────────────────────────────────
    @asynccontextmanager
    async def _heartbeat_pump(self, job_id: int):
        """阶段内心跳泵（async context manager）：handler 执行期间周期刷心跳，根治长阶段饿死。

        独立协程 + 独立会话每 interval 刷一次心跳；``async with`` 包住 handler，无论 handler
        成败 __aexit__ 都停泵（cancel 协程）。心跳泵任何异常绝不影响 handler 本身（吞并 log）。
        阶段开始已在 _run_stages 里 touch 一次，故首刷在一个 interval 之后（源语义）。"""
        stop = asyncio.Event()

        async def _pump() -> None:
            while not stop.is_set():
                try:
                    # 等 interval；期间被 stop 唤醒则退出（不刷），超时到点则刷一次。
                    await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_interval)
                    break  # stop 被 set → 停泵
                except asyncio.TimeoutError:
                    pass
                try:
                    async with self._session_factory() as session:
                        await touch_heartbeat(session, job_id)
                except Exception:
                    logger.exception("心跳泵 touch 失败 job_id={}", job_id)

        task = asyncio.create_task(_pump())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── 阶段执行（自链）──────────────────────────────────────────────
    async def _run_stages(self, job_id: int) -> None:
        """占用成功后：从首个未完成阶段起逐阶段自链执行，直至完成或失败。

        每阶段独立会话（源：每 celery 任务独立 SessionLocal），避免长 handler 占住事务；跨阶段
        数据不靠内存传递（handler 从上一阶段落盘 stats 重建输入）。任一阶段 handler 抛异常 →
        标该阶段 error + fail_job 终止整条链（源语义）。"""
        # 起始阶段：新 job=download；恢复 job=崩溃处首个未完成阶段。
        async with self._session_factory() as session:
            job = await get_job(session, job_id)
            if job is None or job.status in ("completed", "failed"):
                return
            stage: str | None = first_incomplete_stage(job)

        while stage is not None:
            async with self._session_factory() as session:
                job = await get_job(session, job_id)
                if job is None or job.status in ("completed", "failed"):
                    return
                await update_stage(session, job, stage, status="running")
                await touch_heartbeat(session, job_id)
                ctx = {"deadline": time.monotonic() + STAGE_BUDGET_SECONDS[stage]}
                try:
                    # 阶段内心跳泵：handler 期间周期刷心跳，长阶段不被 recover 误判僵死。
                    # __aexit__ 保证 handler 无论成败都停泵。
                    async with self._heartbeat_pump(job_id):
                        stats = await self._handlers[stage](job, session, ctx) or {}
                except Exception as exc:
                    await update_stage(session, job, stage, status="error",
                                       error=f"{type(exc).__name__}: {exc}")
                    await fail_job(session, job, f"{stage}: {exc}")
                    return
                products = stats.pop("products", None)
                await update_stage(session, job, stage, status="done", stats=_slim(stats))
                await touch_heartbeat(session, job_id)
                nxt = next_stage(job, stage)
                if nxt is None:
                    await finish_job(session, job, products or {})
                    return
            stage = nxt

    async def _process(self, job_id: int) -> None:
        """处理单 job：原子占用 → 登记在途 → 逐阶段自链 → finally 撤销在途。

        占不到（别处已占 / 非 queued）直接退，防双重处理。"""
        if not await self.mark_running(job_id):
            return
        # 占用成功后立刻登记在途——recover_stale 据此排除本 job（阶段墙钟长不等于僵死）。
        self._in_flight.add(job_id)
        try:
            await self._run_stages(job_id)
        finally:
            self._in_flight.discard(job_id)

    # ── 内部队列 / 生命周期 ──────────────────────────────────────────
    def submit(self, job_id: int) -> None:
        """把 job_id 立即投入内部队列（非阻塞）。与 scan 循环共用队列 + _process；mark_running 原子
        占用保证同一 job 只处理一次，重复 submit 安全。"""
        self._queue.put_nowait(job_id)

    async def enqueue(self, job_id: int) -> None:
        """把 job 投入调度管线（plan 冻结契约原语）：非终态 job 复位 status='queued' + 刷心跳，
        再 submit 入内部队列（免等下个 scan 周期即被取）。

        两处必需：
        1. **revision 子 job 接线（解死局）**：``create_revision_job`` + ``mark_stages_inherited``
           后，子 job 因 ``update_stage`` 的「queued→running」被翻成 status='running' 且
           heartbeat_at 仍为 NULL——此态 ``mark_running``（WHERE status='queued'）占不到、
           ``recover_stale``（WHERE heartbeat_at<cutoff，SQL 中 NULL 永不命中）也捞不到，成死局。
           enqueue 复位回 queued + touch 心跳，解锁 mark_running 原子占用，管线从 first_incomplete
           （=rewrite）续跑。
        2. **免定时立即发**：置 queued 后直接 submit（宿主 publish.submit 范式），M4 REST 建 job /
           派 revision 后调本原语立即触发，不等下个 poll 周期。

        终态 job（completed/failed）幂等保护：不复位、不入队。"""
        async with self._session_factory() as session:
            job = await get_job(session, job_id)
            if job is None or job.status in ("completed", "failed"):
                return
            job.status = "queued"
            job.updated_at = datetime.utcnow()
            await session.commit()
            await touch_heartbeat(session, job_id)
        self.submit(job_id)

    async def _worker(self) -> None:
        """队列 worker 主循环：阻塞取 job_id → _process；单个 job 异常只记录不退出。"""
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            except Exception:
                logger.exception("视频调度 worker 处理 job {} 异常", job_id)
            finally:
                self._queue.task_done()

    def start(self) -> None:
        """启动生命周期循环：起 concurrency 个队列 worker，后台协程每 poll 周期 recover→scan→submit。"""
        self._stop_event = asyncio.Event()
        if not self._workers:
            for _ in range(self._concurrency):
                self._workers.append(asyncio.create_task(self._worker()))
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台调度循环：每 poll_interval 先回收僵死 running（recover_stale），再扫到期 queued 入队。

        recover_stale 放循环内每轮先跑（而非仅启动一次）：runner 兜底失效 / 进程被信号打断留下的
        running 僵死 job 无需等下次重启，下一个 poll 周期即被复位重排（宿主 publish 范式）。"""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.recover_stale()
            except Exception:
                logger.exception("视频调度器僵死回收失败")
            try:
                for job_id in await self.scan_queued():
                    self._queue.put_nowait(job_id)
            except Exception:
                logger.exception("视频调度器扫表失败")
            # 可被 stop() 立即唤醒的休眠。
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """优雅停：置停止信号 → 等调度循环退出 → 取消并等队列 worker 退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
