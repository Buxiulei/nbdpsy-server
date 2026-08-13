"""受众事件解析器单测:**全部样本来自真号取证快照**,一条都不是手写的。

这个纪律是本仓血案换来的:照一条样例写解析器 → 单测全绿 → 线上 100% 失配。通知流的
五种事件长得像但取笔记 id 的位置各不相同,只看一条 ``liked/item`` 会得出"``item_info.id``
就是笔记 id"这个结论,而它对 ``faved/item``(那是收藏夹 id)和 ``liked/item``+``avatar``
(那是头像文件 id)都是**错的且不会报错**——它会安静地把收藏夹 id 当笔记 id 记一辈子。

夹具 ``tests/fixtures/audience/notification_messages.json`` 从
``data/scene_captures/notification_probe_account_1_20260812T082020Z.json``(号1 实采
922 条赞收藏 + 98 条关注)抽出,结构逐字保留,只把互动者身份三字段做了确定性假名化
(解析器认结构不认具体 userid 串,而把真人的平台 id 与昵称推进 git 历史没有必要)。

这里锁死四条"看一条样例必然写错"的分叉:

1. ``faved/item`` 的笔记在 ``item_info.attach_item_info``,``item_info`` 本体是收藏夹;
2. ``liked/item`` + ``item_info.type=avatar`` 是赞头像,**没有笔记**,而它的
   ``item_info.id`` 是个长得很像 id 的头像文件名——照抄就会记一条假笔记;
3. ``liked/share/item`` 的 ``item_info`` **没有 content 键**,标题只能是 None;
4. connections 的头像字段叫 ``images``(复数)且互动者挂在 ``user`` 而不是 ``user_info``。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.audience_event import AudienceEvent
from app.services import audience_events

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "audience" / "notification_messages.json"
)


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _likes() -> list[dict]:
    return _fixture()["likes"]


def _connections() -> list[dict]:
    return _fixture()["connections"]


def _first(msgs: list[dict], event_type: str, item_type: str | None = None) -> dict:
    for m in msgs:
        if m.get("type") != event_type:
            continue
        if item_type is not None and (m.get("item_info") or {}).get("type") != item_type:
            continue
        return m
    raise AssertionError(f"夹具里没有 type={event_type} item_type={item_type} 的真实样本")


# ---------------- 五种事件各自的规范化 ----------------


def test_like_note_takes_item_info():
    """``liked/item`` + note_info → like_note,笔记取 ``item_info.id`` / ``.content``。"""
    row = audience_events.normalize_like_event(_first(_likes(), "liked/item", "note_info"), 1)

    assert row["event_type"] == "like_note"
    assert row["target_note_id"] == "6a7c1a61000000002402e62c"
    assert row["target_note_title"] == "过度换气｜不是喘不上气，是气吸太多了"
    assert row["platform_event_id"] == "7673038974943840268"
    assert row["event_time"] == 1786518603
    assert row["account_id"] == 1
    assert row["fstatus"] == "both"
    assert row["actor_userid"] and row["actor_nickname"]


def test_fav_note_takes_attach_item_info_not_the_board():
    """``faved/item``:笔记在 attach_item_info,``item_info`` 本体是**收藏夹**。

    照 ``liked/item`` 抄会把收藏夹 id(6248112f…,标题「默认专辑」)记成笔记 id ——
    不报错、不失败,只是从此这一列全是错的。
    """
    msg = _first(_likes(), "faved/item")
    row = audience_events.normalize_like_event(msg, 1)

    assert row["event_type"] == "fav_note"
    assert row["target_note_id"] == "6a7c1a61000000002402e62c"
    assert row["target_note_title"] == "过度换气｜不是喘不上气，是气吸太多了"
    # 反向断言:绝不能是收藏夹那一层
    assert row["target_note_id"] != msg["item_info"]["id"]
    assert row["target_note_title"] != msg["item_info"]["content"]


def test_like_avatar_has_no_note():
    """赞头像:``item_info.id`` 是头像文件名,不是笔记 —— target 必须留空。"""
    msg = _first(_likes(), "liked/item", "avatar")
    row = audience_events.normalize_like_event(msg, 1)

    assert row["event_type"] == "like_avatar"
    assert row["target_note_id"] is None
    assert row["target_note_title"] is None
    assert msg["item_info"]["id"]  # 平台确实给了个 id,只是它不是笔记 id


def test_like_comment_takes_host_note():
    """赞评论:记的是**评论所在的那篇笔记**(可能是别人的笔记,这是真实形态)。"""
    row = audience_events.normalize_like_event(_first(_likes(), "liked/comment"), 1)

    assert row["event_type"] == "like_comment"
    assert row["target_note_id"] == "6a671311000000000f00a3a6"
    assert row["target_note_title"] == "重庆还是有甜妹的"


def test_like_share_has_note_id_but_no_title():
    """``liked/share/item``:有笔记 id,但 ``item_info`` **没有 content 键** → 标题 None。

    真实数据如此。写死"标题必有"的解析器会在这里 KeyError,而给个空串会让
    "平台没给标题"和"标题是空的"混成一个值。
    """
    msg = _first(_likes(), "liked/share/item")
    row = audience_events.normalize_like_event(msg, 1)

    assert row["event_type"] == "like_share"
    assert row["target_note_id"] == "6760f8a0000000000800d54b"
    assert row["target_note_title"] is None
    assert "content" not in msg["item_info"]


def test_connection_uses_user_and_plural_images():
    """connections:互动者挂在 ``user``(不是 user_info),头像键是 ``images``(复数)。"""
    msg = _connections()[0]
    row = audience_events.normalize_connection_event(msg, 7)

    assert row["event_type"] == "follow"
    assert row["account_id"] == 7
    assert row["actor_userid"] == msg["user"]["userid"]
    assert row["actor_nickname"] == msg["user"]["nickname"]
    assert row["actor_image"] == msg["user"]["images"]  # 复数键,likes 那边叫 image
    assert row["target_note_id"] is None
    assert row["fstatus"] == msg["user"]["fstatus"]
    assert row["event_time"] == msg["time"]


# ---------------- 全量夹具的整体性质 ----------------


def test_every_real_like_message_parses():
    """夹具里每一条真实 likes 事件都解析得出来,且 event_type 落在已知六态内。"""
    rows = [audience_events.normalize_like_event(m, 1) for m in _likes()]

    assert all(r is not None for r in rows), "有真实事件被解析器丢掉了"
    assert {r["event_type"] for r in rows} <= set(audience_events.EVENT_TYPES)
    # 五种分叉在夹具里都真的出现过(夹具退化成单一形态时这条会失败)
    assert {r["event_type"] for r in rows} == {
        "like_note", "fav_note", "like_comment", "like_share", "like_avatar",
    }
    for row in rows:
        assert row["platform_event_id"] and row["event_time"] > 0
        assert row["actor_userid"]
        # raw_json 留档:解析口径以后变了还能拿原始 message 回溯
        assert json.loads(row["raw_json"])["id"] == row["platform_event_id"]


def test_unknown_type_returns_none_without_raising():
    """未知 type 返回 None(记 warning),不抛 —— 平台加一种新通知不该打死整轮采集。"""
    msg = dict(_first(_likes(), "liked/item", "note_info"), type="poked/you")

    assert audience_events.normalize_like_event(msg, 1) is None


def test_message_without_event_id_is_dropped():
    """没有平台事件 id 的 message 丢掉:去重键缺了,入库就是一行永远去不掉重的脏数据。"""
    msg = dict(_first(_likes(), "liked/item", "note_info"))
    msg.pop("id")

    assert audience_events.normalize_like_event(msg, 1) is None


# ---------------- 入库幂等 ----------------


@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_replay(db):
    """同一批真实事件重采一遍不产生重复行(平台真的会重复下发,见夹具里那对同 id 事件)。"""
    rows = [r for r in (audience_events.normalize_like_event(m, 1) for m in _likes()) if r]

    first = await audience_events.upsert_events(db, rows)
    second = await audience_events.upsert_events(db, rows)

    stored = (await db.execute(select(AudienceEvent))).scalars().all()
    # 夹具里 30 条真实事件中有一对是**平台重复下发的同一个事件 id**(922 条实采里 921 个
    # 唯一 id),所以入库行数天然少于 message 条数 —— 这正是 UNIQUE 该拦住的东西。
    assert len(stored) == len({r["platform_event_id"] for r in rows}) < len(rows)
    assert first == len(stored)
    assert second == 0, "重采不该再插入任何行"


@pytest.mark.asyncio
async def test_same_event_id_across_accounts_kept(db):
    """去重键带 account_id:同一个事件 id 落在不同自家号下是两行,不能互相顶掉。"""
    msg = _first(_likes(), "liked/item", "note_info")
    rows = [
        audience_events.normalize_like_event(msg, 1),
        audience_events.normalize_like_event(msg, 2),
    ]

    inserted = await audience_events.upsert_events(db, rows)

    assert inserted == 2
    stored = (await db.execute(select(AudienceEvent))).scalars().all()
    assert {r.account_id for r in stored} == {1, 2}


@pytest.mark.asyncio
async def test_upsert_empty_is_noop(db):
    assert await audience_events.upsert_events(db, []) == 0
