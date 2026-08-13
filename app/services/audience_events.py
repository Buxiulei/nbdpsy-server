"""通知流 message → ``audience_events`` 行的规范化 + 幂等入库。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 3.3 节。
本模块**不碰浏览器、不碰网络**,纯函数 + 一个批量 upsert,故可以拿真号取证快照当夹具
逐条断言(见 ``tests/test_audience_events.py``)。

## 为什么这个文件值得写这么多注释

五种事件长得几乎一样,**取笔记 id 的位置却各不相同**,而取错了不会报错 —— 它只会安静地
往库里记一串错 id。本仓已经为"照一条样例写解析器"付过一次代价(单测全绿 / 线上全错),
所以这里把每条分叉的**真实形态**连同反例一起写在代码旁边:

| 平台 type | item_info.type | 规范化 | 笔记 id / 标题在哪 |
|---|---|---|---|
| ``liked/item`` | ``note_info`` | ``like_note`` | ``item_info.id`` / ``.content`` |
| ``liked/item`` | ``avatar`` | ``like_avatar`` | **没有笔记**(``item_info.id`` 是头像文件名) |
| ``faved/item`` | ``board_info`` | ``fav_note`` | ``item_info.attach_item_info.id`` / ``.content`` |
| ``liked/comment`` | ``note_info`` | ``like_comment`` | ``item_info.id`` / ``.content``(评论**所在**的笔记) |
| ``liked/share/item`` | ``note_info`` | ``like_share`` | ``item_info.id``;**没有 content 键** → 标题 None |

connections 那条流字段更薄且**键名不同**:互动者挂在 ``user``(不是 ``user_info``),
头像键叫 ``images``(复数)。照 likes 抄会解析出一片空。

## 两条丢弃纪律

- **未知 type 返回 None 并记 warning,不抛**:平台加一种新通知不该打死整轮采集
  (采回来的其余几百条事件比"严格失败"值钱);
- **缺平台事件 id 或 userid 的 message 丢掉**:前者是去重键,缺了就是一行永远去不掉重的
  脏数据;后者是这条记录存在的全部意义。
"""

import json

from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audience_event import AudienceEvent

# 规范化后的事件类型全集(库里 event_type 列的取值域)
EVENT_TYPES = (
    "like_note", "fav_note", "like_comment", "like_share", "like_avatar", "follow",
)

# 通知 tab「新增关注」里唯一的事件 type
_FOLLOW_TYPE = "follow/you"


def normalize_like_event(msg: dict, account_id: int) -> dict | None:
    """一条 ``/you/likes`` 的 message → 入库行;未知/残缺返回 None(记 warning,不抛)。"""
    msg = msg or {}
    item = msg.get("item_info") or {}
    raw_type = msg.get("type")

    if raw_type == "liked/item":
        # **必须按 item_info.type 再分一次叉**:赞笔记与赞头像共用同一个平台 type,
        # 而赞头像的 item_info.id 是个长得很像笔记 id 的头像文件名(实采
        # "1040g2jo31504tm7b146g5noda2v08d26ag6iiko")—— 不分叉就会凭空造出一篇假笔记。
        if item.get("type") == "avatar":
            event_type, note = "like_avatar", {}
        else:
            event_type, note = "like_note", item
    elif raw_type == "faved/item":
        # 收藏事件的 item_info **是收藏夹**(type=board_info,content 是「默认专辑」这类
        # 夹子名),笔记在它下面的 attach_item_info 里。这是全表最容易抄错的一格。
        event_type, note = "fav_note", item.get("attach_item_info") or {}
    elif raw_type == "liked/comment":
        # 赞的是我们发的评论,记的是**评论所在的那篇笔记** —— 它常常是别人的笔记
        # (我们在别处评论,有人给那条评论点了赞),所以这一列 join 不到自家台账很正常。
        event_type, note = "like_comment", item
    elif raw_type == "liked/share/item":
        event_type, note = "like_share", item
    else:
        logger.warning(f"[audience_events] 未知 likes 事件 type={raw_type!r},丢弃该条")
        return None

    return _row(msg, account_id, event_type, msg.get("user_info") or {}, note)


def normalize_connection_event(msg: dict, account_id: int) -> dict | None:
    """一条 ``/you/connections`` 的 message → 入库行;非关注事件/残缺返回 None。

    ⚠️ connections 是**历史事件流**,不是当前粉丝快照:号1 实采 98 条 vs 主页显示 93 粉丝,
    差的那几个是后来取关的。拿这条流当粉丝数用一定对不上,口径不能混。

    (签名比设计文档里的 ``-> dict`` 宽一格:残缺 message 与 likes 那边同样返回 None,
    两条流的调用方才能用同一句 ``if row is None: continue`` 收口。)
    """
    msg = msg or {}
    if msg.get("type") != _FOLLOW_TYPE:
        logger.warning(
            f"[audience_events] 未知 connections 事件 type={msg.get('type')!r},丢弃该条"
        )
        return None
    # 互动者挂在 ``user``,头像键是 ``images``(复数)—— 与 likes 的 ``user_info`` /
    # ``image`` 都不同名。照 likes 抄会解析出一片空值而且不报错。
    user = msg.get("user") or {}
    return _row(
        msg, account_id, "follow",
        {**user, "image": user.get("images")},
        {},
    )


def _row(msg: dict, account_id: int, event_type: str, actor: dict, note: dict) -> dict | None:
    """组装入库行;去重键(事件 id)或人身键(userid)缺失即丢弃。"""
    event_id = str(msg.get("id") or "").strip()
    userid = str(actor.get("userid") or "").strip()
    if not event_id or not userid:
        logger.warning(
            f"[audience_events] 事件缺 id/userid(type={msg.get('type')!r}),丢弃该条"
        )
        return None
    return {
        "account_id": account_id,
        "platform_event_id": event_id,
        "actor_userid": userid,
        "actor_nickname": actor.get("nickname") or "",
        "actor_image": actor.get("image") or None,
        "event_type": event_type,
        # note 为空 dict = 这类事件本来就没有笔记(follow / like_avatar),不是没读到。
        # ``liked/share/item`` 的 item_info 真的没有 content 键 → 标题给 None,
        # 不给空串:"平台没给标题"和"标题是空的"必须分得开。
        "target_note_id": note.get("id") or None,
        "target_note_title": note.get("content") or None,
        "fstatus": actor.get("fstatus") or None,
        "event_time": int(msg.get("time") or 0),
        "raw_json": json.dumps(msg, ensure_ascii=False),
    }


async def upsert_events(session: AsyncSession, rows: list[dict]) -> int:
    """批量幂等入库,返回**真正新插入**的行数(重复行不计)。

    ``ON CONFLICT(account_id, platform_event_id) DO NOTHING``:重采同一段时间原样跑一遍
    只会全部撞上 UNIQUE 被丢掉。**不做 DO UPDATE** —— 昵称/头像按设计是采集时快照,
    重采时用新值把老值盖掉就等于偷偷改写历史,而本库明确不做历史画像比对。

    平台自己也会重复下发:实采 922 条 message 里只有 921 个唯一事件 id。所以同一批 rows
    内部就可能自带重复,``executemany`` 逐条 DO NOTHING 天然消化掉。
    """
    if not rows:
        return 0
    inserted = 0
    for row in rows:
        result = await session.execute(
            sqlite_insert(AudienceEvent)
            .values(**row)
            .on_conflict_do_nothing(
                index_elements=["account_id", "platform_event_id"]
            )
        )
        inserted += result.rowcount or 0
    await session.commit()
    return inserted
