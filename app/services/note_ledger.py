"""发布笔记永久台账服务:发布成功钩子登记同步任务 + 契约 execute() + upsert/回连。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第 4.3 / 4.4 节。分层与
note_export / matrix_interact 一致(浏览器动作在 app.browser.creator_note_list,
本模块只管台账、落库与关联):

- ``schedule_note_ledger_sync``(sync,发布 published 钩子调,与 ``archive_published_job``
  同址同模式):给发布账号登记一条 ``browser_jobs``(kind=note_ledger_sync)。按
  ``source_publish_job_id`` 幂等;**绝不抛错**阻断发布终态,失败也不重试(下次同步兜住)。
  新发的笔记进列表可能有延迟,故排期 ``not_before`` 推后 ``SYNC_DELAY_SECONDS``~2 倍
  之间的随机时刻——靠落库排期而非进程内 sleep(干等会占死全局浏览器闸)。
- ``execute()`` 契约执行函数(account_worker 子进程消费):持号锁串行 → 浏览器闸 →
  线程内跑同步只读抓取 → upsert 台账,**不碰 browser_jobs 台账**(claim/finish 由调用方);
  任何异常收敛成 ``{"error": reason}``,**绝不抛出**。
- ``note_ledger_sync`` 是纯只读抓取 + upsert,**幂等**,在
  ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里(僵死后可自动重跑)。

**关联不上就留 NULL,绝不猜**:回连 publish_jobs 只认"账号内标题唯一命中"——本批里
标题重复的、标题为空的、库里同标题多条 published 的、以及目标 job 已被别的台账行认领的,
一律不连。回填 ``publish_jobs.note_id`` / ``published_at`` 同样只在唯一命中时做。
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
from app.models.content_archive import ContentArchive
from app.models.publish_job import PublishJob
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# 发布成功后延后多久同步台账(秒):笔记进创作中心列表有延迟,立刻拉大概率抓不到本篇。
# 抓不到也只是这次少一行,下次同步会补上——故取一个宽松值,不做重试。
SYNC_DELAY_SECONDS = 300

_NOTE_URL_BASE = "https://www.xiaohongshu.com/explore/"


# ---------------- 发布成功钩子(登记同步任务)----------------


def schedule_note_ledger_sync(db_path: str, publish_job_id: int) -> Optional[str]:
    """为发布账号登记一条笔记台账同步任务;返回 job id(未登记则 None)。

    幂等:该 publish job 已登记过则跳过。**绝不抛错**——发布终态已先行落库,登记是
    事后副作用,任何异常只告警(与 ``archive_published_job`` 同款纪律)。
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
            # 排期靠落库,派发侧(list_dispatchable)按它过滤未到点的行
            "not_before": (
                datetime.utcnow()
                + timedelta(seconds=random.uniform(SYNC_DELAY_SECONDS, SYNC_DELAY_SECONDS * 2))
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


# ---------------- 契约执行(account_worker 子进程消费)----------------


async def execute(account_id: int, payload: dict) -> dict:
    """同步一次该账号的发布笔记台账(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"note_count","inserted","updated","linked"}``;抓取失败 / 任何异常
    → ``{"error": reason}``,**不抛出**、**不落半截数据**(抓取成功才落库)。
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
            stats = await upsert_notes(session, account_id, notes, now)
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


# ---------------- upsert + 回连 ----------------


def _safe_int(value: Any) -> int:
    """互动快照安全转整数:None / 非数值 / 空串 → 0(容忍千分位逗号与浮点串)。"""
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def published_at_of(raw: dict) -> Optional[datetime]:
    """``visible_time``(unix 秒)→ naive UTC datetime;缺失/非法返回 None。

    只认 visible_time:另一个 ``time`` 字段("2025-09-25 17:15")是北京时区的展示串,
    要用它就得假设时区,而台账其余时间列全是 naive UTC——宁可留空也不猜一个时区。
    """
    raw_value = raw.get("visible_time")
    if raw_value in (None, ""):
        return None
    try:
        # 存 naive UTC(与库内其余时间列同基准),不是 utcfromtimestamp(已弃用)
        return datetime.fromtimestamp(int(raw_value), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        logger.warning(f"[note_ledger] visible_time 非法,published_at 留空: {raw_value!r}")
        return None


def _note_url(note_id: str, xsec_token: Optional[str], xsec_source: Optional[str]) -> str:
    """拼完整可访问链接(带 xsec 两件套;没有 token 时退化成裸 explore 链接)。"""
    params = [(k, v) for k, v in
              (("xsec_token", xsec_token), ("xsec_source", xsec_source)) if v]
    url = f"{_NOTE_URL_BASE}{note_id}"
    return f"{url}?{urlencode(params)}" if params else url


def _ambiguous_titles(notes: list[dict]) -> set[str]:
    """本批中出现 ≥2 次的标题:这些笔记彼此无法靠标题区分,一律不做回连。"""
    counts: dict[str, int] = {}
    for note in notes:
        title = (note.get("display_title") or "").strip()
        if title:
            counts[title] = counts.get(title, 0) + 1
    return {title for title, n in counts.items() if n > 1}


async def _match_publish_job(
    session, account_id: int, title: str
) -> Optional[PublishJob]:
    """按 (account_id, 标题) 找唯一一条 published 发布任务;非唯一/已被占用 → None。

    - 同标题命中多条:无法区分是哪一篇,不猜;
    - 命中的 job 已被别的台账行认领:说明两篇笔记同标题,同样无法区分,不抢。
    """
    jobs = (
        await session.execute(
            select(PublishJob).where(
                PublishJob.account_id == account_id,
                PublishJob.status == "published",
                PublishJob.title == title,
            )
        )
    ).scalars().all()
    if len(jobs) != 1:
        return None
    job = jobs[0]
    taken = await session.scalar(
        select(PublishedNote.id).where(PublishedNote.source_publish_job_id == job.id)
    )
    return None if taken is not None else job


async def _match_archive(session, publish_job_id: int) -> Optional[int]:
    """按发布任务找归档行 id;没有归档返回 None。

    只走"经发布任务"这一条路:每条归档都源自一条 publish job,再按标题单独匹一遍归档
    不会多命中(歧义与 job 侧完全同构),只会多一条猜的路径。
    """
    return await session.scalar(
        select(ContentArchive.id).where(
            ContentArchive.source_publish_job_id == publish_job_id
        )
    )


async def upsert_notes(
    session, account_id: int, notes: list[dict], now: datetime
) -> dict:
    """按 (account_id, note_id) upsert 台账行 + 尽力回连回填;返回计数字典。

    已存在的行刷新 ``last_synced_at`` / 互动快照 / title / url / 体裁 / 发布时间,
    **不动 ``first_seen_at``**。无 id 的条目跳过(台账幂等键都没有,无从落行)。

    回连只在**首次连上前**尝试(已连过的不重找),且必须唯一命中,否则留 NULL。
    """
    ambiguous = _ambiguous_titles(notes)
    inserted = updated = linked = 0

    for raw in notes:
        note_id = (raw.get("id") or "").strip()
        if not note_id:
            continue
        row = (
            await session.execute(
                select(PublishedNote).where(
                    PublishedNote.account_id == account_id,
                    PublishedNote.note_id == note_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = PublishedNote(
                account_id=account_id, note_id=note_id, first_seen_at=now
            )
            session.add(row)
            inserted += 1
        else:
            updated += 1

        title = raw.get("display_title") or ""
        xsec_token = raw.get("xsec_token") or None
        xsec_source = raw.get("xsec_source") or None
        row.title = title
        row.xsec_token = xsec_token
        row.xsec_source = xsec_source
        row.note_url = _note_url(note_id, xsec_token, xsec_source)
        row.note_type = raw.get("type") or None
        row.published_at = published_at_of(raw)
        row.likes = _safe_int(raw.get("likes"))
        row.collects = _safe_int(raw.get("collected_count"))
        row.comments = _safe_int(raw.get("comments_count"))
        row.shares = _safe_int(raw.get("shared_count"))
        row.views = _safe_int(raw.get("view_count"))
        row.last_synced_at = now

        stripped = title.strip()
        if row.source_publish_job_id is None and stripped and stripped not in ambiguous:
            job = await _match_publish_job(session, account_id, stripped)
            if job is not None:
                row.source_publish_job_id = job.id
                # 回填发布任务:唯一命中才做,且不覆盖已有值
                if not (job.note_id or "").strip():
                    job.note_id = note_id
                if job.published_at is None and row.published_at is not None:
                    job.published_at = row.published_at
                linked += 1
        if row.content_archive_id is None and row.source_publish_job_id is not None:
            row.content_archive_id = await _match_archive(
                session, row.source_publish_job_id
            )

    await session.commit()
    logger.info(
        f"[note_ledger] 账号{account_id} 台账同步:新增 {inserted} / 刷新 {updated} / "
        f"回连发布任务 {linked}"
    )
    return {"inserted": inserted, "updated": updated, "linked": linked}
