"""受众分析层:互动者聚合 + 潜客打分 + 漏斗分层 + 活跃度分档。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 4 节。
``audience_rest`` 只做入参校验与视图组装,所有**运营口径**都在这里,理由随口径写在旁边。

## ⚠️ 潜客分是启发式 v1,不是科学模型

转化回流数据(谁最终真的进了私域)**当前根本不存在**,所以这套权重是运营直觉的初版。
它能做的只有一件事:把"高互动意愿但还没转化"这条直觉变成可复现、可解释、可调的排序。
它**不能**告诉你某个人有多大概率会来咨询 —— 拿它当概率用就是在给自己编数据。
真实转化数据到位后必须回来重标定权重(``AUDIENCE_SCORE_WEIGHTS`` 就是为这一天留的)。

## 归一化的口径(套 retention_scheduler 的手法)

五个维度量纲天差地别(互动次数上百 / 跨号数个位数 / epoch 秒上十亿),不归一化的话权重
之比毫无意义。故各维度先在**本次候选集内**做 min-max 归一化再加权 —— 由此:

- 分数只在**同一次查询的候选集内**可比,跨查询、跨天不可比,别拿它做趋势;
- 某维度全同值时 span=0,该维度恒取 0.0(除零保护;全同值本来也不提供区分度)。

## 合规

聚合只按 ``actor_userid`` 做,**不与任何来访者身份表 join**(库里也没有那种表,见
``app/models/audience_event.py`` 的合规段)。自家号 user_id 从 ``xhs_accounts`` **现查**
排除,不硬编码 —— 加号换号后名单自动跟上,而硬编码的名单会在下一次加号时静默失效。
"""

import json
from collections import defaultdict

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audience_event import AudienceEvent
from app.models.audience_self_userid import AudienceSelfUserid
from app.models.xhs_account import XhsAccount

# ---------------- 打分口径(全部 v1 启发式,待真实转化数据校准)----------------

# 五维度默认权重。和为 1 只是好读,代码按权重和归一,改成任意正数都对。
DEFAULT_WEIGHTS: dict[str, float] = {
    "frequency": 0.30,      # 互动事件总数:来得多本身就是意愿
    "cross_account": 0.20,  # 互动过的不同自家号数:跨号 = 对矩阵而非对某一篇感兴趣
    "recency": 0.20,        # 最近一次互动距今,越近越高
    "depth": 0.15,          # 互动形态的强度(收藏 > 赞笔记 > 赞评论/分享)
    "relation": 0.15,       # 关系位置:已关注未进私域最高潜,已是自己人降权
}
SCORE_DIMENSIONS = tuple(DEFAULT_WEIGHTS)

# 各事件类型的"意愿强度"档位(v1 启发式)。取值本身没有单位,只有**相对次序**有意义,
# 而且打分前还要再做一次 min-max 归一化,所以关心的是排序不是绝对值。
#
# - ``fav_note`` 最高:收藏 = "我以后还要回来看",是全流里最强的留存意愿;
# - ``follow`` 次之:关注 = 愿意持续接收,比单篇点赞重,但比"专门存下来"轻;
# - ``like_note`` 中档:对内容本身的认可;
# - ``like_comment`` / ``like_share`` 同档并列:赞的是我们在别处的评论 / 别人分享出去的
#   那一份,离"对这个号的内容感兴趣"隔了一层;
# - ``like_avatar`` 最低:赞头像基本是社交寒暄,与内容无关。
#
# **必须覆盖 EVENT_TYPES 全集**(单测锁死):漏一种类型不会报错,只会让那种互动静默算作
# 深度 0 —— 新增事件类型时最容易忘、也最难发现的一格。
DEPTH_BY_EVENT: dict[str, float] = {
    "fav_note": 1.0,
    "follow": 0.8,
    "like_note": 0.6,
    "like_comment": 0.3,
    "like_share": 0.3,
    "like_avatar": 0.1,
}

# 关系位置的潜客含义(v1 启发式):
# - ``fans``(他关注了我,我没关注他)= 已经明确表达兴趣却还没进私域 → 最高潜;
# - ``none`` / ``follows`` = 还没建立关系,靠其余维度区分 → 中档;
# - ``both``(互关)= 多半已经是自己人 / 同行 / 自家矩阵号 → **降权**,他们不是待转化对象。
RELATION_SCORES: dict[str, float] = {
    "fans": 1.0, "none": 0.6, "follows": 0.6, "both": 0.2,
}
# 平台没给 fstatus 时按"关系未知"处理,取与陌生人同档 —— 不知道关系不该成为加分或减分项
_RELATION_FALLBACK = 0.6

# 高潜阈值(v1 启发式):归一化后 ≥ 它算"高潜"。**它是候选集内的相对位置**,
# 不是绝对概率,所以库里人少时天然有人过线,这是设计如此(总要有相对高潜的人)。
HIGH_POTENTIAL_SCORE = 0.6


def parse_weights(raw: str | None) -> dict[str, float]:
    """解析 ``AUDIENCE_SCORE_WEIGHTS``(JSON 串)→ 五维权重;任何不对劲都回退默认并告警。

    不对劲的定义:JSON 解析失败 / 不是对象 / 某项不是数 / 有负权重 / 权重和 ≤ 0。
    缺哪项用哪项的默认值(允许只覆盖一两个维度)。**绝不带着一份坏权重去排高潜** ——
    权重错了不会报错,只会安静地把该找的人排到第三页去。
    """
    fallback = dict(DEFAULT_WEIGHTS)
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        logger.warning(f"[audience] AUDIENCE_SCORE_WEIGHTS 不是合法 JSON,回退默认: {raw!r}")
        return fallback
    if not isinstance(parsed, dict):
        logger.warning(f"[audience] AUDIENCE_SCORE_WEIGHTS 不是 JSON 对象,回退默认: {raw!r}")
        return fallback
    out: dict[str, float] = {}
    for dim in SCORE_DIMENSIONS:
        value = parsed.get(dim, DEFAULT_WEIGHTS[dim])
        try:
            out[dim] = float(value)
        except (TypeError, ValueError):
            logger.warning(f"[audience] AUDIENCE_SCORE_WEIGHTS.{dim}={value!r} 不是数,回退默认")
            return fallback
    if any(v < 0 for v in out.values()) or sum(out.values()) <= 0:
        logger.warning(f"[audience] AUDIENCE_SCORE_WEIGHTS 取值非法(负权重/和≤0),回退默认: {out}")
        return fallback
    return out


def _raw_signals(actor: dict) -> dict[str, float]:
    """把一个互动者的聚合信号摊成五个可归一化的数。"""
    types = actor.get("event_types") or ()
    return {
        "frequency": float(actor.get("event_count") or 0),
        "cross_account": float(actor.get("account_count") or 0),
        "recency": float(actor.get("last_event_time") or 0),
        # 取**最强的那一种**互动,不取平均:收藏过就是收藏过,不该被他另外点的十个赞
        # 稀释回去(平均值会让"赞得多的浅互动者"压过"只收藏过一次的深意愿者")。
        "depth": max((DEPTH_BY_EVENT.get(t, 0.0) for t in types), default=0.0),
        "relation": RELATION_SCORES.get(actor.get("fstatus"), _RELATION_FALLBACK),
    }


def score_actors(actors: list[dict], weights: dict[str, float]) -> None:
    """就地给互动者打潜客分:写入 ``potential_score`` 与 ``score_detail``。

    ``score_detail`` 是**可解释性契约**:每个维度带 raw(原始信号)/ normalized(候选集内
    归一化值)/ weight(生效权重)。运营问"凭什么这个人排第一"时,答案就在这三个数里,
    而不是"模型说的"。
    """
    if not actors:
        return
    total_weight = sum(weights.get(d, 0.0) for d in SCORE_DIMENSIONS)
    if total_weight <= 0:  # parse_weights 已保证 >0,这里是纯防御
        weights, total_weight = dict(DEFAULT_WEIGHTS), sum(DEFAULT_WEIGHTS.values())

    signals = [_raw_signals(a) for a in actors]
    normalized: list[dict[str, float]] = [{} for _ in actors]
    for dim in SCORE_DIMENSIONS:
        values = [s[dim] for s in signals]
        low, high = min(values), max(values)
        span = high - low
        for slot, value in zip(normalized, values):
            slot[dim] = 0.0 if span <= 0 else (value - low) / span

    for actor, raw, norm in zip(actors, signals, normalized):
        score = sum(weights.get(d, 0.0) * norm[d] for d in SCORE_DIMENSIONS)
        actor["potential_score"] = round(score / total_weight, 6)
        actor["score_detail"] = {
            d: {
                # 次数 / 跨号数 / epoch 秒本来就是整数,还原成 int 免得回执里出现 5.0
                "raw": int(raw[d]) if d in ("frequency", "cross_account", "recency")
                else round(raw[d], 6),
                "normalized": round(norm[d], 6),
                "weight": weights.get(d, 0.0),
            }
            for d in SCORE_DIMENSIONS
        }


# ---------------- 漏斗分层(运营决策口径,写死在这里并注释理由)----------------

# "高频"的门槛(v1 启发式):3 次。依据是号1 实采的分布 —— 452 个互动者摊 922 次互动,
# 人均 2 次出头,所以 3 次即"明显高于随手路过"。这是**相对分布的经验值不是心理学结论**,
# 库里数据形态变了要回来重看。
FREQUENT_EVENT_THRESHOLD = 3

# 漏斗五层。顺序即"离转化由远到近",但**它不是严格的时间序漏斗** —— 人不一定按这个顺序
# 走(可能一上来就关注)。它是一张**当前状态分布图**,回答"现在各类人各有多少"。
FUNNEL_LAYERS = (
    # 陌生 + 只来过一两次:量最大、价值最低,列出来是为了让上面几层的占比有分母
    "stranger_touch",
    # 陌生 + 反复来:**这层就是需求里的"高互动意愿但还没转化"**,运营最该看的一层
    "stranger_frequent",
    # 已关注我(fans)但互动浅:关注了却不怎么看,内容没接住他 —— 选题信号
    "follower_shallow",
    # 已关注我且高频:最接近私域的一层,导流动作优先对他们做
    "follower_active",
    # 互关:多半已是自己人/同行/自家矩阵号,不是待转化对象(打分里也被降权)
    "mutual",
)


def funnel_layer(actor: dict) -> str:
    """把一个互动者归进漏斗某层。五层是**划分**:任何 (fstatus, 次数) 组合都恰好落一层。

    漏一种组合,漏斗各层人数加起来就对不上总人数 —— 而那正是运营拿来做决策的数。
    """
    fstatus = actor.get("fstatus")
    frequent = int(actor.get("event_count") or 0) >= FREQUENT_EVENT_THRESHOLD
    if fstatus == "both":
        return "mutual"
    if fstatus == "fans":
        return "follower_active" if frequent else "follower_shallow"
    # none / follows / 平台没给:都还没关注我们,归到"陌生"两层
    return "stranger_frequent" if frequent else "stranger_touch"


# 活跃度分档(v1 启发式):按互动次数切四档,门槛与漏斗的"高频"同源。
# 用左闭区间的下界表示,``activity_band`` 从高往低找第一个够得着的。
ACTIVITY_BANDS = ("once", "low", "mid", "high")
_BAND_FLOORS = ((10, "high"), (5, "mid"), (2, "low"), (0, "once"))


def activity_band(event_count: int) -> str:
    """互动次数 → 活跃度档位(1 次 once / 2-4 low / 5-9 mid / 10+ high)。"""
    for floor, band in _BAND_FLOORS:
        if event_count >= floor:
            return band
    return "once"


# ---------------- 聚合查询 ----------------


async def self_account_userids(session: AsyncSession) -> set[str]:
    """自家号 user_id 名单 = 活名单 ∪ 追加型登记表(**进过矩阵就永远排除**)。

    自家号互刷的互动照常入库(它也是数据),但分析默认把它们剔除 —— 不剔的话"最活跃的
    受众"永远是自家矩阵号,整个库就废了。硬编码一份名单会在加号时静默失效;而**只查
    活名单会在删号时静默失效**(2026-08-13 号9 事故:账号行被移出系统后,它 55 条互刷
    事件以互动第一名顶在漏斗头部,还顶着改过的昵称「淡三花」)。所以:每次调用把活名单
    合并进 ``audience_self_userids`` 登记表(新号自动登记、只进不出),排除按登记表走。
    空串不进名单:那会退化成"排除 fstatus 缺失的真受众"。
    """
    rows = (await session.execute(
        select(XhsAccount.user_id).where(XhsAccount.user_id.is_not(None))
    )).scalars().all()
    live = {(uid or "").strip() for uid in rows} - {""}
    known = set((await session.execute(
        select(AudienceSelfUserid.user_id)
    )).scalars().all())
    missing = live - known
    if missing:
        for uid in sorted(missing):
            await session.execute(
                sqlite_insert(AudienceSelfUserid)
                .values(user_id=uid)
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
        await session.commit()
    return known | live


async def load_actor_aggregates(
    session: AsyncSession,
    *,
    account_ids: list[int] | None = None,
    exclude_userids=frozenset(),
) -> list[dict]:
    """按 ``actor_userid`` 聚合出互动者列表(**不打分**,打分由 ``score_actors`` 单独做)。

    Args:
        account_ids: 只看这些自家号收到的互动;None = 全部(admin 口径)。
        exclude_userids: 排除的 userid 集合(默认调用方传自家号那批)。

    Returns:
        每人一个 dict:昵称/头像(**最近一次**快照)、事件数、跨号数与号列表、首末互动时刻、
        各事件类型计数、最近一次的 fstatus。按最近互动时间倒序。

    为什么在 Python 里聚合而不是写一条 GROUP BY:要的东西里有"各事件类型计数"(需要
    按类型摊开)和"最近一次的昵称/关系"(需要按时间取最后一行),用 SQL 表达要三四个子查询
    再 join 回来。受众库是分析资产、量级在万级以内,一次全取回来在内存里摊开更直白也更好改。
    真到十万级再谈把它推回 SQL。
    """
    # 按时间升序遍历,**同秒时再按 id 定序**:平台一秒内下发两条(赞了又收藏)完全常见,
    # 少了第二个排序键,"最近一次的关系/昵称"取到哪一条就成了 SQLite 的自由发挥。
    stmt = select(AudienceEvent).order_by(
        AudienceEvent.event_time.asc(), AudienceEvent.id.asc()
    )
    if account_ids is not None:
        if not account_ids:
            return []
        stmt = stmt.where(AudienceEvent.account_id.in_(account_ids))
    if exclude_userids:
        stmt = stmt.where(AudienceEvent.actor_userid.not_in(list(exclude_userids)))

    buckets: dict[str, dict] = {}
    accounts: dict[str, set[int]] = defaultdict(set)
    for row in (await session.execute(stmt)).scalars().all():
        bucket = buckets.get(row.actor_userid)
        if bucket is None:
            bucket = buckets[row.actor_userid] = {
                "actor_userid": row.actor_userid,
                "event_count": 0,
                "event_types": {},
                "first_event_time": row.event_time,
                "last_event_time": row.event_time,
            }
        bucket["event_count"] += 1
        bucket["event_types"][row.event_type] = (
            bucket["event_types"].get(row.event_type, 0) + 1
        )
        bucket["first_event_time"] = min(bucket["first_event_time"], row.event_time)
        bucket["last_event_time"] = max(bucket["last_event_time"], row.event_time)
        accounts[row.actor_userid].add(row.account_id)
        # 行按 event_time 升序遍历,所以最后写进去的就是**最近一次**的快照。
        # 昵称/头像会变、关系会演变,拿最早那次去判"他还没关注我"是错的。
        bucket["actor_nickname"] = row.actor_nickname
        bucket["actor_image"] = row.actor_image
        bucket["fstatus"] = row.fstatus

    for userid, bucket in buckets.items():
        bucket["account_ids"] = sorted(accounts[userid])
        bucket["account_count"] = len(bucket["account_ids"])
    return sorted(
        buckets.values(), key=lambda b: (-b["last_event_time"], b["actor_userid"])
    )
