"""代管账号笔记数量上限淘汰:评分/选篇纯逻辑 + 每日调度器(代管账号计划 D3)。

设计 docs/design/2026-08-10-managed-accounts-design.md 第五节。

需求原话:「每个代管账号可设笔记数量上限,默认 100 篇;每天拉取数据后发现超了,把
(浏览/点赞/收藏/评论/增粉)加权平均最低的几篇删掉,维持上限」。

**一行执行链路都不新增**:库存读 ``published_notes`` 永久台账,指标读 ``note_metrics``
最新快照,删除建 ``browser_jobs`` 的 ``note_delete`` 任务(真号已验的删除链),调度器骨架
套 ``NoteMetricsScheduler`` / ``DraftCleanScheduler`` 同一模板。本模块只做「该删谁」的决策。

**四条安全轨是这个功能的主体,不是附加项**(淘汰 = 不可逆删除):

1. **宽限期**(``RETENTION_GRACE_DAYS``,默认 7 天):发布不足这么多天的笔记不参与
   淘汰。新笔记曝光要几天才铺开,不设宽限期等于每天把刚发的内容当"表现最差"删掉;
2. **无指标不杀**(硬规则,无开关):join 不上 ``note_metrics`` 的笔记一律不进淘汰
   名单,只在审计明细里记「无指标跳过」。join 不上 = 我们对这篇一无所知,删它等于抽签;
3. **单日单号删除封顶**(``RETENTION_DAILY_DELETE_MAX``,默认 5):首次启用时库存可能
   远超上限(140 篇对 100 的帽),不封顶就是第一天一口气删 40 篇;
4. **保护位**(``published_notes.protected``):被标记的笔记**永不进候选,但仍计入库存**。
   上面三条守的是"数据不足时别动手",这条守的是**打分口径本身不适用**的那类笔记 ——
   淘汰按五指标加权删最低的几篇,前提是"低互动 = 低价值",而全矩阵置顶的**功能位笔记**
   (品牌片、二维码导流笔记)浏览量只有 11-13、天然垫底,却是门面与转化入口。仍计入
   库存是刻意的:平台上确实有这一篇,不算它等于把 note_cap 悄悄放大。

外加 kill switch ``RETENTION_ENABLED=0``:照常打分、照常落审计,只是不建删除 job。

**收敛与同名两道**(对抗审查补的,不补就是每天重删同一批):

- **收敛**:删成功的笔记在台账落 ``deleted_at``,库存/计数/候选一律排掉它;删除任务还在途
  的笔记本轮不重复选。没有这一步,被删掉的笔记仍在库存里计数,第二天照样被选中、照样再建
  一条删除任务,而平台上早就没有这张卡片了(幽灵 job)—— 淘汰永远不收敛。见
  ``reconcile_deletions``;
- **同名**:删除是**按标题**定位卡片的(平台导出无 note_id),所以标题在该号台账里不唯一时,
  同名的那几篇**全部**排除出淘汰候选 —— 删错人的风险不可接受,宁可这几篇永远不淘汰。

**时区口径**(最容易错的地方):``published_notes.platform_published_at`` 是 naive UTC
(平台 visible_time unix 秒转),而 ``note_metrics.publish_time`` 是创作中心 Excel 导出的
**北京时间原文串**(「2026年05月22日10时59分14秒」)。join 前必须把 UTC 时刻 +8 小时
再取日期,否则北京时间 00:00-08:00 发布的笔记全部 join 不上 —— 它们会被安全轨②默默
排除,表现为"这批笔记永远不参与淘汰",而且不报任何错。
"""

import asyncio
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.models.browser_job import BrowserJob
from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.published_note import PublishedNote
from app.models.retention_run import RetentionRun
from app.models.xhs_account import XhsAccount

# 非请求上下文的进程内直调,台账 operator_id 约定用 0(同 note_metrics_scheduler)
_SYSTEM_OPERATOR_ID = 0
# 建的删除任务 kind:复用既有 note_delete 链路(payload 形态与 note_delete.start_delete 一致)
DELETE_JOB_KIND = "note_delete"

# note_cap 缺失/非法(NULL / ≤0)时的回退上限。**方向只能往大回退**:cap=0 的语义是
# "整个号都超限",会按日封顶把号一路删空 —— 方向反了的兜底比没有兜底危险得多。
DEFAULT_NOTE_CAP = 100
# 收敛时每号回看多少条审计行去找"记过 job_id 的删除任务"。删除任务的终态在小时级内落定
# (僵死恢复阈值 15 分钟),每号每天至多一行审计,所以 30 行 ≈ 30 天,比需要的窗口大两个数量级。
_RECONCILE_LOOKBACK_RUNS = 30
# 删除任务的未终态(还没跑完 = 结果未知,这一轮不许再选同一篇)
_INFLIGHT_JOB_STATUSES = ("queued", "running")

# 参与打分的五个指标(需求点名:浏览/点赞/收藏/评论/增粉)。顺序即审计明细里的展示顺序。
METRIC_FIELDS = ("views", "likes", "collects", "comments", "follows")
# 权重默认值(与 settings.RETENTION_WEIGHTS 的出厂值同源,解析失败时回退到这里)
DEFAULT_WEIGHTS: dict[str, float] = {
    "views": 1.0, "likes": 2.0, "collects": 3.0, "comments": 3.0, "follows": 5.0,
}

# 创作中心导出时间串 → 日期(容忍「2026年05月09日…」与「2026-05-09 …」两种写法)。
# 与 note_metrics_service._parse_publish_date 同一套正则,刻意不 import:那边返回 date
# 对象供趋势计算,这边要的是用于建索引的 "YYYY-MM-DD" 串,共用会把两处需求绑死。
_PUBLISH_DATE_RE = re.compile(r"(\d{4})[年-](\d{1,2})[月-](\d{1,2})")
# 创作中心导出时间是北京时间,库内时间列是 naive UTC,join 前的时差补偿
_BEIJING_OFFSET = timedelta(hours=8)

# 跳过原因(进审计明细,给人读的)
SKIP_GRACE = "宽限期内(发布不足 {days} 天),不参与淘汰"
SKIP_NO_METRICS = "无指标跳过(note_metrics 里 join 不到这篇,不知道表现就不删)"
SKIP_NO_PUBLISH_TIME = "无发布时间(台账两个时间列都空),无法判宽限期,不参与淘汰"
SKIP_DUP_TITLE = "同名歧义跳过(该号台账里有 {count} 篇同名笔记,按标题删会删错人)"
SKIP_AMBIGUOUS_KEY = "指标映射歧义((标题, 发布日)在台账里对应多行),按无指标处理"
SKIP_DELETE_IN_FLIGHT = "已有未完成的淘汰删除任务在途,本轮不重复选它"
SKIP_PROTECTED = "保护位跳过(该笔记已标记 protected,永不参与淘汰;仍计入库存)"
# 运行告警(不是某一篇笔记的事,但只有 details 这一个自由字段能落)
WARN_BAD_CAP = (
    "⚠️ 运行告警:note_cap 非法({raw}),本轮按默认上限 {fallback} 篇计算 —— "
    "绝不把非法上限当 0(那等于按单日封顶把整个号一路删空)"
)


# ---------------- 纯逻辑(不碰 DB,可直接单测) ----------------


def parse_weights(raw: str | None) -> dict[str, float]:
    """解析 RETENTION_WEIGHTS(JSON 串)→ 五指标权重;任何不对劲都回退默认并告警。

    不对劲的定义:JSON 解析失败 / 不是对象 / 某项不是数 / 有负权重 / 权重和 ≤ 0。
    **绝不带着一份坏权重去删笔记** —— 权重错了不会报错,只会安静地删错篇。
    缺哪项就用哪项的默认值(允许只覆盖其中一两个指标)。
    """
    fallback = dict(DEFAULT_WEIGHTS)
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        logger.warning(f"[retention] RETENTION_WEIGHTS 不是合法 JSON,回退默认权重: {raw!r}")
        return fallback
    if not isinstance(parsed, dict):
        logger.warning(f"[retention] RETENTION_WEIGHTS 不是 JSON 对象,回退默认权重: {raw!r}")
        return fallback
    out: dict[str, float] = {}
    for field in METRIC_FIELDS:
        value = parsed.get(field, DEFAULT_WEIGHTS[field])
        try:
            out[field] = float(value)
        except (TypeError, ValueError):
            logger.warning(
                f"[retention] RETENTION_WEIGHTS.{field}={value!r} 不是数,回退默认权重"
            )
            return fallback
    if any(v < 0 for v in out.values()) or sum(out.values()) <= 0:
        logger.warning(f"[retention] RETENTION_WEIGHTS 取值非法(负权重/权重和≤0),回退默认: {out}")
        return fallback
    return out


def metric_publish_date(publish_time: str | None) -> str | None:
    """从导出原文发布时间串取 "YYYY-MM-DD";取不出返回 None(该行不进索引)。"""
    m = _PUBLISH_DATE_RE.search(publish_time or "")
    if not m:
        return None
    try:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    except ValueError:  # pragma: no cover - 正则已保证是数字
        return None


def ledger_publish_date(published_at: datetime | None) -> str | None:
    """台账里的 naive UTC 时刻 → 北京日期串,用于与导出指标对齐(见模块 docstring 时区段)。"""
    if published_at is None:
        return None
    return (published_at + _BEIJING_OFFSET).strftime("%Y-%m-%d")


def note_key(note_id: str | None, title: str | None) -> tuple[str, str]:
    """台账行的匹配键:有 note_id 就用它,没有(台账同步还没补上 id)才退回标题。

    删除任务的回执里只带得回这两样,所以收敛链的两端(选篇时排除在途、事后标记已删)
    用同一个键,免得一头按 id 一头按标题对不上。
    """
    return ("id", note_id) if note_id else ("title", title or "")


def build_metrics_index(rows) -> dict[tuple[str, str], dict]:
    """把某号的 note_metrics 行摊成 {(title, 北京发布日): 五指标} 索引。

    同 (title, 日) 出现多行(同一天发了两篇同名笔记)时保留**先出现的那行**——调用方按
    id 升序取行,所以"先"即"先入库的那篇",结果确定。

    "丢弃后来行"看着像是在赌运气,实际无害:同名笔记在 ``build_plan`` 里已被同名歧义那道
    整批挡在候选之外(标题不唯一 → 该标题全部行不参与淘汰),被丢掉的那行的指标永远不会
    拿来给谁定生死。真正保证正确性的是那道,不是这里的先来后到。
    """
    index: dict[tuple[str, str], dict] = {}
    for row in rows:
        day = metric_publish_date(row.publish_time)
        if day is None:
            continue
        key = (row.title, day)
        if key in index:
            continue
        index[key] = {f: int(getattr(row, f, 0) or 0) for f in METRIC_FIELDS}
    return index


def score_candidates(candidates: list[dict], weights: dict[str, float]) -> None:
    """就地给候选篇打分(写入 ``score`` 键):五指标各自 min-max 归一化后加权平均。

    为什么先归一化:浏览量上万、增粉个位数,不归一化的话权重之比毫无意义(views 会
    以量纲碾压其余四项)。归一化基准是**本次候选集内**的极值,所以分数只在同一次运行内
    可比,跨天不可比 —— 审计明细里存的是当次分数,不要拿来做趋势。

    单篇候选 / 某指标全同值 → 该指标 span=0,归一化恒取 0.0(除零保护;全同值本来也
    不提供任何区分度)。全零指标的篇因此得 0 分,排在最前(最该淘汰),符合需求语义。
    """
    if not candidates:
        return
    total_weight = sum(weights.get(f, 0.0) for f in METRIC_FIELDS)
    if total_weight <= 0:  # parse_weights 已保证 >0,这里是纯防御
        total_weight = sum(DEFAULT_WEIGHTS.values())
        weights = dict(DEFAULT_WEIGHTS)
    normalized: list[dict[str, float]] = [{} for _ in candidates]
    for field in METRIC_FIELDS:
        values = [float((c.get("metrics") or {}).get(field, 0) or 0) for c in candidates]
        low, high = min(values), max(values)
        span = high - low
        for slot, value in zip(normalized, values):
            slot[field] = 0.0 if span <= 0 else (value - low) / span
    for candidate, slot in zip(candidates, normalized):
        raw = sum(weights.get(f, 0.0) * slot[f] for f in METRIC_FIELDS)
        candidate["score"] = round(raw / total_weight, 6)


def _rank_key(candidate: dict) -> tuple:
    """淘汰排序键:得分升序;同分先淘汰**发布更早**的;再同则按标题定序(结果可复现)。"""
    published_at = candidate.get("published_at") or ""
    return (candidate.get("score", 0.0), published_at, candidate.get("title") or "")


def ambiguity_sets(notes: list[dict]) -> tuple[set, set]:
    """算出该号台账里的两种歧义,返回 (重名标题集合, 撞车的 (标题, 北京发布日) 键集合)。

    ①**重名标题**:删除是**按标题**定位卡片的(平台导出无 note_id),标题不唯一时删掉的
    可能是同名里的另一篇。所以同名的那几篇**全部**退出淘汰候选 —— 删错人的风险不可接受,
    宁可这几篇永远不参与淘汰;
    ②**撞车的 join 键**:``(标题, 北京发布日)`` 是台账与 note_metrics 的 join 键,它在台账
    侧一对多时就不知道那份指标是哪一篇的,按无指标处理(退回安全轨②)。

    两者是**两个问题**:①问"能不能安全地删",②问"这份指标是不是这一篇的"。①是更严的
    条件(标题唯一 ⇒ 键唯一),所以在当前调用顺序下 ② 实际上永远不会触发;留着它是因为
    它守的那件事与①无关,而且这个函数把两者一起算出来,读的人能一眼看到两道都在。
    """
    title_counts = Counter(n.get("title") for n in notes)
    key_counts = Counter(
        (n.get("title"), ledger_publish_date(n.get("published_at"))) for n in notes
    )
    return (
        {t for t, c in title_counts.items() if c > 1},
        {k for k, c in key_counts.items() if c > 1},
    )


def _warning_entry(message: str) -> dict:
    """运行告警的伪明细行:key 集合与真明细一模一样,``title`` 为 None 即可辨认。

    审计表只有 ``details_json`` 一个自由字段,告警没有别的地方可落;共用同一套 key 是为了
    让读方(``GET /api/retention-runs``)不必分两种形状解析。
    """
    return {
        "note_id": None, "title": None, "published_at": None,
        "eligible": False, "skip_reason": message,
        "metrics": None, "score": None, "selected": False, "job_id": None,
    }


def build_plan(
    notes: list[dict],
    metrics_index: dict[tuple[str, str], dict],
    *,
    cap: int,
    now: datetime,
    grace_days: int,
    weights: dict[str, float],
    daily_max: int,
    inflight_keys: frozenset | None = None,
) -> dict:
    """纯函数:给定库存 + 指标索引,算出本次该淘汰哪几篇与全量得分明细。

    ``notes`` 每项 ``{"note_id","title","published_at"(naive UTC datetime|None),"protected"}``,
    调用方须**已排除**台账里 ``deleted_at`` 非空的行(那些是已经删掉的,不该再计入库存);
    ``protected`` 为真的行照常计入库存,但**永不入选**(安全轨④,见模块 docstring);
    ``inflight_keys`` 是删除任务还在途的笔记键(见 ``note_key``):它们照常计入库存(平台上
    还在),但**不再入选**,而且会从"超出多少"里扣掉 —— 那几篇的删除已经承诺出去了。
    ``now`` 为 naive UTC。返回
    ``{"note_count","cap","over_cap","eligible_count","selected","details"}``,
    其中 ``selected`` 的元素与 ``details`` 里的**同一批 dict 对象**(便于调用方回填 job_id)。

    没超上限就直接收工:一篇都不算、一篇都不删(needed ≤ 0 时连打分都不做)。
    """
    warnings: list[dict] = []
    if cap <= 0:
        # note_cap 缺失/非法时**往大回退**,绝不当 0 用:cap=0 的语义是"整个号都超限",
        # 会按单日封顶一天删 5 篇、几周把号删空。兜底方向反了比没有兜底危险得多。
        warnings.append(_warning_entry(
            WARN_BAD_CAP.format(raw=cap, fallback=DEFAULT_NOTE_CAP)
        ))
        logger.warning(f"[retention] note_cap 非法({cap}),回退默认 {DEFAULT_NOTE_CAP}")
        cap = DEFAULT_NOTE_CAP

    note_count = len(notes)
    over_cap = max(0, note_count - cap)
    details: list[dict] = []
    candidates: list[dict] = []
    if over_cap <= 0:
        return {
            "note_count": note_count, "cap": cap, "over_cap": 0,
            "eligible_count": 0, "selected": [], "details": warnings,
        }

    inflight = inflight_keys or frozenset()
    dup_titles, dup_keys = ambiguity_sets(notes)  # 同名两道,语义见 ambiguity_sets
    # 重名各有几篇,只为写进审计明细给人看("有 3 篇同名")
    dup_title_counts = Counter(n.get("title") for n in notes if n.get("title") in dup_titles)
    # 在途的删除任务是**已经承诺出去的删除**:它们跑完后库存就会少这么多,所以本轮的"超出
    # 多少"要先把它们减掉。不减的话每一轮都会在没跑完的删除之上再叠一批,最后删过头 ——
    # 10 篇对 5 的帽,第一轮建了 5 条还没跑完,第二轮又按 over_cap=5 再建 5 条,直接删空。
    inflight_count = sum(
        1 for n in notes if note_key(n.get("note_id"), n.get("title")) in inflight
    )

    cutoff = now - timedelta(days=grace_days)
    for note in notes:
        published_at = note.get("published_at")
        title = note.get("title")
        entry = {
            "note_id": note.get("note_id"),
            "title": title,
            "published_at": published_at.isoformat() if published_at else None,
            "eligible": False,
            "skip_reason": None,
            "metrics": None,
            "score": None,
            "selected": False,
            "job_id": None,
        }
        details.append(entry)
        if note.get("protected"):  # 安全轨④:运营显式保下的功能位,打分口径对它不适用
            entry["skip_reason"] = SKIP_PROTECTED
            continue
        if title in dup_titles:  # 同名歧义①:删错人的风险不可接受
            entry["skip_reason"] = SKIP_DUP_TITLE.format(count=dup_title_counts[title])
            continue
        if note_key(note.get("note_id"), title) in inflight:
            # 上一轮建的删除任务还没跑完 = 删了没删还不知道,这一轮再选它就是幽灵重删
            entry["skip_reason"] = SKIP_DELETE_IN_FLIGHT
            continue
        if published_at is None:
            entry["skip_reason"] = SKIP_NO_PUBLISH_TIME
            continue
        if published_at > cutoff:  # 安全轨①:宽限期内不碰
            entry["skip_reason"] = SKIP_GRACE.format(days=grace_days)
            continue
        day = ledger_publish_date(published_at)
        if (title, day) in dup_keys:  # 同名歧义②:指标映射不唯一 → 当作没指标
            entry["skip_reason"] = SKIP_AMBIGUOUS_KEY
            continue
        metrics = metrics_index.get((title, day))
        if metrics is None:  # 安全轨②:无指标不杀
            entry["skip_reason"] = SKIP_NO_METRICS
            continue
        entry["eligible"] = True
        entry["metrics"] = dict(metrics)
        candidates.append(entry)

    score_candidates(candidates, weights)
    # 安全轨③:单日单号删除封顶;再与"还超出多少"(扣掉在途的删除)和"有几篇够格"取小
    take = min(max(0, over_cap - inflight_count), max(0, daily_max), len(candidates))
    selected = sorted(candidates, key=_rank_key)[:take]
    for entry in selected:
        entry["selected"] = True
    # 明细排序:够格的按淘汰优先级(分低在前),不够格的垫底 —— 谁最危险一眼可见;
    # 运行告警不是笔记,统一缀在最后
    details.sort(key=lambda e: (0, _rank_key(e)) if e["eligible"] else (1, ("", "", "")))
    details.extend(warnings)
    return {
        "note_count": note_count, "cap": cap, "over_cap": over_cap,
        "eligible_count": len(candidates), "selected": selected, "details": details,
    }


# ---------------- DB 层(读库存/指标 → 落审计 → 建删除 job) ----------------


def _delete_succeeded(raw: str | None) -> bool:
    """note_delete 的终态结果算不算"这篇真删掉了":要求 ``deleted ≥ 1``。

    done 但 ``deleted=0`` 意味着执行方跑完了却一张卡都没删掉(标题对不上等),那篇还在
    平台上,不能标成已删 —— 标错了它就永远退出淘汰,再也不会被清理。
    """
    try:
        result = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return False
    if not isinstance(result, dict):
        return False
    try:
        return int(result.get("deleted") or 0) >= 1
    except (TypeError, ValueError):
        return False


async def _mark_note_deleted(session, account_id: int, note_id, title, now) -> bool:
    """给这篇笔记的台账行落 ``deleted_at``;真标了返回 True(幂等:只动 deleted_at 仍空的行)。

    有 note_id 就按 id 定位(账号内唯一,不会认错)。没有 note_id 时只能按标题定位,此时
    **要求同标题的未删行恰好一行**才动手 —— 多行说明是同名场景(选篇那道已经把同名整批
    挡在候选外,能走到这里只可能是事后又发了同名的一篇),宁可漏标一次让它下轮继续被同名
    歧义挡下,也不能把 deleted_at 落到错误的那一行(那等于凭空注销一篇还在的笔记)。
    """
    stmt = select(PublishedNote).where(
        PublishedNote.account_id == account_id,
        PublishedNote.deleted_at.is_(None),
    )
    stmt = (
        stmt.where(PublishedNote.note_id == note_id) if note_id
        else stmt.where(PublishedNote.title == title)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return False
    if note_id is None and len(rows) > 1:
        logger.warning(
            f"[retention] 账号 {account_id} 标题 {title!r} 有 {len(rows)} 篇未删台账行,"
            f"无法确定删掉的是哪一篇,跳过 deleted_at 标记"
        )
        return False
    rows[0].deleted_at = now
    return True


async def reconcile_deletions(session, account, *, now: datetime) -> frozenset:
    """淘汰链的收敛步:把已真删掉的笔记在台账上标记 ``deleted_at``,返回删除任务仍在途的笔记键。

    **每轮选篇之前都先跑这一步**。没有它会怎样:一篇被删除任务真删掉的笔记仍留在库存里
    计数,第二天照样被选中、照样再建一条删除任务 —— 而平台上早就没有这张卡片了(幽灵 job),
    淘汰永远不收敛,审计表天天多一行"删了同一篇"。

    数据来源是审计明细自己记下的 job_id(往回看 ``_RECONCILE_LOOKBACK_RUNS`` 行),拿它去
    ``browser_jobs`` 查终态:

    - ``done`` 且结果 ``deleted ≥ 1`` → 落 ``deleted_at``(判据取最严,见 ``_delete_succeeded``);
    - ``queued`` / ``running`` → 还在途,这一轮把这篇排除出候选(结果未知,不许再建一条);
    - ``error``(含僵死恢复标 ``unknown`` 的行)→ 不标记,那篇下轮重新进候选。删除确实没生效
      时重试是对的;若是 unknown 而平台上其实已删,下轮会因找不到同题卡片再落 error,由
      「台账计数 ≠ 平台真实篇数」那条已知边界兜着(见 guide 的 known_limitations)。

    幂等:``deleted_at IS NULL`` 是更新条件,重复跑不会改已标记的行。
    """
    details_rows = list((await session.execute(
        select(RetentionRun.details_json)
        .where(RetentionRun.account_id == account.id)
        .order_by(RetentionRun.id.desc())
        .limit(_RECONCILE_LOOKBACK_RUNS)
    )).scalars().all())
    # job_id -> (note_id, title);同一 job_id 只会出现一次,setdefault 防审计重复行
    tracked: dict[str, tuple] = {}
    for raw in details_rows:
        try:
            entries = json.loads(raw) if raw else []
        except (TypeError, ValueError):  # 单行审计存坏不该拖垮整轮收敛
            logger.warning(f"[retention] 账号 {account.id} 有一行审计明细 JSON 解不开,跳过")
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            job_id = (entry or {}).get("job_id") if isinstance(entry, dict) else None
            if job_id:
                tracked.setdefault(job_id, (entry.get("note_id"), entry.get("title")))
    if not tracked:
        return frozenset()

    jobs = list((await session.execute(
        select(BrowserJob).where(BrowserJob.id.in_(list(tracked)))
    )).scalars().all())
    inflight: set = set()
    marked = 0
    for job in jobs:
        note_id, title = tracked[job.id]
        if job.status in _INFLIGHT_JOB_STATUSES:
            inflight.add(note_key(note_id, title))
            continue
        if job.status == "done" and _delete_succeeded(job.result):
            marked += int(await _mark_note_deleted(session, account.id, note_id, title, now))
    if marked:
        await session.commit()
        logger.info(f"[retention] 账号 {account.id} 收敛:{marked} 篇已删笔记退出库存")
    return frozenset(inflight)


async def plan_account_retention(
    session, account, *, now: datetime | None = None, daily_max: int | None = None
) -> dict:
    """收敛淘汰链 → 读该号库存与最新指标 → 算出淘汰计划(**不建任何删除任务**)。

    唯一的写是 ``reconcile_deletions`` 落的 ``deleted_at``:那不是决策,是把"这篇已经被删了"
    这个既成事实记到台账上,幂等且无外部副作用,所以预演路径走这里也照跑 —— 否则预览看到的
    库存会比真实多出已删的那几篇,与真删轮次的口径对不上。

    ``daily_max`` 省略时用 ``RETENTION_DAILY_DELETE_MAX``;REST 手动触发会传"当日剩余额度"
    进来,免得连点几次绕过单日封顶。

    库存真值取 ``published_notes``(我们的永久台账)里 ``deleted_at`` 为空的行。⚠️ 它与平台
    真实笔记数会双向漂移:运营在 App 里手工删过 → 台账偏多(cap 被虚高计数触发);手工发的
    还没被台账同步捞回来 → 台账偏少(该淘汰的没淘汰)。本版**不做平台侧对账**(那要给每号
    多起一次浏览器会话,会把同号会话额度打满),靠安全轨①②③把漂移后果压住,详见设计第七节
    (保护位那道守的是另一件事 —— 打分口径对功能位不适用,与台账漂移无关)。
    """
    now = now or datetime.utcnow()
    inflight = await reconcile_deletions(session, account, now=now)
    rows = list((await session.execute(
        select(PublishedNote)
        .where(PublishedNote.account_id == account.id)
        .where(PublishedNote.deleted_at.is_(None))
        .order_by(PublishedNote.id)
    )).scalars().all())
    notes = [
        {
            "note_id": r.note_id,
            "title": r.title,
            # 平台权威时间优先;它可能为空(台账同步还没补上),退回本机发布时刻
            "published_at": r.platform_published_at or r.published_at,
            "protected": bool(r.protected),
        }
        for r in rows
    ]
    metric_rows = list((await session.execute(
        select(NoteMetric)
        .where(NoteMetric.account_id == account.id)
        .order_by(NoteMetric.id)
    )).scalars().all())
    return build_plan(
        notes,
        build_metrics_index(metric_rows),
        # note_cap 为 NULL/0/负数时这里传 0 给 build_plan,由它回退默认并在明细里记告警
        cap=int(account.note_cap or 0),
        now=now,
        grace_days=int(settings.RETENTION_GRACE_DAYS),
        weights=parse_weights(settings.RETENTION_WEIGHTS),
        daily_max=int(
            settings.RETENTION_DAILY_DELETE_MAX if daily_max is None else daily_max
        ),
        inflight_keys=inflight,
    )


async def execute_retention(
    session, account, *, dry_run: bool, record: bool,
    now: datetime | None = None, daily_max: int | None = None,
) -> dict:
    """跑一次该号的淘汰:计划 →(record)落审计行 →(非 dry_run)建 note_delete job。

    两个开关是**正交**的,别合并:
    - ``dry_run=True``:不建删除 job(只算)。来源有二 —— kill switch
      ``RETENTION_ENABLED=0``,以及 REST 手动触发的预演;
    - ``record=True``:落 retention_runs 审计行。调度器恒为 True(**先落审计再建 job**:
      反过来的话审计写失败就会出现"删除任务在跑、库里查不到任何依据"的黑箱);
      REST 的手动预演恒为 False —— 预演一旦留下当日审计行,调度器当天就会认为"跑过了"
      而跳过真实轮次,变成"点了一下预览,今天就不淘汰了"的静默失效。

    ``daily_max`` 省略即用配置里的单日封顶;REST 手动触发传的是"当日剩余额度"(封顶减去
    该号当天已建的删除数),否则连点几次就把封顶绕过去了。
    """
    now = now or datetime.utcnow()
    plan = await plan_account_retention(session, account, now=now, daily_max=daily_max)
    selected = plan["selected"]
    if not dry_run:
        # job_id 先生成:审计明细里要写它,而审计必须先于 job 落库
        for entry in selected:
            entry["job_id"] = uuid.uuid4().hex
    deleted_count = 0 if dry_run else len(selected)

    if record:
        session.add(RetentionRun(
            account_id=account.id,
            run_date=(now.strftime("%Y-%m-%d")),
            platform_note_count=plan["note_count"],
            cap=plan["cap"],
            eligible_count=plan["eligible_count"],
            deleted_count=deleted_count,
            dry_run=dry_run,
            details_json=json.dumps(plan["details"], ensure_ascii=False),
        ))
        await session.commit()

    if not dry_run and selected:
        for entry in selected:
            # payload 形态与 note_delete.start_delete 一字不差:执行方按标题在笔记管理页
            # 定位卡片删除,deletion_id 供它双写 note_deletions 旧台账。
            # ⚠️ 删除是**按标题**的(平台导出无 note_id),同号存在同名笔记时删掉的可能是
            # 同名里的另一篇 —— 这条边界继承自既有删除链,不是本功能引入的。
            session.add(BrowserJob(
                id=entry["job_id"],
                kind=DELETE_JOB_KIND,
                account_id=account.id,
                operator_id=_SYSTEM_OPERATOR_ID,
                payload=json.dumps(
                    {"title": entry["title"], "count": 1, "deletion_id": entry["job_id"]},
                    ensure_ascii=False,
                ),
                status="queued",
            ))
        await session.commit()
        logger.info(
            f"[retention] 账号 {account.id} 库存 {plan['note_count']}/{plan['cap']},"
            f"本轮淘汰 {deleted_count} 篇:"
            + "、".join(f"{e['title']}({e['score']})" for e in selected)
        )

    return {
        "account_id": account.id,
        "run_date": now.strftime("%Y-%m-%d"),
        "note_count": plan["note_count"],
        "cap": plan["cap"],
        "over_cap": plan["over_cap"],
        "eligible_count": plan["eligible_count"],
        "selected_count": len(selected),
        "deleted_count": deleted_count,
        "dry_run": dry_run,
        "details": plan["details"],
    }


class RetentionScheduler:
    """每日淘汰调度:每 interval 秒醒来,给"今天还没跑且当日数据已到位"的代管号跑一轮。

    结构套 CookieChecker / NoteMetricsScheduler 模板(start/_run_loop/stop,
    interval>0 才注册),注册点与 NoteMetricsScheduler 完全同款(worker 进程)。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台调度循环。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台循环:每 interval 秒跑一轮 scan_once,单轮异常不打断循环。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                deleted = await self.scan_once()
                if deleted:
                    logger.info(f"[retention_scheduler] 本轮建了 {deleted} 条笔记删除任务")
            except Exception:
                logger.exception("[retention_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮,返回本轮真建的删除 job 条数。

        两道前置(顺序即语义):
        1. **本 UTC 日已跑过就跳过** —— 每号每天至多一轮,retention_runs 有当日行即算跑过
           (含 kill switch 的 dry_run 行:那天的账已经算过了,重复算只会刷屏审计表);
        2. **当日 note_metrics 快照必须已存在** —— 需求原话是"每天拉取数据后发现超了",
           淘汰必须挂在数据拉取之后。没有当日快照就本 tick 跳过等下轮,绝不拿隔夜数据杀笔记。
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")  # 与 note_export snapshot_date 同口径
        now_naive = now.replace(tzinfo=None)  # 与库内 naive UTC 时间列可比
        deleted_total = 0
        async with self._session_factory() as session:
            accounts = list((await session.execute(
                select(XhsAccount)
                .where(XhsAccount.managed.is_(True))
                .order_by(XhsAccount.id)
            )).scalars().all())
            if not accounts:
                return 0
            ran_today = set((await session.execute(
                select(RetentionRun.account_id)
                .where(RetentionRun.run_date == today)
                .distinct()
            )).scalars().all())
            snapped_today = set((await session.execute(
                select(NoteMetricDaily.account_id)
                .where(NoteMetricDaily.snapshot_date == today)
                .distinct()
            )).scalars().all())

            for account in accounts:
                if self._is_stopping():
                    break
                if account.id in ran_today:
                    continue
                if account.id not in snapped_today:
                    logger.debug(
                        f"[retention_scheduler] 账号 {account.id} 今日尚无数据快照,"
                        f"等下轮(淘汰必须挂在数据拉取之后)"
                    )
                    continue
                result = await execute_retention(
                    session,
                    account,
                    dry_run=not settings.RETENTION_ENABLED,
                    record=True,
                    now=now_naive,
                )
                deleted_total += result["deleted_count"]
        return deleted_total

    def _is_stopping(self) -> bool:
        """是否已收到停止信号(未 start 时视为不停止,便于直接调 scan_once 测试)。"""
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, timeout: float) -> None:
        """可被 stop() 立即打断的休眠;未 start 时退化为普通 sleep。"""
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        """优雅停:置停止信号 → 等后台循环退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
