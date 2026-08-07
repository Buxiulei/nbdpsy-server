"""排队可见性:轮询响应里的 ``queue`` 段 + **派发判据的唯一真源**。

运营原话(2026-08-07):"任务排队的话,应该返回排队编号,并且显示当前执行的任务的编号,
这样就知道在排队,前面还有多少个任务。"

背景:当天同号会话总闸(``app/worker.py`` ``_apply_session_cap``)上线后出现过全矩阵
9 个号全部满帽、11 条任务排队、running=0 的局面,单条任务排了 40 分钟以上;而调用方
轮询只看得到 ``status=queued`` —— 既不知道排第几,也不知道为什么等、还要等多久,只能
干等,或者误以为卡死然后重试(重试只会多灌一条进队列,让队列更长)。

**为什么判据收在本模块**:派发层(Supervisor)与读侧(本模块)必须对同一批行给出同一个
结论,否则运营会看到 ``blocked_by=null`` 却排了 40 分钟,或看到 ``session_cap`` 而任务
其实早就派出去了。所以下面五处判据是**唯一真源**,worker 与读侧都只调这里、谁也不复刻:

1. 什么算"一次会话" —— ``browser_session_filter`` / ``publish_session_filter``;
2. 闸放不放行 —— ``layer_of`` + ``cap_allows``;
3. 一批任务怎么排序 —— ``norm_created`` + ``queue_sort_key``;
4. 排期到没到点 —— ``not_before_of`` / ``schedule_due`` / ``publish_due_filter``;
5. 帽值与并发上限的默认取值 —— ``configured_*``。

**读侧只能看到库**:worker 的 ``self._procs``(存活子进程表)是进程内状态,读侧看不见,
只能用"库里有没有 running/publishing 行"做等价代理 —— 子进程在跑必然有一条 running 行,
所以代理只会在"子进程刚起还没认领"的几百毫秒里偏保守,不会漏报。这一点在
``blocked_by`` 的文档里对调用方讲明。
"""

import json
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_, select

from app.core.config import settings
from app.core.db import get_session
from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob

# 同号会话频次总闸的统计窗口(秒):风控红线按"一小时几次"看,窗口就是一小时滚动
SESSION_WINDOW_SECONDS = 3600

# 触发方分层:系统自发(operator_id 非正)守严帽,运营触发(operator_id>0)守宽帽
LAYER_SYSTEM = "system"
LAYER_OPERATOR = "operator"

# 派发批次里的任务来源标签(排序键的第二位,也是 running 段的 kind 取值来源)
SOURCE_BROWSER = "browser"
SOURCE_PUBLISH = "publish"

# 派发层看得见、读侧看不见的三种等待,对外的 blocked_by 取值
BLOCKED_SESSION_CAP = "session_cap"
BLOCKED_ACCOUNT_BUSY = "account_busy"
BLOCKED_GLOBAL_CONCURRENCY = "global_concurrency"


# ---------------- 判据一:帽值与并发上限的默认取值 ----------------


def configured_session_cap() -> int:
    """系统自发任务的同号一小时会话帽(≤0 = 关掉这一层)。"""
    return getattr(settings, "ACCOUNT_HOURLY_SESSION_CAP", 4)


def configured_operator_session_cap() -> int:
    """运营触发任务的同号一小时会话帽(比系统那层宽;≤0 = 关掉这一层)。"""
    return getattr(settings, "ACCOUNT_HOURLY_OPERATOR_SESSION_CAP", 12)


def configured_max_procs() -> int:
    """全局 account_worker 子进程上限。"""
    return settings.BROWSER_CONCURRENCY


# ---------------- 判据二:什么算"一次会话" ----------------


def session_window_cutoff(now: datetime) -> datetime:
    """滚动窗口下界:早于此刻的终态会话已经滚出窗口,不再占额度。"""
    return now - timedelta(seconds=SESSION_WINDOW_SECONDS)


def browser_session_filter(cutoff: datetime):
    """browser_jobs 里算作"一次会话"的行:窗口内终态 + 全部 running(在飞)。

    ``queued`` 不算 —— 还没起浏览器,数进去会自锁(排队的任务把自己挡在闸外)。
    """
    return or_(
        and_(
            BrowserJob.status.in_(("done", "error")),
            BrowserJob.updated_at >= cutoff,
        ),
        BrowserJob.status == "running",
    )


def publish_session_filter(cutoff: datetime):
    """publish_jobs 里算作"一次会话"的行:窗口内已发布 + 全部 publishing(在飞)。

    发布链在 account_worker 里直接调 ``sync_client.publish_once``,**不在 browser_jobs
    留痕**,不单独数就会漏掉最重的那类会话。``started_at`` 是会话开始时刻。
    """
    return or_(
        and_(
            PublishJob.status == "published",
            PublishJob.started_at >= cutoff,
        ),
        PublishJob.status == "publishing",
    )


# ---------------- 判据三:闸放不放行 ----------------


def layer_of(operator_id) -> str:
    """按触发方判该任务吃哪一层帽:operator_id>0 = 运营触发,其余 = 系统自发。"""
    return LAYER_OPERATOR if (operator_id and operator_id > 0) else LAYER_SYSTEM


def cap_allows(
    layer: str,
    *,
    budget: int,
    op_budget: int,
    session_cap: int,
    operator_session_cap: int,
) -> bool:
    """本层此刻放不放行一个任务:帽值 ≤0 = 该层关闭(运维逃生口),否则看剩余额度。"""
    if layer == LAYER_OPERATOR:
        return operator_session_cap <= 0 or op_budget > 0
    return session_cap <= 0 or budget > 0


# ---------------- 判据四:批次排序 ----------------


def norm_created(value) -> datetime:
    """把 created_at 归一成可排序 datetime(repo 侧可能给 ISO 字符串;缺失当最旧)。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.min


def queue_sort_key(created, source: str, job_id, operator_id: int) -> tuple:
    """派发批次的排序键:created_at 升序(oldest first),同刻按来源/id 稳定。

    第三位 ``job_id`` 在 browser(str)与 publish(int)之间类型不同,但只有前两位全相等
    才会比到它,而两族的 ``source`` 必不相同,故不会撞出 TypeError。
    """
    return (norm_created(created), source, job_id, operator_id)


# ---------------- 判据五:排期到没到点 ----------------


def not_before_of(payload: dict | None, job_id=None) -> datetime | None:
    """取 payload 里的 ``not_before`` 排期时刻;无 → None;值坏了也当 None(不卡死任务)。

    带时区偏移的值归一成 **naive UTC**(全仓的时间基准,与 publish 入口
    ``_parse_schedule_time`` 同款):库里的 created_at / utcnow 都是 naive,不归一的话
    aware 值一比较就是 TypeError。
    """
    raw = (payload or {}).get("not_before")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning(f"[browser_jobs] not_before 值非法,按立即可派处理 job_id={job_id}")
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def schedule_due(job: dict, now: datetime) -> bool:
    """排期是否到点:payload 无 ``not_before`` 视为立即可派;值坏了也放行。"""
    nb = not_before_of(job.get("payload"), job.get("id"))
    return nb is None or nb <= now


def publish_due_filter(now: datetime):
    """publish_jobs 的到期条件:schedule_time 与 next_retry_at 均为空或已到。"""
    return and_(
        or_(PublishJob.schedule_time.is_(None), PublishJob.schedule_time <= now),
        or_(PublishJob.next_retry_at.is_(None), PublishJob.next_retry_at <= now),
    )


# ---------------- 读侧:组装 queue 段 ----------------


def _utc(value: datetime | None) -> str | None:
    """naive UTC datetime → 带 ``+00:00`` 的 ISO 串(与 publish-jobs 的时间字段同款)。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


async def for_browser_job(row: dict, session=None) -> dict | None:
    """browser_jobs 台账行 → ``queue`` 段;非排队态 / 无账号任务 → None。

    无账号的 ``op_images`` 不排账号队列(supervisor 进程内直接执行),没有位次可言。
    """
    if row.get("status") != "queued" or row.get("account_id") is None:
        return None
    now = datetime.utcnow()
    return await _build(
        session=session,
        account_id=row["account_id"],
        operator_id=row.get("operator_id") or 0,
        source=SOURCE_BROWSER,
        job_id=row["id"],
        not_before=not_before_of(row.get("payload"), row.get("id")),
        now=now,
    )


async def for_browser_job_id(job_id: str) -> dict | None:
    """按 id 取台账行再组装(给手里只有 id、没有整行的轮询端点用,多一次主键点查)。"""
    async with get_session() as session:
        job = await session.get(BrowserJob, job_id)
        if job is None:
            return None
        row = {
            "id": job.id,
            "status": job.status,
            "account_id": job.account_id,
            "operator_id": job.operator_id,
            "created_at": job.created_at,
            "payload": _loads(job.payload),
        }
        return await for_browser_job(row, session)


async def for_publish_job(job: PublishJob, session=None) -> dict | None:
    """publish_jobs 行 → ``queue`` 段;非 pending → None。

    未到点的定时稿(schedule_time 在未来)也走这里:它同样是 pending,只是还没进待派
    队列,位次为 null、``detail.not_before`` 给到点时刻。
    """
    if job.status != "pending":
        return None
    now = datetime.utcnow()
    # 发布的"排期"由两列共同决定:定时发布时刻与重试退避时刻,取晚的那个当到点时刻
    scheduled = [t for t in (job.schedule_time, job.next_retry_at) if t is not None]
    return await _build(
        session=session,
        account_id=job.account_id,
        operator_id=job.created_by or 0,
        source=SOURCE_PUBLISH,
        job_id=job.id,
        not_before=max(scheduled) if scheduled else None,
        now=now,
    )


def _loads(raw):
    """payload JSON 串 → dict(坏值当空,排期判据自会按"立即可派"处理)。"""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


async def _build(*, session=None, **kwargs) -> dict:
    """组装 queue 段;调用方已有会话就复用(避免 sqlite 上无谓地再开一个)。"""
    if session is not None:
        return await _build_in(session, **kwargs)
    async with get_session() as own:
        return await _build_in(own, **kwargs)


async def _build_in(
    session,
    *,
    account_id: int,
    operator_id: int,
    source: str,
    job_id,
    not_before: datetime | None,
    now: datetime,
) -> dict:
    """组装一条排队任务的 queue 段。

    查询代价(全部单账号范围,轮询是高频调用):

    1. 该号 queued 的 browser_jobs(走 ``ix_browser_jobs_account_status``);
    2. 该号已到点 pending 的 publish_jobs(走 ``ix_publish_jobs_account_status``);
    3. 该号窗口内会话行(browser)—— 同一个 (account_id, status) 索引;
    4. 该号窗口内会话行(publish)—— 同上;
    5. 全局 running/publishing 的 account_id 去重(只按 status 过滤,行数 = 在跑的任务
       数,个位数)。

    共 5 条查询,除第 5 条外全部按账号收窄,没有全表扫。
    """
    cutoff = session_window_cutoff(now)
    queue = await _account_queue(session, account_id, now)
    running = await _running_of(session, account_id)
    events = await _session_events(session, account_id, cutoff)
    busy_accounts = await _busy_accounts(session)

    position = next(
        (i + 1 for i, (_key, src, jid) in enumerate(queue) if src == source and jid == job_id),
        None,
    )
    used = len(events)
    layer = layer_of(operator_id)
    session_cap = configured_session_cap()
    operator_session_cap = configured_operator_session_cap()
    max_procs = configured_max_procs()

    blocked_by: str | None = None
    detail: dict = {}
    if not_before is not None and not_before > now:
        # 排期未到:派发层这一轮根本看不到它,谈不上被哪道闸拦住(位次同为 null)。
        # 先判这条,否则一条排到半小时后的任务会被报成 account_busy —— 那是当下的账号
        # 状态,不是它在等的东西,运营会照着一个假原因去排查。
        detail = {"not_before": _utc(not_before)}
    elif running is not None:
        # 同号严格串行:该号已有子进程在跑,后面的一律等它跑完(派发层第一道 continue)
        blocked_by = BLOCKED_ACCOUNT_BUSY
        detail = {"running_id": running["id"], "running_kind": running["kind"]}
    elif len(busy_accounts) >= max_procs:
        # 全局子进程封顶,余下账号排队等下轮(派发层第二道 break)
        blocked_by = BLOCKED_GLOBAL_CONCURRENCY
        detail = {"running_procs": len(busy_accounts), "max_procs": max_procs}
    elif not cap_allows(
        layer,
        budget=session_cap - used,
        op_budget=operator_session_cap - used,
        session_cap=session_cap,
        operator_session_cap=operator_session_cap,
    ):
        cap = operator_session_cap if layer == LAYER_OPERATOR else session_cap
        blocked_by = BLOCKED_SESSION_CAP
        detail = {
            "used": used,
            "cap": cap,
            "kind_of_cap": layer,
            "window_resets_at": _utc(_window_resets_at(events, used, cap)),
        }

    return {
        "position": position,
        "ahead": None if position is None else position - 1,
        "account_queue_depth": len(queue),
        "running": running,
        "blocked_by": blocked_by,
        "detail": detail,
    }


async def _account_queue(session, account_id: int, now: datetime) -> list[tuple]:
    """该号**已到点**的待派队列,按派发层排序口径升序:[(排序键, 来源, job_id)]。

    browser 与 publish 合成一条队列,因为派发层就是把两者并进同一个 ``work[acc]`` 里
    一起排序、一起吃这一轮的名额 —— 分开数会让运营看到"前面 0 个"却仍在等发布。
    """
    browser_rows = (
        (
            await session.execute(
                select(
                    BrowserJob.id,
                    BrowserJob.created_at,
                    BrowserJob.operator_id,
                    BrowserJob.payload,
                )
                .where(BrowserJob.account_id == account_id)
                .where(BrowserJob.status == "queued")
            )
        )
        .all()
    )
    publish_rows = (
        (
            await session.execute(
                select(PublishJob.id, PublishJob.created_at, PublishJob.created_by)
                .where(PublishJob.account_id == account_id)
                .where(PublishJob.status == "pending")
                .where(publish_due_filter(now))
            )
        )
        .all()
    )
    items: list[tuple] = []
    for jid, created, op_id, payload in browser_rows:
        if not schedule_due({"payload": _loads(payload), "id": jid}, now):
            continue  # 排期未到:派发层这一轮根本看不到它,队列里也不该占位
        items.append(
            (queue_sort_key(created, SOURCE_BROWSER, jid, op_id or 0), SOURCE_BROWSER, jid)
        )
    for jid, created, created_by in publish_rows:
        items.append(
            (queue_sort_key(created, SOURCE_PUBLISH, jid, created_by or 0), SOURCE_PUBLISH, jid)
        )
    items.sort(key=lambda t: t[0])
    return items


async def _running_of(session, account_id: int) -> dict | None:
    """该号当前正在执行的任务(运营要的"当前执行的任务编号");没有则 None。

    ``started_at``:publish_jobs 有真列;browser_jobs **没有**开始时刻列,认领与心跳都写
    同一个 ``updated_at``,拿它冒充开始时刻会让人算错"跑了多久",故给 null,另附
    ``heartbeat_at``(执行方周期 touch,证明还活着)。
    """
    row = (
        await session.execute(
            select(BrowserJob.id, BrowserJob.kind, BrowserJob.heartbeat_at)
            .where(BrowserJob.account_id == account_id)
            .where(BrowserJob.status == "running")
            .order_by(BrowserJob.created_at)
            .limit(1)
        )
    ).first()
    if row is not None:
        return {
            "id": row[0],
            "kind": row[1],
            "started_at": None,
            "heartbeat_at": _utc(row[2]),
        }
    pub = (
        await session.execute(
            select(PublishJob.id, PublishJob.started_at)
            .where(PublishJob.account_id == account_id)
            .where(PublishJob.status == "publishing")
            .order_by(PublishJob.id)
            .limit(1)
        )
    ).first()
    if pub is not None:
        return {
            "id": pub[0],
            "kind": SOURCE_PUBLISH,
            "started_at": _utc(pub[1]),
            "heartbeat_at": None,
        }
    return None


async def _session_events(session, account_id: int, cutoff: datetime) -> list:
    """该号窗口内的会话事件时刻列表;在飞会话记 None(没有到期时刻)。

    ``len()`` 即派发层 ``_recent_session_counts`` 数出来的那个数 —— 两边共用
    ``browser_session_filter`` / ``publish_session_filter``,行集合逐行相同。
    """
    events: list = []
    browser_rows = (
        await session.execute(
            select(BrowserJob.status, BrowserJob.updated_at)
            .where(BrowserJob.account_id == account_id)
            .where(browser_session_filter(cutoff))
        )
    ).all()
    for status, updated_at in browser_rows:
        events.append(None if status == "running" else updated_at)
    publish_rows = (
        await session.execute(
            select(PublishJob.status, PublishJob.started_at)
            .where(PublishJob.account_id == account_id)
            .where(publish_session_filter(cutoff))
        )
    ).all()
    for status, started_at in publish_rows:
        events.append(None if status == "publishing" else started_at)
    return events


async def _busy_accounts(session) -> set:
    """库里有任务在跑的账号集合 —— worker ``self._procs`` 的等价代理(同号至多 1 个子进程)。"""
    busy = set()
    for row in (
        await session.execute(
            select(BrowserJob.account_id).where(BrowserJob.status == "running").distinct()
        )
    ).all():
        if row[0] is not None:
            busy.add(row[0])
    for row in (
        await session.execute(
            select(PublishJob.account_id).where(PublishJob.status == "publishing").distinct()
        )
    ).all():
        busy.add(row[0])
    return busy


def _window_resets_at(events: list, used: int, cap: int) -> datetime | None:
    """该层额度重新有位的时刻:要等第 ``used - cap + 1`` 早的那次会话滚出 60 分钟窗口。

    不是简单的"最早那次 + 1 小时" —— 超帽超了不止一格时(帽值调小、或运营层与系统层
    共用同一份计数),滚出一次还是不够,得滚出足够多次才回到 ``used < cap``。

    在飞会话(events 里的 None)没有到期时刻:要滚出的条数超过有时刻的条数时返回 None
    —— 那说明光等时间解不开,得先等在飞的那几次跑完。
    """
    need = used - cap + 1
    timed = sorted(t for t in events if t is not None)
    if need <= 0 or need > len(timed):
        return None
    return timed[need - 1] + timedelta(seconds=SESSION_WINDOW_SECONDS)
