"""受众行为库 REST(5 端点):互动者身份 / 纵向轨迹 / 群体切片 / 潜客漏斗。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 4.2 节。
数据是采集单落进 ``audience_events`` 的行;**纯 DB 读,不起浏览器**,零会话成本可随意调。
所有运营口径(打分权重 / 漏斗层 / 活跃度档)都在 ``app.services.audience_analytics``,
这里只做入参校验、鉴权收窄与视图组装。

## ⚠️ 合规边界(写进 manifest,给调用方也看见)

这是**受众公开行为分析**,不是个人档案追踪:

- 只有平台在通知流里已经公开给我们的字段(userid / 昵称 / 头像 / 关系 / 公开笔记);
- 库里**没有也永远不会有** ``actor_userid`` → 来访者真实身份(姓名/手机/预约/咨询关系)
  的关联字段或表。想拿这里的 userid 去对来访者名单,答案是不行,别问第二次;
- 昵称/头像是采集时快照,会变不追溯。

## 三条读数口径(错了看不出来,所以写在这里也写进 manifest)

1. **自家号默认排除**(``exclude_self=true``)。不排的话"最活跃的受众"永远是自家矩阵号
   —— 它们互刷的量碾压真实受众。排除名单从 ``xhs_accounts.user_id`` **现查**,不硬编码;
2. **潜客分在整个可见人群里归一化,再按 filter 筛**。反过来做(先筛再归一)会让同一个人
   在"看全部"和"只看粉丝"两个视图里拿到两个分数,那个数运营立刻就不会再信;
3. **潜客分是 v1 启发式,不是概率**。转化回流数据当前不存在,权重是运营直觉的初版。
   它只保证"按这套规则排序是可复现、可解释、可调的",不保证任何人有多大可能来咨询。
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import visible_account_ids
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.audience_event import AudienceEvent
from app.models.xhs_account import XhsAccount
from app.services import audience_analytics as analytics
from app.services.audience_events import EVENT_TYPES

router = APIRouter()

# 列表排序键。**认不出就报错,绝不静默按默认排** —— 那样调用方拿到的顺序不是他要的,
# 而排序错了在一屏数据里根本看不出来。
_SORTS = {
    "score": lambda a: (-a["potential_score"], a["actor_userid"]),
    "events": lambda a: (-a["event_count"], a["actor_userid"]),
    "recent": lambda a: (-a["last_event_time"], a["actor_userid"]),
}
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500
# 平台给的四种关系取值(采集时快照)
_FSTATUS_VALUES = ("both", "fans", "follows", "none")
# 「他关注了我」的关系取值:fans 单向关注我、both 互关。none/follows/未知都不算。
_FOLLOWED_US = ("fans", "both")
# 漏斗每层带几个代表人物(运营只看数字看不到人,就没法决定下一步做什么)
_FUNNEL_EXAMPLES = 5
# 内容偏好取前几篇
_TOP_NOTES = 20

# 漏斗层的中文标签与运营含义(层定义本身在服务层,这里只是给人看的说明)
_LAYER_LABELS = {
    "stranger_touch": ("陌生轻触", "还没关注、也只来过一两次:量最大价值最低,给上面几层当分母"),
    "stranger_frequent": ("陌生高频", "反复来看却始终没关注 —— 这层就是「高互动意愿但还没转化」"),
    "follower_shallow": ("已关注浅互动", "关注了却不怎么看:内容没接住他,是选题信号"),
    "follower_active": ("已关注高频", "最接近私域的一层,导流动作优先对他们做"),
    "mutual": ("互关", "多半已是自己人/同行/自家矩阵号,不是待转化对象(打分里也降权)"),
}

_COMPLIANCE_NOTE = (
    "⚠️ **受众公开行为分析,不是个人档案**:只含平台通知流已公开给我们的字段"
    "(userid/昵称/头像/关系/公开笔记)。库里没有也不会有 userid → 来访者真实身份"
    "(姓名/手机/预约/咨询关系)的关联;不采集互动者主页。昵称/头像是采集时快照,会变不追溯。"
)
_SCORE_NOTE = (
    "潜客分是 **v1 启发式,不是概率**:转化回流数据(谁最终进了私域)当前不存在,"
    "权重是运营直觉的初版(AUDIENCE_SCORE_WEIGHTS 可配)。它只保证排序可复现、可解释、"
    "可调,**不保证任何人有多大可能来咨询**。分数在候选人群内 min-max 归一化后加权,"
    "所以它是**相对位置**,跨账号范围/跨时间不可比。"
)
_SELF_NOTE = (
    "默认 exclude_self=true 剔除自家矩阵号(user_id 从 xhs_accounts 现查,不硬编码)。"
    "自家号互刷照常入库,只是不进分析 —— 不剔的话「最活跃受众」永远是自家号。"
    "排查自家互刷量时传 exclude_self=false。"
)

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/audience/overview",
        "summary": "受众汇总卡:互动者总数 / 关系分布 / 近 7-30 天新增互动者 / 高潜人数",
        "admin_only": False,
        "params": {
            "exclude_self": "query,bool=true(是否剔除自家矩阵号,见 notes)",
        },
        "returns": "{total_actors, total_events, by_fstatus:{both|fans|follows|none:n}, "
                   "new_actors_7d, new_actors_30d, high_potential_count, "
                   "scoring:{weights, high_potential_score, calibration}, "
                   "self_exclude:{enabled, account_userids}, compliance}",
        "errors": "401=缺 apikey",
        "notes": "纯 DB 读,不起浏览器、不消耗任何账号会话额度,可随意调。"
                 "数据来自主站通知页采集单(kind=audience_sync)落的事件流,**只有赞/收藏/关注**"
                 "——评论和@(/you/mentions)本版不采。"
                 "by_fstatus 是**最近一次互动时的关系快照**:both 互关 / fans 他关注我 / "
                 "follows 我关注他 / none 陌生人;平台没给的归 unknown。"
                 "new_actors_* 按**首次**互动落在窗口内算(老受众这次又来了不算新增)。"
                 + _SELF_NOTE + _SCORE_NOTE + _COMPLIANCE_NOTE,
    },
    {
        "method": "GET", "path": "/api/audience/actors",
        "summary": "互动者列表(可按潜客分/互动次数/最近互动排序,按关系与互动类型筛)",
        "admin_only": False,
        "params": {
            "sort": "query,str=score(score 潜客分 | events 互动次数 | recent 最近互动)",
            "fstatus": "query,str|None(both/fans/follows/none,只看这类关系)",
            "followed": "query,bool|None(true=**他关注了我**的人[fans+both];false=还没关注我的"
                        "[none+follows];省略=都要)",
            "event_type": "query,str|None(like_note/fav_note/like_comment/like_share/"
                          "like_avatar/follow,只看做过这类互动的人)",
            "exclude_self": "query,bool=true",
            "limit": f"query,int={_DEFAULT_LIMIT}(1-{_MAX_LIMIT})",
            "offset": "query,int=0",
        },
        "returns": "{total, limit, offset, sort, actors:[{actor_userid, actor_nickname, "
                   "actor_image, event_count, event_types:{type:n}, account_ids, "
                   "account_count, first_event_time, last_event_time, fstatus, "
                   "potential_score, funnel_layer, activity_band}]}",
        "errors": "400=sort/fstatus/event_type/limit 取值非法;401=缺 apikey",
        "notes": "按 caller 可见账号收窄(admin 全见)。"
                 "followed 与 fstatus 的区别:fstatus 是精确到某一种关系,followed 是"
                 "**「他关注了我没有」**这一刀(fans+both vs none+follows)——漏斗上下游看这个。"
                 "⚠️ **潜客分在整个可见人群里归一化后才做 filter**:同一个人在「看全部」和"
                 "「只看粉丝」两个视图里是同一个分数。分页也在排序之后做,total 是筛后总数。"
                 "event_types 是该人各类互动的次数明细;account_ids 是他互动过的**自家号**"
                 "(跨号 = 对矩阵而非某一篇感兴趣,这是潜客分里权重第二高的维度)。"
                 + _SCORE_NOTE + _SELF_NOTE,
    },
    {
        "method": "GET", "path": "/api/audience/actors/{userid}",
        "summary": "单个互动者的完整纵向轨迹:事件时间线 + 跨号分布 + 关系演变 + 潜客分明细",
        "admin_only": False,
        "params": {
            "userid": "path,str(平台 userid,24 位 hex)",
            "exclude_self": "query,bool=true(自家号默认不可查,与列表口径一致)",
            "timeline_limit": "query,int=200(时间线最多返回几条事件)",
        },
        "returns": "{actor_userid, actor_nickname, actor_image, event_count, "
                   "first_event_time, last_event_time, fstatus, event_types, "
                   "potential_score, score_detail:{dim:{raw,normalized,weight}}, "
                   "funnel_layer, activity_band, "
                   "by_account:[{account_id, account_name, event_count, "
                   "first_event_time, last_event_time}], "
                   "relation_history:[{event_time, fstatus}], "
                   "timeline:[{event_time, event_type, account_id, target_note_id, "
                   "target_note_title, fstatus}]}",
        "errors": "404=该 userid 在可见范围内没有互动记录(或是被排除的自家号);401=缺 apikey",
        "notes": "时间线**最近的在前**。relation_history 只记**变化点**(一串重复的 none "
                 "不刷屏),它是「关系怎么演变」的全部依据 —— 从 none 变 fans 那一刻就是"
                 "这个人被内容打动的时刻。"
                 "⚠️ score_detail 里的 normalized 是**在整个可见人群里**的归一化位置,"
                 "所以单人页的分数与列表页完全一致(只拿自己归一化的话分数恒为 0,那是纯误导)。"
                 + _SCORE_NOTE + _COMPLIANCE_NOTE,
    },
    {
        "method": "GET", "path": "/api/audience/funnel",
        "summary": "潜客漏斗:五层分层人数 + 各层代表人物",
        "admin_only": False,
        "params": {"exclude_self": "query,bool=true"},
        "returns": "{total, frequent_event_threshold, layers:[{layer, label, meaning, "
                   "count, share, examples:[{actor_userid, actor_nickname, event_count, "
                   "potential_score}]}]}",
        "errors": "401=缺 apikey",
        "notes": "五层是**划分**:各层 count 之和等于 total(空层也照列 count=0,"
                 "少一层会被读成「这层没这个概念」)。层定义写死在服务层常量里并注释理由,"
                 "不散落在 SQL —— 它是运营决策不是查询细节。"
                 "**stranger_frequent(陌生高频)是这张图的重点**:反复来看却始终没关注,"
                 "正是「高互动意愿但还没转化」的那批人。"
                 "⚠️ 「高频」门槛(frequent_event_threshold)是 v1 经验值(实采分布里人均 2 次"
                 "出头,故取 3),不是心理学结论;库里数据形态变了要回来重看。" + _SELF_NOTE,
    },
    {
        "method": "GET", "path": "/api/audience/segments",
        "summary": "群体切片:关系分布 / 活跃度分档 / 互动类型分布 / 内容偏好(最受互动的笔记)",
        "admin_only": False,
        "params": {
            "exclude_self": "query,bool=true",
            "top_notes": f"query,int={_TOP_NOTES}(内容偏好取前几篇)",
        },
        "returns": "{total_actors, by_relation:{fstatus:n}, by_activity:{once|low|mid|high:n}, "
                   "by_event_type:{type:n}, content_preference:[{note_id, title, "
                   "actor_count, event_count}], bands:{band:说明}}",
        "errors": "401=缺 apikey",
        "notes": "by_relation / by_activity 按**人**计数,by_event_type 按**事件**计数 ——"
                 "两个分母不同,别放在同一张饼图里。"
                 "活跃度档位:once=1 次 / low=2-4 / mid=5-9 / high=10+(v1 经验值)。"
                 "content_preference 按事件数降序,actor_count 是**去重后的人数** ——"
                 "同一个人赞了又收藏算 2 个事件 1 个人,只看事件数会把一个铁粉读成一片热度。"
                 "note_id 是被互动的笔记,**不一定是我们发的**:赞评论那类事件记的是评论"
                 "所在的那篇,可能是别人的笔记。" + _SELF_NOTE,
    },
]


# ---------------- 公共取数 ----------------


async def _population(exclude_self: bool) -> tuple[list[dict], list[int] | None, int]:
    """取当前调用方可见的**已打分**互动者全集 + 可见账号范围 + 自家号数量。

    打分在这里做一次(整个人群),之后所有端点的 filter / 分层 / 分页都在结果上做 ——
    这是"同一个人在不同视图里必须是同一个分数"那条口径的唯一实现点。
    """
    operator = current_operator()
    async with get_session() as session:
        account_ids = await visible_account_ids(operator, session)
        self_userids = await analytics.self_account_userids(session)
        actors = await analytics.load_actor_aggregates(
            session,
            account_ids=account_ids,
            exclude_userids=self_userids if exclude_self else frozenset(),
        )
    analytics.score_actors(actors, analytics.parse_weights(settings.AUDIENCE_SCORE_WEIGHTS))
    for actor in actors:
        actor["funnel_layer"] = analytics.funnel_layer(actor)
        actor["activity_band"] = analytics.activity_band(actor["event_count"])
    return actors, account_ids, len(self_userids)


def _resolve_exclude_self(exclude_self: bool | None) -> bool:
    """未显式指定时按服务端配置(``AUDIENCE_SELF_EXCLUDE``,出厂 true)。"""
    return settings.AUDIENCE_SELF_EXCLUDE if exclude_self is None else exclude_self


def _public(actor: dict) -> dict:
    """列表视图:只交出公开身份 + 行为聚合,不带原始事件(那是单人页的事)。"""
    return {
        "actor_userid": actor["actor_userid"],
        "actor_nickname": actor["actor_nickname"],
        "actor_image": actor["actor_image"],
        "event_count": actor["event_count"],
        "event_types": actor["event_types"],
        "account_ids": actor["account_ids"],
        "account_count": actor["account_count"],
        "first_event_time": actor["first_event_time"],
        "last_event_time": actor["last_event_time"],
        "fstatus": actor["fstatus"],
        "potential_score": actor["potential_score"],
        "funnel_layer": actor["funnel_layer"],
        "activity_band": actor["activity_band"],
    }


def _epoch_days_ago(days: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())


# ---------------- 端点 ----------------


@router.get("/api/audience/overview")
async def audience_overview_endpoint(exclude_self: bool | None = None) -> dict:
    """受众汇总卡(纯 DB 读)。"""
    exclude = _resolve_exclude_self(exclude_self)
    actors, _account_ids, self_count = await _population(exclude)

    by_fstatus: dict[str, int] = {}
    for actor in actors:
        key = actor["fstatus"] or "unknown"
        by_fstatus[key] = by_fstatus.get(key, 0) + 1

    week, month = _epoch_days_ago(7), _epoch_days_ago(30)
    return {
        "total_actors": len(actors),
        "total_events": sum(a["event_count"] for a in actors),
        "by_fstatus": by_fstatus,
        # 「新增」按**首次**互动落在窗口内算:老受众这次又来了不是新增
        "new_actors_7d": sum(1 for a in actors if a["first_event_time"] >= week),
        "new_actors_30d": sum(1 for a in actors if a["first_event_time"] >= month),
        "high_potential_count": sum(
            1 for a in actors if a["potential_score"] >= analytics.HIGH_POTENTIAL_SCORE
        ),
        "scoring": {
            "weights": analytics.parse_weights(settings.AUDIENCE_SCORE_WEIGHTS),
            "high_potential_score": analytics.HIGH_POTENTIAL_SCORE,
            "calibration": (
                "v1 启发式加权,**待真实转化数据校准**:谁最终进了私域这份数据当前不存在,"
                "所以权重是运营直觉的初版,分数只是相对排序不是概率。"
            ),
        },
        "self_exclude": {"enabled": exclude, "account_userids": self_count},
        "compliance": _COMPLIANCE_NOTE,
    }


@router.get("/api/audience/actors")
async def list_audience_actors_endpoint(
    sort: str = "score",
    fstatus: str | None = None,
    followed: bool | None = None,
    event_type: str | None = None,
    exclude_self: bool | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """互动者列表。"""
    if sort not in _SORTS:
        raise HTTPException(
            400, f"sort={sort!r} 不认识,可选:{'/'.join(_SORTS)}"
        )
    if fstatus is not None and fstatus not in _FSTATUS_VALUES:
        # 不认识就报错,不静默返空:空列表会被读成"确实没有这类人",而不是"你写错了"
        raise HTTPException(
            400, f"fstatus={fstatus!r} 不认识,可选:{'/'.join(_FSTATUS_VALUES)}"
        )
    if event_type is not None and event_type not in EVENT_TYPES:
        raise HTTPException(
            400, f"event_type={event_type!r} 不认识,可选:{'/'.join(EVENT_TYPES)}"
        )
    if not 1 <= limit <= _MAX_LIMIT or offset < 0:
        raise HTTPException(400, f"limit 要在 1-{_MAX_LIMIT},offset 不能为负")

    actors, _account_ids, _self_count = await _population(
        _resolve_exclude_self(exclude_self)
    )
    # **打分之后才 filter**:同一个人在任何视图里都是同一个分数(见模块 docstring 口径 2)
    if fstatus is not None:
        actors = [a for a in actors if a["fstatus"] == fstatus]
    if followed is not None:
        # 「他关注了我没有」这一刀:fans(单向关注我)与 both(互关)都算关注了我
        actors = [
            a for a in actors
            if (a["fstatus"] in _FOLLOWED_US) is followed
        ]
    if event_type is not None:
        actors = [a for a in actors if event_type in a["event_types"]]
    actors.sort(key=_SORTS[sort])
    return {
        "total": len(actors),
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "actors": [_public(a) for a in actors[offset:offset + limit]],
    }


@router.get("/api/audience/actors/{userid}")
async def audience_actor_trajectory_endpoint(
    userid: str, exclude_self: bool | None = None, timeline_limit: int = 200,
) -> dict:
    """单个互动者的完整纵向轨迹。"""
    if not 1 <= timeline_limit <= 2000:
        raise HTTPException(400, "timeline_limit 要在 1-2000")

    exclude = _resolve_exclude_self(exclude_self)
    actors, account_ids, _self_count = await _population(exclude)
    actor = next((a for a in actors if a["actor_userid"] == userid), None)
    if actor is None:
        # 自家号被排除时也走这条:从列表看不见、直接敲 URL 却能看见,那是两套口径
        raise NotFoundError(f"互动者 {userid} 在可见范围内没有互动记录")

    async with get_session() as session:
        stmt = (
            select(AudienceEvent)
            .where(AudienceEvent.actor_userid == userid)
            .order_by(AudienceEvent.event_time.desc())
        )
        if account_ids is not None:
            stmt = stmt.where(AudienceEvent.account_id.in_(account_ids))
        events = list((await session.execute(stmt)).scalars().all())
        names = dict((await session.execute(
            select(XhsAccount.id, XhsAccount.name)
            .where(XhsAccount.id.in_({e.account_id for e in events}))
        )).all())

    by_account: dict[int, dict] = {}
    for event in events:
        slot = by_account.setdefault(event.account_id, {
            "account_id": event.account_id,
            "account_name": names.get(event.account_id),
            "event_count": 0,
            "first_event_time": event.event_time,
            "last_event_time": event.event_time,
        })
        slot["event_count"] += 1
        slot["first_event_time"] = min(slot["first_event_time"], event.event_time)
        slot["last_event_time"] = max(slot["last_event_time"], event.event_time)

    # 关系演变**只记变化点**:一串重复的 none 不刷屏,而 none → fans 那一刻正是
    # 这个人被内容打动的时刻 —— 这条时间线的全部价值就在那几个拐点上。
    relation_history: list[dict] = []
    for event in reversed(events):  # 按时间正序走一遍
        if not relation_history or relation_history[-1]["fstatus"] != event.fstatus:
            relation_history.append(
                {"event_time": event.event_time, "fstatus": event.fstatus}
            )

    return {
        **_public(actor),
        "score_detail": actor["score_detail"],
        "by_account": sorted(by_account.values(), key=lambda b: b["account_id"]),
        "relation_history": relation_history,
        "timeline": [
            {
                "event_time": e.event_time,
                "event_type": e.event_type,
                "account_id": e.account_id,
                "target_note_id": e.target_note_id,
                "target_note_title": e.target_note_title,
                "fstatus": e.fstatus,
            }
            for e in events[:timeline_limit]
        ],
    }


@router.get("/api/audience/funnel")
async def audience_funnel_endpoint(exclude_self: bool | None = None) -> dict:
    """潜客漏斗:五层人数 + 各层代表人物。"""
    actors, _account_ids, _self_count = await _population(
        _resolve_exclude_self(exclude_self)
    )
    total = len(actors)
    buckets: dict[str, list[dict]] = {layer: [] for layer in analytics.FUNNEL_LAYERS}
    for actor in actors:
        buckets[actor["funnel_layer"]].append(actor)

    layers = []
    for layer in analytics.FUNNEL_LAYERS:
        members = sorted(buckets[layer], key=_SORTS["score"])
        label, meaning = _LAYER_LABELS[layer]
        layers.append({
            "layer": layer,
            "label": label,
            "meaning": meaning,
            "count": len(members),
            "share": round(len(members) / total, 4) if total else 0.0,
            "examples": [
                {
                    "actor_userid": m["actor_userid"],
                    "actor_nickname": m["actor_nickname"],
                    "event_count": m["event_count"],
                    "potential_score": m["potential_score"],
                }
                for m in members[:_FUNNEL_EXAMPLES]
            ],
        })
    return {
        "total": total,
        "frequent_event_threshold": analytics.FREQUENT_EVENT_THRESHOLD,
        "layers": layers,
    }


@router.get("/api/audience/segments")
async def audience_segments_endpoint(
    exclude_self: bool | None = None, top_notes: int = _TOP_NOTES,
) -> dict:
    """群体切片:关系分布 / 活跃度分档 / 互动类型分布 / 内容偏好。"""
    if not 1 <= top_notes <= 200:
        raise HTTPException(400, "top_notes 要在 1-200")

    actors, account_ids, _self_count = await _population(
        _resolve_exclude_self(exclude_self)
    )
    by_relation: dict[str, int] = {}
    by_activity: dict[str, int] = {band: 0 for band in analytics.ACTIVITY_BANDS}
    by_event_type: dict[str, int] = {}
    for actor in actors:
        key = actor["fstatus"] or "unknown"
        by_relation[key] = by_relation.get(key, 0) + 1
        by_activity[actor["activity_band"]] += 1
        for event_type, count in actor["event_types"].items():
            by_event_type[event_type] = by_event_type.get(event_type, 0) + count

    return {
        "total_actors": len(actors),
        "by_relation": by_relation,
        "by_activity": by_activity,
        "by_event_type": by_event_type,
        "content_preference": await _content_preference(actors, account_ids, top_notes),
        "bands": {
            "once": "只互动过 1 次", "low": "2-4 次", "mid": "5-9 次", "high": "10 次以上",
        },
    }


async def _content_preference(
    actors: list[dict], account_ids: list[int] | None, top_notes: int
) -> list[dict]:
    """最受互动的笔记:按事件数降序,同时给**去重人数**。

    只看事件数会把一个铁粉(赞了又收藏又赞评论)读成一片热度,所以 actor_count 必须一起给。
    """
    userids = {a["actor_userid"] for a in actors}
    if not userids:
        return []
    stmt = (
        select(
            AudienceEvent.target_note_id,
            AudienceEvent.target_note_title,
            AudienceEvent.actor_userid,
        )
        .where(AudienceEvent.target_note_id.is_not(None))
        .where(AudienceEvent.actor_userid.in_(userids))
    )
    # 与列表口径一致地按可见账号收窄:少了这一句,非 admin 会从"内容偏好"里
    # 看到他无权账号收到的互动(人名看不见,笔记标题却漏出去了)
    if account_ids is not None:
        stmt = stmt.where(AudienceEvent.account_id.in_(account_ids))
    async with get_session() as session:
        rows = (await session.execute(stmt)).all()

    notes: dict[str, dict] = {}
    for note_id, title, actor_userid in rows:
        slot = notes.setdefault(note_id, {
            "note_id": note_id, "title": title, "event_count": 0, "actors": set(),
        })
        slot["event_count"] += 1
        slot["actors"].add(actor_userid)
        # 标题按最后见到的写:平台改标题后旧行留着老标题,以新的为准
        if title:
            slot["title"] = title
    ordered = sorted(
        notes.values(), key=lambda n: (-n["event_count"], n["note_id"])
    )[:top_notes]
    return [
        {
            "note_id": n["note_id"], "title": n["title"],
            "actor_count": len(n["actors"]), "event_count": n["event_count"],
        }
        for n in ordered
    ]
