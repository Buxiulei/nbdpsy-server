"""矩阵互刷完成率总账:每篇公开笔记 × 池内其余每个号,做完了几格。

老板的 KPI 是一句话:**池内每个账号的历史所有公开笔记,都要被其余每个账号点赞 + 收藏,
完成率 100%**。在这个模块之前,内容运营是逐号翻 self-interactions 再手拼分母报数 ——
已经两次把指标视图当成台账分母翻车。这里给一份**与调度侧同口径**的权威数,口径逐条写在
下面并注明理由;改口径之前先读 ``interaction_backfill.plan_round``,那边是真正派单的地方。

## 口径(每条都对着 plan_round 的选篇)

- **池** = ``xhs_accounts`` 全部行,**不看 cookie 是否有效、不看 managed**。KPI 说的是
  "池内每个号",不是"现在跑得动的号";把 cookie 失效的号从分母里摘掉,等于把欠账藏起来
  (⚠️ 反过来说:``plan_round`` 只派给 ``cookie_status='valid'`` 的号,所以失效号的
  pending 谁也接不走 —— 那是运维要处理的事,不是把它从总账里抹掉的理由);
- **每篇的应互动方** = 池内除作者外全部号(``pool_size - 1``)。自己给自己点赞不算数,
  对应 plan_round 里那句 ``n.account_id != aid``;
- **分母笔记** = ``deleted_at IS NULL`` 且 ``note_id IS NOT NULL`` 且 ``permission_code == 0``。
  permission_code **必须显式等于 0**:NULL 是"未知"不是公开(plan_round 同款注释,写成
  ``!= 1`` 就会把未知当公开);
- **已覆盖** = 该 (笔记, actor) 在 ``note_interactions`` 里有 ``done`` / ``skipped`` 行,
  且 actor 在池内。``skipped`` 的语义是"去点的时候平台上已经是目标态" —— 对那个号来说赞
  就在那篇笔记上,只是不是这一次点的。与 ``interaction_backfill._COMPLETE_STATUSES`` /
  ``self_interactions._PRESENT_STATUSES`` 同口径;只数 ``done`` 会少算一大半(生产实测
  1215 done 对 1893 skipped)。

## 三处"看不出来的坑",都在结构里显式给了数

1. **不进分母的笔记不能悄悄消失**。没 note_id(台账行在、平台 id 还没同步回来)和非公开
   (permission_code ≠ 0)这两类都盘在 ``unschedulable`` 里:前者是"去同步台账"的运维动作,
   后者是按老板"公开"口径主动排除的。不列出来的话,运营看到"公开笔记 40 篇"却数不清
   平台上那 48 篇卡片去哪了,就会怀疑整张表;
2. **作者号已退出矩阵的笔记不属于任何账号行**。生产真实存在(号9 的账号行被移出系统,
   10 篇笔记与 364 条互动行还留在库里)。它们的作者不在池内 → plan_round 也永远不会再
   为它们派单 → 不进 ``accounts`` / ``totals``,但计数落在顶层 ``unowned_notes``,
   **绝不静默吞掉**;
3. **totals 的完成率按格子重算,不是各号完成率求平均**。平均会让只有 7 篇笔记的新号和
   70 篇的主力号一样重,算出来的"矩阵完成率"和实际欠账量对不上。

## 一个已知的宽口径(未来可能要收严)

本模块认定"这一格做完了"的条件是该 (笔记, actor) **有任意一个** done/skipped 动作行,
而 plan_round 判"这篇对这个 actor 做完了"要求 like 与 collect **两个动作都**到位。
理论上会出现"赞成了、藏没成"的半成品格子在这里算完成、调度侧却还会再选它。
生产当前实测两者完全相等(1554 = 1554,没有半成品格子),故按更简单的口径实现;
真出现半成品(``combos_covered`` 长期高于运营手数的量)时回到这里把条件收严成两动作齐全。

**纯 DB 读**:三条 select 全量取回内存里摊开(全表量级:池 9 个号 / 笔记 362 行 /
互动台账 3356 行,万级以内)。理由与 ``audience_analytics.load_actor_aggregates`` 同:
要的东西(按 owner 分桶 + 按 actor 分桶 + 零互动篇计数)用一条 GROUP BY 表达要好几个
子查询再 join 回来,在内存里摊开更直白也更好改。真到十万级再谈推回 SQL。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note_interaction import NoteInteraction
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount

# 算作"这一格在平台上已经到位"的状态。**skipped 必须在内**(理由见模块 docstring),
# error 不在内(那一下没成)。与 interaction_backfill._COMPLETE_STATUSES 同口径。
COVERED_STATUSES = ("done", "skipped")

COVERAGE_NOTE = (
    "完成率 = 已覆盖格子 / 应做格子;一格 = (一篇公开笔记, 一个非作者的池内号)。"
    "分母笔记 = 未删除 + 有 note_id + permission_code=0(NULL 是未知不算公开);"
    "已覆盖 = 该格在 note_interactions 有 done/skipped 行(skipped = 平台上本来就是目标态,"
    "与补量调度同口径,只数 done 会少算一大半)。"
    "池含全部账号(cookie 失效的号照样算欠账,但调度只派给 valid 号,失效号的 pending "
    "谁也接不走 —— 那是运维动作)。"
    "unschedulable 的两类不进分母:ledger_no_note_id 要去同步台账,nonpublic 是按「公开」"
    "口径主动排除。unowned_notes 是作者号已退出矩阵的笔记,不进任何账号行也不进 totals。"
)


def _pct(covered: int, total: int) -> float:
    """完成率(保留 1 位小数)。

    **分母为 0 时给 100.0**:没有应做的活就是没有欠账。给 0.0 会让"这个号还没发过公开
    笔记"在看板上长得和"一格都没做"一模一样,而这两件事的运营动作完全相反。
    """
    if total <= 0:
        return 100.0
    return round(covered / total * 100, 1)


def _row(account_id: int, name: str, bucket: dict, pool_size: int) -> dict:
    """把一个 owner 的分桶算成一行账。"""
    combos_total = bucket["public_notes"] * max(0, pool_size - 1)
    covered = bucket["covered"]
    return {
        "account_id": account_id,
        "name": name,
        "public_notes": bucket["public_notes"],
        "combos_total": combos_total,
        "combos_covered": covered,
        "combos_pending": combos_total - covered,
        "completion_pct": _pct(covered, combos_total),
        "unschedulable": {
            "ledger_no_note_id": bucket["ledger_no_note_id"],
            "nonpublic": bucket["nonpublic"],
        },
        "zero_touch_notes": bucket["zero_touch_notes"],
    }


async def compute_coverage(
    session: AsyncSession, *, account_ids: list[int] | None = None
) -> dict:
    """算出全池互刷完成率总账(纯查询,不写库、不起浏览器)。

    Args:
        session: 调用方的会话(本函数只读,不管理事务边界)。
        account_ids: 只算这些**笔记作者号**的账;None = 全部(admin 口径)。
            ``actors`` 出勤表始终按全池列出 —— 欠账是"谁还没去点",收窄它会让某个号的
            pending 凭空少掉,那正是这张表要回答的问题。

    Returns:
        ``{pool_size, accounts, actors, totals, unowned_notes}``;
        视图层(``interaction_coverage_rest``)再补 generated_at 与 note。
    """
    accounts = (
        (await session.execute(select(XhsAccount).order_by(XhsAccount.id)))
        .scalars()
        .all()
    )
    pool = {a.id: a for a in accounts}
    pool_size = len(pool)

    notes = (
        await session.execute(
            select(
                PublishedNote.account_id,
                PublishedNote.note_id,
                PublishedNote.permission_code,
            ).where(PublishedNote.deleted_at.is_(None))
        )
    ).all()

    # 已覆盖的格子 (actor, note_id)。**actor 限池内**:退出矩阵的号点过的赞不再算数
    # (KPI 说的是"池内每个账号"),生产库里就躺着 364 条这样的行。
    covered_pairs = {
        (actor, note_id)
        for actor, note_id in (
            await session.execute(
                select(
                    NoteInteraction.actor_account_id, NoteInteraction.note_id
                ).where(NoteInteraction.status.in_(COVERED_STATUSES))
            )
        ).all()
        if actor in pool
    }

    visible = None if account_ids is None else set(account_ids)
    buckets: dict[int, dict] = {
        aid: {
            "public_notes": 0,
            "covered": 0,
            "ledger_no_note_id": 0,
            "nonpublic": 0,
            "zero_touch_notes": 0,
        }
        for aid in pool
        if visible is None or aid in visible
    }
    # 每个 actor 已做的格子数(应做数最后按"分母篇数 - 他自己的篇数"算,见下)
    actor_done: dict[int, int] = {aid: 0 for aid in pool}
    denominator_by_owner: dict[int, int] = {}
    unowned_notes = 0

    for owner_id, note_id, permission_code in notes:
        bucket = buckets.get(owner_id)
        if bucket is None:
            if owner_id not in pool:
                # 作者号已退出矩阵:plan_round 也永远不会再为它派单(它连账号行都没了),
                # 不进任何账号行 —— 但**不能悄悄吞掉**,顶层单独报数
                unowned_notes += 1
            continue  # 池内但不在可见范围:本次查询不关心
        # 先判 note_id 缺失再判非公开,一行只进一类 —— 两个 unschedulable 相加 = 不可调度总数
        if note_id is None:
            bucket["ledger_no_note_id"] += 1
            continue
        if permission_code != 0:
            bucket["nonpublic"] += 1
            continue

        bucket["public_notes"] += 1
        denominator_by_owner[owner_id] = denominator_by_owner.get(owner_id, 0) + 1
        actors_done_here = 0
        for actor_id in pool:
            if actor_id == owner_id:
                continue  # 不给自己点赞:这一格根本不存在,既不算应做也不算已做
            if (actor_id, note_id) in covered_pairs:
                actors_done_here += 1
                actor_done[actor_id] += 1
        bucket["covered"] += actors_done_here
        if actors_done_here == 0:
            # 一格都没到位:这些篇在选篇队列尾部(按 first_seen_at 倒序,老笔记最后轮到),
            # 会被覆盖,不是"漏了"
            bucket["zero_touch_notes"] += 1

    rows = [
        _row(aid, pool[aid].name, bucket, pool_size)
        for aid, bucket in sorted(buckets.items())
    ]

    denominator_total = sum(denominator_by_owner.values())
    actors = [
        {
            "account_id": aid,
            "name": pool[aid].name,
            "done_skipped_combos": actor_done[aid],
            # 他该做的 = 分母里**不是他自己写的**那些篇
            "pending_combos": (
                denominator_total - denominator_by_owner.get(aid, 0) - actor_done[aid]
            ),
        }
        for aid in sorted(pool)
    ]

    # totals 的完成率**按格子重算**,不是各号完成率求平均 —— 平均会让 7 篇的新号和 70 篇的
    # 主力号一样重,算出来的矩阵完成率和实际欠账量对不上
    combos_total = sum(r["combos_total"] for r in rows)
    combos_covered = sum(r["combos_covered"] for r in rows)
    totals = {
        "public_notes": sum(r["public_notes"] for r in rows),
        "combos_total": combos_total,
        "combos_covered": combos_covered,
        "combos_pending": combos_total - combos_covered,
        "completion_pct": _pct(combos_covered, combos_total),
        "unschedulable": {
            "ledger_no_note_id": sum(
                r["unschedulable"]["ledger_no_note_id"] for r in rows
            ),
            "nonpublic": sum(r["unschedulable"]["nonpublic"] for r in rows),
        },
        "zero_touch_notes": sum(r["zero_touch_notes"] for r in rows),
    }
    return {
        "pool_size": pool_size,
        "accounts": rows,
        "actors": actors,
        "totals": totals,
        "unowned_notes": unowned_notes,
    }
