"""笔记指标服务层:导出行 upsert(最新快照 + 每日趋势)+ RBAC 收窄的读取。

约定(与 account_service 一致):纯业务逻辑,用调用方传入的 AsyncSession——只 add/query/commit,
不自开引擎/事务边界。
- upsert_notes:SQLite 兼容的"先 select 唯一键、有则 update 无则 insert",不用 dialect-specific
  upsert。每行同时维护 NoteMetric(最新快照,updated_at=传入 now)与 NoteMetricDaily
  (按含 snapshot_date 的唯一键:当天覆盖、跨天加行);返回处理条数。
- list_notes / note_trend:经 assert_account_access(admin 全见,operator 仅授权号,无权抛
  AccessDenied),读最新快照 / 某笔记的 daily 升序序列。
- account_trends:面向数分 agent 的一次性分析包——账号级日汇总(含增量)+ 每篇笔记
  最新态/率值/日序列(含增量),口径自带说明(meta.field_notes)。
"""

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import assert_account_access
from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.operator import Operator
from app.models.xhs_account import XhsAccount

# 11 指标列:每次 upsert 从导出行覆盖到两表(缺列时保留模型默认 int 0 / float 0.0)。
_METRIC_FIELDS = (
    "likes",
    "collects",
    "comments",
    "danmu",
    "shares",
    "reposts",
    "follows",
    "exposure",
    "views",
    "cover_ctr",
    "avg_view_duration",
)


def _apply_metrics(obj, row: dict) -> None:
    """把导出行里出现的指标字段覆盖到 ORM 对象;缺失字段不动(留模型默认或旧值)。"""
    for field in _METRIC_FIELDS:
        if field in row:
            setattr(obj, field, row[field])


def _note_view(m: NoteMetric) -> dict:
    """把最新快照序列化为对外视图(account_id/title/publish_time + 11 指标 + updated_at)。"""
    view = {
        "account_id": m.account_id,
        "title": m.title,
        "publish_time": m.publish_time,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    for field in _METRIC_FIELDS:
        view[field] = getattr(m, field)
    return view


def _daily_view(d: NoteMetricDaily) -> dict:
    """把每日趋势行序列化为对外视图(含 snapshot_date + 11 指标)。"""
    view = {
        "account_id": d.account_id,
        "title": d.title,
        "publish_time": d.publish_time,
        "snapshot_date": d.snapshot_date,
    }
    for field in _METRIC_FIELDS:
        view[field] = getattr(d, field)
    return view


async def upsert_notes(
    session: AsyncSession,
    account_id: int,
    rows: list[dict],
    snapshot_date: str,
    now: datetime,
) -> int:
    """按唯一键 upsert 每行到 NoteMetric(最新快照)与 NoteMetricDaily(当天),返回处理条数。

    每行须含 title / publish_time;指标字段按 _METRIC_FIELDS 覆盖(缺列保留旧值/默认)。
    NoteMetric 唯一键 (account_id, title, publish_time)——重复导出覆盖成最新,updated_at=now;
    NoteMetricDaily 唯一键含 snapshot_date——当天重导覆盖、跨天加行。
    """
    for row in rows:
        title = row["title"]
        publish_time = row["publish_time"]

        # 最新快照:有则更新、无则插入
        snapshot = (
            await session.execute(
                select(NoteMetric).where(
                    NoteMetric.account_id == account_id,
                    NoteMetric.title == title,
                    NoteMetric.publish_time == publish_time,
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            snapshot = NoteMetric(
                account_id=account_id, title=title, publish_time=publish_time
            )
            session.add(snapshot)
        _apply_metrics(snapshot, row)
        snapshot.updated_at = now

        # 当天趋势行:同 snapshot_date 覆盖、跨天加行
        daily = (
            await session.execute(
                select(NoteMetricDaily).where(
                    NoteMetricDaily.account_id == account_id,
                    NoteMetricDaily.title == title,
                    NoteMetricDaily.publish_time == publish_time,
                    NoteMetricDaily.snapshot_date == snapshot_date,
                )
            )
        ).scalar_one_or_none()
        if daily is None:
            daily = NoteMetricDaily(
                account_id=account_id,
                title=title,
                publish_time=publish_time,
                snapshot_date=snapshot_date,
            )
            session.add(daily)
        _apply_metrics(daily, row)

    await session.commit()
    return len(rows)


async def list_notes(
    session: AsyncSession, operator: Operator, account_id: int
) -> list[dict]:
    """RBAC 收窄后按 id 升序读该号最新快照;operator 无授权抛 AccessDenied。"""
    await assert_account_access(operator, account_id, session)
    rows = (
        await session.execute(
            select(NoteMetric)
            .where(NoteMetric.account_id == account_id)
            .order_by(NoteMetric.id)
        )
    ).scalars().all()
    return [_note_view(m) for m in rows]


async def note_trend(
    session: AsyncSession,
    operator: Operator,
    account_id: int,
    title: str,
    publish_time: str,
) -> list[dict]:
    """RBAC 收窄后读某笔记的 daily 序列,按 snapshot_date 升序;无授权抛 AccessDenied。"""
    await assert_account_access(operator, account_id, session)
    rows = (
        await session.execute(
            select(NoteMetricDaily)
            .where(
                NoteMetricDaily.account_id == account_id,
                NoteMetricDaily.title == title,
                NoteMetricDaily.publish_time == publish_time,
            )
            .order_by(NoteMetricDaily.snapshot_date)
        )
    ).scalars().all()
    return [_daily_view(d) for d in rows]


# ─────────────────── 数分 agent 一次性分析包(account_trends)───────────────────

# 可求和的量指标(账号级日汇总口径;cover_ctr/avg_view_duration 是率/均值,不求和)
_SUMMABLE_FIELDS = (
    "exposure", "views", "likes", "collects", "comments", "shares", "follows",
)


def _parse_publish_date(publish_time: str):
    """从导出原文发布时间串解析日期(容忍「2026年05月09日…」与「2026-05-09 …」两种);失败 None。"""
    m = re.search(r"(\d{4})[年-](\d{1,2})[月-](\d{1,2})", publish_time or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None


def _delta(cur: dict, prev: dict | None) -> dict | None:
    """两快照日之间的增量(含间隔天数,供 agent 日均化);无上一快照返回 None。"""
    if prev is None:
        return None
    out = {f: (cur.get(f) or 0) - (prev.get(f) or 0) for f in _SUMMABLE_FIELDS}
    try:
        d_cur = datetime.strptime(cur["snapshot_date"], "%Y-%m-%d")
        d_prev = datetime.strptime(prev["snapshot_date"], "%Y-%m-%d")
        out["days_between"] = (d_cur - d_prev).days
    except (KeyError, ValueError):
        out["days_between"] = None
    return out


def _rates(m: dict) -> dict:
    """率值(分母 views,4 位小数;views=0 时全 None——新笔记零观看没有率可言)。"""
    views = m.get("views") or 0
    if views <= 0:
        return {k: None for k in
                ("like_rate", "collect_rate", "comment_rate", "engage_rate", "follow_rate")}
    likes, collects, comments = (m.get("likes") or 0), (m.get("collects") or 0), (m.get("comments") or 0)
    return {
        "like_rate": round(likes / views, 4),
        "collect_rate": round(collects / views, 4),
        "comment_rate": round(comments / views, 4),
        "engage_rate": round((likes + collects + comments) / views, 4),
        "follow_rate": round((m.get("follows") or 0) / views, 4),
    }


async def account_trends(
    session: AsyncSession, operator: Operator, account_id: int
) -> dict:
    """RBAC 收窄后返回该号的完整趋势分析包(一次拉取,数分 agent 免二次组装)。

    结构:account(账号态)+ meta(快照覆盖 + 口径说明)+ account_daily(账号级日汇总,
    含相邻快照增量)+ notes(每篇:最新态 + 率值 + 发布距今天数 + 日序列含增量,按最新
    views 降序)。所有指标是快照当日的**累计值**;增量 delta 是与上一快照日的差,带
    days_between 供日均化(快照可能断档,不能默认间隔 1 天)。
    """
    await assert_account_access(operator, account_id, session)
    account = await session.get(XhsAccount, account_id)

    daily_rows = (
        await session.execute(
            select(NoteMetricDaily)
            .where(NoteMetricDaily.account_id == account_id)
            .order_by(NoteMetricDaily.snapshot_date, NoteMetricDaily.id)
        )
    ).scalars().all()
    latest_rows = (
        await session.execute(
            select(NoteMetric).where(NoteMetric.account_id == account_id)
        )
    ).scalars().all()

    today = datetime.now(timezone.utc).date()
    snapshot_dates = sorted({d.snapshot_date for d in daily_rows})

    # 账号级日汇总:同快照日跨笔记求和 + note_count;再算相邻快照增量
    by_date: dict[str, dict] = {}
    for d in daily_rows:
        agg = by_date.setdefault(
            d.snapshot_date,
            {"snapshot_date": d.snapshot_date, "note_count": 0,
             **{f: 0 for f in _SUMMABLE_FIELDS}},
        )
        agg["note_count"] += 1
        for f in _SUMMABLE_FIELDS:
            agg[f] += getattr(d, f) or 0
    account_daily = []
    prev = None
    for date in snapshot_dates:
        cur = by_date[date]
        cur["delta"] = _delta(cur, prev)
        account_daily.append(cur)
        prev = cur

    # 每篇笔记:最新态 + 率值 + 日序列(含增量),按最新 views 降序
    series_by_note: dict[tuple, list] = {}
    for d in daily_rows:
        series_by_note.setdefault((d.title, d.publish_time), []).append(_daily_view(d))
    notes = []
    for m in latest_rows:
        latest = _note_view(m)
        series = series_by_note.get((m.title, m.publish_time), [])
        prev_row = None
        for row in series:
            row["delta"] = _delta(row, prev_row)
            prev_row = row
        pub_date = _parse_publish_date(m.publish_time)
        notes.append({
            "title": m.title,
            "publish_time": m.publish_time,
            "days_since_publish": (today - pub_date).days if pub_date else None,
            "latest": latest,
            "rates": _rates(latest),
            "series": series,
        })
    notes.sort(key=lambda n: n["latest"].get("views") or 0, reverse=True)

    return {
        "account": {
            "id": account_id,
            "name": account.name if account else None,
            "nickname": account.nickname if account else None,
            "cookie_status": account.cookie_status if account else None,
        },
        "meta": {
            "snapshot_dates": snapshot_dates,
            "latest_snapshot_date": snapshot_dates[-1] if snapshot_dates else None,
            "notes_tracked": len(notes),
            "field_notes": {
                "指标口径": "所有指标为快照日的累计值(非当日增量);exposure=曝光,views=观看,"
                          "cover_ctr=封面点击率(XHS 原生百分数),avg_view_duration=人均观看时长(秒)",
                "delta": "与上一快照日的差;days_between=两快照间隔天数(快照可能断档,"
                         "日均请除以 days_between,不要默认间隔 1 天)",
                "rates": "率值分母均为最新 views:engage_rate=(likes+collects+comments)/views;"
                         "views=0 时为 null",
                "排序": "notes 按最新 views 降序;series 按 snapshot_date 升序",
            },
        },
        "account_daily": account_daily,
        "notes": notes,
    }
