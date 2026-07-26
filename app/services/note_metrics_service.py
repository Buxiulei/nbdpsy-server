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
    """率值(分母 views,4 位小数;views=0 时全 None——新笔记零观看没有率可言)。

    follow_rate_t1 需要历史快照,这里算不了,恒定给 None 占位——由 account_trends 覆盖成真实值。
    恒定带这个键是为了让消费方看到的字段集合稳定:拿不到就是 null,而不是"这个键时有时无"。
    """
    views = m.get("views") or 0
    if views <= 0:
        return {k: None for k in
                ("like_rate", "collect_rate", "comment_rate", "engage_rate",
                 "follow_rate", "follow_rate_t1")}
    likes, collects, comments = (m.get("likes") or 0), (m.get("collects") or 0), (m.get("comments") or 0)
    return {
        "like_rate": round(likes / views, 4),
        "collect_rate": round(collects / views, 4),
        "comment_rate": round(comments / views, 4),
        "engage_rate": round((likes + collects + comments) / views, 4),
        "follow_rate": round((m.get("follows") or 0) / views, 4),
        "follow_rate_t1": None,
    }


def _follow_rate_t1(latest: dict, series: list[dict]) -> float | None:
    """同窗涨粉率:follows(T-1)÷ 上一快照日的 views(≈截至昨日的观看),对齐分子分母口径。

    series 已按 snapshot_date 升序;分母取"快照日严格早于最新快照日"的最近一行的 views。
    没有更早快照、或该 views<=0 时返回 None(没有可用同窗分母,不猜)。
    """
    if len(series) < 2:
        return None
    latest_date = series[-1]["snapshot_date"]
    prev_views = next(
        (row.get("views") for row in reversed(series[:-1])
         if row["snapshot_date"] < latest_date),
        None,
    )
    if prev_views is None or prev_views <= 0:
        return None
    return round((latest.get("follows") or 0) / prev_views, 4)


# ── 逐字段口径元数据(随数据下发,LLM 数分 agent 直接读,不必外部文档)──────────
#
# **纪律:desc/window 只写在创作中心「内容分析」表头悬停 ⓘ **抄来的官方原文**;
# 抄不到的一律 window="unknown" + desc=None,绝不猜——猜一个 T-1 比留空更危险,
# agent 会当真据此推算(这正是 cover_ctr 那类误判的成因)。
#
# window 取值:"T"=实时(截止目前) / "T-1"=每天更新(截至昨日) / "unknown"=未核实
# source 取值:"platform"=平台导出原生列 / "derived"=我们算的
_FIELD_META: dict = {
    # ── 平台原生列(2026-07-26 逐个悬停 ⓘ 抄录)──
    "exposure": {
        "label": "曝光", "unit": "次", "window": "T-1", "source": "platform",
        "desc": "每天更新数据,该篇笔记截至昨日获得的曝光数之和",
    },
    "views": {
        "label": "观看", "unit": "次", "window": "T", "source": "platform",
        "desc": "实时更新数据,笔记截止目前的观看数",
    },
    "cover_ctr": {
        "label": "封面点击率", "unit": "百分数(如 18.7 表示 18.7%)",
        "window": "T-1", "source": "platform",
        "desc": "每天更新数据,该篇笔记截至昨日的封面点击次数÷封面曝光次数(视频下滑不计入)",
    },
    "likes": {
        "label": "点赞", "unit": "个", "window": "T", "source": "platform",
        "desc": "实时更新数据,笔记截止目前的点赞数",
    },
    "comments": {
        "label": "评论", "unit": "条", "window": "T", "source": "platform",
        "desc": "实时更新数据,笔记截止目前的评论数",
    },
    "collects": {
        "label": "收藏", "unit": "个", "window": "T", "source": "platform",
        "desc": "实时更新数据,笔记截止目前的收藏数",
    },
    "follows": {
        "label": "涨粉", "unit": "人", "window": "T-1", "source": "platform",
        "desc": "每天更新数据,当前作品截至目前每日新增直接涨粉数的加和",
    },
    "shares": {
        "label": "分享", "unit": "次", "window": "T", "source": "platform",
        "desc": "实时更新数据,当前作品截至目前累计分享数",
    },
    "avg_view_duration": {
        "label": "人均观看时长", "unit": "秒", "window": "T-1", "source": "platform",
        "desc": "每天更新数据,笔记被观看的平均时长",
    },
    "danmu": {
        "label": "弹幕", "unit": "条", "window": "T", "source": "platform",
        "desc": "实时更新数据,笔记截止目前的弹幕数(竖屏视频弹幕来自老版本用户)",
    },
    "reposts": {
        "label": "转载", "unit": "次", "window": "unknown", "source": "platform",
        "desc": "当前导出表中无此列,恒为 0(保留字段,勿据此分析)",
    },
    # ── 我们算的派生字段 ──
    "like_rate": {
        "label": "点赞率(派生)", "unit": "比率(0-1)", "window": "T", "source": "derived",
        "desc": "likes / views;分子分母同为实时 T,口径一致",
    },
    "collect_rate": {
        "label": "收藏率(派生)", "unit": "比率(0-1)", "window": "T", "source": "derived",
        "desc": "collects / views;分子分母同为实时 T,口径一致",
    },
    "comment_rate": {
        "label": "评论率(派生)", "unit": "比率(0-1)", "window": "T", "source": "derived",
        "desc": "comments / views;分子分母同为实时 T,口径一致",
    },
    "engage_rate": {
        "label": "互动率(派生)", "unit": "比率(0-1)", "window": "T", "source": "derived",
        "desc": "(likes+collects+comments) / views;四者同为实时 T,口径一致。"
                "已核实同窗,不存在 follow_rate 那类跨窗问题,可直接当互动转化率用。",
    },
    "follow_rate": {
        "label": "涨粉率(派生)", "unit": "比率(0-1)", "window": "混合(T-1/T)",
        "source": "derived",
        "desc": "follows(T-1,每天更新,截至昨日) / views(T,实时,含今日)——**分子分母不同期**:"
                "分母含今日观看、分子不含今日涨粉,所以**只会偏低、不会偏高**,笔记越新偏得越多。"
                "实测偏低幅度(生产库 2026-07-25/07-26 两个连续快照日、78 条笔记,"
                "以「当日新增观看÷累计观看」度量,该比例即本字段相对同窗算法偏低的幅度):"
                "发布 0-2 天 中位 5.3%/最大 5.3%(**n=1,单点,不能当结论**);"
                "3-6 天 中位 4.8%/最大 18.2%(**n=4,样本少**);"
                "14-29 天 中位 0.0%/最大 5.9%(n=4);30 天+ 中位 0.0%/最大 4.8%(n=69)。"
                "7-13 天档无样本。样本严重偏老(78 条里 69 条是 30 天+),新笔记两档证据弱。"
                "**发布当天/次日的值根本不能用**:follows 是 T-1 口径,还没覆盖到发布日,"
                "本值必然趋近 0——这不是「偏低」而是「无意义」,不要解读成涨粉能力差。"
                "发布约两周以上:实测中位偏差 0%、最大 6%,可直接用。"
                "要同窗口径请改用 follow_rate_t1。",
    },
    "follow_rate_t1": {
        "label": "涨粉率·同窗(派生)", "unit": "比率(0-1)", "window": "T-1",
        "source": "derived",
        "desc": "同窗版涨粉率:follows(T-1,截至昨日) / **上一快照日**的 views"
                "(≈截至昨日的观看),分子分母都对齐到昨日,规避 follow_rate 的跨窗偏低。"
                "**不是精确值**:上一快照是昨天**某个时刻**采的(观看数只截到那一刻),"
                "而 follows 覆盖到昨日 24:00,分子比分母多覆盖几小时 → 本值会**轻微偏高**。"
                "实测与 follow_rate 之差:老笔记 +0.0%~+0.9%,一条 2 天新笔记 +5.6%。"
                "⚠️ 上述「几小时」是按**上一快照日就是昨天**估的;快照会断档"
                "(生产上出现过 07-23 → 07-25 隔两天),此时分母停在更早那天而分子仍到昨日,"
                "偏高幅度按间隔天数放大。间隔看 series 里的 snapshot_date 与 delta.days_between。"
                "该笔记没有更早的快照日(或上一快照日 views<=0)时为 null。",
    },
    "delta": {
        "label": "相邻快照增量(派生)", "unit": "同各字段", "window": "跨快照差",
        "source": "derived",
        "desc": "本快照日各量指标减去上一快照日;**务必配合 days_between 日均化**"
                "(快照可能断档,不能默认间隔 1 天)。注意 T-1 口径字段(exposure/follows)"
                "与 T 口径字段(views/likes/…)的增量含义不同期,不要跨口径相除。",
    },
    "days_between": {
        "label": "两快照间隔天数(派生)", "unit": "天", "window": "跨快照差",
        "source": "derived",
        "desc": "本快照日与上一快照日相差的天数;为 null 表示无上一快照或日期不可解析",
    },
}

_FIELD_NOTES: dict = {
    "读法": "所有指标为快照日的**累计值**(非当日增量);逐字段的官方口径、"
           "时间窗(window)、单位、来源见 meta.field_meta。",
    "⚠️时间窗不一致": "平台各指标更新频率不同:window='T' 是实时(截止目前),"
                "'T-1' 是每天更新(截至昨日),'unknown' 表示我们**未核实**"
                "(绝非默认值,不要当成 T 或 T-1 去推算)。"
                "**跨不同 window 的字段相除得到的比率不可当真实转化率**"
                "——分子分母不同期。已知踩坑:views/exposure 算不出 cover_ctr;"
                "follow_rate(follows T-1 ÷ views T)系统性偏低。",
    "排序": "notes 按最新 views 降序;series 按 snapshot_date 升序",
}


def field_meta_block() -> dict:
    """口径说明块(field_meta + field_notes),供任何下发指标的响应挂到 meta 里复用。

    口径要随数据一起下发:消费 agent 拿到裸字段名不必望文生义,也不必查外部文档。
    """
    return {"field_meta": _FIELD_META, "field_notes": _FIELD_NOTES}


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
        rates = _rates(latest)
        # 有历史快照才算得出同窗分母,把 _rates 里的 None 占位覆盖成真实值
        rates["follow_rate_t1"] = _follow_rate_t1(latest, series)
        notes.append({
            "title": m.title,
            "publish_time": m.publish_time,
            "days_since_publish": (today - pub_date).days if pub_date else None,
            "latest": latest,
            "rates": rates,
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
            **field_meta_block(),
        },
        "account_daily": account_daily,
        "notes": notes,
    }
