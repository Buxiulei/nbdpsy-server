"""受众分析层单测:潜客打分 / 漏斗分层 / 活跃度分档 / 自家号排除。

打分是**启发式 v1,不是科学模型** —— 转化回流数据(谁最终进了私域)当前根本不存在。
所以这里锁的不是"分数对不对"(没有真值可对),而是**这套加权确实按声明的方向工作**:
互动多的比互动少的高、跨号的比单号的高、收藏的比赞评论的高、已经互关的自己人被降权。
真实转化数据到位后权重要重调,那时这些方向性断言仍然成立,只有权重值会变。
"""

import json

import pytest

from app.models import AudienceEvent, XhsAccount
from app.services import audience_analytics as an


def _actor(userid: str, **over) -> dict:
    """一个打分入参的最小骨架(各维度信号齐全,便于逐维度单变量对比)。"""
    base = {
        "actor_userid": userid,
        "event_count": 5,
        "account_count": 1,
        "last_event_time": 1_786_000_000,
        "event_types": ["like_note"],
        "fstatus": "none",
    }
    base.update(over)
    return base


def _scored(actors: list[dict], weights: dict | None = None) -> dict[str, float]:
    an.score_actors(actors, weights or an.DEFAULT_WEIGHTS)
    return {a["actor_userid"]: a["potential_score"] for a in actors}


# ---------------- 权重可配 ----------------


def test_weights_parse_and_override():
    parsed = an.parse_weights(json.dumps({"frequency": 9.0}))

    assert parsed["frequency"] == 9.0
    # 没覆盖的维度沿用默认(允许只调一两项)
    assert parsed["recency"] == an.DEFAULT_WEIGHTS["recency"]


@pytest.mark.parametrize(
    "raw", ["", "不是 json", "[1,2]", '{"frequency":"多"}', '{"frequency":-1}',
            '{"frequency":0,"cross_account":0,"recency":0,"depth":0,"relation":0}'],
)
def test_bad_weights_fall_back_to_defaults(raw):
    """坏权重一律回退默认:权重错了不会报错,只会安静地把高潜排错序。"""
    assert an.parse_weights(raw) == an.DEFAULT_WEIGHTS


def test_configured_weights_actually_change_ranking():
    """权重是真的生效的:只给 depth 权重时,收藏过的那个必须反超互动更多的那个。"""
    actors = [
        _actor("多但浅", event_count=50, event_types=["like_comment"]),
        _actor("少但深", event_count=1, event_types=["fav_note"]),
    ]
    only_depth = {"frequency": 0.0, "cross_account": 0.0, "recency": 0.0,
                  "depth": 1.0, "relation": 0.0}

    scores = _scored(actors, only_depth)

    assert scores["少但深"] > scores["多但浅"]


# ---------------- 五个维度各自的方向 ----------------


def test_frequency_dimension():
    scores = _scored([_actor("少", event_count=1), _actor("多", event_count=20)])
    assert scores["多"] > scores["少"]


def test_cross_account_dimension():
    """跨号互动 = 对矩阵而非对某一篇感兴趣,是更强的潜客信号。"""
    scores = _scored([_actor("单号", account_count=1), _actor("跨号", account_count=4)])
    assert scores["跨号"] > scores["单号"]


def test_recency_dimension():
    scores = _scored([
        _actor("很久以前", last_event_time=1_700_000_000),
        _actor("昨天", last_event_time=1_786_000_000),
    ])
    assert scores["昨天"] > scores["很久以前"]


def test_depth_ordering_fav_beats_like_beats_comment():
    """收藏 > 赞笔记 > 赞评论/分享:收藏是更强的"我以后还要看"意愿信号。"""
    assert (
        an.DEPTH_BY_EVENT["fav_note"]
        > an.DEPTH_BY_EVENT["like_note"]
        > an.DEPTH_BY_EVENT["like_comment"]
    )
    assert an.DEPTH_BY_EVENT["like_comment"] == an.DEPTH_BY_EVENT["like_share"]
    # 每种规范化事件都要有档位,否则聚合到一个没登记的类型就 KeyError 打死整轮打分
    from app.services.audience_events import EVENT_TYPES

    assert set(an.DEPTH_BY_EVENT) == set(EVENT_TYPES)


def test_depth_takes_the_strongest_signal():
    """一个人有多种互动时按**最强的那种**算深度,不取平均 —— 收藏过就是收藏过,
    不该被他另外点的十个赞稀释回去。"""
    scores = _scored([
        _actor("只点赞", event_types=["like_note"]),
        _actor("赞很多外加收藏一次", event_types=["like_note", "fav_note"]),
    ])
    assert scores["赞很多外加收藏一次"] > scores["只点赞"]


def test_relation_fans_highest_mutual_penalized():
    """``fans``(关注了我却还没进私域)最高潜;``both``(已是自己人)降权。"""
    scores = _scored([
        _actor("粉丝", fstatus="fans"),
        _actor("陌生人", fstatus="none"),
        _actor("互关", fstatus="both"),
    ])
    assert scores["粉丝"] > scores["陌生人"] > scores["互关"]


# ---------------- 归一化的边界 ----------------


def test_all_equal_dimension_contributes_zero_not_crash():
    """某维度全同值 → span=0,该维度恒取 0(除零保护;全同值本来也没有区分度)。"""
    actors = [_actor("甲"), _actor("乙")]

    scores = _scored(actors)

    assert scores["甲"] == scores["乙"] == 0.0
    assert actors[0]["score_detail"]["frequency"]["normalized"] == 0.0


def test_single_actor_does_not_divide_by_zero():
    actors = [_actor("独苗")]
    an.score_actors(actors, an.DEFAULT_WEIGHTS)
    assert actors[0]["potential_score"] == 0.0


def test_score_detail_is_explainable():
    """分项明细必须能解释分数:每个维度都带原始值 + 归一化值 + 权重,不是黑盒。"""
    actors = [_actor("甲", event_count=1), _actor("乙", event_count=9)]
    an.score_actors(actors, an.DEFAULT_WEIGHTS)

    detail = actors[1]["score_detail"]
    assert set(detail) == set(an.DEFAULT_WEIGHTS)
    for dim, slot in detail.items():
        assert set(slot) == {"raw", "normalized", "weight"}
        assert slot["weight"] == an.DEFAULT_WEIGHTS[dim]
    assert detail["frequency"]["raw"] == 9
    assert detail["frequency"]["normalized"] == 1.0


def test_score_is_bounded_0_1():
    actors = [_actor("低", event_count=1, account_count=1, fstatus="both",
                     event_types=["like_avatar"], last_event_time=1),
              _actor("高", event_count=99, account_count=9, fstatus="fans",
                     event_types=["fav_note"], last_event_time=1_786_000_000)]
    scores = _scored(actors)
    assert scores["低"] == 0.0 and scores["高"] == 1.0


def test_scoring_empty_list_is_noop():
    an.score_actors([], an.DEFAULT_WEIGHTS)  # 不抛即通过


# ---------------- 漏斗分层 / 活跃度分档 ----------------


def test_funnel_layers_cover_every_actor_exactly_once():
    """分层必须是**划分**:任何 (fstatus, 互动次数) 组合都恰好落进一层。

    漏一种组合 = 漏斗里的人数加起来对不上总人数,而那是运营拿来做决策的数。
    """
    combos = [
        {"fstatus": f, "event_count": n}
        for f in ("none", "follows", "fans", "both", None)
        for n in (1, an.FREQUENT_EVENT_THRESHOLD, an.FREQUENT_EVENT_THRESHOLD + 5)
    ]
    layers = [an.funnel_layer(c) for c in combos]

    assert all(layer in an.FUNNEL_LAYERS for layer in layers)
    assert len(set(layers)) > 1


def test_funnel_stranger_frequent_is_the_high_potential_layer():
    """反复来看却始终没关注的陌生人 —— 这层就是"高互动意愿但还没转化"的人。"""
    assert an.funnel_layer(
        {"fstatus": "none", "event_count": an.FREQUENT_EVENT_THRESHOLD}
    ) == "stranger_frequent"
    assert an.funnel_layer({"fstatus": "none", "event_count": 1}) == "stranger_touch"
    assert an.funnel_layer({"fstatus": "fans", "event_count": 1}) == "follower_shallow"
    assert an.funnel_layer({"fstatus": "both", "event_count": 99}) == "mutual"


def test_activity_bands_are_ordered_and_total():
    bands = [an.activity_band(n) for n in (1, 3, 7, 50)]
    assert bands == ["once", "low", "mid", "high"]
    assert set(bands) <= set(an.ACTIVITY_BANDS)


# ---------------- 自家号排除(合规 + 口径)----------------


@pytest.mark.asyncio
async def test_self_account_userids_read_live_not_hardcoded(db):
    """自家号 user_id **现查 xhs_accounts**,不硬编码 —— 加号/换号后名单自动跟上。"""
    db.add(XhsAccount(id=1, name="内容号", user_id="self-aaa"))
    db.add(XhsAccount(id=2, name="互动号", user_id="self-bbb"))
    db.add(XhsAccount(id=3, name="没登录过的号", user_id=None))
    await db.commit()

    assert await an.self_account_userids(db) == {"self-aaa", "self-bbb"}


@pytest.mark.asyncio
async def test_actor_aggregates_exclude_self_and_aggregate_across_accounts(db):
    """聚合按 userid 跨自家号合并;自家号互刷的行默认剔除。"""
    db.add(XhsAccount(id=1, name="号一", user_id="self-aaa"))
    db.add(XhsAccount(id=2, name="号二", user_id="self-bbb"))
    await db.commit()
    for account_id, actor, etype, when, fstatus in (
        (1, "outsider", "like_note", 100, "none"),
        (2, "outsider", "fav_note", 200, "fans"),
        (1, "self-aaa", "like_note", 300, "both"),
    ):
        db.add(AudienceEvent(
            account_id=account_id, platform_event_id=f"{actor}-{when}",
            actor_userid=actor, actor_nickname=actor, event_type=etype,
            fstatus=fstatus, event_time=when, raw_json="{}",
        ))
    await db.commit()

    rows = await an.load_actor_aggregates(db, exclude_userids={"self-aaa", "self-bbb"})

    assert [r["actor_userid"] for r in rows] == ["outsider"]
    row = rows[0]
    assert row["event_count"] == 2
    assert row["account_count"] == 2 and row["account_ids"] == [1, 2]
    assert row["first_event_time"] == 100 and row["last_event_time"] == 200
    assert row["event_types"] == {"like_note": 1, "fav_note": 1}
    # 关系取**最近一次**快照:关系会演变,拿最早那次去判"还没关注我"就是错的
    assert row["fstatus"] == "fans"


@pytest.mark.asyncio
async def test_actor_aggregates_can_keep_self_accounts(db):
    """排除是**默认**不是强制:排查自家互刷量时要看得见它们。"""
    db.add(XhsAccount(id=1, name="号一", user_id="self-aaa"))
    await db.commit()
    db.add(AudienceEvent(
        account_id=1, platform_event_id="e1", actor_userid="self-aaa",
        actor_nickname="号一", event_type="like_note", fstatus="both",
        event_time=1, raw_json="{}",
    ))
    await db.commit()

    rows = await an.load_actor_aggregates(db, exclude_userids=frozenset())

    assert [r["actor_userid"] for r in rows] == ["self-aaa"]


@pytest.mark.asyncio
async def test_actor_aggregates_narrow_to_visible_accounts(db):
    """按可见账号收窄(非 admin 只看得到自己被授权的号收到的互动)。"""
    db.add(XhsAccount(id=1, name="号一", user_id="self-aaa"))
    db.add(XhsAccount(id=2, name="号二", user_id="self-bbb"))
    await db.commit()
    for account_id in (1, 2):
        db.add(AudienceEvent(
            account_id=account_id, platform_event_id=f"e{account_id}",
            actor_userid=f"u{account_id}", actor_nickname="谁", event_type="like_note",
            fstatus="none", event_time=account_id, raw_json="{}",
        ))
    await db.commit()

    rows = await an.load_actor_aggregates(db, account_ids=[1])

    assert [r["actor_userid"] for r in rows] == ["u1"]


@pytest.mark.asyncio
async def test_self_userids_ignore_blank(db):
    """空串 user_id 不能进排除名单 —— 那会把 fstatus 缺失的真受众一起排掉。"""
    db.add(XhsAccount(id=1, name="空号", user_id=""))
    await db.commit()
    assert await an.self_account_userids(db) == set()


# ---------------- 号9 泄漏回归(2026-08-13 事故) ----------------


@pytest.mark.asyncio
async def test_former_self_account_still_excluded_after_row_deleted(db):
    """账号行被删后其 user_id 仍在排除名单(号9 事故复现):进过矩阵就永远排除。"""
    acct = XhsAccount(id=9, name="米之木木", user_id="self-ex9")
    db.add(acct)
    await db.commit()
    # 第一次调用:活名单合并进登记表
    assert "self-ex9" in await an.self_account_userids(db)

    # 号被移出系统(2026-08-13 生产真实发生:xhs_accounts 只剩 9 行)
    await db.delete(acct)
    await db.commit()

    # 活名单已无此号,但登记表记得它——排除名单必须仍含
    assert "self-ex9" in await an.self_account_userids(db)


@pytest.mark.asyncio
async def test_new_account_auto_registered(db):
    """新加的号在下一次调用即自动进登记表(加号场景不回退)。"""
    assert await an.self_account_userids(db) == set()
    db.add(XhsAccount(id=2, name="新号", user_id="self-new"))
    await db.commit()

    assert "self-new" in await an.self_account_userids(db)
