"""browser_jobs 统一台账仓储测试:async/sync 双通道 + 认领纪律 + 僵死恢复。

隔离手法:每测试独立临时 sqlite 文件库(sync 侧 sqlite3 直连需要真实文件路径),
monkeypatch app.core.db 的 engine/async_session,async 侧经 get_session 落同一库。

覆盖(任务书六项):
- enqueue/get 回环(async 与 sync 读一致,payload/result 反序列化);
- 乐观认领仅一次成功(claim_job_sync 与 async claim_job 同一纪律);
- finish 写终态(done/error + result);
- count_unfinished_for_operator 双表合计(browser_jobs 未终态 + publish_jobs
  pending/publishing,他人任务不计);
- recover_stale:幂等类(cookie_check/note_export)重置 queued,非幂等类
  (note_delete/op_images)置 error + unknown 标记与人工核对指引;心跳新鲜的不动;
  重复调用幂等(第二次 0 行);
- op_images 复合 id("opimg_{session_id}_{ext_job_id}")回查。
"""

import sqlite3
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.models.publish_job import PublishJob
from app.services import browser_jobs_repo as repo


@pytest_asyncio.fixture
async def jobs_db(tmp_path, monkeypatch):
    """临时 sqlite 文件库 + monkeypatch 全局 engine/async_session;yield 库文件路径。"""
    from app.core.db import Base

    db_path = str(tmp_path / "jobs.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)

    import app.models  # noqa: F401  触发模型注册到 Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", session_factory)
    try:
        yield db_path
    finally:
        await engine.dispose()


def _set_heartbeat(db_path: str, job_id: str, dt: datetime) -> None:
    """直改某行心跳时间(模拟执行方久未心跳)。"""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE browser_jobs SET heartbeat_at=? WHERE id=?",
            (dt.isoformat(sep=" "), job_id),
        )
        conn.commit()


# ---------------- enqueue / get 回环 ----------------


async def test_enqueue_get_roundtrip(jobs_db):
    """async enqueue → async get_job / sync get_job_sync 读到同一行,payload 反序列化。"""
    jid = await repo.enqueue(
        "cookie_check", {"k": "值"}, operator_id=7, account_id=3
    )
    row = await repo.get_job(jid)
    assert row is not None
    assert row["id"] == jid
    assert row["kind"] == "cookie_check"
    assert row["account_id"] == 3
    assert row["operator_id"] == 7
    assert row["payload"] == {"k": "值"}
    assert row["status"] == "queued"
    assert row["result"] is None
    assert row["claimed_by"] is None

    sync_row = repo.get_job_sync(jobs_db, jid)
    assert sync_row["id"] == jid
    assert sync_row["payload"] == {"k": "值"}
    assert sync_row["status"] == "queued"

    assert await repo.get_job("no-such-id") is None
    assert repo.get_job_sync(jobs_db, "no-such-id") is None


async def test_enqueue_sync_roundtrip(jobs_db):
    """sync enqueue_sync 落的行,async get_job 读得到(双通道同库同格式)。"""
    jid = repo.enqueue_sync(jobs_db, "note_export", {}, operator_id=5, account_id=9)
    row = await repo.get_job(jid)
    assert row is not None
    assert row["kind"] == "note_export"
    assert row["account_id"] == 9
    assert row["operator_id"] == 5
    assert row["payload"] == {}
    assert row["status"] == "queued"


# ---------------- 乐观认领仅一次成功 ----------------


async def test_claim_only_once(jobs_db):
    """同一 queued 行两次认领,仅第一次成功(rowcount=1 纪律);running 后不可再领。"""
    jid = await repo.enqueue("note_export", {}, operator_id=1, account_id=2)

    first = repo.claim_job_sync(jobs_db, jid, "worker-A")
    assert first is not None
    assert first["status"] == "running"
    assert first["claimed_by"] == "worker-A"
    assert first["heartbeat_at"] is not None

    second = repo.claim_job_sync(jobs_db, jid, "worker-B")
    assert second is None  # 已被 A 领走

    # async 侧同一纪律:running 行也领不到
    assert await repo.claim_job(jid, "inline-C") is None
    # 行归属未被覆盖
    assert (await repo.get_job(jid))["claimed_by"] == "worker-A"


# ---------------- finish 写终态 ----------------


async def test_finish_writes_terminal(jobs_db):
    """finish_job_sync / finish_job 写终态与 result,读回反序列化一致。"""
    jid = await repo.enqueue("note_export", {}, operator_id=1, account_id=2)
    repo.claim_job_sync(jobs_db, jid, "worker-A")
    repo.finish_job_sync(jobs_db, jid, "done", {"note_count": 42})
    row = await repo.get_job(jid)
    assert row["status"] == "done"
    assert row["result"] == {"note_count": 42}

    jid2 = await repo.enqueue("note_delete", {"title": "t"}, operator_id=1, account_id=2)
    await repo.claim_job(jid2, "inline")
    await repo.finish_job(jid2, "error", {"error": "boom"})
    row2 = repo.get_job_sync(jobs_db, jid2)
    assert row2["status"] == "error"
    assert row2["result"] == {"error": "boom"}


# ---------------- count_unfinished 双表合计 ----------------


async def test_count_unfinished_across_two_tables(jobs_db):
    """browser_jobs(queued/running)+ publish_jobs(pending/publishing)合计;终态与他人不计。"""
    op = 7
    # browser_jobs:queued + running 计,done 不计;他人(op=8)不计
    await repo.enqueue("cookie_check", {}, operator_id=op, account_id=1)
    running = await repo.enqueue("note_export", {}, operator_id=op, account_id=1)
    repo.claim_job_sync(jobs_db, running, "w")
    finished = await repo.enqueue("note_export", {}, operator_id=op, account_id=1)
    repo.claim_job_sync(jobs_db, finished, "w")
    repo.finish_job_sync(jobs_db, finished, "done", {"note_count": 1})
    await repo.enqueue("cookie_check", {}, operator_id=8, account_id=1)

    # publish_jobs:pending + publishing 计,published 不计;他人不计
    async with db_module.async_session() as s:
        for status, created_by in (
            ("pending", op), ("publishing", op), ("published", op), ("pending", 8),
        ):
            s.add(PublishJob(
                account_id=1, title="t", content="c", images_json="[]",
                topics_json="[]", status=status, created_by=created_by,
            ))
        await s.commit()

    assert await repo.count_unfinished_for_operator(op) == 4  # 2 browser + 2 publish
    assert await repo.count_unfinished_for_operator(8) == 2
    assert await repo.count_unfinished_for_operator(999) == 0


# ---------------- list_dispatchable ----------------


async def test_list_dispatchable_only_queued(jobs_db):
    """仅 queued 行按 created_at 升序返回,含 kind/account_id/created_at 字段。"""
    a = await repo.enqueue("cookie_check", {}, operator_id=1, account_id=11)
    b = await repo.enqueue("note_export", {}, operator_id=1, account_id=22)
    claimed = await repo.enqueue("note_delete", {}, operator_id=1, account_id=33)
    repo.claim_job_sync(jobs_db, claimed, "w")

    rows = await repo.list_dispatchable()
    assert [r["id"] for r in rows] == [a, b]
    assert rows[0]["kind"] == "cookie_check" and rows[0]["account_id"] == 11
    assert rows[1]["kind"] == "note_export" and rows[1]["account_id"] == 22
    assert all(r["created_at"] is not None for r in rows)


# ---------------- recover_stale ----------------


async def test_recover_stale_by_kind_semantics(jobs_db):
    """僵死行:幂等类重置 queued 自动重跑;非幂等类置 error + unknown 指引;新鲜行不动。"""
    stale_cutoff = datetime.utcnow() - timedelta(
        seconds=repo.STALE_AFTER_SECONDS + 60
    )
    jobs = {}
    for kind in ("cookie_check", "note_export", "note_delete", "op_images"):
        jid = await repo.enqueue(kind, {}, operator_id=1, account_id=1)
        repo.claim_job_sync(jobs_db, jid, "dead-worker")
        _set_heartbeat(jobs_db, jid, stale_cutoff)
        jobs[kind] = jid

    # 心跳新鲜的 running 行(heartbeat_sync 刚 touch 过)不受处置
    fresh = await repo.enqueue("note_delete", {}, operator_id=1, account_id=1)
    repo.claim_job_sync(jobs_db, fresh, "alive-worker")
    repo.heartbeat_sync(jobs_db, fresh)

    handled = await repo.recover_stale()
    assert handled == 4

    # 幂等类:重置 queued,认领痕迹清空
    for kind in ("cookie_check", "note_export"):
        row = await repo.get_job(jobs[kind])
        assert row["status"] == "queued", kind
        assert row["claimed_by"] is None and row["heartbeat_at"] is None
        assert row["result"] is None

    # 非幂等类:error + unknown 标记 + 人工核对指引
    for kind in ("note_delete", "op_images"):
        row = await repo.get_job(jobs[kind])
        assert row["status"] == "error", kind
        assert row["result"]["unknown"] is True
        assert "unknown" in row["result"]["error"]
        assert "人工核对" in row["result"]["error"]

    # 新鲜行不动
    assert (await repo.get_job(fresh))["status"] == "running"

    # 幂等:第二次调用无行可处置(error 行不再是 running;queued 行未被认领)
    assert await repo.recover_stale() == 0


# ---------------- 进程内消费的角色接缝 ----------------


async def test_spawn_inline_role_gate_and_claim_discipline(jobs_db, monkeypatch):
    """NBDPSY_ROLE=api → 纯 enqueue 不消费;默认 all → claim→execute→finish 落终态。"""
    import asyncio

    # role=api:spawn_inline 不起任务,行停在 queued(执行交 worker 进程)
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    jid_api = await repo.enqueue("cookie_check", {}, operator_id=1, account_id=1)

    async def _boom():
        raise AssertionError("role=api 不应执行")

    repo.spawn_inline(jid_api, _boom)
    await asyncio.sleep(0.05)
    assert (await repo.get_job(jid_api))["status"] == "queued"

    # role=all(默认):进程内消费,execute 返回含 "error" 键 → 终态 error
    monkeypatch.delenv("NBDPSY_ROLE", raising=False)
    jid_err = await repo.enqueue("note_export", {}, operator_id=1, account_id=1)

    async def _err():
        return {"error": "语义失败"}

    repo.spawn_inline(jid_err, _err)
    for _ in range(100):
        row = await repo.get_job(jid_err)
        if row["status"] != "queued" and row["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert row["status"] == "error"
    assert row["result"] == {"error": "语义失败"}

    # 已被认领(running)的行,spawn_inline 领不到 → 不双跑
    jid_taken = await repo.enqueue("note_export", {}, operator_id=1, account_id=1)
    repo.claim_job_sync(jobs_db, jid_taken, "worker-X")

    async def _steal():
        raise AssertionError("已被 worker 领走,进程内不应执行")

    repo.spawn_inline(jid_taken, _steal)
    await asyncio.sleep(0.05)
    row = await repo.get_job(jid_taken)
    assert row["status"] == "running" and row["claimed_by"] == "worker-X"


# ---------------- op_images 复合 id ----------------


async def test_op_images_composite_id_roundtrip(jobs_db):
    """op_images 用 "opimg_{session_id}_{ext_job_id}" 复合 id 显式落库,回查一致。"""
    session_id = "abc123def456"
    composite = f"opimg_{session_id}_1"
    jid = await repo.enqueue(
        "op_images", {"prompts": ["p1", "p2"]}, operator_id=3,
        account_id=None, job_id=composite,
    )
    assert jid == composite

    row = await repo.get_job(composite)
    assert row["kind"] == "op_images"
    assert row["account_id"] is None
    assert row["payload"] == {"prompts": ["p1", "p2"]}

    sync_row = repo.get_job_sync(jobs_db, composite)
    assert sync_row["id"] == composite
    assert sync_row["payload"]["prompts"] == ["p1", "p2"]
