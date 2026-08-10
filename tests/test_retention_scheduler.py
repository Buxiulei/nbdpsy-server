"""笔记数量上限淘汰:评分/选篇纯逻辑 + 调度器行为。

这个组件会**不可逆地删线上笔记**,所以测试锁的不是"它能删",而是"它不该删的时候一篇
都不碰":三条安全轨(宽限期 / 无指标不杀 / 单日封顶)各有一条用例,kill switch 有一条,
"当日数据没到位就别动手"有一条。评分那几条锁的是"选出来的确实是最差的那篇"——选错篇
和删错篇后果一样。

patch 纪律:配置打在 ``app.core.config.settings`` 属性上(被测模块 import 的是同一个对象);
BrowserJob 探针打在**被测模块命名空间**(``retention.BrowserJob``),不是模型源模块。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.browser_job import BrowserJob
from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.published_note import PublishedNote
from app.models.retention_run import RetentionRun
from app.models.xhs_account import XhsAccount
from app.services import retention_scheduler as retention

_NOW = datetime(2026, 8, 10, 12, 0, 0)  # 纯逻辑用例的固定基准(naive UTC)
_EVEN = {"views": 1.0, "likes": 1.0, "collects": 1.0, "comments": 1.0, "follows": 1.0}


def _metrics(views=0, likes=0, collects=0, comments=0, follows=0) -> dict:
    return {"views": views, "likes": likes, "collects": collects,
            "comments": comments, "follows": follows}


def _note(title: str, days_ago: float, note_id: str | None = None) -> dict:
    return {
        "note_id": note_id or f"n-{title}",
        "title": title,
        "published_at": _NOW - timedelta(days=days_ago),
    }


def _index(*pairs) -> dict:
    """[(note dict, metrics dict), ...] → build_plan 吃的指标索引(键按北京日期算)。"""
    return {
        (n["title"], retention.ledger_publish_date(n["published_at"])): m
        for n, m in pairs
    }


# ---------------- 权重解析 ----------------


def test_parse_weights_uses_defaults_and_allows_partial_override():
    """出厂默认可解析;只覆盖其中一项时,其余项各自退回默认值。"""
    assert retention.parse_weights(settings.RETENTION_WEIGHTS) == retention.DEFAULT_WEIGHTS
    partial = retention.parse_weights('{"views": 9}')
    assert partial["views"] == 9.0
    assert partial["follows"] == retention.DEFAULT_WEIGHTS["follows"]


@pytest.mark.parametrize("raw", [
    None, "", "not json", "[1,2,3]", '{"views": "很多"}',
    '{"views": -1}', '{"views":0,"likes":0,"collects":0,"comments":0,"follows":0}',
])
def test_parse_weights_falls_back_on_anything_wrong(raw):
    """JSON 坏 / 不是对象 / 值不是数 / 负权重 / 权重和为 0 → 一律回退默认。

    **绝不带着一份坏权重去删笔记**:权重错了不会报错,只会安静地删错篇。
    """
    assert retention.parse_weights(raw) == retention.DEFAULT_WEIGHTS


# ---------------- 评分 ----------------


def test_score_min_max_normalizes_within_candidate_set():
    """每指标在候选集内归一化:全项最高 → 1.0,全项最低 → 0.0,中间值按比例。"""
    best = {"metrics": _metrics(100, 100, 100, 100, 100)}
    mid = {"metrics": _metrics(50, 50, 50, 50, 50)}
    worst = {"metrics": _metrics(0, 0, 0, 0, 0)}
    retention.score_candidates([best, mid, worst], _EVEN)
    assert best["score"] == 1.0
    assert worst["score"] == 0.0
    assert mid["score"] == pytest.approx(0.5)


def test_score_weights_shift_the_ranking():
    """权重是有效的:同一组数据,改权重能把冠军换人(否则加权平均等于摆设)。

    A 浏览量碾压、B 增粉碾压。views 重 → A 高;follows 重 → B 高。
    """
    def pair():
        return ({"metrics": _metrics(views=1000, follows=0)},
                {"metrics": _metrics(views=0, follows=10)})

    a, b = pair()
    retention.score_candidates([a, b], {"views": 10, "likes": 0, "collects": 0,
                                        "comments": 0, "follows": 1})
    assert a["score"] > b["score"]

    a2, b2 = pair()
    retention.score_candidates([a2, b2], {"views": 1, "likes": 0, "collects": 0,
                                          "comments": 0, "follows": 10})
    assert b2["score"] > a2["score"]


def test_score_all_zero_note_ranks_first_for_deletion():
    """全零指标的篇得 0 分、排在淘汰序最前 —— 需求语义(表现最差的先删)。"""
    zero = {"metrics": _metrics(), "title": "全零", "published_at": "2026-01-01"}
    some = {"metrics": _metrics(likes=1), "title": "有一点", "published_at": "2026-01-01"}
    retention.score_candidates([zero, some], _EVEN)
    assert zero["score"] == 0.0
    assert sorted([some, zero], key=retention._rank_key)[0] is zero


def test_score_single_candidate_does_not_divide_by_zero():
    """只有一篇候选(span=0)不能崩:归一化恒 0,得分 0.0。"""
    only = {"metrics": _metrics(views=999, likes=8)}
    retention.score_candidates([only], _EVEN)
    assert only["score"] == 0.0


def test_score_empty_candidates_is_noop():
    """空候选集直接返回(没超上限时连打分都不该跑)。"""
    retention.score_candidates([], _EVEN)  # 不抛即通过


# ---------------- 选篇 ----------------


def _plan(notes, index, *, cap, grace_days=7, daily_max=5, weights=None):
    return retention.build_plan(
        notes, index, cap=cap, now=_NOW, grace_days=grace_days,
        weights=weights or _EVEN, daily_max=daily_max,
    )


def test_plan_does_nothing_at_exactly_cap():
    """笔记数**恰好等于**上限 → 一篇不删,连明细都不算(没超就没有淘汰这回事)。"""
    notes = [_note(f"t{i}", 30) for i in range(3)]
    index = _index(*[(n, _metrics()) for n in notes])
    plan = _plan(notes, index, cap=3)
    assert plan["over_cap"] == 0
    assert plan["selected"] == []
    assert plan["details"] == []
    assert plan["eligible_count"] == 0


def test_plan_grace_period_protects_new_notes():
    """安全轨①:发布不足宽限期的笔记不参与淘汰,**哪怕它指标最差**。

    没有这条,每天都会把刚发的内容当"表现最差"删掉——新笔记曝光要几天才铺开。
    这里新笔记全零、老笔记有量,若宽限期失效被删的会是新笔记。
    """
    fresh = _note("刚发的", 1)
    old = _note("老的", 30)
    index = _index((fresh, _metrics()), (old, _metrics(views=100, likes=5)))
    plan = _plan([fresh, old], index, cap=1, grace_days=7)

    assert [e["title"] for e in plan["selected"]] == ["老的"]
    fresh_detail = next(e for e in plan["details"] if e["title"] == "刚发的")
    assert fresh_detail["eligible"] is False
    assert "宽限期" in fresh_detail["skip_reason"]


def test_plan_excludes_notes_without_metrics():
    """安全轨②:join 不到 note_metrics 的笔记一律不进淘汰名单(不知道表现就不删)。"""
    known = _note("有指标", 30)
    unknown = _note("没指标", 30)
    index = _index((known, _metrics(views=10)))
    plan = _plan([known, unknown], index, cap=1)

    assert [e["title"] for e in plan["selected"]] == ["有指标"]
    detail = next(e for e in plan["details"] if e["title"] == "没指标")
    assert detail["eligible"] is False and detail["metrics"] is None
    assert "无指标" in detail["skip_reason"]


def test_plan_caps_deletions_per_run():
    """安全轨③:超上限 10 篇也只删 daily_max 篇(首次启用不许大屠杀)。"""
    notes = [_note(f"t{i}", 30) for i in range(12)]
    index = _index(*[(n, _metrics(views=i)) for i, n in enumerate(notes)])
    plan = _plan(notes, index, cap=2, daily_max=3)

    assert plan["over_cap"] == 10
    assert len(plan["selected"]) == 3
    # 删的确实是浏览量最低的三篇
    assert sorted(e["title"] for e in plan["selected"]) == ["t0", "t1", "t2"]


def test_plan_take_never_exceeds_eligible_count():
    """够格的比该删的少时,只删够格的那几篇(不会去凑数删被安全轨挡下的)。

    注意 cap 用 1 不用 0:``cap<=0`` 现在是「上限非法」的信号,会回退默认 100(见
    ``test_plan_illegal_cap_falls_back_to_default``),不能再拿它当"全部超限"的简写。
    """
    old = _note("老的", 30)
    fresh1, fresh2 = _note("新1", 1), _note("新2", 2)
    index = _index((old, _metrics()), (fresh1, _metrics()), (fresh2, _metrics()))
    plan = _plan([old, fresh1, fresh2], index, cap=1, daily_max=5)

    assert plan["over_cap"] == 2
    assert len(plan["selected"]) == 1 and plan["selected"][0]["title"] == "老的"


def test_plan_illegal_cap_falls_back_to_default_not_zero():
    """note_cap 非法(0/负/NULL→0)→ 回退默认 100 并在明细里记告警,**绝不当 0 用**。

    方向反了的兜底比没有兜底危险得多:cap=0 的语义是"整个号都超限",按单日封顶一天删 5 篇,
    几周就把号删空。这里 3 篇笔记对一个非法 cap,正确结果是一篇都不删。
    """
    notes = [_note(f"t{i}", 30) for i in range(3)]
    index = _index(*[(n, _metrics()) for n in notes])

    for bad_cap in (0, -5):
        plan = _plan(notes, index, cap=bad_cap)
        assert plan["cap"] == retention.DEFAULT_NOTE_CAP
        assert plan["over_cap"] == 0 and plan["selected"] == []
        # 告警落在 details 里(审计表只有这一个自由字段);title 为 None 即"这不是一篇笔记"
        warnings = [d for d in plan["details"] if d["title"] is None]
        assert len(warnings) == 1
        assert "note_cap 非法" in warnings[0]["skip_reason"]
        assert str(retention.DEFAULT_NOTE_CAP) in warnings[0]["skip_reason"]


def test_ambiguity_sets_reports_dup_titles_and_dup_join_keys():
    """同名两道各自的判据:重名标题集合 与 撞车的 (标题, 北京发布日) 键集合。

    ②(join 键撞车)在①(标题不唯一)收口之后实际不会再触发——标题唯一是更严的条件,
    所以它只能在这一层单测。两道守的是两件事:①"能不能安全地删"(按标题删会删错人),
    ②"这份指标是不是这一篇的"。
    """
    same_day_a, same_day_b = _note("同名同日", 30), _note("同名同日", 30)
    diff_day = _note("同名不同日", 30)
    diff_day2 = _note("同名不同日", 40)
    unique = _note("独一份", 30)
    notes = [same_day_a, same_day_b, diff_day, diff_day2, unique]

    dup_titles, dup_keys = retention.ambiguity_sets(notes)
    assert dup_titles == {"同名同日", "同名不同日"}
    # 同名不同日两篇的 join 键各不相同,只有同名同日那对撞车
    assert dup_keys == {("同名同日", retention.ledger_publish_date(same_day_a["published_at"]))}
    # 标题唯一 ⇒ 键唯一:①是更严的那道
    assert unique["title"] not in dup_titles


def test_plan_excludes_all_notes_sharing_a_title():
    """同名歧义①:标题在该号台账里不唯一 → 该标题**全部**行退出候选,哪怕它们最该删。

    删除是按标题定位卡片的(平台导出无 note_id),同名场景下删掉的可能是同名里的另一篇。
    这里两篇同名的全零(最该删),唯一标题的那篇有量;正确结果是删有量的那篇——宁可删掉
    次差的,也不能冒删错人的风险。
    """
    dup1, dup2 = _note("撞名了", 30, note_id="n-1"), _note("撞名了", 31, note_id="n-2")
    solo = _note("独一份", 30)
    index = _index((dup1, _metrics()), (dup2, _metrics()), (solo, _metrics(views=100)))
    plan = _plan([dup1, dup2, solo], index, cap=2)

    assert [e["title"] for e in plan["selected"]] == ["独一份"]
    dup_details = [e for e in plan["details"] if e["title"] == "撞名了"]
    assert len(dup_details) == 2
    for entry in dup_details:
        assert entry["eligible"] is False
        assert "同名歧义" in entry["skip_reason"]


def test_plan_excludes_notes_with_delete_job_in_flight():
    """在途的删除任务:那一篇本轮不重复选,而且要从"还超出多少"里扣掉。

    不扣的话每轮都会在没跑完的删除之上再叠一批——第一轮建了删除还没跑完,第二轮又按原来的
    over_cap 再建一批,最后删过头。这里 2 篇对 1 的帽,差生的删除已在途,正确结果是**一篇
    都不再删**(等它跑完库存自然回到 1),而不是转头去删优等生。
    """
    bad, good = _note("差生", 30), _note("优等生", 30)
    index = _index((bad, _metrics()), (good, _metrics(views=500)))
    inflight = frozenset({retention.note_key(bad["note_id"], bad["title"])})
    plan = retention.build_plan(
        [bad, good], index, cap=1, now=_NOW, grace_days=7, weights=_EVEN,
        daily_max=5, inflight_keys=inflight,
    )

    assert plan["selected"] == [], "在途删除之上又叠了一条,会删过头"
    detail = next(e for e in plan["details"] if e["title"] == "差生")
    assert detail["eligible"] is False and "在途" in detail["skip_reason"]


def test_plan_details_carry_full_audit_payload():
    """明细是事后唯一依据:每篇都要有五指标、得分、是否入淘汰这几项。"""
    a, b = _note("A", 30), _note("B", 30)
    index = _index((a, _metrics(views=1)), (b, _metrics(views=100)))
    plan = _plan([a, b], index, cap=1)

    picked = next(e for e in plan["details"] if e["selected"])
    assert picked["title"] == "A"
    assert set(picked["metrics"]) == set(retention.METRIC_FIELDS)
    assert picked["score"] is not None
    assert next(e for e in plan["details"] if e["title"] == "B")["selected"] is False


# ---------------- 时区口径 ----------------


def test_ledger_date_converts_utc_to_beijing():
    """台账 naive UTC → 北京日期:UTC 前一天 16:00 之后就是北京的第二天。

    join 不做这个换算的话,北京时间 00:00-08:00 发布的笔记全部 join 不上指标 ——
    然后被安全轨②默默排除,表现为"这批笔记永远不参与淘汰",且不报任何错。
    """
    assert retention.ledger_publish_date(datetime(2026, 5, 21, 16, 30)) == "2026-05-22"
    assert retention.ledger_publish_date(datetime(2026, 5, 22, 2, 59)) == "2026-05-22"
    assert retention.ledger_publish_date(None) is None


def test_metric_publish_date_parses_both_export_formats():
    """导出原文两种写法都认;认不出返回 None(该行不进索引)。"""
    assert retention.metric_publish_date("2026年05月22日10时59分14秒") == "2026-05-22"
    assert retention.metric_publish_date("2026-5-9 08:00") == "2026-05-09"
    assert retention.metric_publish_date("无法解析") is None


# ---------------- 调度器(落库行为) ----------------


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """临时文件库 + 会话工厂(调度器只吃 session_factory,不碰全局 engine)。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ret.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _seed_account(factory, account_id, *, managed=True, note_cap=1) -> None:
    async with factory() as s:
        s.add(XhsAccount(
            id=account_id, name=f"号{account_id}", user_id=f"u-{account_id}",
            cookie_status="valid", login_cookies="enc",
            managed=managed, note_cap=note_cap,
        ))
        await s.commit()


async def _seed_note(
    factory, account_id, title, *, days_ago=30, note_id=..., **metric_values
) -> None:
    """一篇台账笔记 + 一行能 join 上的最新指标(不传指标即全零)。

    ``note_id=None`` 造的是台账同步还没补上平台 id 的行(pending_id 态),收敛只能按标题匹配。
    """
    published_at = datetime.utcnow() - timedelta(days=days_ago)
    beijing = published_at + timedelta(hours=8)
    if note_id is ...:
        note_id = f"nid-{account_id}-{title}"
    async with factory() as s:
        s.add(PublishedNote(
            account_id=account_id, note_id=note_id, title=title,
            published_at=published_at, platform_published_at=published_at,
            first_seen_at=published_at, permission_code=0, sync_status="orphan",
        ))
        s.add(NoteMetric(
            account_id=account_id, title=title,
            publish_time=beijing.strftime("%Y年%m月%d日%H时%M分%S秒"),
            **{f: metric_values.get(f, 0) for f in retention.METRIC_FIELDS},
        ))
        await s.commit()


async def _seed_today_snapshot(factory, account_id) -> None:
    """当日数据快照(调度器的前置:淘汰必须挂在数据拉取之后)。"""
    async with factory() as s:
        s.add(NoteMetricDaily(
            account_id=account_id, title="任意", publish_time="p",
            snapshot_date=_today(),
        ))
        await s.commit()


async def _delete_jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == retention.DELETE_JOB_KIND)
        )).scalars().all())


async def _runs(factory) -> list[RetentionRun]:
    async with factory() as s:
        return list((await s.execute(
            select(RetentionRun).order_by(RetentionRun.id)
        )).scalars().all())


async def _seed_over_cap_account(factory, account_id=1, note_cap=1) -> None:
    """一个超上限的代管号:差生(全零)+ 优等生(有量),当日快照已到位。"""
    await _seed_account(factory, account_id, note_cap=note_cap)
    await _seed_note(factory, account_id, "差生")
    await _seed_note(factory, account_id, "优等生", views=500, likes=50, collects=20,
                     comments=10, follows=5)
    await _seed_today_snapshot(factory, account_id)


async def _ledger(factory, account_id=1) -> list[PublishedNote]:
    async with factory() as s:
        return list((await s.execute(
            select(PublishedNote)
            .where(PublishedNote.account_id == account_id)
            .order_by(PublishedNote.id)
        )).scalars().all())


async def _finish_job(factory, job_id, *, status="done", **result) -> None:
    """把删除任务置成终态 + 结果(执行方跑完之后 browser_jobs 里的样子)。"""
    async with factory() as s:
        job = await s.get(BrowserJob, job_id)
        job.status = status
        job.result = json.dumps(result, ensure_ascii=False)
        await s.commit()


async def _run_once(factory, account_id=1, **kwargs) -> dict:
    """直接跑一轮 execute_retention。

    刻意绕开调度器那道「每号每天至多一轮」的闸 —— 收敛要看的正是"上一轮删完之后下一轮
    还会不会再选同一篇",在同一个测试里连跑两轮最直接。
    """
    async with factory() as s:
        account = await s.get(XhsAccount, account_id)
        return await retention.execute_retention(
            s, account, dry_run=False, record=True, **kwargs
        )


@pytest.mark.asyncio
async def test_reconcile_marks_deleted_note_and_inventory_converges(session_factory):
    """删成功 → 台账落 deleted_at → 下一轮库存少一篇、不再建第二条删除任务(淘汰收敛)。

    没有这一步就是**幽灵重删**:被删掉的笔记仍在库存里计数,第二天照样被选中、照样再建
    一条删除任务,而平台上早就没有这张卡片了 —— 天天删同一篇,永远删不完。
    """
    await _seed_over_cap_account(session_factory)  # cap=1,差生(全零)+ 优等生(有量)

    first = await _run_once(session_factory)
    assert first["deleted_count"] == 1
    jobs = await _delete_jobs(session_factory)
    await _finish_job(session_factory, jobs[0].id, deleted=1, remaining=0)

    second = await _run_once(session_factory)
    assert second["note_count"] == 1, "已删的笔记还留在库存里计数"
    assert second["over_cap"] == 0 and second["deleted_count"] == 0
    assert len(await _delete_jobs(session_factory)) == 1, "又给同一篇建了一条幽灵删除任务"

    rows = {r.title: r for r in await _ledger(session_factory)}
    assert rows["差生"].deleted_at is not None
    assert rows["优等生"].deleted_at is None
    # 台账行**不物理删**:"我们发过这篇"的事实要留着,deleted_at 只是收敛标记
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(session_factory):
    """重复收敛不改已标记的行:deleted_at 只落一次,时刻不被后续轮次刷新。"""
    await _seed_over_cap_account(session_factory)
    await _run_once(session_factory)
    jobs = await _delete_jobs(session_factory)
    await _finish_job(session_factory, jobs[0].id, deleted=1, remaining=0)

    async with session_factory() as s:
        account = await s.get(XhsAccount, 1)
        await retention.reconcile_deletions(s, account, now=datetime(2026, 1, 1, 0, 0))
    first_mark = {r.title: r.deleted_at for r in await _ledger(session_factory)}
    assert first_mark["差生"] == datetime(2026, 1, 1, 0, 0)

    async with session_factory() as s:
        account = await s.get(XhsAccount, 1)
        await retention.reconcile_deletions(s, account, now=datetime(2026, 6, 6, 6, 6))
    assert {r.title: r.deleted_at for r in await _ledger(session_factory)} == first_mark


@pytest.mark.asyncio
async def test_reconcile_matches_by_title_when_note_id_missing(session_factory):
    """台账行还没补上平台 note_id(pending_id 态)时,收敛按标题匹配那一行。"""
    await _seed_account(session_factory, 1, note_cap=1)
    await _seed_note(session_factory, 1, "差生", note_id=None)
    await _seed_note(session_factory, 1, "优等生", views=500, note_id=None)

    await _run_once(session_factory)
    jobs = await _delete_jobs(session_factory)
    await _finish_job(session_factory, jobs[0].id, deleted=1, remaining=0)
    await _run_once(session_factory)

    rows = {r.title: r for r in await _ledger(session_factory)}
    assert rows["差生"].deleted_at is not None and rows["优等生"].deleted_at is None


@pytest.mark.asyncio
async def test_inflight_delete_job_blocks_a_second_round(session_factory):
    """上一轮的删除任务还没跑完 → 这一轮一篇都不再选(不是转头去删次差的那篇)。

    在途 = 删了没删还不知道。既不能重复给它建第二条任务,也不能当它不存在而把下一篇也删了
    —— 那两条都跑完就删过头了。
    """
    await _seed_over_cap_account(session_factory)
    await _run_once(session_factory)  # 建了差生的删除任务,状态仍是 queued

    second = await _run_once(session_factory)

    assert second["deleted_count"] == 0
    assert len(await _delete_jobs(session_factory)) == 1
    detail = next(d for d in second["details"] if d["title"] == "差生")
    assert "在途" in detail["skip_reason"]


@pytest.mark.asyncio
async def test_failed_delete_job_puts_the_note_back_in_candidates(session_factory):
    """删除任务落 error → 那篇下一轮重新进候选(删除确实没生效,重试是对的)。"""
    await _seed_over_cap_account(session_factory)
    await _run_once(session_factory)
    jobs = await _delete_jobs(session_factory)
    await _finish_job(session_factory, jobs[0].id, status="error", error="找不到同题卡片")

    second = await _run_once(session_factory)

    assert second["deleted_count"] == 1
    assert len(await _delete_jobs(session_factory)) == 2
    assert (await _ledger(session_factory))[0].deleted_at is None


@pytest.mark.asyncio
async def test_done_with_zero_deleted_is_not_treated_as_deleted(session_factory):
    """done 但 deleted=0(跑完了一张卡也没删掉)不算删成功,绝不落 deleted_at。

    标错了这一篇就永远退出淘汰、再也不会被清理 —— 判据必须取最严的那个。
    """
    await _seed_over_cap_account(session_factory)
    await _run_once(session_factory)
    jobs = await _delete_jobs(session_factory)
    await _finish_job(session_factory, jobs[0].id, deleted=0, remaining=1)

    second = await _run_once(session_factory)

    assert {r.title: r.deleted_at for r in await _ledger(session_factory)}["差生"] is None
    assert second["note_count"] == 2  # 仍在库存里


@pytest.mark.asyncio
async def test_scan_falls_back_to_default_cap_when_note_cap_illegal(session_factory):
    """账号的 note_cap 非法(0)→ 本轮按默认 100 算,一篇不删,审计里留一条告警。"""
    await _seed_account(session_factory, 1, note_cap=0)
    await _seed_note(session_factory, 1, "甲")
    await _seed_note(session_factory, 1, "乙", views=1)
    await _seed_today_snapshot(session_factory, 1)

    assert await retention.RetentionScheduler(session_factory, 1).scan_once() == 0
    assert await _delete_jobs(session_factory) == []

    runs = await _runs(session_factory)
    assert len(runs) == 1 and runs[0].cap == retention.DEFAULT_NOTE_CAP
    warnings = [d for d in json.loads(runs[0].details_json) if d["title"] is None]
    assert len(warnings) == 1 and "note_cap 非法" in warnings[0]["skip_reason"]


@pytest.mark.asyncio
async def test_scan_deletes_worst_note_and_records_audit(session_factory):
    """超上限 → 建一条 note_delete 任务删掉得分最低那篇,并落一行全量审计。"""
    await _seed_over_cap_account(session_factory)

    deleted = await retention.RetentionScheduler(session_factory, 1).scan_once()

    assert deleted == 1
    jobs = await _delete_jobs(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "queued" and job.account_id == 1 and job.operator_id == 0
    payload = json.loads(job.payload)
    # payload 形态必须与 note_delete.start_delete 一字不差,否则执行方拿不到 deletion_id
    assert payload == {"title": "差生", "count": 1, "deletion_id": job.id}

    runs = await _runs(session_factory)
    assert len(runs) == 1
    run = runs[0]
    assert (run.account_id, run.run_date, run.deleted_count) == (1, _today(), 1)
    assert run.platform_note_count == 2 and run.cap == 1 and run.eligible_count == 2
    assert run.dry_run is False
    details = json.loads(run.details_json)
    assert {d["title"] for d in details} == {"差生", "优等生"}
    picked = next(d for d in details if d["selected"])
    assert picked["title"] == "差生" and picked["job_id"] == job.id


@pytest.mark.asyncio
async def test_scan_writes_audit_before_creating_delete_job(session_factory, tmp_path, monkeypatch):
    """**审计必须先于删除任务落库**:反过来的话审计写失败就是"任务在跑、查不到依据"的黑箱。

    探针在 BrowserJob 被构造那一刻,用一条独立 sqlite 连接数已提交的 retention_runs ——
    数得到 1 行,就证明审计那次 commit 已经发生。
    """
    await _seed_over_cap_account(session_factory)
    db_file = str(tmp_path / "ret.db")
    seen: dict = {}
    real_cls = retention.BrowserJob

    def _probe(**kwargs):
        if "audit_rows" not in seen:
            conn = sqlite3.connect(db_file)
            try:
                seen["audit_rows"] = conn.execute(
                    "SELECT COUNT(*) FROM retention_runs"
                ).fetchone()[0]
            finally:
                conn.close()
        return real_cls(**kwargs)

    monkeypatch.setattr(retention, "BrowserJob", _probe)

    await retention.RetentionScheduler(session_factory, 1).scan_once()

    assert seen["audit_rows"] == 1, "建删除任务时审计行还没落库(顺序反了)"


@pytest.mark.asyncio
async def test_scan_skips_account_that_already_ran_today(session_factory):
    """当日已有审计行 → 跳过(每号每天至多一轮,否则一小时一次会把笔记删空)。"""
    await _seed_over_cap_account(session_factory)
    async with session_factory() as s:
        s.add(RetentionRun(account_id=1, run_date=_today(), platform_note_count=2,
                           cap=1, eligible_count=2, deleted_count=1, dry_run=False))
        await s.commit()

    assert await retention.RetentionScheduler(session_factory, 1).scan_once() == 0
    assert await _delete_jobs(session_factory) == []


@pytest.mark.asyncio
async def test_scan_waits_for_today_snapshot(session_factory):
    """当日数据快照还没到位 → 本轮不动手(需求:每天**拉取数据后**发现超了才删)。

    快照补上后同一个调度器再跑一轮就该动手 —— 证明它是"等",不是"永远不做"。
    """
    await _seed_account(session_factory, 1, note_cap=1)
    await _seed_note(session_factory, 1, "差生")
    await _seed_note(session_factory, 1, "优等生", views=500)
    scheduler = retention.RetentionScheduler(session_factory, 1)

    assert await scheduler.scan_once() == 0
    assert await _runs(session_factory) == []

    await _seed_today_snapshot(session_factory, 1)
    assert await scheduler.scan_once() == 1


@pytest.mark.asyncio
async def test_scan_ignores_non_managed_accounts(session_factory):
    """水军号(managed=false)不参与淘汰,哪怕笔记数远超它那个 note_cap。"""
    await _seed_account(session_factory, 2, managed=False, note_cap=1)
    await _seed_note(session_factory, 2, "甲")
    await _seed_note(session_factory, 2, "乙", views=1)
    await _seed_today_snapshot(session_factory, 2)

    assert await retention.RetentionScheduler(session_factory, 1).scan_once() == 0
    assert await _runs(session_factory) == []


@pytest.mark.asyncio
async def test_kill_switch_audits_without_deleting(session_factory, monkeypatch):
    """RETENTION_ENABLED=0:照常算、照常落审计(dry_run=1),**一条删除任务都不建**。"""
    monkeypatch.setattr(settings, "RETENTION_ENABLED", False)
    await _seed_over_cap_account(session_factory)

    assert await retention.RetentionScheduler(session_factory, 1).scan_once() == 0
    assert await _delete_jobs(session_factory) == []

    runs = await _runs(session_factory)
    assert len(runs) == 1 and runs[0].dry_run is True and runs[0].deleted_count == 0
    # 名单照算:关着跑几天就是为了看它会选谁
    details = json.loads(runs[0].details_json)
    picked = [d for d in details if d["selected"]]
    assert [d["title"] for d in picked] == ["差生"]
    assert picked[0]["job_id"] is None


@pytest.mark.asyncio
async def test_scan_records_run_even_when_under_cap(session_factory):
    """没超上限也落一行 deleted_count=0 的审计 —— 当天"算过了"这件事本身要留痕。"""
    await _seed_account(session_factory, 1, note_cap=10)
    await _seed_note(session_factory, 1, "唯一一篇")
    await _seed_today_snapshot(session_factory, 1)

    assert await retention.RetentionScheduler(session_factory, 1).scan_once() == 0
    runs = await _runs(session_factory)
    assert len(runs) == 1 and runs[0].deleted_count == 0 and runs[0].cap == 10
    assert await _delete_jobs(session_factory) == []
