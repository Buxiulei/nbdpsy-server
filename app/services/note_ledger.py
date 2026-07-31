"""发布笔记永久台账服务:发布当场落行(T0)+ 列表同步补 id(T1)+ 定时纠正(T2)。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第 4.1.1 / 4.4 / 4.5 节。

**主写路径是 T0,不是同步**。发布成功那一刻我们手里已经有标题、正文、媒体、生成用户、
生成时间,当场就该落库;note_id 与平台时间才是当场拿不到、只能事后补的那部分,不该让
整条台账记录等它。三段时序:

- **T0 ``record_published_note``**(sync,发布 published 钩子调,与 ``archive_published_job``
  同址):**纯 DB 写入,不碰浏览器**,写死 account_id / title / published_at(本机时钟)/
  generated_at / operator_id / source_publish_job_id / content_archive_id,
  平台侧字段留空、``sync_status='pending_id'``。按 source_publish_job_id 幂等。
- **T1 ``schedule_note_ledger_sync``**(同址,**必须排在 T0 之后**——台账行要先于同步
  存在,否则同步回来的数据没有落点):登记一条 ``note_ledger_sync`` 去补 id。笔记进列表
  有延迟,T1 匹配不到是常态,留着 pending_id 交给 T2,不重试不阻断。
- **T2 ``execute`` → ``sync_notes``**:全量同步,补 pending_id、刷互动快照、纠正
  title(运营可能在平台改过标题)、列表有台账没有的建 orphan 行;**台账有、列表里查不到
  的只记录不删**——笔记可能被删或被限流,台账是历史事实的记录,不因平台侧消失而抹掉。

两个钩子都幂等、都**绝不抛错**阻断发布终态。``execute`` 沿用 note_export 的收敛纪律
(号锁 + browser_slot 闸 + 异常收敛成 ``{"error"}`` 绝不上抛)。

**匹配不上就留着,绝不猜**:把列表里的笔记认到某条 pending_id 台账行上,要求标题相等
**且**平台时间落在 ``MATCH_WINDOW_SECONDS`` 内**且**候选唯一;一条 pending 行被认走后
不再参与后续匹配。任何一条不满足就不认——宁可留着 pending_id 等下次,也不认错笔记。
"""

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.creator_note_list import CreatorNoteListError, fetch_posted_notes
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.publish_job import PublishJob
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# 发布成功后延后多久跑 T1 同步(秒):笔记进创作中心列表有延迟,立刻拉大概率抓不到本篇。
# 抓不到也只是这次少补一条 id,T2 定时同步会兜住——故取宽松值且不做重试。
SYNC_DELAY_SECONDS = 300

# 把列表笔记认到 pending_id 台账行上时,平台发布时间与我们记的 published_at 允许的最大
# 偏差(秒)。两者本就有分钟级差异(T0 是发布成功那一刻的本机时钟,visible_time 是平台
# 侧时刻),取 30 分钟足够宽容;同标题笔记若隔了半小时以上,就不该被认成同一篇。
MATCH_WINDOW_SECONDS = 1800

_NOTE_URL_BASE = "https://www.xiaohongshu.com/explore/"


# ---------------- T0:发布成功当场落台账行(主写路径)----------------


def record_published_note(db_path: str, publish_job_id: int) -> Optional[int]:
    """发布成功当场建一行台账;返回台账行 id(已存在或失败返回 None)。

    **纯 DB 写入,不碰浏览器**:此刻已知的内容侧信息全部写死,平台侧字段留空等同步补。
    幂等:同一 source_publish_job_id 已有行则跳过。**绝不抛错**——发布终态已先行落库,
    建台账是事后副作用,任何异常只告警(与 ``archive_published_job`` 同款纪律)。
    """
    try:
        now = datetime.utcnow()
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT id, account_id, title, created_by, created_at"
                " FROM publish_jobs WHERE id=?",
                (publish_job_id,),
            ).fetchone()
            if job is None:
                logger.warning(f"[note_ledger] 发布 job={publish_job_id} 不存在,不建台账行")
                return None
            dup = conn.execute(
                "SELECT id FROM published_notes WHERE source_publish_job_id=?",
                (publish_job_id,),
            ).fetchone()
            if dup is not None:
                logger.info(f"[note_ledger] 发布 job={publish_job_id} 已有台账行,跳过")
                return None
            # 正文与媒体在归档那边,台账只存指针;归档失败(或还没落)则留 NULL
            archive = conn.execute(
                "SELECT id FROM content_archive WHERE source_publish_job_id=?",
                (publish_job_id,),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO published_notes"
                "(account_id,note_id,xsec_token,xsec_source,note_url,note_type,"
                " platform_published_at,title,published_at,generated_at,operator_id,"
                " source_publish_job_id,content_archive_id,sync_status,"
                " first_seen_at,last_synced_at,likes,collects,comments,shares,views)"
                " VALUES (?,NULL,NULL,NULL,NULL,NULL,NULL,?,?,?,?,?,?,'pending_id',"
                " ?,?,0,0,0,0,0)",
                (
                    job["account_id"],
                    job["title"] or "",
                    _fmt(now),
                    job["created_at"],
                    job["created_by"],
                    publish_job_id,
                    archive["id"] if archive is not None else None,
                    _fmt(now),
                    _fmt(now),
                ),
            )
            note_row_id = cur.lastrowid
            conn.commit()
        logger.info(
            f"[note_ledger] 发布 job={publish_job_id} 已落台账行 {note_row_id}"
            f"(账号 {job['account_id']},待补 note_id)"
        )
        return note_row_id
    except Exception as exc:  # noqa: BLE001 — 建台账绝不阻断发布终态
        logger.warning(
            f"[note_ledger] 建台账行失败 job={publish_job_id}(忽略,不阻断发布): {exc}"
        )
        return None


def _fmt(dt: datetime) -> str:
    """与 SQLAlchemy DateTime 落库格式一致的 UTC 串(sync 侧用)。"""
    return dt.isoformat(sep=" ")


# ---------------- T1:登记补 id 的同步任务 ----------------


def schedule_note_ledger_sync(db_path: str, publish_job_id: int) -> Optional[str]:
    """登记一条笔记台账同步任务(补 note_id);返回 job id(未登记则 None)。

    **必须在 ``record_published_note`` 之后调用**:台账行要先于同步存在。
    幂等:该 publish job 已登记过则跳过。**绝不抛错**(同 T0 纪律)。
    """
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT id, account_id FROM publish_jobs WHERE id=?", (publish_job_id,)
            ).fetchone()
            if job is None:
                logger.warning(
                    f"[note_ledger] 发布 job={publish_job_id} 不存在,不登记台账同步"
                )
                return None
            dup = conn.execute(
                "SELECT id FROM browser_jobs WHERE kind='note_ledger_sync'"
                " AND json_extract(payload, '$.source_publish_job_id')=? LIMIT 1",
                (publish_job_id,),
            ).fetchone()
            if dup is not None:
                logger.info(
                    f"[note_ledger] 发布 job={publish_job_id} 已登记过台账同步,跳过"
                )
                return None

        payload = {
            "source_publish_job_id": publish_job_id,
            # 排期靠落库,派发侧(list_dispatchable)按它过滤未到点的行;执行方不 sleep 干等
            "not_before": (
                datetime.utcnow()
                + timedelta(
                    seconds=random.uniform(SYNC_DELAY_SECONDS, SYNC_DELAY_SECONDS * 2)
                )
            ).isoformat(sep=" "),
        }
        # operator_id=0(非请求上下文的进程内直调):不占运营的未终态配额
        job_id = browser_jobs_repo.enqueue_sync(
            db_path, "note_ledger_sync", payload, 0, account_id=job["account_id"]
        )
        logger.info(
            f"[note_ledger] 发布 job={publish_job_id} 已登记台账同步任务 {job_id}"
            f"(账号 {job['account_id']})"
        )
        return job_id
    except Exception as exc:  # noqa: BLE001 — 登记绝不阻断发布终态
        logger.warning(
            f"[note_ledger] 登记台账同步失败 job={publish_job_id}(忽略,不阻断发布): {exc}"
        )
        return None


# ---------------- T1/T2:契约执行(account_worker 子进程消费)----------------


async def execute(account_id: int, payload: dict) -> dict:
    """同步一次该账号的笔记列表并纠正台账(契约函数,不碰 browser_jobs 台账)。

    成功返回同步计数;抓取失败 / 任何异常 → ``{"error": reason}``,**不抛出**、
    **不落半截数据**(抓取成功才落库)。同步失败不影响已有台账行——T0 已经把内容侧
    信息完整落库,这里只是补平台侧字段。
    """
    now = datetime.utcnow()
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过台账同步"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含落库)。
            async with browser_slot():
                notes = await asyncio.to_thread(_fetch_sync, account_id, cookies)
        # 抓取成功才落库:用 get_session()(测试对 async_session monkeypatch 生效)。
        async with get_session() as session:
            stats = await sync_notes(session, account_id, notes, now)
        return {"note_count": len(notes), **stats}
    except CreatorNoteListError as exc:
        logger.warning(f"笔记台账同步失败 account_id={account_id} reason={exc.reason}")
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"笔记台账同步任务异常 account_id={account_id}")
        return {"error": f"笔记台账同步任务异常:{exc}"}


def _fetch_sync(account_id: int, cookies: list[dict]) -> list[dict]:
    """同一线程内:建 SyncClient → start → 只读抓列表 → stop 收尾(finally 防泄漏)。

    headed 真屏沿用 SyncClient 默认;纯只读抓取不看图,block_images 省内存(同 note_export)。
    """
    client = SyncClient(account_id, cookies, block_images=True)
    try:
        start = client.start()
        if not start.get("success"):
            raise CreatorNoteListError(f"browser_start_failed: {start.get('error')}")
        return fetch_posted_notes(client.page, account_id)
    finally:
        client.stop()


# ---------------- 同步落库 ----------------


def _safe_int(value: Any) -> int:
    """互动快照安全转整数:None / 非数值 / 空串 → 0(容忍千分位逗号与浮点串)。"""
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def platform_published_at_of(raw: dict) -> Optional[datetime]:
    """``visible_time``(unix 秒)→ naive UTC datetime;缺失/非法返回 None。

    只认 visible_time:另一个 ``time`` 字段("2025-09-25 17:15")是北京时区的展示串,
    要用它就得假设时区,而库内时间列全是 naive UTC——宁可留空也不猜一个时区。
    """
    raw_value = raw.get("visible_time")
    if raw_value in (None, ""):
        return None
    try:
        # 存 naive UTC(与库内其余时间列同基准),不是 utcfromtimestamp(已弃用)
        return datetime.fromtimestamp(int(raw_value), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        logger.warning(
            f"[note_ledger] visible_time 非法,platform_published_at 留空: {raw_value!r}"
        )
        return None


def _note_url(note_id: str, xsec_token: Optional[str], xsec_source: Optional[str]) -> str:
    """拼完整可访问链接(带 xsec 两件套;没有 token 时退化成裸 explore 链接)。"""
    params = [(k, v) for k, v in
              (("xsec_token", xsec_token), ("xsec_source", xsec_source)) if v]
    url = f"{_NOTE_URL_BASE}{note_id}"
    return f"{url}?{urlencode(params)}" if params else url


def _apply_platform_fields(row: PublishedNote, raw: dict, now: datetime) -> None:
    """把列表接口的平台侧字段覆盖到台账行(含用 display_title 纠正标题)。"""
    note_id = (raw.get("id") or "").strip()
    xsec_token = raw.get("xsec_token") or None
    xsec_source = raw.get("xsec_source") or None
    row.note_id = note_id
    row.xsec_token = xsec_token
    row.xsec_source = xsec_source
    row.note_url = _note_url(note_id, xsec_token, xsec_source)
    row.note_type = raw.get("type") or None
    row.platform_published_at = platform_published_at_of(raw)
    # 平台标题是权威展示值:运营可能在平台改过,同步时纠正过来。但**平台给空串时不覆盖**
    # ——接口实测存在 display_title 为空的笔记,拿空串盖掉 T0 记下的真标题等于把内容侧
    # 唯一的文字信息抹了;T0 那份是保底,只在平台确实给了标题时才纠正。
    platform_title = raw.get("display_title") or ""
    if platform_title or not (row.title or ""):
        row.title = platform_title
    row.likes = _safe_int(raw.get("likes"))
    row.collects = _safe_int(raw.get("collected_count"))
    row.comments = _safe_int(raw.get("comments_count"))
    row.shares = _safe_int(raw.get("shared_count"))
    row.views = _safe_int(raw.get("view_count"))
    row.last_synced_at = now


def _pick_pending(
    pending: list[PublishedNote], raw: dict, platform_at: Optional[datetime]
) -> tuple[Optional[PublishedNote], bool]:
    """在待补 id 的台账行里找这篇列表笔记的归属。

    返回 ``(认定的行, 是否认不准)``:

    - 标题非空且相等、平台时间(已知时)落在 ``MATCH_WINDOW_SECONDS`` 内、候选唯一
      → 认定该行;
    - 候选多于一条(同标题多篇待补,时间也都在窗口内)→ ``(None, True)`` **认不准**:
      这篇多半就是我们发的,只是分不清是哪一条 pending。此时既不认也**不建 orphan 行**
      ——建了就是往台账里塞一条明知是假的"非本系统发布",留着 pending 等下次更干净;
    - 一条候选都没有(标题对不上,或同标题但时间差得远 = 另一篇同名笔记)
      → ``(None, False)``,按 orphan 建行。
    """
    title = (raw.get("display_title") or "").strip()
    if not title:
        return None, False
    candidates = [row for row in pending if (row.title or "").strip() == title]
    if platform_at is not None:
        candidates = [
            row
            for row in candidates
            if abs((row.published_at - platform_at).total_seconds())
            <= MATCH_WINDOW_SECONDS
        ]
    if len(candidates) == 1:
        return candidates[0], False
    return None, len(candidates) > 1


async def sync_notes(session, account_id: int, notes: list[dict], now: datetime) -> dict:
    """把一次列表抓取的结果同步进台账;返回计数字典。

    四条路径:
    - 台账里已有该 note_id → 刷新平台侧字段与互动快照(纠正 title);
    - 匹配上某条 pending_id 行 → 补平台侧字段,置 linked,并回填该行对应的
      ``publish_jobs.note_id`` / ``published_at``(行自带 job id,无需靠标题猜);
    - 同标题 pending 有多条、认不准 → 什么都不做(既不认也不建 orphan,见 ``_pick_pending``);
    - 确实不是我们发的 → 建 orphan 行(``source_publish_job_id=NULL``,只有平台侧信息);
    - **台账里有、列表里查不到的 → 只记日志不删行**(笔记可能被删/被限流,台账是历史
      事实的记录)。
    """
    rows = (
        await session.execute(
            select(PublishedNote).where(PublishedNote.account_id == account_id)
        )
    ).scalars().all()
    by_note_id = {r.note_id: r for r in rows if r.note_id}
    pending = [r for r in rows if not r.note_id]

    refreshed = linked = orphan = ambiguous = 0
    seen_ids: set[str] = set()

    for raw in notes:
        note_id = (raw.get("id") or "").strip()
        if not note_id:
            continue  # 无 id 落不了台账幂等键(抓取层已告警)
        seen_ids.add(note_id)
        row = by_note_id.get(note_id)
        if row is not None:
            _apply_platform_fields(row, raw, now)
            refreshed += 1
            continue

        platform_at = platform_published_at_of(raw)
        row, unsure = _pick_pending(pending, raw, platform_at)
        if row is not None:
            pending.remove(row)  # 一条 pending 只能被认走一次
            _apply_platform_fields(row, raw, now)
            row.sync_status = "linked"
            await _backfill_publish_job(session, row)
            linked += 1
        elif unsure:
            # 同标题的 pending 不止一条:认不准就都不动,也不建 orphan(见 _pick_pending)
            ambiguous += 1
        else:
            row = PublishedNote(
                account_id=account_id,
                # 非本系统发布:没有 T0 时刻可用,退而用平台时间;连它都没有就记同步时刻
                published_at=platform_at or now,
                sync_status="orphan",
                first_seen_at=now,
            )
            _apply_platform_fields(row, raw, now)
            session.add(row)
            orphan += 1

    missing = [r for r in rows if r.note_id and r.note_id not in seen_ids]
    if missing:
        # 只记录不删:笔记可能被删或被限流,台账是历史事实的记录,不因平台侧消失而抹掉
        logger.warning(
            f"[note_ledger] 账号{account_id} 有 {len(missing)} 条台账笔记未出现在列表里"
            f"(可能被删/被限流,行保留不删): {[r.note_id for r in missing][:10]}"
        )

    await session.commit()
    stats = {
        "refreshed": refreshed,
        "linked": linked,
        "orphan": orphan,
        "ambiguous": ambiguous,
        "pending_remaining": len(pending),
        "missing": len(missing),
    }
    logger.info(f"[note_ledger] 账号{account_id} 台账同步:{stats}")
    return stats


async def _backfill_publish_job(session, row: PublishedNote) -> None:
    """把补到的 note_id 回填给台账行自己的发布任务;已有值不覆盖。

    这里没有任何猜测成分:台账行是 T0 由那条发布任务亲自建的,``source_publish_job_id``
    就是权威归属,不需要拿标题去匹 publish_jobs。
    """
    if row.source_publish_job_id is None:
        return
    job = await session.get(PublishJob, row.source_publish_job_id)
    if job is None:
        return
    if not (job.note_id or "").strip():
        job.note_id = row.note_id
    if job.published_at is None:
        job.published_at = row.published_at
