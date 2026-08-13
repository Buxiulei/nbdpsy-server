"""历史笔记互动补量(点赞 + 收藏):选篇 + 四层节流 + 撞墙即停 + 互动台账记账。

设计 docs/design/2026-08-02-interaction-backfill-design.md。

**这个功能的主体是节流,不是互动**。全矩阵补一遍是 139 篇公开笔记 × 6 个号 ≈ 834 次
互动、约 9.3 小时纯浏览器时间,而且全是**对老笔记的集中互动** —— 平台眼里最典型的补量
特征,风险显著高于"新笔记发布后互动"。参考实测:一小时 5 次会话的量级就已经把两个账号
弹上验证墙(其中一个靠人工手机扫码才解开)。所以下面四层闸**一层都不能少**:

1. **日上限**(``NOTE_INTERACTION_DAILY_LIMIT``,默认 20 篇/号/天):全量补完 ≈ 6 天,
   这是**刻意的**,不是性能问题;
2. **单轮上限**(``NOTE_INTERACTION_ROUND_LIMIT``,默认 5 篇):超出的留给下一轮;
3. **间隔抖动**:两篇之间 ``random.uniform(60, 240)`` 秒,**绝不连续快跑**;
4. **共用 ``browser_slot``**:不额外起并发,与其它浏览器任务排队。

这四层管的是"补得多快",管不了"白开多少次注定失败的会话",所以另有**两个断路器**
(2026-08-13 事故驱动,判据与实据见 ``plan_round`` 上面那段注释):半死的 actor 熔断
(带半开探测)、被多个号报"主页找不到"的笔记熔断。两者都只在**选篇/选 actor 阶段**判,
浏览器层一个字不动 —— 注定失败的会话压根不该被派出去。

**一轮 = 一个 actor 一次会话里做 ≤ M 篇**,不是"一篇一次会话"。会话频次才是被弹墙的
直接原因(见上),所以宁可一次会话开久一点(整轮全程持有 ``browser_slot``,最长
``ROUND_BUDGET_SECONDS``),也不把 5 篇拆成 5 次起浏览器。这是本模块唯一一处**刻意
违反**"领了任务不要长时间占闸"惯例的地方,理由就是风控优先级高于闸的周转率。

**撞墙即停比做完更重要**:任一篇互动时 ``page.url`` 出现 ``captcha`` / ``website-login``
即中止本轮、不再处理剩余篇目,该 actor 置 ``cookie_status='restricted'`` 并落
``risk_events``;**已完成的部分照常记账,不回滚**。

三条触发路径:

- **自动**:台账同步(T2)新建 orphan 行(= 发现了手工新增的笔记)后调
  ``schedule_after_sync``,给矩阵内其余账号各登记一条互动任务。**不触发评论** ——
  评论只在本系统发布时触发(需求第 2 条);
- **手工批量**:REST ``POST /api/interaction-backfills``(scope=account/all/newcomer);
- 已有的**发布后矩阵互动**(``app.services.matrix_interact``)不动。

``interaction_backfill`` **非幂等**,不在 ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里:
僵死重跑会重复处理、白开浏览器、消耗当日配额、增加风控暴露。虽然 ``_icon_action`` 在
平台侧会 skipped 不重复点(重复点 = 取消赞),但那救的是正确性,救不了这些代价。

收敛纪律照抄 ``note_export`` / ``note_purpose``:号锁 → 浏览器闸 → 线程内跑同步浏览器,
任何异常收敛成 ``{"error": reason}`` **绝不上抛**。
"""

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.login_detector import PAGE_TEXT_JS, classify_wall_text, is_wall_url
from app.browser.matrix_interact import MatrixInteractError, interact_with_note
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.models.browser_job import BrowserJob
from app.models.note_interaction import NoteInteraction
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo, risk_events
from app.services.cookie_check import load_account_cookies

# browser_jobs 的 kind(登记 / 派发 / 轮询三处同名)
JOB_KIND = "interaction_backfill"

# 三种范围(REST 与自动路径共用同一套语义)
SCOPE_ACCOUNT = "account"      # 给某账号的历史笔记互动(由其余矩阵号来做)
SCOPE_ALL = "all"              # 给所有账号的历史笔记互动
SCOPE_NEWCOMER = "newcomer"    # 某新号去给其余所有账号的历史笔记互动
SCOPES = (SCOPE_ACCOUNT, SCOPE_ALL, SCOPE_NEWCOMER)

# 两篇之间的间隔抖动(秒)。**这是第三层闸**:补量最怕的就是"连续快跑"的机器节奏,
# 一分钟到四分钟的随机间隔才像人在刷。改小它等于把补量特征直接写在脸上。
MIN_GAP_SECONDS = 60
MAX_GAP_SECONDS = 240

# 单轮浏览器段的时间预算(秒)。5 篇 × (互动 ~40s + 间隔最长 240s) 最坏能跑到 ~28 分钟,
# 而账号子进程硬超时是 ``ACCOUNT_PROC_TIMEOUT``(默认 1800s)—— 撞上就是被强杀,
# 已完成的部分连账都记不上。故预算内做几篇算几篇,剩下的留给下一轮(本就是节流的意思)。
ROUND_BUDGET_SECONDS = 1200

# error 行的冷却期(小时):一次渲染抖动不该让一篇笔记永久失去补量机会,但也不能每轮
# 都拿它白开一次页。冷却期内不再选它,过期可再试一次。
ERROR_RETRY_COOLDOWN_HOURS = 24

# 笔记熔断认的那一种错:在发布者主页里翻不到这篇笔记卡。它与"按钮点不动"不同 ——
# 后者是页内交互问题(卡片找到了),前者是**这篇根本没出现在主页上**。
#
# ⚠️ 翻不到有两种成因,台账区分不了:①这篇被平台屏蔽/限流,主页不展示它(运营核实事项);
# ②它在主页里排得太靠后,超出了定位的滚动预算(定位侧的债)。**两种情况下继续每轮重试
# 都同样徒劳**,所以熔断一视同仁地停掉调度,再把篇目列进 ``suppressed_notes`` 交给人判 ——
# 别在文案里把它一口咬定成"被屏蔽了"。
NOTE_MISSING_ERROR_KIND = "note_not_found"

# 自动路径给各 actor 散开的窗口(秒):一次同步能触发好几个号,不散开就是一拥而上。
AUTO_WINDOW_SECONDS = 1800

# 记账的两个动作(与 ``interact_with_note`` 返回的 actions 键一致)
ACTIONS = ("like", "collect")

# 视为"这篇对这个号已经做完了"的动作状态:done=点成了,skipped=平台上本来就是目标态。
# 两者都不必再开页(重复点 = 取消赞),error 则给冷却后重试。
_COMPLETE_STATUSES = ("done", "skipped")


# ---------------- 选篇与节流(本模块的重心) ----------------


def _round_limit_of(requested: Optional[int]) -> int:
    """本轮篇数上限:调用方给了就用它,但**永远不超过配置的单轮上限**。

    调用方传进来的 limit 只能往小了压,不能放大 —— 单轮上限是风控闸,不是默认值。
    """
    cap = max(1, int(settings.NOTE_INTERACTION_ROUND_LIMIT))
    try:
        wanted = int(requested) if requested is not None else cap
    except (TypeError, ValueError):
        wanted = cap
    return max(1, min(cap, wanted))


def _day_start(now: datetime) -> datetime:
    """当日零点(库内时间列全是 naive UTC,配额也按 UTC 日算)。"""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _used_today(rows: list[NoteInteraction], actor_id: int, now: datetime) -> int:
    """该 actor 今天已经**开过页**的篇数(按 note_id 去重)。

    台账行只在真的开过笔记页之后才写(选篇跳过的、没轮到的都不写),所以数它就是数
    今天花掉的页面访问 —— 配额闸要闸的正是这个,而不是"成功了几篇"。
    """
    start = _day_start(now)
    return len(
        {
            row.note_id
            for row in rows
            if row.actor_account_id == actor_id and row.done_at >= start
        }
    )


def _note_state(
    rows: list[NoteInteraction], actor_id: int, note_id: str, now: datetime
) -> tuple[bool, bool]:
    """这篇对这个 actor 的状态 → ``(是否已做完, 是否在错误冷却期内)``。

    做完 = 两个动作都是 done/skipped;冷却 = 最近一次尝试距今不足
    ``ERROR_RETRY_COOLDOWN_HOURS``(刚试过就别再为它开一次页)。
    """
    mine = [
        row
        for row in rows
        if row.actor_account_id == actor_id and row.note_id == note_id
    ]
    if not mine:
        return False, False
    done = {row.action for row in mine if row.status in _COMPLETE_STATUSES}
    complete = all(action in done for action in ACTIONS)
    cooling = any(
        row.done_at > now - timedelta(hours=ERROR_RETRY_COOLDOWN_HOURS) for row in mine
    )
    return complete, cooling


# ---------------- 两个断路器(2026-08-13 事故驱动) ----------------
#
# 上面那几层闸管的是"补得多快",管不了"白开多少次注定失败的会话"。事故实据:
#
# - **actor 侧**:号12 自 08-08 起 96 连败,全部同一个 ``profile_not_loaded``(登录态在,
#   但打开任何发布者主页都渲染不出笔记卡片 —— 账号会话半死)。它**没撞验证墙**,所以
#   "撞墙即停 → 置 restricted"那套一次都没触发,风控台账零记录;error 冷却一过就再试,
#   96 次白开的浏览器会话全是实打实的风控暴露。
# - **笔记侧**:三篇 views=0/0/1 的笔记被全部 9 个 actor 报 ``note_not_found``,各积了
#   16-18 个 error 还在被冷却重试。actor 的报告是准确的 —— 那几篇大概率被平台屏蔽,
#   主页根本不展示,再重试一万次也是徒劳。
#   ⚠️ 但建库时刻按 K=3 实测,达阈值的**共 10 篇**,其中 6 篇 views 在 67~520 之间,
#   不像被屏蔽的 —— ``note_not_found`` 混了第二种成因(它在主页里排太靠后,超出定位的
#   滚动预算)。熔断对两者一视同仁(继续重试都同样徒劳),但**别在对外文案里一口咬定
#   "被屏蔽了"**,成因交给人判。见 ``NOTE_MISSING_ERROR_KIND``。
#
# 两者都在**选篇/选 actor 阶段**判掉,浏览器层一个字不动:白开的会话根本不该被派出去。

# 上一次为某个 actor 打过的熔断日志(actor_id → 错误类别):只在类别变化时打一次。
# 熔断期间每轮都跳过它,不去重的话日志会被同一句话刷屏,真出问题反而看不见
# (手法照抄 ``InteractionBackfillScheduler._last_idle_reason``)。
_last_breaker_log: dict[int, str] = {}

# 断路器A 的三种结论
BREAKER_CLOSED = "closed"  # 正常,照常派活
BREAKER_OPEN = "open"      # 熔断,本轮不派活
BREAKER_PROBE = "probe"    # 半开,放行但**本轮只做一篇**


def _error_kind(detail: Optional[str]) -> str:
    """从台账 ``detail`` 里取错误类别:``note_not_found: 主页没有这篇 | forensics=...``
    → ``note_not_found``。

    先切 ``|`` 再切 ``:`` —— 反过来会被取证 JSON 里的冒号切坏。
    """
    head = (detail or "").split("|", 1)[0]
    return head.split(":", 1)[0].strip()[:64] or "unknown"


def _actor_breaker_state(
    rows: list[NoteInteraction], actor_id: int, now: datetime
) -> tuple[str, Optional[str]]:
    """这个 actor 的断路器状态 → ``(closed/open/probe, 最近的错误类别)``。

    判据:该 actor 最近 ``INTERACTION_ACTOR_BREAKER_N`` 条台账行(**不分笔记**,按
    ``done_at`` 倒序,同刻按 id 兜底)**全是 error** 即熔断。一篇失败落两行(赞 + 藏),
    故默认的 6 ≈ 连续三篇整篇失败 —— 单动作失败(赞成了藏没成)会留下一条 done 行,
    自然不算数,那种是页内交互问题,不是账号半死。

    **半开探测**:最新那条 error 距今超过 ``INTERACTION_ACTOR_BREAKER_COOLDOWN_H`` 小时
    就放它去探一次。没有半开就是死锁 —— 被熔断的号永不被选中,也就永远产不出成功记录
    来复位。探测成功即**自然复位**(最近 N 条不再全 error);再失败则从新的那条 error
    重新计时。半开这一轮**只做一篇**(见 ``plan_round`` 里 take 的收窄):一轮上限是 5 篇,
    真让半死的号一次探 5 篇,等于每个冷却周期照旧白开 5 次页,断路器就只剩一半意义了。
    """
    window = max(1, int(settings.INTERACTION_ACTOR_BREAKER_N))
    mine = sorted(
        (row for row in rows if row.actor_account_id == actor_id),
        key=lambda row: (row.done_at, row.id or 0),
        reverse=True,
    )[:window]
    if len(mine) < window or any(row.status != "error" for row in mine):
        return BREAKER_CLOSED, None
    kind = _error_kind(mine[0].detail)
    cooldown = timedelta(hours=max(0, int(settings.INTERACTION_ACTOR_BREAKER_COOLDOWN_H)))
    if mine[0].done_at <= now - cooldown:
        return BREAKER_PROBE, kind
    return BREAKER_OPEN, kind


def _settle_actor_breaker(
    rows: list[NoteInteraction], actor_ids: list[int], now: datetime
) -> tuple[set[int], set[int]]:
    """结算 actor 熔断 → ``(熔断的, 半开待探的)``。

    **必须在笔记熔断之前跑**:后者要拿这里的结果判投票资格。

    熔断时告警一次(带 actor id 与最近错误类别),恢复/半开时清掉记录 —— 下次再熔断会
    重新告警。不去重的话熔断期间每轮都刷同一句话,真出问题反而看不见。
    """
    tripped: set[int] = set()
    probing: set[int] = set()
    for actor_id in actor_ids:
        state, kind = _actor_breaker_state(rows, actor_id, now)
        if state == BREAKER_OPEN:
            tripped.add(actor_id)
            if _last_breaker_log.get(actor_id) != kind:
                logger.warning(
                    f"[interaction_backfill] 账号{actor_id} 已熔断:最近 "
                    f"{settings.INTERACTION_ACTOR_BREAKER_N} 条互动全部失败({kind}),"
                    f"本轮不派活;"
                    f"{settings.INTERACTION_ACTOR_BREAKER_COOLDOWN_H}h 后半开探测一次"
                )
                _last_breaker_log[actor_id] = kind
            continue
        _last_breaker_log.pop(actor_id, None)
        if state == BREAKER_PROBE:
            probing.add(actor_id)
            logger.info(
                f"[interaction_backfill] 账号{actor_id} 熔断冷却已过({kind}),"
                f"本轮半开探测**只做一篇**;成功即自动复位"
            )
    return tripped, probing


def _suppressed_note_ids(rows: list[NoteInteraction], voters: set[int]) -> set[str]:
    """结算笔记熔断:被 ≥K 个**有资格**的不同 actor 在主页里翻不到的篇。

    **只数有资格 actor 的票**(``voters`` = valid 且未被 actor 熔断)。理由是证据偏倚:
    号12 那种半死 actor 对**每一篇**笔记都报 ``note_not_found``,它的票恒为真、不承载
    任何信息;裸数票会让 K 变相打折(多一个半死号就少要一个真信号)。它现在是
    ``restricted``,历史票据此自动失效;将来任何 actor 半死,先被断路器A 熔断,票随之失效。

    **不需要清票逻辑**:``note_interactions`` 是 ``UNIQUE(actor, note, action)`` 覆盖式
    更新 —— 半死 actor 恢复后重试成功会把自己那两行 error 覆盖成 done,票自动翻转。
    """
    quorum = max(1, int(settings.INTERACTION_NOTE_BREAKER_ACTORS))
    votes: dict[str, set[int]] = {}
    for row in rows:
        if (
            row.actor_account_id in voters
            and row.status == "error"
            and _error_kind(row.detail) == NOTE_MISSING_ERROR_KIND
        ):
            votes.setdefault(row.note_id, set()).add(row.actor_account_id)
    return {note_id for note_id, actors in votes.items() if len(actors) >= quorum}


def _no_plan(reason: str, suppressed: Optional[list[str]] = None) -> dict:
    """挑不出东西来时的统一返回形状(**不是错误**,补完了就是补完了)。"""
    return {
        "actor_account_id": None,
        "targets": [],
        "reason": reason,
        "suppressed_notes": suppressed or [],
    }


async def plan_round(
    session,
    scope: str,
    target_account_id: Optional[int] = None,
    actor_account_id: Optional[int] = None,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """规划一轮补量:挑 actor + 挑篇,**已按四层闸里的前两层 + 两个断路器截断**。

    返回 ``{"actor_account_id": int|None, "targets": [...], "reason": str|None,
    "suppressed_notes": [note_id...]}``;挑不出东西来时 actor/targets 为空并给
    ``reason``(**不是错误**,补完了就是补完了)。

    ``suppressed_notes`` = 本轮被**笔记熔断**踢出候选的篇(见下),带出来是给人看的:
    多个号都在主页里翻不到它,系统这边只会安静地不再调度 —— 不列出来就没人会去查那是
    平台屏蔽了,还是它在主页里排太靠后翻不到(见 ``NOTE_MISSING_ERROR_KIND`` 注释)。

    两个断路器**按顺序结算,顺序不能调**(2026-08-13 事故驱动,判据与理由见上面那段注释):

    1. **actor 熔断**:最近 N 条台账行全是 error 的号本轮不派活;冷却过后半开探测放它
       走一轮,那一轮**只做一篇**(探 5 篇等于照旧白开 5 次页);
    2. **笔记熔断**:被 ≥K 个**有资格**(valid 且未被第 1 步熔断)的不同 actor 报过
       ``note_not_found`` 的篇移出候选、**不再调度**。先跑第 1 步正是为了让半死 actor
       的票在这一步失效 —— 它对每篇都报找不到,票恒为真、不承载信息。**它只判"多个号
       都翻不到"这个事实,不判成因**(屏蔽 vs 主页里排太靠后),成因交给人。

    **笔记熔断不自动恢复**(平台屏蔽不会自己好,定位翻不到也不会自己好)。人工核实、
    确认这篇又该补了之后的恢复路径:**手工删掉该 note_id 的 error 台账行**
    (``DELETE FROM note_interactions WHERE note_id=? AND status='error'``),票清零,
    下一轮它自然重新入池 —— 不需要任何开关。

    三种 scope 的选篇口径:

    - ``account``:被互动的是 ``target_account_id`` 这个号的笔记,互动方是矩阵内其余号;
    - ``all``:所有号的笔记都算,互动方是矩阵内任一号(**排除给自己点赞**);
    - ``newcomer``:互动方固定是 ``actor_account_id`` 这个新号,被互动的是其余所有号。

    ``actor_account_id`` 一旦给定就是**硬指定**(newcomer 的语义,也是 execute 复用本函数
    时锁定自己的方式);不给则在候选 actor 里挑**今天用得最少**的那个(号间公平,
    也让日上限自然地把负载摊开)。

    共同的硬条件(与设计一致,一条都不放宽):

    - 互动方必须是 ``cookie_status='valid'`` 的号(与矩阵互动同口径);
    - 被互动的笔记必须有 ``note_id``、且 ``permission_code == 0`` **公开**
      —— **NULL 是未知,同样不碰**:私密笔记访客根本看不到,点赞收藏无从谈起;
    - 笔记所属账号必须有 ``user_id``(主页路径定位要它,没有就无从进主页);
    - 已做完的篇跳过、刚失败过的篇在冷却期内跳过(**避免白开浏览器**);
    - 篇数取 ``min(单轮上限, 该 actor 当日剩余配额, 调用方 limit)``。

    优先级:**新发现的笔记 > 最近发布的 > 更早的**(``first_seen_at`` 降序为主,
    平台发布时间降序次之)—— 手工新增的笔记同步当场 first_seen_at 最新,自然浮到最前。
    """
    now = now or datetime.utcnow()
    if scope not in SCOPES:
        return _no_plan(f"未知 scope: {scope}")

    accounts = (await session.execute(select(XhsAccount))).scalars().all()
    by_id = {a.id: a for a in accounts}
    valid_ids = [
        a.id for a in accounts if a.cookie_status == "valid" and a.login_cookies
    ]

    # ── 谁去互动(actor 候选)与互动谁(owner 范围) ──
    if scope == SCOPE_NEWCOMER:
        if actor_account_id is None:
            return _no_plan("scope=newcomer 必须指定 actor_account_id")
        owners = {a.id for a in accounts if a.id != actor_account_id}
    elif scope == SCOPE_ACCOUNT:
        if target_account_id is None:
            return _no_plan("scope=account 必须指定 target_account_id")
        owners = {target_account_id}
    else:
        owners = {a.id for a in accounts}

    actor_pool = [actor_account_id] if actor_account_id is not None else list(valid_ids)
    actor_pool = [aid for aid in actor_pool if aid in valid_ids]
    if not actor_pool:
        return _no_plan("没有可用的互动账号(需 cookie_status=valid 且有 cookie)")

    # 台账按**全部 valid 号**取(不只本轮的 actor_pool):笔记熔断要数"有资格 actor 的票",
    # 那是个全局判据,只看 actor_pool 会因为 scope 不同而数出不同的票。
    ledger = (
        (
            await session.execute(
                select(NoteInteraction).where(
                    NoteInteraction.actor_account_id.in_(valid_ids)
                )
            )
        )
        .scalars()
        .all()
    )

    # ── 断路器A:actor 熔断(**先于笔记熔断结算**,它的结果是后者的投票资格) ──
    tripped, probing = _settle_actor_breaker(ledger, valid_ids, now)
    blocked = [aid for aid in actor_pool if aid in tripped]
    actor_pool = [aid for aid in actor_pool if aid not in tripped]
    if not actor_pool:
        return _no_plan(
            f"候选互动账号 {blocked} 已因连续失败熔断,本轮不派活"
            f"({settings.INTERACTION_ACTOR_BREAKER_COOLDOWN_H}h 后半开探测一次)"
        )

    # ── 候选笔记:公开 + 有 note_id + 作者有 user_id,按优先级排好序 ──
    notes = (
        (
            await session.execute(
                select(PublishedNote)
                .where(
                    # 已删笔记(淘汰真删/平台收走后由对账补标)不再派单:总账端点
                    # 按 deleted_at IS NULL 计分母,这里不滤两边就会分叉——调度给
                    # 死笔记白开页,而总账早已不数它(coverage 单交付时点出的口径分歧)
                    PublishedNote.deleted_at.is_(None),
                    PublishedNote.account_id.in_(owners),
                    PublishedNote.note_id.is_not(None),
                    # permission_code 必须**显式等于 0**:写 `!= 1` 或 `not permission_code`
                    # 会把 NULL(未知)当成公开,那正是设计点名不许犯的错
                    PublishedNote.permission_code == 0,
                )
                .order_by(
                    PublishedNote.first_seen_at.desc(),
                    PublishedNote.platform_published_at.desc(),
                    PublishedNote.published_at.desc(),
                    PublishedNote.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    notes = [
        n for n in notes
        if (owner := by_id.get(n.account_id)) is not None and (owner.user_id or "").strip()
    ]

    # ── 断路器B:笔记熔断(只数有资格 actor 的票,资格已由断路器A 结算过) ──
    suppressed_all = _suppressed_note_ids(
        ledger, {aid for aid in valid_ids if aid not in tripped}
    )
    suppressed = sorted({n.note_id for n in notes if n.note_id in suppressed_all})
    notes = [n for n in notes if n.note_id not in suppressed_all]
    if not notes:
        detail = (
            f";另有 {len(suppressed)} 篇被熔断(≥"
            f"{settings.INTERACTION_NOTE_BREAKER_ACTORS} 个号在发布者主页翻不到它,"
            f"需人工核实)"
            if suppressed
            else ""
        )
        return _no_plan(
            "没有符合条件的公开笔记(只互动 permission_code=0 且已知 note_id 的笔记)"
            + detail,
            suppressed,
        )

    daily_cap = max(1, int(settings.NOTE_INTERACTION_DAILY_LIMIT))
    round_cap = _round_limit_of(limit)
    capped: list[int] = []
    best: tuple[int, int, list[PublishedNote]] | None = None  # (今日用量, actor_id, 候选篇)
    for aid in sorted(actor_pool):
        used = _used_today(ledger, aid, now)
        if used >= daily_cap:
            capped.append(aid)
            continue
        eligible = [
            n for n in notes
            if n.account_id != aid  # 不给自己点赞
            and not any(_note_state(ledger, aid, n.note_id, now))
        ]
        if not eligible:
            continue
        if best is None or used < best[0]:
            best = (used, aid, eligible)

    if best is None:
        reason = (
            f"账号 {capped} 今日已达互动上限({daily_cap} 篇/天),留给明天"
            if capped
            else "候选账号都没有可补的笔记(都做完了或还在失败冷却期内)"
        )
        return _no_plan(reason, suppressed)

    used, actor_id, eligible = best
    take = min(round_cap, daily_cap - used)
    if actor_id in probing:
        # 半开探测只做一篇:一轮上限是 5 篇,让半死的号一次探 5 篇等于每个冷却周期照旧
        # 白开 5 次页 —— 断路器就只剩一半意义了。探成了下一轮自然复位,照常做满一轮。
        take = 1
    targets = [
        {
            "note_row_id": n.id,
            "note_id": n.note_id,
            "title": n.title or "",
            "publisher_account_id": n.account_id,
            "publisher_user_id": by_id[n.account_id].user_id,
        }
        for n in eligible[:take]
    ]
    return {
        "actor_account_id": actor_id,
        "targets": targets,
        "reason": None,
        "suppressed_notes": suppressed,
    }


# ---------------- 任务登记(REST 手工 / 台账同步自动) ----------------


async def start_backfill(
    scope: str,
    target_account_id: Optional[int] = None,
    actor_account_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    """REST 手工触发一轮补量;登记 browser_jobs 台账,返回 ``{"job_id", "actor_account_id"}``。

    **actor 在登记时就定下来**(台账行的 ``account_id`` 要它:派发按账号分子进程、
    号锁与 profile 也按它走);具体做哪几篇则留到执行时再挑一次 —— 排队期间日配额可能
    已被别的轮次吃掉,拿登记那一刻的快照去做等于绕过日上限。

    挑不出 actor(都达日上限 / 没有可补的笔记 / 号被熔断)时**不登记任务**,返回
    ``job_id=None`` + ``reason``:开一个注定空转的浏览器任务毫无意义。

    ``suppressed_notes`` 原样带出:被笔记熔断踢掉的篇是**人工核实事项**(多个号都在主页
    里翻不到它),不在回执里露出来就没人会发现 —— 系统这边只会安静地不再调度它们。
    """
    async with get_session() as session:
        plan = await plan_round(
            session, scope, target_account_id, actor_account_id, limit
        )
    if plan["actor_account_id"] is None:
        return {
            "job_id": None,
            "actor_account_id": None,
            "reason": plan["reason"],
            "suppressed_notes": plan["suppressed_notes"],
        }

    actor = plan["actor_account_id"]
    payload = {
        "scope": scope,
        "target_account_id": target_account_id,
        "actor_account_id": actor,
        "limit": limit,
    }
    job_id = browser_jobs_repo.enqueue_from_request(JOB_KIND, payload, account_id=actor)
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(actor, payload))
    logger.info(
        f"[interaction_backfill] 已登记补量任务 {job_id}(scope={scope} actor={actor} "
        f"本轮候选 {len(plan['targets'])} 篇,熔断 {len(plan['suppressed_notes'])} 篇)"
    )
    return {
        "job_id": job_id,
        "actor_account_id": actor,
        "reason": None,
        "suppressed_notes": plan["suppressed_notes"],
    }


async def schedule_after_sync(account_id: int) -> list[str]:
    """台账同步发现手工新增笔记(新建 orphan 行)后,给矩阵内其余账号各登记一条互动任务。

    **不触发评论**:评论只在本系统发布时触发(需求第 2 条),补量只做点赞 + 收藏。

    去重与散开都是必须的:同步会被反复触发(每次发布 T1 + 手工 + 定时),不去重就会在
    队列里堆一串同号任务;不散开就是几个号一拥而上 —— 两者都直接把补量特征放大。
    达日上限 / 没得可补的号直接跳过,不登记空转任务。

    **绝不抛错**:登记是同步的事后副作用,炸了不能把已经落好库的同步结果拖成 error。
    """
    try:
        job_ids: list[str] = []
        async with get_session() as session:
            in_flight = set(
                (
                    await session.execute(
                        select(BrowserJob.account_id).where(
                            BrowserJob.kind == JOB_KIND,
                            BrowserJob.status.in_(("queued", "running")),
                        )
                    )
                )
                .scalars()
                .all()
            )
            actors = (
                (
                    await session.execute(
                        select(XhsAccount.id).where(
                            XhsAccount.cookie_status == "valid",
                            XhsAccount.id != account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for actor in actors:
                if actor in in_flight:
                    logger.info(
                        f"[interaction_backfill] 账号{actor} 已有在途补量任务,不重复登记"
                    )
                    continue
                plan = await plan_round(
                    session, SCOPE_ACCOUNT, account_id, actor
                )
                if not plan["targets"]:
                    continue
                payload = {
                    "scope": SCOPE_ACCOUNT,
                    "target_account_id": account_id,
                    "actor_account_id": actor,
                    "limit": None,
                    # 排期靠落库,派发侧(list_dispatchable)按它过滤未到点的行;
                    # 执行方不 sleep 干等(那会占死 browser_slot)
                    "not_before": (
                        datetime.utcnow()
                        + timedelta(seconds=random.uniform(0, AUTO_WINDOW_SECONDS))
                    ).isoformat(sep=" "),
                }
                # operator_id=0(非请求上下文的进程内直调):不占运营的未终态配额
                job_ids.append(
                    await browser_jobs_repo.enqueue(
                        JOB_KIND, payload, 0, account_id=actor
                    )
                )
        if job_ids:
            logger.info(
                f"[interaction_backfill] 账号{account_id} 新增笔记已登记 {len(job_ids)} 条"
                f"互动补量任务(窗口 {AUTO_WINDOW_SECONDS}s)"
            )
        return job_ids
    except Exception as exc:  # noqa: BLE001 — 登记失败绝不影响台账同步结果
        logger.warning(
            f"[interaction_backfill] 登记补量任务失败 account_id={account_id}(忽略): {exc}"
        )
        return []


# ---------------- 契约执行(account_worker 子进程消费) ----------------


async def execute(account_id: int, payload: dict) -> dict:
    """执行一轮互动补量(契约函数,不碰 browser_jobs 台账)。

    ``account_id`` 是**互动方**(actor),与台账行的 account_id 一致。

    成功返回 ``{"picked","liked","collected","skipped","failed","notes"}``;账号无 cookie /
    浏览器起不来 / 撞墙中止 → 带 ``"error"`` 键(台账落 error),**绝不抛出**。

    撞墙时:已完成的部分照常记账不回滚,actor 置 ``cookie_status='restricted'`` 并落
    ``risk_events``,本轮剩余篇目一篇都不再碰。
    """
    payload = payload or {}
    scope = payload.get("scope") or SCOPE_ACCOUNT
    target_account_id = payload.get("target_account_id")
    limit = payload.get("limit")
    try:
        async with get_session() as session:
            # 执行时重挑一次(不是用登记时的快照):排队期间日配额可能已被别的轮次吃掉
            plan = await plan_round(
                session, scope, target_account_id, account_id, limit
            )
        targets = plan["targets"]
        if not targets:
            # 没得可做**不是失败**:补完了、达上限了都走这条路,任务落 done 附 reason
            return {**_empty_result(), "reason": plan["reason"]}

        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过互动补量"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。整轮(含篇间抖动)都在闸内
            # ——见模块 docstring:会话频次的风险高于闸的周转率。
            async with browser_slot():
                outcome = await asyncio.to_thread(
                    _interact_sync, account_id, cookies, targets
                )
        # 记账先于风控写回:已完成的部分不因后续处置丢失
        await _record_outcome(account_id, outcome.get("results") or [])
        stats = _summarize(targets, outcome)
        wall = outcome.get("wall")
        if wall:
            await _handle_wall(account_id, wall)
            stats["error"] = (
                f"撞风控墙({wall.get('wall_type')})已中止本轮:已完成 "
                f"{stats['handled']}/{stats['picked']} 篇,账号已置 restricted 并落 "
                f"risk_events;**不要立刻重试**,先按墙的类型处置(scan_qr=人工扫码,"
                f"rate_limit=晾置别再碰)"
            )
        elif outcome.get("error"):
            stats["error"] = outcome["error"]
        return stats
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"互动补量任务异常 account_id={account_id}")
        return {"error": f"互动补量任务异常:{exc}"}


def _empty_result() -> dict:
    return {"picked": 0, "handled": 0, "liked": 0, "collected": 0, "failed": 0, "notes": []}


def _entry_forensics(entry: dict) -> Optional[dict]:
    """这一篇要随任务结果带出的失败现场:整篇失败用汇总那份,单动作失败取第一份。

    ``_summarize`` 只把动作 **状态** 摊平进 ``notes``,不带动作详情;取证要是不在这里
    捞一把,单个动作失败(比如赞成了、藏没成)那份现场就只落进 ``note_interactions``,
    看 ``browser_jobs.result`` 的人根本看不到。整篇失败时顶层那份本就复用自动作级,
    不会出现同一页抓两遍。
    """
    if entry.get("forensics"):
        return entry["forensics"]
    for action in ACTIONS:
        got = ((entry.get("actions") or {}).get(action) or {}).get("forensics")
        if got:
            return got
    return None


def _summarize(targets: list[dict], outcome: dict) -> dict:
    """把逐篇结果汇总成对外计数(done 与 skipped 都算"到位",两者平台状态相同)。"""
    results = outcome.get("results") or []
    liked = sum(
        1 for r in results
        if (r.get("actions") or {}).get("like", {}).get("status") in _COMPLETE_STATUSES
    )
    collected = sum(
        1 for r in results
        if (r.get("actions") or {}).get("collect", {}).get("status") in _COMPLETE_STATUSES
    )
    return {
        "picked": len(targets),
        "handled": len(results),
        "liked": liked,
        "collected": collected,
        "failed": sum(1 for r in results if r.get("error")),
        "notes": [
            {
                "note_id": r["note_id"],
                "like": (r.get("actions") or {}).get("like", {}).get("status"),
                "collect": (r.get("actions") or {}).get("collect", {}).get("status"),
                "error": r.get("error"),
                "forensics": _entry_forensics(r),
            }
            for r in results
        ],
    }


def _interact_sync(account_id: int, cookies: list[dict], targets: list[dict]) -> dict:
    """同一线程内:建 SyncClient → start → **一次会话里逐篇互动** → stop 收尾。

    返回 ``{"results": [...], "wall": dict|None, "error": str|None}``。

    三条硬约定:

    - **一轮一次会话**:5 篇共用一个 camoufox,不是一篇起一次 —— 会话频次才是被弹墙的
      直接原因;
    - **篇间抖动 ``random.uniform(60, 240)`` 秒**。页内节奏(进页停留、滚动、动作间隔)
      全走 ``SyncHumanActions.wait``,那是 ``interact_with_note`` 内部的事;篇间这一段
      刻意**不用** ``human.wait`` —— 它带疲劳系数(最高 ×2)与 ±15% 抖动,240s 会被放大到
      ~550s,既超出设计给的区间,也能把单轮预算直接顶穿;
    - **撞墙即停**:每篇之后(以及定位失败时)看一眼 ``page.url``,是验证墙就立刻 break,
      剩余篇目一篇都不碰。撞墙那一篇**不记账** —— 它压根没被真正处理,记成 error 只会
      让它白白进冷却期。

    headed 真屏沿用 SyncClient 默认(``headless=False``);互动要点真实卡片与按钮,
    **不 block_images**(缺封面会影响卡片布局与坐标,与 matrix_interact 一致)。
    """
    results: list[dict] = []
    wall: Optional[dict] = None
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {
                "results": results,
                "wall": None,
                "error": f"browser_start_failed: {start.get('error')}",
            }
        page = client.page
        deadline = time.monotonic() + ROUND_BUDGET_SECONDS
        for index, target in enumerate(targets):
            if index:
                gap = random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS)
                if time.monotonic() + gap > deadline:
                    logger.info(
                        f"[interaction_backfill] 账号{account_id} 本轮预算用尽,"
                        f"剩余 {len(targets) - index} 篇留给下一轮"
                    )
                    break
                logger.info(f"[interaction_backfill] 账号{account_id} 篇间间隔 {gap:.0f}s")
                time.sleep(gap)
            entry, wall = _interact_one(page, account_id, target)
            if wall is not None:
                logger.warning(
                    f"[interaction_backfill] 账号{account_id} 撞墙,立刻中止本轮"
                    f"(已完成 {len(results)} 篇)"
                )
                break
            results.append(entry)
        return {"results": results, "wall": wall, "error": None}
    finally:
        client.stop()


def _interact_one(page, account_id: int, target: dict) -> tuple[dict, Optional[dict]]:
    """处理一篇:互动 → 查墙。返回 ``(这篇的结果, 撞墙取证或 None)``。

    撞墙时结果不参与记账(调用方直接丢弃):那一篇没被真正处理。
    """
    entry: dict[str, Any] = {
        "note_id": target["note_id"],
        "note_row_id": target.get("note_row_id"),
        "actions": {},
        "error": None,
        # 失败现场取证(``interact_with_note`` 在动作失败当场抓的),原样透传不加工
        "forensics": None,
    }
    profile_url = (
        f"https://www.xiaohongshu.com/user/profile/{target['publisher_user_id']}"
    )
    try:
        outcome = interact_with_note(
            page,
            account_id,
            target["publisher_user_id"],
            target["title"],
            note_id=target["note_id"],
        )
        entry["actions"] = outcome.get("actions") or {}
        entry["error"] = outcome.get("error")
        entry["forensics"] = outcome.get("forensics")
    except MatrixInteractError as exc:
        # 定位类失败(笔记没找到 / 详情没打开):这一页确实开过了,记 error 走冷却重试
        entry["error"] = exc.reason
    except Exception as exc:  # noqa: BLE001 — 单篇异常不阻断整轮(墙由下面统一判)
        logger.warning(f"[interaction_backfill] 账号{account_id} 单篇互动异常: {exc}")
        entry["error"] = f"interact_exception: {exc}"
    return entry, _wall_if_any(page, profile_url)


def _wall_if_any(page, target_url: str) -> Optional[dict]:
    """当前页是不是风控验证墙;是则返回取证 dict(形状与 cookie 检测那套一致)。

    **URL 是硬判据**(``is_wall_url``),正文只用来分型(扫码 / 限流)——两者的运营动作
    不同。读不出正文不影响判定:判定失败宁可当没撞墙,也不能因为取证失败漏掉真墙,
    故 URL 命中就一定返回 dict。
    """
    try:
        landed = page.url
    except Exception:  # noqa: BLE001 — 连 URL 都读不到(页没了),当没撞墙,交由上层收敛
        return None
    if not is_wall_url(landed):
        return None
    text = ""
    try:
        text = page.evaluate(PAGE_TEXT_JS) or ""
    except Exception:  # noqa: BLE001 — 取证失败不影响判定,URL 才是硬判据
        pass
    wall = {
        "wall_type": classify_wall_text(text),
        "target_url": target_url,
        "landed_url": landed,
        "page_text": text,
    }
    logger.warning(
        f"[interaction_backfill] 撞风控墙 type={wall['wall_type']} landed={landed} "
        f"text={text[:60]!r}"
    )
    return wall


# ---------------- 记账与风控写回 ----------------


# 台账 detail 里那段取证 JSON 的长度上限(字符)。取证各字段在 ``matrix_interact.
# _shrink_forensics`` 里已逐个截断过,正常一份 600~900 字符,这个上限只是兜底 —— 真被
# 截断的话 JSON 会不完整,但 detail 本来就是给人看的排查字符串,不完整也比整行撑爆强。
_DETAIL_FORENSICS_MAX = 1200


def _detail_with_forensics(reason: Optional[str], forensics: Optional[dict]) -> Optional[str]:
    """把失败原因与现场取证拼成台账 ``detail`` 单列字符串(**不新增表、不新增列**)。

    没有取证就原样返回 reason —— done / skipped 行的 detail 保持和以前一模一样。
    """
    if not forensics:
        return reason
    try:
        dumped = json.dumps(forensics, ensure_ascii=False, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 — 序列化失败也不能拖累记账
        dumped = f"(取证序列化失败: {exc})"
    return f"{reason or ''} | forensics={dumped[:_DETAIL_FORENSICS_MAX]}"


async def _record_outcome(actor_account_id: int, results: list[dict]) -> None:
    """把这一轮逐篇逐动作的结果写进 ``note_interactions``(按唯一键 upsert)。

    失败动作的 ``detail`` 里**带上失败当场的现场取证**(``_detail_with_forensics``):
    这张表是排查时最先被翻的地方,只写一句「like_button_not_found」等于什么都没说。

    定位失败(整篇没打开)记成两个动作都 error:页确实开过了,当日配额与冷却都该算上。
    **记账失败只告警**:台账是效率优化不是正确性依据,为它把已经做完的互动判成失败
    毫无意义(平台状态才是真相)。
    """
    if not results:
        return
    now = datetime.utcnow()
    try:
        async with get_session() as session:
            existing = {
                (row.note_id, row.action): row
                for row in (
                    await session.execute(
                        select(NoteInteraction).where(
                            NoteInteraction.actor_account_id == actor_account_id,
                            NoteInteraction.note_id.in_(
                                [r["note_id"] for r in results]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            }
            for result in results:
                note_id = result["note_id"]
                for action in ACTIONS:
                    outcome = (result.get("actions") or {}).get(action)
                    if outcome is None:
                        status = "error"
                        detail = result.get("error") or "note_not_opened"
                    else:
                        status = outcome.get("status") or "error"
                        detail = _detail_with_forensics(
                            outcome.get("reason"), outcome.get("forensics")
                        )
                    row = existing.get((note_id, action))
                    if row is None:
                        session.add(
                            NoteInteraction(
                                actor_account_id=actor_account_id,
                                note_id=note_id,
                                action=action,
                                status=status,
                                detail=detail,
                                done_at=now,
                            )
                        )
                    else:
                        row.status = status
                        row.detail = detail
                        row.done_at = now
            await session.commit()
    except Exception:  # noqa: BLE001 — 记账失败不改变平台上已经发生的事实
        logger.exception(
            f"[interaction_backfill] 互动台账记账失败(忽略)actor={actor_account_id}"
        )


async def _handle_wall(actor_account_id: int, wall: dict) -> None:
    """撞墙处置:actor 置 ``cookie_status='restricted'`` + 事件落 ``risk_events``。

    ``restricted`` ≠ ``invalid``:cookie 没坏,是账号被挂了验证墙,运营动作是拿手机
    小红书 App 扫码(``scan_qr``)或干脆晾着别再碰(``rate_limit``),不是重新登录。
    置了 restricted,周期巡检与数据补采都会自动绕开这个号(它们本就按这个状态过滤)。
    **绝不上抛**:处置失败不该把"已经完成的那几篇"的记账结果也拖没。
    """
    try:
        async with get_session() as session:
            account = await session.get(XhsAccount, actor_account_id)
            if account is not None:
                account.cookie_status = "restricted"
                account.last_check_at = datetime.utcnow()
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[interaction_backfill] 置 restricted 失败(忽略)actor={actor_account_id}"
        )
    await risk_events.record_wall(get_session, actor_account_id, wall, JOB_KIND)
