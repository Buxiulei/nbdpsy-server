"""account_trends 分析包测试:日汇总/增量/率值/断档间隔/排序/RBAC。

隔离:tmp sqlite + async_sessionmaker,admin operator(assert_account_access 放行)。
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.operator import Operator
from app.models.xhs_account import XhsAccount
from app.services.note_metrics_service import account_trends


@pytest.fixture
async def smk(tmp_path):
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _admin() -> Operator:
    return Operator(id=1, name="root", apikey_hash="x", role="admin", enabled=True)


def _daily(acc, title, pub, date, **m):
    return NoteMetricDaily(
        account_id=acc, title=title, publish_time=pub, snapshot_date=date, **m)


async def _seed(smk) -> int:
    """1 账号 2 笔记 × 2 快照日(隔 2 天,模拟断档);A 火 B 冷。"""
    async with smk() as s:
        acc = XhsAccount(name="号A", cookie_status="valid")
        s.add(acc)
        await s.commit()
        acc_id = acc.id
        pub = "2026年07月01日10时00分00秒"
        # 快照日 07-23:A(曝光100/观看50/赞5/藏2/评1) B(曝光10/观看0)
        s.add(_daily(acc_id, "A", pub, "2026-07-23",
                     exposure=100, views=50, likes=5, collects=2, comments=1, follows=1))
        s.add(_daily(acc_id, "B", pub, "2026-07-23", exposure=10, views=0))
        # 快照日 07-25(隔 2 天):A 涨到 200/80/9/4/2;B 仍 0 观看
        s.add(_daily(acc_id, "A", pub, "2026-07-25",
                     exposure=200, views=80, likes=9, collects=4, comments=2, follows=1))
        s.add(_daily(acc_id, "B", pub, "2026-07-25", exposure=12, views=0))
        # 最新快照表(latest 态)
        s.add(NoteMetric(account_id=acc_id, title="A", publish_time=pub,
                         exposure=200, views=80, likes=9, collects=4, comments=2,
                         follows=1, updated_at=datetime.utcnow()))
        s.add(NoteMetric(account_id=acc_id, title="B", publish_time=pub,
                         exposure=12, views=0, updated_at=datetime.utcnow()))
        await s.commit()
        return acc_id


async def test_trends_full_package(smk):
    acc_id = await _seed(smk)
    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    # meta:快照覆盖
    assert pkg["meta"]["snapshot_dates"] == ["2026-07-23", "2026-07-25"]
    assert pkg["meta"]["latest_snapshot_date"] == "2026-07-25"
    assert pkg["meta"]["notes_tracked"] == 2
    assert pkg["account"]["id"] == acc_id

    # 账号级日汇总 + 增量(带断档间隔 days_between=2)
    d1, d2 = pkg["account_daily"]
    assert d1["snapshot_date"] == "2026-07-23" and d1["note_count"] == 2
    assert d1["exposure"] == 110 and d1["views"] == 50 and d1["delta"] is None
    assert d2["exposure"] == 212 and d2["views"] == 80
    assert d2["delta"]["exposure"] == 102 and d2["delta"]["views"] == 30
    assert d2["delta"]["days_between"] == 2

    # notes 按最新 views 降序:A 在前
    note_a, note_b = pkg["notes"]
    assert note_a["title"] == "A" and note_b["title"] == "B"
    # 发布距今天数已解析(中文日期格式)
    assert isinstance(note_a["days_since_publish"], int)
    # 率值:A like_rate=9/80;B views=0 → 全 None
    assert note_a["rates"]["like_rate"] == round(9 / 80, 4)
    assert note_a["rates"]["engage_rate"] == round((9 + 4 + 2) / 80, 4)
    assert note_b["rates"]["like_rate"] is None
    # 每篇序列增量
    assert note_a["series"][0]["delta"] is None
    assert note_a["series"][1]["delta"]["views"] == 30
    assert note_a["series"][1]["delta"]["days_between"] == 2


async def test_trends_rbac_denied(smk):
    """无授权 operator → AccessDenied。"""
    from app.auth.guards import AccessDenied

    acc_id = await _seed(smk)
    op = Operator(id=9, name="op", apikey_hash="x", role="operator", enabled=True)
    async with smk() as s:
        with pytest.raises(AccessDenied):
            await account_trends(s, op, acc_id)


async def test_trends_empty_account(smk):
    """无任何快照的账号:结构完整、列表为空,不炸。"""
    async with smk() as s:
        acc = XhsAccount(name="空号", cookie_status="valid")
        s.add(acc)
        await s.commit()
        pkg = await account_trends(s, _admin(), acc.id)
    assert pkg["account_daily"] == [] and pkg["notes"] == []
    assert pkg["meta"]["latest_snapshot_date"] is None


async def test_trends_notes_sorted_by_latest_views_desc(smk):
    """notes 真按最新 views 降序,且 meta 的排序口径说的就是这件事(实现 + 文案双盯)。

    _seed 里 A/B 的入库序恰好等于 views 降序,区分不出"排了"还是"没排";这里故意让
    入库序与 views 降序相反,顺序对了才说明真排过。series 同理:先入库晚的快照日。
    """
    async with smk() as s:
        acc = XhsAccount(name="号S", cookie_status="valid")
        s.add(acc)
        await s.commit()
        acc_id = acc.id
        pub = "2026年07月01日10时00分00秒"
        # 入库序 冷→热(30/500/120),views 降序应为 热(500)→中(120)→冷(30)
        for title, views, dates in (
            ("冷", 30, ("2026-07-25",)),
            ("热", 500, ("2026-07-25", "2026-07-23")),  # 晚的快照日先入库
            ("中", 120, ("2026-07-25",)),
        ):
            s.add(NoteMetric(account_id=acc_id, title=title, publish_time=pub,
                             views=views, updated_at=datetime.utcnow()))
            for d in dates:
                s.add(_daily(acc_id, title, pub, d, views=views))
        await s.commit()

    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    got = [n["latest"]["views"] for n in pkg["notes"]]
    assert got == [500, 120, 30]  # 真按最新 views 降序
    assert got != [30, 500, 120]  # 这份数据确实能区分"排了"和"照入库序原样返回"
    # series 按 snapshot_date 升序(与入库序相反,升序才是排出来的)
    hot = next(n for n in pkg["notes"] if n["title"] == "热")
    assert [r["snapshot_date"] for r in hot["series"]] == ["2026-07-23", "2026-07-25"]

    order_note = pkg["meta"]["field_notes"]["排序"]
    assert "降序" in order_note and "views" in order_note
    assert "snapshot_date" in order_note and "升序" in order_note


# ── 逐字段口径元数据(field_meta)随数据下发 ──────────────────────────────

async def test_field_meta_ships_with_data(smk):
    """13 个字段 + 派生字段都要有口径;未核实的必须显式 unknown 而非留空或猜。"""
    from app.services.note_metrics_service import _METRIC_FIELDS

    acc_id = await _seed(smk)
    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    fm = pkg["meta"]["field_meta"]
    # 11 个平台指标字段全覆盖
    for f in _METRIC_FIELDS:
        assert f in fm, f"字段 {f} 缺口径"
        assert fm[f]["window"] in ("T", "T-1", "unknown"), fm[f]
        assert fm[f]["source"] in ("platform", "derived")
        assert fm[f].get("label")
    # 派生字段也要标口径
    for f in ("engage_rate", "follow_rate", "delta", "days_between"):
        assert f in fm, f"派生字段 {f} 缺口径"


async def test_trends_field_meta_keeps_all_six_rates(smk):
    """本端点是率值的唯一出处:六个派生率的口径必须全在,且 notes[].rates 真的下发。

    /notes 的 field_meta 已收窄成只声明平台原生列,这里不能跟着一起少。
    """
    six = ("like_rate", "collect_rate", "comment_rate",
           "engage_rate", "follow_rate", "follow_rate_t1")

    acc_id = await _seed(smk)
    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    fm = pkg["meta"]["field_meta"]
    for f in six:
        assert f in fm, f"派生率 {f} 缺口径"
    # 声明=下发:六个率在数据里也都有键(值可为 null)
    assert set(six) <= set(pkg["notes"][0]["rates"])


async def test_verified_windows_match_official_wording(smk):
    """已核实字段的 window 必须与平台原文一致(抄错等于埋雷)。"""
    acc_id = await _seed(smk)
    async with smk() as s:
        fm = (await account_trends(s, _admin(), acc_id))["meta"]["field_meta"]

    # 实时(截止目前)
    for f in ("views", "likes", "comments", "collects"):
        assert fm[f]["window"] == "T", f
        assert "实时" in fm[f]["desc"], f
    # 每天更新(截至昨日)
    for f in ("exposure", "cover_ctr", "follows"):
        assert fm[f]["window"] == "T-1", f
        assert "每天更新" in fm[f]["desc"], f


async def test_unverified_fields_are_explicit_unknown(smk):
    """未核实的字段:window=unknown 且 desc 为 None——绝不猜一个值糊弄 agent。"""
    acc_id = await _seed(smk)
    async with smk() as s:
        fm = (await account_trends(s, _admin(), acc_id))["meta"]["field_meta"]

    # reposts 是唯一未核实的:当前导出表根本没有这列,恒为 0
    assert fm["reposts"]["window"] == "unknown"
    assert "无此列" in fm["reposts"]["desc"]
    # 其余 10 个平台字段都已从平台 ⓘ 抄到原文,不得留 unknown
    for f in ("exposure", "views", "cover_ctr", "likes", "comments", "collects",
              "follows", "shares", "avg_view_duration", "danmu"):
        assert fm[f]["window"] in ("T", "T-1"), f
        assert fm[f]["desc"], f


async def test_follow_rate_cross_window_trap_is_flagged(smk):
    """follow_rate 是 T-1 分子 ÷ T 分母,必须显式标出这个陷阱(否则 agent 会当真实转化率)。"""
    acc_id = await _seed(smk)
    async with smk() as s:
        fm = (await account_trends(s, _admin(), acc_id))["meta"]["field_meta"]

    assert "T-1" in fm["follow_rate"]["window"] or "混合" in fm["follow_rate"]["window"]
    assert "不同期" in fm["follow_rate"]["desc"]
    # engage_rate 反之:四者同为 T,不该被标成陷阱
    assert fm["engage_rate"]["window"] == "T"
    assert "口径一致" in fm["engage_rate"]["desc"]


# ── follow_rate_t1:同窗涨粉率(分母换成上一快照日的 views)──────────────────

async def test_follow_rate_t1_uses_previous_snapshot_views(smk):
    """分母取上一快照日的 views(07-23 的 50),不是最新 views(80)。"""
    acc_id = await _seed(smk)
    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    note_a = pkg["notes"][0]
    assert note_a["title"] == "A"
    assert note_a["rates"]["follow_rate_t1"] == round(1 / 50, 4)
    # 跨窗版仍按最新 views 算 → 必然更低,这个差正是要暴露给运营的偏差
    assert note_a["rates"]["follow_rate"] == round(1 / 80, 4)


async def test_follow_rate_t1_none_without_earlier_snapshot(smk):
    """只有一个快照日 → 没有同窗分母 → null(绝不拿最新 views 顶替)。"""
    async with smk() as s:
        acc = XhsAccount(name="号C", cookie_status="valid")
        s.add(acc)
        await s.commit()
        acc_id = acc.id
        pub = "2026年07月20日10时00分00秒"
        s.add(_daily(acc_id, "新篇", pub, "2026-07-25", views=40, follows=2))
        s.add(NoteMetric(account_id=acc_id, title="新篇", publish_time=pub,
                         views=40, follows=2, updated_at=datetime.utcnow()))
        await s.commit()
    async with smk() as s:
        rates = (await account_trends(s, _admin(), acc_id))["notes"][0]["rates"]

    assert rates["follow_rate_t1"] is None
    assert rates["follow_rate"] == round(2 / 40, 4)


async def test_follow_rate_t1_none_when_previous_views_zero(smk):
    """上一快照日 views=0(发布当天还没观看)→ null,不除零也不硬凑。"""
    async with smk() as s:
        acc = XhsAccount(name="号D", cookie_status="valid")
        s.add(acc)
        await s.commit()
        acc_id = acc.id
        pub = "2026年07月24日09时00分00秒"
        s.add(_daily(acc_id, "当日篇", pub, "2026-07-24", views=0))
        s.add(_daily(acc_id, "当日篇", pub, "2026-07-25", views=60, follows=3))
        s.add(NoteMetric(account_id=acc_id, title="当日篇", publish_time=pub,
                         views=60, follows=3, updated_at=datetime.utcnow()))
        await s.commit()
    async with smk() as s:
        rates = (await account_trends(s, _admin(), acc_id))["notes"][0]["rates"]

    assert rates["follow_rate_t1"] is None
    assert rates["follow_rate"] == round(3 / 60, 4)


def test_follow_rate_t1_key_always_present():
    """键恒定存在(含 views=0 的全 None 分支)——字段集合稳定,拿不到是 null 而非缺键。"""
    from app.services.note_metrics_service import _rates

    assert _rates({"views": 0})["follow_rate_t1"] is None
    assert _rates({"views": 10, "follows": 1})["follow_rate_t1"] is None


async def test_follow_rate_meta_quantifies_bias_and_points_to_t1(smk):
    """follow_rate 的 desc 要给出方向/量级/弱样本标注/不可用窗口并指向 t1;t1 自身要写残余误差。"""
    acc_id = await _seed(smk)
    async with smk() as s:
        fm = (await account_trends(s, _admin(), acc_id))["meta"]["field_meta"]

    desc = fm["follow_rate"]["desc"]
    assert "偏低" in desc                       # 方向
    assert "18.2%" in desc and "n=69" in desc   # 实测量级
    assert "n=1" in desc and "n=4" in desc      # 弱证据必须标出,不许当结论
    assert "无意义" in desc                     # 发布当天/次日根本不能用
    assert "follow_rate_t1" in desc             # 有出路

    t1 = fm["follow_rate_t1"]
    assert t1["window"] == "T-1" and t1["source"] == "derived"
    assert t1["unit"] == "比率(0-1)" and "派生" in t1["label"]
    assert "偏高" in t1["desc"] and "null" in t1["desc"]  # 诚实写残余误差与缺值
    # engage_rate 已核实同窗:给 skill 侧一个能结案的明确说法
    assert "已核实同窗" in fm["engage_rate"]["desc"]
