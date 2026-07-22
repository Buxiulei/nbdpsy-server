"""视频调度器（VideoScheduler，方案 C）纯 DB 状态机单测——mock handler，隔离临时 db。

覆盖调度契约全套：
- 占用防双发：同一 queued job 并发 mark_running 只一处返回 True。
- 阶段自链顺序：transport 7 阶（STAGE_ORDER）与 remake 11 阶（REMAKE_STAGE_ORDER）逐阶段自链
  按序执行，末阶段后 finish（status=completed）。
- 心跳泵：handler 执行期间周期 touch，且成/败退出 async with 后都停泵。
- 僵死恢复：running 且心跳超 15min → 复位回 queued + 重排前 touch + retry_count 递增；续跑从
  first_incomplete_stage 起（download 已 done → 从 transcript 续）。
- max_retries 判死：retry_count 达上限的僵死 job → 直接 failed，不再重排。
- deadline 透传：每阶段 ctx["deadline"] == monotonic + STAGE_BUDGET_SECONDS[stage]。
- _slim 落库：handler 返回的大列表被剔除，只留标量字段；products 弹出进 finish_job。
- revision job：派生后前五阶段标 inherited done → first_incomplete=rewrite。

视频 job 模型由并行 Track M1（app/models/video_job.py）产出；本文件在测试内建**同构临时模型**
``VideoTransportJob``（字段/列名与源 models/video_transport.py 一致），并 monkeypatch 调度模块的
``VideoJob`` 全局。合流以 M1 为准——届时删本临时模型、改 import M1 的真模型即可。
"""

import asyncio
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.video import scheduler as sched_mod
from app.video.scheduler import (
    REMAKE_STAGE_ORDER,
    STAGE_BUDGET_SECONDS,
    STAGE_ORDER,
    VideoScheduler,
    create_job,
    create_revision_job,
    first_incomplete_stage,
    mark_stages_inherited,
)


# ── 同构临时模型（独立 Base，完全隔离，不污染宿主 app.core.db.Base.metadata）──────────
class _TempBase(DeclarativeBase):
    """本测试专用声明式基类，与宿主 Base 分离，避免临时表注册进全局 metadata。"""


class VideoTransportJob(_TempBase):
    """源 models/video_transport.py:VideoTransportJob 的同构副本（M2 独立开发期占位）。"""

    __tablename__ = "video_transport_jobs"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500), nullable=False)
    video_id = Column(String(64), index=True)
    title = Column(String(500))
    duration_seconds = Column(Integer)
    mode = Column(String(20), nullable=False, default="transport")
    parent_job_id = Column(Integer, index=True)
    status = Column(String(20), default="queued", index=True)
    stage = Column(String(20), default="download")
    stages = Column("stages_json", JSON, default=dict)
    options = Column("options_json", JSON, default=dict)
    products = Column("products_json", JSON, default=dict)
    term_sheet = Column("term_sheet_json", JSON, default=list)
    error = Column(Text)
    retry_count = Column(Integer, default=0)
    heartbeat_at = Column(DateTime)
    created_by = Column(Integer, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@pytest.fixture(autouse=True)
def _patch_model(monkeypatch):
    """把调度模块的 VideoJob 全局指向临时同构模型（job_store 函数族 / 调度器查询据此建/查表）。"""
    monkeypatch.setattr(sched_mod, "VideoJob", VideoTransportJob)


@pytest_asyncio.fixture
async def vf(tmp_path):
    """隔离的 async_sessionmaker（仅建临时模型这张表），供调度器多会话操作用。"""
    url = f"sqlite+aiosqlite:///{tmp_path}/vt.db"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_TempBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_TempBase.metadata.drop_all)
        await engine.dispose()


# ── 建数据 / mock handler 辅助 ─────────────────────────────────────────────
async def _get(vf, job_id: int) -> VideoTransportJob:
    async with vf() as s:
        return await s.get(VideoTransportJob, job_id)


def _recorder_handlers(order, recorded: list):
    """为 order 里每个阶段构造一个只记录当前阶段名（job.stage）的 mock handler。"""

    async def rec(job, session, ctx):
        recorded.append(job.stage)
        return {}

    return {stage: rec for stage in order}


# ── 占用防双发 ─────────────────────────────────────────────────────────────
async def test_mark_running_atomic_dedup(vf):
    """同一 queued job 两处并发 mark_running：只一处返回 True（原子占用防双发）。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1)
        job_id = job.id

    scheduler = VideoScheduler(vf)
    r1, r2 = await asyncio.gather(
        scheduler.mark_running(job_id), scheduler.mark_running(job_id)
    )
    assert sorted([r1, r2]) == [False, True]  # 恰一处占到
    assert (await _get(vf, job_id)).status == "running"


async def test_scan_queued_selects_queued_only(vf):
    """scan_queued 只选 status=queued，按 id 升序；running/completed 不选。"""
    async with vf() as s:
        q1 = await create_job(s, url="a", options={}, user_id=1)
        q2 = await create_job(s, url="b", options={}, user_id=1)
        run = await create_job(s, url="c", options={}, user_id=1)
        run.status = "running"
        done = await create_job(s, url="d", options={}, user_id=1)
        done.status = "completed"
        await s.commit()
        q1_id, q2_id, run_id, done_id = q1.id, q2.id, run.id, done.id

    scheduler = VideoScheduler(vf)
    ids = await scheduler.scan_queued()
    assert ids == [q1_id, q2_id]
    assert run_id not in ids and done_id not in ids


# ── 阶段自链顺序 ───────────────────────────────────────────────────────────
async def test_transport_chain_order(vf):
    """transport job：占用 → 逐阶段自链按 STAGE_ORDER 执行 → completed。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1, mode="transport")
        job_id = job.id

    recorded: list[str] = []
    scheduler = VideoScheduler(vf, handlers=_recorder_handlers(STAGE_ORDER, recorded))
    await scheduler._process(job_id)

    assert recorded == STAGE_ORDER
    job = await _get(vf, job_id)
    assert job.status == "completed"
    assert job.error is None


async def test_remake_chain_order(vf):
    """remake job：逐阶段自链按 REMAKE_STAGE_ORDER（11 阶）执行 → completed。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1, mode="remake")
        job_id = job.id

    recorded: list[str] = []
    scheduler = VideoScheduler(vf, handlers=_recorder_handlers(REMAKE_STAGE_ORDER, recorded))
    await scheduler._process(job_id)

    assert recorded == REMAKE_STAGE_ORDER
    assert (await _get(vf, job_id)).status == "completed"


# ── deadline 透传 ─────────────────────────────────────────────────────────
async def test_deadline_passed_per_stage(vf):
    """每阶段 ctx["deadline"] == 该阶段进入时 monotonic + STAGE_BUDGET_SECONDS[stage]。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1, mode="transport")
        job_id = job.id

    budgets: dict[str, float] = {}

    async def capture(job, session, ctx):
        budgets[job.stage] = ctx["deadline"] - time.monotonic()
        return {}

    scheduler = VideoScheduler(vf, handlers={s: capture for s in STAGE_ORDER})
    await scheduler._process(job_id)

    for stage in STAGE_ORDER:
        assert abs(budgets[stage] - STAGE_BUDGET_SECONDS[stage]) < 1.0, stage


# ── _slim 落库 + products 弹出 ─────────────────────────────────────────────
async def test_slim_persisted_and_products_popped(vf):
    """handler 大列表字段被 _slim 剔除只留标量；末阶段 products 弹出进 finish_job(products)。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1, mode="transport")
        job_id = job.id

    async def default_h(job, session, ctx):
        return {}

    async def translate_h(job, session, ctx):
        # 标量留、大列表剔（源 _slim 铁律：大数据须落盘，stages_json 只留路径 + 计数）
        return {"segments_path": "/x/translated.json", "segment_count": 3,
                "big_list": list(range(5000)), "warnings": ["w1", "w2"]}

    async def deliver_h(job, session, ctx):
        return {"products": {"final_video": "/x/final.mp4"}, "note": "done"}

    handlers = {s: default_h for s in STAGE_ORDER}
    handlers["translate"] = translate_h
    handlers["deliver"] = deliver_h

    scheduler = VideoScheduler(vf, handlers=handlers)
    await scheduler._process(job_id)

    job = await _get(vf, job_id)
    assert job.status == "completed"
    # translate stats 只留标量，big_list / warnings（列表）被剔
    tstats = job.stages["translate"]["stats"]
    assert tstats == {"segments_path": "/x/translated.json", "segment_count": 3}
    # products 弹出进 job.products，不留在 deliver stats
    assert job.products == {"final_video": "/x/final.mp4"}
    assert "products" not in job.stages["deliver"]["stats"]
    assert job.stages["deliver"]["stats"] == {"note": "done"}


# ── 心跳泵：周期 touch + 成/败都停泵 ───────────────────────────────────────
async def test_heartbeat_pump_periodic_and_stops_on_success(vf, monkeypatch):
    """handler 执行期间心跳泵周期 touch；正常退出 async with 后停泵（计数不再增长）。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1)
        job_id = job.id

    calls = {"n": 0}
    orig = sched_mod.touch_heartbeat

    async def counting_touch(session, jid):
        calls["n"] += 1
        await orig(session, jid)

    monkeypatch.setattr(sched_mod, "touch_heartbeat", counting_touch)

    scheduler = VideoScheduler(vf, heartbeat_interval=0.03)
    async with scheduler._heartbeat_pump(job_id):
        await asyncio.sleep(0.2)  # 期间泵应刷多次（~6）
    during = calls["n"]
    assert during >= 3, "心跳泵应周期 touch"

    await asyncio.sleep(0.1)
    assert calls["n"] == during, "退出 async with 后应已停泵"


async def test_heartbeat_pump_stops_on_failure(vf, monkeypatch):
    """handler 抛异常路径：__aexit__ 仍停泵（成/败都停）。"""
    async with vf() as s:
        job = await create_job(s, url="u", options={}, user_id=1)
        job_id = job.id

    calls = {"n": 0}
    orig = sched_mod.touch_heartbeat

    async def counting_touch(session, jid):
        calls["n"] += 1
        await orig(session, jid)

    monkeypatch.setattr(sched_mod, "touch_heartbeat", counting_touch)

    scheduler = VideoScheduler(vf, heartbeat_interval=0.03)
    with pytest.raises(RuntimeError):
        async with scheduler._heartbeat_pump(job_id):
            await asyncio.sleep(0.1)
            raise RuntimeError("boom")
    after = calls["n"]

    await asyncio.sleep(0.1)
    assert calls["n"] == after, "异常路径也应停泵"


# ── 僵死恢复：first_incomplete 续跑 + 重排前 touch ─────────────────────────
async def test_recover_stale_resumes_and_touches(vf):
    """running 且心跳超 15min（download 已 done）→ 复位 queued + retry_count++ + 重排前 touch；
    续跑从 first_incomplete=transcript 起，跑完 completed。"""
    stale_hb = datetime.utcnow() - timedelta(minutes=16)
    async with vf() as s:
        job = VideoTransportJob(
            url="u", mode="transport", status="running",
            stages={"download": {"status": "done", "stats": {"video_path": "/v.mp4"}}},
            heartbeat_at=stale_hb, retry_count=0,
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)
        job_id = job.id

    recorded: list[str] = []
    scheduler = VideoScheduler(vf, handlers=_recorder_handlers(STAGE_ORDER, recorded))

    recovered = await scheduler.recover_stale()
    assert recovered == 1

    job = await _get(vf, job_id)
    assert job.status == "queued"  # 复位回 queued，由主循环重占
    assert job.retry_count == 1  # 递增
    assert job.heartbeat_at > stale_hb  # 重排前 touch（刷新过）
    assert first_incomplete_stage(job) == "transcript"  # download 已 done → 从 transcript 续

    # 续跑：从 transcript 起自链到 deliver（download 不重跑）
    await scheduler._process(job_id)
    assert recorded == STAGE_ORDER[1:]  # transcript..deliver
    assert (await _get(vf, job_id)).status == "completed"


async def test_recover_stale_skips_in_flight(vf):
    """在途 job（_in_flight）即便心跳超时也不复位（阶段墙钟长不等于僵死）。"""
    stale_hb = datetime.utcnow() - timedelta(minutes=16)
    async with vf() as s:
        job = VideoTransportJob(url="u", mode="transport", status="running",
                                heartbeat_at=stale_hb, retry_count=0)
        s.add(job)
        await s.commit()
        await s.refresh(job)
        job_id = job.id

    scheduler = VideoScheduler(vf)
    scheduler._in_flight.add(job_id)  # 模拟本进程真处理中
    recovered = await scheduler.recover_stale()
    assert recovered == 0
    assert (await _get(vf, job_id)).status == "running"  # 未复位


# ── max_retries 判死 ──────────────────────────────────────────────────────
async def test_recover_stale_max_retries_marks_failed(vf):
    """retry_count 已达上限的僵死 job → 直接 failed，不再重排。"""
    stale_hb = datetime.utcnow() - timedelta(minutes=16)
    async with vf() as s:
        job = VideoTransportJob(url="u", mode="transport", status="running",
                                heartbeat_at=stale_hb, retry_count=sched_mod._MAX_RETRIES)
        s.add(job)
        await s.commit()
        await s.refresh(job)
        job_id = job.id

    scheduler = VideoScheduler(vf)
    recovered = await scheduler.recover_stale()
    assert recovered == 0  # 未恢复（判死）

    job = await _get(vf, job_id)
    assert job.status == "failed"
    assert "超过最大重试次数" in job.error


# ── 生命周期循环：recover + scan + 自链 + stop ────────────────────────────
async def test_scheduler_loop_recovers_and_processes(vf):
    """start：每 poll 周期先 recover_stale（僵死 running 复位 queued）再 scan→submit，
    新 queued 与复位的僵死 job 都自链跑完 completed；可 stop。"""
    stale_hb = datetime.utcnow() - timedelta(minutes=16)
    async with vf() as s:
        fresh = await create_job(s, url="fresh", options={}, user_id=1, mode="transport")
        stale = VideoTransportJob(url="stale", mode="transport", status="running",
                                  heartbeat_at=stale_hb, retry_count=0)
        s.add(stale)
        await s.commit()
        await s.refresh(stale)
        fresh_id, stale_id = fresh.id, stale.id

    recorded: list[str] = []
    scheduler = VideoScheduler(
        vf, poll_interval=0.02, handlers=_recorder_handlers(STAGE_ORDER, recorded))
    scheduler.start()
    try:
        for _ in range(300):
            f = await _get(vf, fresh_id)
            st = await _get(vf, stale_id)
            if f.status == "completed" and st.status == "completed":
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    assert (await _get(vf, fresh_id)).status == "completed"
    assert (await _get(vf, stale_id)).status == "completed"


# ── revision job：前五阶段 done → first_incomplete=rewrite ─────────────────
async def test_revision_job_first_incomplete_is_rewrite(vf):
    """派生 revision job（mode=remake，parent 指向父）后标继承前五阶段 done → first_incomplete=rewrite。"""
    async with vf() as s:
        parent = await create_job(s, url="u", options={"voice": "S_x"}, user_id=7,
                                  mode="remake")
        job = await create_revision_job(
            s, parent, instructions="把片头改短", edit_plan=[{"type": "global_param"}])
        # revision 不重跑最贵前五阶段（download/analyze/transcript/resegment/translate）
        inherited = REMAKE_STAGE_ORDER[:5]
        await mark_stages_inherited(s, job, inherited, parent.id)
        job_id = job.id
        parent_id = parent.id

    job = await _get(vf, job_id)
    assert job.mode == "remake"
    assert job.parent_job_id == parent_id
    assert job.options["revision"]["instructions"] == "把片头改短"
    assert first_incomplete_stage(job) == "rewrite"  # 前五阶段 done → 第 6 阶 rewrite
