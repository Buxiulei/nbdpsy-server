"""browser_jobs 统一台账仓储:async(API/supervisor 侧)+ sync(account_worker 子进程侧)双通道。

设计见 docs/design/2026-07-24-api-worker-split-design.md 第三节。契约要点:

- **async 侧**(经 app.core.db.get_session,测试对 async_session 的 monkeypatch 生效):
  enqueue / get_job / count_unfinished_for_operator / list_dispatchable / recover_stale;
- **sync 侧**(sqlite3 直连,WAL 下短事务;account_worker 子进程无事件循环依赖):
  claim_job_sync / finish_job_sync / heartbeat_sync,另有 enqueue_sync / get_job_sync
  供 REST 请求路径的同步 start_*/get_*(FastAPI handler 内不可 await 的兼容签名)使用;
- **认领纪律**:UPDATE ... WHERE status='queued',rowcount=1 才算领到——单 worker
  也保持该纪律,为多消费方并存(进程内消费 + supervisor 派生子进程)留正确性;
- **终态判定约定**(执行方与读方共同遵守):execute 抛异常、或返回 dict 含 "error" 键
  → 台账 status="error";否则 status="done"。result 原样序列化存储。

进程内消费(P1→P2 过渡接缝):NBDPSY_ROLE=all(默认,开发/测试/单进程小部署)时,
start_* 在 enqueue 后经 spawn_inline 起进程内执行——走与 worker 完全相同的
claim→execute→finish 纪律,supervisor(P2)上线后同一 job 至多一方 claim 成功,
绝不双跑;NBDPSY_ROLE=api 时为纯 enqueue,执行完全交给 worker 进程。
"""

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import func, or_, select, update

from app.auth.context import AuthError, current_operator
from app.core.db import get_session
from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob

# 僵死判定阈值:running 行心跳超此秒数视为执行方已死(心跳周期 300s,三倍容错)。
STALE_AFTER_SECONDS = 900

# 幂等类 kind:僵死后重置 queued 自动重跑;其余(note_delete/op_images)非幂等,
# 置 error + 结果未知指引,绝不自动重跑(删除不可逆 / 生图烧钱)。
# note_ledger_sync 是纯只读抓取 + 按 (account_id, note_id) upsert,重跑只会刷新同一批行。
_IDEMPOTENT_KINDS = ("cookie_check", "note_export", "note_ledger_sync")

# 进程内消费任务强引用集合:防未完成的 asyncio.Task 被 GC 提前回收。
_inline_tasks: set[asyncio.Task] = set()


# ---------------- async 侧(API / supervisor) ----------------


async def enqueue(
    kind: str,
    payload: dict,
    operator_id: int,
    account_id: int | None = None,
    job_id: str | None = None,
) -> str:
    """登记一条 queued 台账行,返回对外轮询 id(可显式传 job_id,如 op_images 复合串)。"""
    jid = job_id or uuid.uuid4().hex
    async with get_session() as session:
        session.add(
            BrowserJob(
                id=jid,
                kind=kind,
                account_id=account_id,
                operator_id=operator_id,
                payload=json.dumps(payload, ensure_ascii=False),
                status="queued",
            )
        )
        await session.commit()
    return jid


async def get_job(job_id: str) -> dict | None:
    """按 id 取台账行的全字段 dict(payload/result 已反序列化);不存在返回 None。"""
    async with get_session() as session:
        row = await session.get(BrowserJob, job_id)
    return _orm_to_dict(row) if row is not None else None


async def count_unfinished_for_operator(operator_id: int) -> int:
    """数该 operator 的未终态任务:browser_jobs(queued/running)+ publish_jobs(pending/publishing)。

    运营配额(OPERATOR_PENDING_QUOTA)的计数依据;两表合计。
    """
    async with get_session() as session:
        browser_count = await session.scalar(
            select(func.count())
            .select_from(BrowserJob)
            .where(
                BrowserJob.operator_id == operator_id,
                BrowserJob.status.in_(("queued", "running")),
            )
        )
        publish_count = await session.scalar(
            select(func.count())
            .select_from(PublishJob)
            .where(
                PublishJob.created_by == operator_id,
                PublishJob.status.in_(("pending", "publishing")),
            )
        )
    return int(browser_count or 0) + int(publish_count or 0)


async def list_dispatchable() -> list[dict]:
    """取可派发的 queued 行(按 created_at 升序),供 supervisor 扫描派单。

    payload 里带 ``not_before``(未来时刻)的行按排期过滤掉,下轮到点再派 —— 延时任务
    (matrix_interact)靠这条落库排期实现,执行方**不得**领了任务再进程内 sleep 等待
    (会占死 browser_slot 闸,阻塞 cookie_check / note_export / 发布)。
    """
    now = datetime.utcnow()
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(BrowserJob)
                    .where(BrowserJob.status == "queued")
                    .order_by(BrowserJob.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [d for d in (_orm_to_dict(r) for r in rows) if _schedule_due(d, now)]


def _schedule_due(job: dict, now: datetime) -> bool:
    """排期是否到点:payload 无 ``not_before`` 视为立即可派;值坏了也放行(不卡死任务)。"""
    raw = (job.get("payload") or {}).get("not_before")
    if not raw:
        return True
    try:
        return datetime.fromisoformat(raw) <= now
    except (TypeError, ValueError):
        logger.warning(f"[browser_jobs] not_before 值非法,按立即可派处理 job_id={job.get('id')}")
        return True


async def recover_stale() -> int:
    """僵死恢复:running 且心跳超 STALE_AFTER_SECONDS 的行按 kind 语义处置,返回处置行数。

    - 幂等类(cookie_check/note_export):重置 queued(清 claimed_by/heartbeat/result),
      supervisor 下轮扫描自动重跑;
    - 非幂等类(note_delete/op_images):置 error,result 带 "unknown": True 标记 +
      人工核对指引(删除不可逆/生图烧钱,绝不自动重跑;读方据 unknown 标记译语义)。
    幂等可重复调用:处置过的行不再是 running,再跑返回 0。
    """
    cutoff = datetime.utcnow() - timedelta(seconds=STALE_AFTER_SECONDS)
    handled = 0
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(BrowserJob).where(
                        BrowserJob.status == "running",
                        or_(
                            BrowserJob.heartbeat_at.is_(None),
                            BrowserJob.heartbeat_at < cutoff,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.kind in _IDEMPOTENT_KINDS:
                row.status = "queued"
                row.claimed_by = None
                row.heartbeat_at = None
                row.result = None
            else:
                row.status = "error"
                row.result = json.dumps(
                    {
                        "error": (
                            f"执行进程中断,任务结果未知(unknown):{row.kind} 为"
                            "非幂等操作,不自动重跑;请人工核对实际结果(创作中心/产物"
                            "目录)后再决定是否重新发起"
                        ),
                        "unknown": True,
                    },
                    ensure_ascii=False,
                )
            row.updated_at = datetime.utcnow()
            handled += 1
        await session.commit()
    if handled:
        logger.warning(f"[browser_jobs] 僵死恢复处置 {handled} 行(心跳超 {STALE_AFTER_SECONDS}s)")
    return handled


async def claim_job(job_id: str, worker_tag: str) -> dict | None:
    """async 乐观认领:queued→running(rowcount=1 才算领到),返回认领后的行 dict。

    进程内消费(spawn_inline)用;与 claim_job_sync 同一纪律,两侧对同一 job
    至多一方成功。
    """
    now = datetime.utcnow()
    async with get_session() as session:
        res = await session.execute(
            update(BrowserJob)
            .where(BrowserJob.id == job_id, BrowserJob.status == "queued")
            .values(
                status="running", claimed_by=worker_tag, heartbeat_at=now, updated_at=now
            )
        )
        await session.commit()
        if res.rowcount != 1:
            return None
    return await get_job(job_id)


async def finish_job(
    job_id: str, status: str, result: dict, worker_tag: str | None = None
) -> bool:
    """async 写终态(done/error)与结果;进程内消费用。

    终态守卫(与 publish 侧 C1 同款纪律):只覆盖仍处 running 的行;传 worker_tag 时
    还要求 claimed_by 是自己——被僵死恢复处置/被他方重新认领的行,迟到的原执行方
    写不进(rowcount=0 → 告警丢弃,返回 False),unknown 人工核对语义不被静默抹除。
    """
    stmt = (
        update(BrowserJob)
        .where(BrowserJob.id == job_id, BrowserJob.status == "running")
        .values(
            status=status,
            result=json.dumps(result, ensure_ascii=False),
            updated_at=datetime.utcnow(),
        )
    )
    if worker_tag:
        stmt = stmt.where(BrowserJob.claimed_by == worker_tag)
    async with get_session() as session:
        res = await session.execute(stmt)
        await session.commit()
    if res.rowcount != 1:
        logger.warning(
            f"[browser_jobs] 迟到终态被守卫丢弃 job_id={job_id} status={status}"
            f"(行已被恢复处置或他方认领)")
        return False
    return True


# ---------------- sync 侧(account_worker 子进程 / REST 同步签名) ----------------


def claim_job_sync(db_path: str, job_id: str, worker_tag: str) -> dict | None:
    """sync 乐观认领:UPDATE ... WHERE status='queued',rowcount=1 才返回行 dict。"""
    now = _now_str()
    with sqlite3.connect(db_path, timeout=30) as conn:
        cur = conn.execute(
            "UPDATE browser_jobs SET status='running', claimed_by=?, heartbeat_at=?, "
            "updated_at=? WHERE id=? AND status='queued'",
            (worker_tag, now, now, job_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
    return get_job_sync(db_path, job_id)


def finish_job_sync(
    db_path: str, job_id: str, status: str, result: dict,
    worker_tag: str | None = None,
) -> bool:
    """sync 写终态(done/error)与结果;守卫语义同 async 侧(见 finish_job)。"""
    sql = "UPDATE browser_jobs SET status=?, result=?, updated_at=? WHERE id=? AND status='running'"
    params = [status, json.dumps(result, ensure_ascii=False), _now_str(), job_id]
    if worker_tag:
        sql += " AND claimed_by=?"
        params.append(worker_tag)
    with sqlite3.connect(db_path, timeout=30) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        if cur.rowcount != 1:
            logger.warning(
                f"[browser_jobs] 迟到终态被守卫丢弃 job_id={job_id} status={status}")
            return False
    return True


async def heartbeat(job_id: str) -> None:
    """async 心跳 touch(进程内消费路径用)。"""
    now = datetime.utcnow()
    async with get_session() as session:
        await session.execute(
            update(BrowserJob)
            .where(BrowserJob.id == job_id)
            .values(heartbeat_at=now, updated_at=now)
        )
        await session.commit()


async def heartbeat_loop(job_id: str, interval_s: float = 300.0) -> None:
    """执行期伴生心跳循环:每 interval touch 一次,由执行方 finally cancel。

    评审 F3:inline/_run_op_images 认领后若零心跳,长任务(下拉锁等待/大导出)超 900s
    会被同进程僵死恢复误判——幂等类重置双跑、非幂等类误报 unknown。本循环与
    account_worker 的 300s 心跳线程对等。
    """
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await heartbeat(job_id)
            except Exception:  # 心跳失败不杀执行,下轮再试
                logger.warning(f"[browser_jobs] 心跳失败 job_id={job_id}(下轮重试)")
    except asyncio.CancelledError:
        pass


def heartbeat_sync(db_path: str, job_id: str) -> None:
    """sync 心跳 touch:执行方周期(300s)调用,证明自己还活着。"""
    now = _now_str()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute(
            "UPDATE browser_jobs SET heartbeat_at=?, updated_at=? WHERE id=?",
            (now, now, job_id),
        )
        conn.commit()


def enqueue_sync(
    db_path: str,
    kind: str,
    payload: dict,
    operator_id: int,
    account_id: int | None = None,
    job_id: str | None = None,
) -> str:
    """sync 登记一条 queued 台账行(REST 同步 start_* 路径用),返回对外轮询 id。"""
    jid = job_id or uuid.uuid4().hex
    now = _now_str()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute(
            "INSERT INTO browser_jobs (id, kind, account_id, operator_id, payload, "
            "status, result, claimed_by, heartbeat_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?, ?)",
            (
                jid,
                kind,
                account_id,
                operator_id,
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
    return jid


def get_job_sync(db_path: str, job_id: str) -> dict | None:
    """sync 按 id 取台账行 dict(payload/result 已反序列化;时间字段为库内字符串)。"""
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM browser_jobs WHERE id=?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    entry = dict(row)
    entry["payload"] = _loads(entry.get("payload"), default={})
    entry["result"] = _loads(entry.get("result"), default=None)
    return entry


# ---------------- REST 请求路径便捷层(同步 start_*/get_* 用) ----------------


def current_db_path() -> str:
    """当前生效 engine 的 sqlite 库文件路径(读模块属性,测试 monkeypatch engine 生效)。"""
    import app.core.db as db_module

    return db_module.engine.url.database


def enqueue_from_request(
    kind: str, payload: dict, account_id: int | None = None, job_id: str | None = None
) -> str:
    """REST 请求上下文里的同步 enqueue:operator 取自认证 ContextVar,库取当前 engine。

    非请求上下文(进程内直调/测试)无 operator 时记 operator_id=0。
    """
    try:
        operator_id = current_operator().id
    except AuthError:
        operator_id = 0
    return enqueue_sync(
        current_db_path(), kind, payload, operator_id, account_id=account_id, job_id=job_id
    )


# ---------------- 进程内消费(P1→P2 过渡接缝) ----------------


def inline_execution_enabled() -> bool:
    """是否在本进程内消费任务:仅 NBDPSY_ROLE=all(默认)时开。

    role=api → 纯 enqueue,执行交 worker 进程;role=worker → 不挂 REST,不会走到这里。
    supervisor(P2)上线后本开关由角色接缝统一收口。
    """
    return os.getenv("NBDPSY_ROLE", "all") == "all"


def spawn_inline(job_id: str, execute_call: Callable[[], Awaitable[dict]]) -> None:
    """在本进程事件循环内消费一条刚 enqueue 的任务(NBDPSY_ROLE=api 时不跑)。

    严格走 claim→execute→finish 纪律:claim 失败(已被 worker 领走)即放弃,绝不双跑;
    execute 抛异常或返回含 "error" 键的 dict → 终态 error,否则 done。
    """
    if not inline_execution_enabled():
        return
    task = asyncio.create_task(_run_inline(job_id, execute_call))
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)  # 完成即释放强引用,防泄漏


async def _run_inline(job_id: str, execute_call: Callable[[], Awaitable[dict]]) -> None:
    """进程内消费体:任何异常兜底写终态,绝不留 running 悬挂、绝不让 task 未观测异常。"""
    tag = f"inline:{os.getpid()}"
    try:
        row = await claim_job(job_id, worker_tag=tag)
        if row is None:
            return  # 已被其它消费方领走(认领纪律保证不双跑)
        hb = asyncio.create_task(heartbeat_loop(job_id))  # 执行期心跳,防误判僵死
        try:
            try:
                result = await execute_call()
            except Exception as exc:  # 兜底:执行崩溃也要落终态,别让轮询方死等
                logger.exception(f"[browser_jobs] 进程内执行崩溃 job_id={job_id}")
                await finish_job(job_id, "error", {"error": str(exc)}, worker_tag=tag)
                return
            status = "error" if isinstance(result, dict) and "error" in result else "done"
            await finish_job(
                job_id, status, result if isinstance(result, dict) else {}, worker_tag=tag
            )
        finally:
            hb.cancel()
    except Exception:  # 台账操作自身失败:只能记日志(重启后 recover_stale 兜底处置)
        logger.exception(f"[browser_jobs] 进程内消费台账操作失败 job_id={job_id}")


# ---------------- 内部工具 ----------------


def _now_str() -> str:
    """与 SQLAlchemy DateTime 落库格式一致的 UTC 时间串(可与 ORM 侧字典序比较)。"""
    return datetime.utcnow().isoformat(sep=" ")


def _loads(raw, default):
    """宽容反序列化:空/坏 JSON 给 default,不让读路径炸。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _orm_to_dict(row: BrowserJob) -> dict:
    """ORM 行 → 全字段 dict(payload/result 已反序列化)。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "account_id": row.account_id,
        "operator_id": row.operator_id,
        "payload": _loads(row.payload, default={}),
        "status": row.status,
        "result": _loads(row.result, default=None),
        "claimed_by": row.claimed_by,
        "heartbeat_at": row.heartbeat_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
