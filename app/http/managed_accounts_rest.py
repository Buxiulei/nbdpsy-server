"""代管账号计划的管理面 REST(5 端点):代管开关 / 笔记上限 / 笔记保护位 / 淘汰审计 / 手动触发。

设计 docs/design/2026-08-10-managed-accounts-design.md 第六节。需求原话是"全部后台处理,
通过 API 给前端控制" —— 本模块就是那个控制面,决策与执行都在
``app/services/retention_scheduler.py``,这里只做入参校验、鉴权与视图组装。

两个概念在这里落地:
- **代管账号**(``managed=1``):内容号。发笔记不指定 account_id 时广播给它们
  (见 ``app/http/publish_rest.py``),笔记数量上限淘汰也只对它们跑;
- **水军号**(``managed=0``):互动号。**行为一字不变** —— 互动补量 / 矩阵互动 /
  cookie 巡检 / 数据采集照旧覆盖全部账号,这一列只是语义标记。

``POST /api/retention-runs`` 的 ``dry_run`` **默认 true**:淘汰是不可逆删除,前端控制面
的默认动作必须是"给我看会删谁",不能是"删给我看"。

**保护位**(``PUT /api/accounts/{id}/notes/{note_id}/protected``)是淘汰口径的显式例外:
淘汰按五指标加权删最低的几篇,底下压着"低互动 = 低价值"这条假设,而它对**功能位笔记**
(全矩阵置顶的品牌片、二维码导流笔记)不成立 —— 那些浏览量天然垫底却是门面与转化入口。
标了保护位的笔记**永不进候选,但仍计入库存**(平台上确实有这一篇)。
"""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.models.retention_run import RetentionRun
from app.models.xhs_account import XhsAccount
from app.services.retention_scheduler import (
    DELETE_JOB_KIND, execute_retention, plan_account_retention,
)

router = APIRouter()

# note_cap 的取值区间:下限 1(0 等于"把这个号清空",不是上限该表达的意思);
# 上限 1000(远超任何真实账号的存量,纯粹挡住误传的天文数字/负数)
_MIN_NOTE_CAP = 1
_MAX_NOTE_CAP = 1000
# 审计流水默认返回条数
_DEFAULT_RUN_LIMIT = 20


MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/managed-accounts",
        "summary": "列账号的代管状态:managed / note_cap / 笔记数 / 最近一次淘汰运行",
        "admin_only": False, "params": {},
        "returns": "{accounts: [{account_id, name, nickname, managed, note_cap, "
                   "note_count, protected_count, "
                   "last_retention_run: {run_date, deleted_count, dry_run}|null}], "
                   "retention: {enabled, grace_days, daily_delete_max, weights, check_interval}}",
        "errors": "",
        "notes": "按 caller 可见账号收窄(admin 全见),与 GET /api/accounts 同门。"
                 "**managed=true 是「代管账号」(内容号)**:①POST /api/publish-jobs 不传 "
                 "account_id 时广播发给它们;②笔记数量上限淘汰只对它们跑。"
                 "**managed=false 是「水军号」(互动号)**,行为一字不变——互动补量 / "
                 "矩阵互动 / cookie 巡检 / 数据采集照旧覆盖全部账号,这个标记不影响它们。"
                 "note_count 是 **published_notes 台账计数**(已被淘汰删掉的不计),"
                 "不是平台真实笔记数:"
                 "运营在 App 里手删过 → 台账偏多;手工发的还没被台账同步捞回来 → 台账偏少。"
                 "淘汰判上限用的就是这个数,所以差异大时先跑一次台账同步 "
                 "POST /api/accounts/{id}/note-ledger-syncs 再看。"
                 "protected_count 是该号**保护位**篇数(永不参与淘汰的功能位笔记,见 "
                 "PUT /api/accounts/{account_id}/notes/{note_id}/protected)。"
                 "⚠️ **它是 note_count 的子集,不是另一堆** —— 保护位仍占着 note_cap 的名额,"
                 "所以「可被淘汰的篇数」是 note_count - protected_count;"
                 "protected_count 逼近 note_cap 时该号实际已经没有淘汰空间了。"
                 "retention 段是当前生效的淘汰参数(只读,改要动服务端配置)。",
    },
    {
        "method": "PUT", "path": "/api/accounts/{account_id}/managed",
        "summary": "设置该号是否代管、以及它的笔记数量上限",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "managed": "body,bool|None(省略=不改;true=代管账号/内容号,false=水军号/互动号)",
            "note_cap": f"body,int|None(省略=不改;{_MIN_NOTE_CAP}-{_MAX_NOTE_CAP},默认 100)",
        },
        "returns": "{account_id, name, managed, note_cap}",
        "errors": "403=无该号授权;404=账号不存在;"
                  f"422=note_cap 越界({_MIN_NOTE_CAP}-{_MAX_NOTE_CAP})或传了这两个之外的字段",
        "notes": "空请求体 {} 是 no-op,返回当前状态(可当「查一个号」用)。"
                 "**把一个号设成 managed=true 立刻有两个后果**:①之后不带 account_id 的发布"
                 "会发到它;②它进入笔记数量上限淘汰的作用域——若当前笔记数已超 note_cap,"
                 "下一次淘汰轮次就会开始删(每号每天最多删 "
                 f"RETENTION_DAILY_DELETE_MAX(默认 {settings.RETENTION_DAILY_DELETE_MAX})篇)。"
                 "拿不准先 POST /api/retention-runs 预演一次看它会选谁。"
                 "调小 note_cap 同理:那不是一个描述性字段,是删除动作的触发线。",
    },
    {
        "method": "PUT", "path": "/api/accounts/{account_id}/notes/{note_id}/protected",
        "summary": "给某篇已发布笔记标 / 撤**保护位**(标了就永不被淘汰删掉)",
        "admin_only": False,
        "params": {
            "account_id": "path,int(笔记所属账号)",
            "note_id": "path,str(平台笔记 id;台账里待补 id 的行没有它,标不了)",
            "protected": "body,bool(**必填**,true=标上保护位,false=撤回)",
        },
        "returns": "{account_id, note_id, title, protected}",
        "errors": "403=无该号授权;404=该号台账里没有这篇笔记;"
                  "422=protected 缺失或传了它之外的字段",
        "notes": "**这是淘汰口径的显式例外,不是一个描述性标记**。淘汰按五指标加权删得分"
                 "最低的几篇,底下压着「低互动 = 低价值」这条假设 —— 它对**功能位笔记**"
                 "(全矩阵置顶的品牌片、二维码导流笔记)不成立:那些浏览量天然只有十几,"
                 "按分就是「最差的那篇」,而删除**不可逆**。标了保护位的笔记"
                 "**永不进淘汰候选**,审计明细里记「保护位跳过」。"
                 "⚠️ **仍计入库存**:平台上确实有这一篇,它占着 note_cap 的名额。所以保护位"
                 "多了不会让淘汰变松,只会让淘汰压力集中到剩下那些能删的笔记上 —— "
                 "保护位篇数逼近 note_cap 时,该号实际已经没有淘汰空间(见 "
                 "GET /api/managed-accounts 的 protected_count)。"
                 "幂等:重复标同一个值是 no-op,返回当前态。"
                 "按 **(account_id, note_id) 一起**定位,只对上 note_id 不算 —— 传错号能改"
                 "别人的保护位的话,一次参数写错就会把某个号的功能位摘掉,而摘掉之后它随时"
                 "会被下一轮淘汰删走。",
    },
    {
        "method": "GET", "path": "/api/retention-runs",
        "summary": "笔记淘汰审计流水(每次运行删了谁、每篇多少分,全量明细)",
        "admin_only": False,
        "params": {
            "account_id": "query,int|None(不传=全部可见账号)",
            "limit": f"query,int(默认 {_DEFAULT_RUN_LIMIT},按新→旧取前 N)",
        },
        "returns": "{runs: [{id, account_id, run_date, note_count, cap, eligible_count, "
                   "deleted_count, dry_run, created_at, details: [{note_id, title, "
                   "published_at, eligible, skip_reason, metrics, score, selected, job_id}]}]}",
        "errors": "403=account_id 越权",
        "notes": "**淘汰不可逆,这条流水就是事后唯一的依据**:details 是全量明细"
                 "(不只被删的那几篇),每篇带五指标原值、归一化加权得分、是否入淘汰名单、"
                 "真建了删除任务的话 job_id 是多少(拿它去 "
                 "GET /api/note-deletions/{deletion_id} 看删除结果)。"
                 "score 只在**同一次运行内**可比:打分前每个指标先在当次候选集内做 min-max "
                 "归一化,跨天的分数不同基准,别拿来做趋势。"
                 "eligible=false 的原因见 skip_reason,六种:**保护位**(运营显式标了 "
                 "protected,永不参与淘汰 —— 见 "
                 "PUT /api/accounts/{account_id}/notes/{note_id}/protected)、宽限期内(发布不足 "
                 f"RETENTION_GRACE_DAYS(默认 {settings.RETENTION_GRACE_DAYS})天)、"
                 "note_metrics 里 join 不到这篇(不知道表现就不删)、台账无发布时间、"
                 "**同名歧义**(该号台账里有多篇同名笔记——删除按标题定位卡片,同名会删错人,"
                 "所以同名的整批永不参与淘汰)、**删除任务在途**(上一轮建的还没跑完,不重复选)。"
                 "details 里 **title 为 null 的行是运行告警不是笔记**(如 note_cap 非法回退默认),"
                 "读 skip_reason 即可。"
                 "dry_run=true 的行是「只算没删」(服务端 RETENTION_ENABLED=0 的 kill switch 态)。",
    },
    {
        "method": "POST", "path": "/api/retention-runs",
        "summary": "手动跑一次笔记上限淘汰(**dry_run 默认 true,只预演不删**)",
        "admin_only": False,
        "params": {
            "account_id": "body,int|None(不传=全部可见的代管账号)",
            "dry_run": "body,bool=true(true=只返回将删名单与得分,**不建任何删除任务**;"
                       "false=真删)",
        },
        "returns": "{dry_run, runs: [{account_id, run_date, note_count, cap, over_cap, "
                   "eligible_count, selected_count, deleted_count, dry_run, details:[...]}]}",
        "errors": "400=指定的账号不是代管账号(managed=false)/ 一个可见的代管账号都没有;"
                  "403=account_id 越权;404=账号不存在;"
                  "409=**dry_run=false 撞上三道真删闸之一**(见 notes):kill switch 关着 / "
                  "该号当天已经真删过一轮 / 该号当天已到单日封顶;"
                  "422=请求体传了 account_id / dry_run 之外的字段",
        "notes": "**dry_run 默认 true 是刻意的**:删除不可逆,控制面的默认动作必须是"
                 "「给我看会删谁」。看 selected_count 与 details 里 selected=true 的那几篇,"
                 "确认无误再带 dry_run=false 重发。"
                 "dry_run=true **不落审计行**——预演一旦留下当日 retention_runs 行,"
                 "当天的自动轮次就会认为「跑过了」而跳过,变成「点了下预览今天就不淘汰了」的"
                 "静默失效。dry_run=false 才落审计行并建删除任务。"
                 "选篇口径与自动轮次**完全同一套代码**:超上限才动手;只在发布超过 "
                 f"RETENTION_GRACE_DAYS(默认 {settings.RETENTION_GRACE_DAYS})天且能 join 到 "
                 "note_metrics 的笔记里选;单次单号最多 "
                 f"RETENTION_DAILY_DELETE_MAX(默认 {settings.RETENTION_DAILY_DELETE_MAX})篇。"
                 "唯一差别是自动轮次要等当日数据快照到位,手动触发不等(用的是库里现有的"
                 "最新指标,可能是昨天的)。"
                 "**dry_run=false 要过三道闸,任一不过整批 409、一条任务都不建**:"
                 "①RETENTION_ENABLED=0(kill switch 关着)不许真删;"
                 "②该号当天已有真删轮次(自动或手动)不许再叠一轮;"
                 f"③当天已建的删除任务数已达 RETENTION_DAILY_DELETE_MAX"
                 f"(默认 {settings.RETENTION_DAILY_DELETE_MAX})不许再建 —— 封顶按**当天累计**算,"
                 "不是按每次运行算,所以连点几次不会绕过它,本次实际可删的是当天剩余额度。"
                 "广播真删(不传 account_id)只要有一个号不过闸就整批拒,不部分执行。"
                 "预演不受这三道限制。"
                 "真删走的是既有 note_delete 链路(异步、每篇一条任务、同号串行),job_id 在 "
                 "details[].job_id,轮询 GET /api/note-deletions/{deletion_id}。"
                 "⚠️ 删除**按标题**定位卡片(平台导出无 note_id),同号有同名笔记时删掉的"
                 "可能是同名里的另一篇。",
    },
]


class ManagedUpdateRequest(BaseModel):
    """代管设置的部分更新入参:两个字段都可省略(省略=不改),多余字段直接 422。"""

    model_config = ConfigDict(extra="forbid")
    managed: bool | None = None
    note_cap: int | None = Field(default=None, ge=_MIN_NOTE_CAP, le=_MAX_NOTE_CAP)


class ProtectedUpdateRequest(BaseModel):
    """保护位入参。``protected`` **必填**(不像 managed 那样可省略)。

    这个端点只有一个动作,省略它就没有可执行的语义;而"空体 = 查当前态"那套在这里是
    危险的默认 —— 调用方以为自己标上了、实际什么也没发生,而没标上的后果是那篇笔记随时
    可能被淘汰删走。要查当前态请读 GET /api/accounts/{id}/published-notes。
    """

    model_config = ConfigDict(extra="forbid")
    protected: bool


class RetentionRunRequest(BaseModel):
    """手动触发淘汰的入参。``dry_run`` 默认 true —— 控制面的安全默认,理由见 manifest。"""

    model_config = ConfigDict(extra="forbid")
    account_id: int | None = None
    dry_run: bool = True


def _retention_settings_view() -> dict:
    """当前生效的淘汰参数(只读回显:前端要能解释"为什么这篇没被选")。"""
    return {
        "enabled": bool(settings.RETENTION_ENABLED),
        "grace_days": int(settings.RETENTION_GRACE_DAYS),
        "daily_delete_max": int(settings.RETENTION_DAILY_DELETE_MAX),
        "weights": settings.RETENTION_WEIGHTS,
        "check_interval": int(settings.RETENTION_CHECK_INTERVAL),
    }


def _run_view(row: RetentionRun) -> dict:
    """审计行 → 对外视图;details_json 解开返回(存坏了给空表,不让一行坏数据打死整个列表)。"""
    try:
        details = json.loads(row.details_json) if row.details_json else []
    except (TypeError, ValueError):
        details = []
    return {
        "id": row.id,
        "account_id": row.account_id,
        "run_date": row.run_date,
        # 列名叫 platform_note_count,但存的是台账计数(见模型注释),对外用不误导的名字
        "note_count": row.platform_note_count,
        "cap": row.cap,
        "eligible_count": row.eligible_count,
        "deleted_count": row.deleted_count,
        "dry_run": bool(row.dry_run),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "details": details,
    }


async def _real_run_quota(session, accounts, now: datetime) -> dict[int, int]:
    """真删三闸;全部通过才返回 {账号 id: 本轮可建的删除条数}(当日剩余额度)。

    自动轮次身上那三道闸,手动触发原本**一道都不过**:kill switch 关着照样能删、当天自动
    轮次删过了还能再叠一轮、单日封顶是按"每次运行"算的所以连点三次就是 15 篇。手动触发和
    自动轮次跑的是同一套选篇代码,却绕开了同一套限速 —— 这里把三道补齐。

    1. ``RETENTION_ENABLED=0`` → 409(kill switch 关着就是不许删,只能 dry_run 预演);
    2. 当日该号已有真删审计行 → 409(每号每天至多一轮,防重复叠删);
    3. 当日剩余额度 = 单日封顶 − 该号当天已建的 note_delete 数;≤0 → 409。额度按**当天已建**
       算而不是按本次运行算,是为了让"连点几次"和"跑一次"受同一个封顶约束。

    **一个号不过就整批拒**(不部分执行):广播真删跑到一半 409 会留下"前几个号删了、后几个
    没删"的半批状态,事后谁也说不清删到哪了。预演路径不受这三道限制 —— 它没有副作用。
    """
    if not settings.RETENTION_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="淘汰总开关 RETENTION_ENABLED=0(kill switch 关着),真删被拒;"
                   "此时只能用 dry_run=true 预演会删谁。要真删请让管理员打开开关后重试",
        )
    today = now.strftime("%Y-%m-%d")  # 与审计行 run_date / 调度器同口径:UTC 日界
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ids = [a.id for a in accounts]
    ran_today = set((await session.execute(
        select(RetentionRun.account_id)
        .where(RetentionRun.account_id.in_(ids))
        .where(RetentionRun.run_date == today)
        .where(RetentionRun.dry_run.is_(False))
        .distinct()
    )).scalars().all())
    deleted_today = dict((await session.execute(
        select(BrowserJob.account_id, func.count())
        .where(BrowserJob.account_id.in_(ids))
        .where(BrowserJob.kind == DELETE_JOB_KIND)
        .where(BrowserJob.created_at >= today_start)
        .group_by(BrowserJob.account_id)
    )).all())

    daily_max = int(settings.RETENTION_DAILY_DELETE_MAX)
    blocked: list[str] = []
    quota: dict[int, int] = {}
    for account in accounts:
        if account.id in ran_today:
            blocked.append(
                f"账号 {account.id}({account.name}):今天({today} UTC)已经真删过一轮"
            )
            continue
        used = int(deleted_today.get(account.id, 0))
        if daily_max - used <= 0:
            blocked.append(
                f"账号 {account.id}({account.name}):今天已建 {used} 条删除任务,"
                f"已到单日封顶 {daily_max} 篇"
            )
            continue
        quota[account.id] = daily_max - used
    if blocked:
        raise HTTPException(
            status_code=409,
            detail="真删被拒 —— 「每号每天至多一轮」与单日封顶对手动触发同样生效"
                   "(否则连点几次就把封顶绕过去了):" + ";".join(blocked)
                   + "。dry_run=true 的预演不受此限,随时可跑",
        )
    return quota


@router.get("/api/managed-accounts")
async def list_managed_accounts_endpoint() -> dict:
    """列账号代管状态 + 笔记数 + 最近一次淘汰运行(按 caller 可见账号收窄)。"""
    operator = current_operator()
    async with get_session() as session:
        ids = await visible_account_ids(operator, session)
        stmt = select(XhsAccount).order_by(XhsAccount.id)
        if ids is not None:
            if not ids:
                return {"accounts": [], "retention": _retention_settings_view()}
            stmt = stmt.where(XhsAccount.id.in_(ids))
        accounts = list((await session.execute(stmt)).scalars().all())
        account_ids = [a.id for a in accounts]

        # 只数还在的:deleted_at 非空 = 已被淘汰删掉,不该再计入库存(见 reconcile_deletions)
        counts = dict((await session.execute(
            select(PublishedNote.account_id, func.count())
            .where(PublishedNote.account_id.in_(account_ids))
            .where(PublishedNote.deleted_at.is_(None))
            .group_by(PublishedNote.account_id)
        )).all()) if account_ids else {}

        # 保护位篇数。**与 note_count 同一个口径**(同样排掉 deleted_at 非空的行),
        # 否则会出现 protected_count > note_count 这种读不懂的数。它是 note_count 的子集。
        protected_counts = dict((await session.execute(
            select(PublishedNote.account_id, func.count())
            .where(PublishedNote.account_id.in_(account_ids))
            .where(PublishedNote.deleted_at.is_(None))
            .where(PublishedNote.protected.is_(True))
            .group_by(PublishedNote.account_id)
        )).all()) if account_ids else {}

        # 每号最近一次运行:按 id 倒序取全部再在内存里认第一条(审计量极低,一天几行)
        latest: dict[int, RetentionRun] = {}
        if account_ids:
            for row in (await session.execute(
                select(RetentionRun)
                .where(RetentionRun.account_id.in_(account_ids))
                .order_by(RetentionRun.id.desc())
            )).scalars().all():
                latest.setdefault(row.account_id, row)

    return {
        "accounts": [
            {
                "account_id": a.id,
                "name": a.name,
                "nickname": a.nickname,
                "managed": bool(a.managed),
                "note_cap": int(a.note_cap or 0),
                "note_count": int(counts.get(a.id, 0)),
                "protected_count": int(protected_counts.get(a.id, 0)),
                "last_retention_run": (
                    {
                        "run_date": latest[a.id].run_date,
                        "deleted_count": latest[a.id].deleted_count,
                        "dry_run": bool(latest[a.id].dry_run),
                    }
                    if a.id in latest else None
                ),
            }
            for a in accounts
        ],
        "retention": _retention_settings_view(),
    }


@router.put("/api/accounts/{account_id}/managed")
async def update_managed_endpoint(account_id: int, payload: ManagedUpdateRequest) -> dict:
    """设置该号是否代管与笔记上限;省略的字段不改,空请求体是 no-op(返当前状态)。"""
    operator = current_operator()
    fields = payload.model_fields_set
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        if "managed" in fields and payload.managed is not None:
            account.managed = payload.managed
        if "note_cap" in fields and payload.note_cap is not None:
            account.note_cap = payload.note_cap
        await session.commit()
        return {
            "account_id": account.id,
            "name": account.name,
            "managed": bool(account.managed),
            "note_cap": int(account.note_cap or 0),
        }


@router.put("/api/accounts/{account_id}/notes/{note_id}/protected")
async def update_note_protected_endpoint(
    account_id: int, note_id: str, payload: ProtectedUpdateRequest
) -> dict:
    """标 / 撤某篇笔记的保护位;返回当前态。幂等,重复标同一个值是 no-op。

    定位必须 **(account_id, note_id) 一起**:note_id 虽然平台侧唯一,但只按它定位意味着
    传错 account_id 也能改成 —— 一次参数写错就把别的号的功能位摘掉了,而摘掉之后那篇随时
    会被下一轮淘汰删走。对不上就 404,不静默改。
    """
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        note = (await session.execute(
            select(PublishedNote)
            .where(PublishedNote.account_id == account_id)
            .where(PublishedNote.note_id == note_id)
        )).scalars().first()
        if note is None:
            raise NotFoundError(
                f"账号 {account_id} 的台账里没有笔记 {note_id};"
                f"确认 note_id 与所属账号都对得上(台账里待补 id 的行还没有 note_id,"
                f"可先跑一次 POST /api/accounts/{account_id}/note-ledger-syncs)"
            )
        note.protected = payload.protected
        await session.commit()
        return {
            "account_id": account_id,
            "note_id": note_id,
            "title": note.title,
            "protected": bool(note.protected),
        }


@router.get("/api/retention-runs")
async def list_retention_runs_endpoint(
    account_id: int | None = None, limit: int = _DEFAULT_RUN_LIMIT
) -> dict:
    """淘汰审计流水(新→旧);指定 account_id 时显式鉴权,否则按可见账号收窄。"""
    operator = current_operator()
    async with get_session() as session:
        stmt = select(RetentionRun)
        if account_id is not None:
            await assert_account_access(operator, account_id, session)
            stmt = stmt.where(RetentionRun.account_id == account_id)
        else:
            ids = await visible_account_ids(operator, session)
            if ids is not None:
                if not ids:
                    return {"runs": []}
                stmt = stmt.where(RetentionRun.account_id.in_(ids))
        rows = (await session.execute(
            stmt.order_by(RetentionRun.id.desc()).limit(limit)
        )).scalars().all()
        return {"runs": [_run_view(r) for r in rows]}


@router.post("/api/retention-runs", status_code=200)
async def start_retention_run_endpoint(payload: RetentionRunRequest) -> dict:
    """手动跑一次淘汰。**dry_run 默认 true**:只返回将删名单与得分,不建任何删除任务。

    dry_run=true 走 ``plan_account_retention``(纯只读,连审计行都不落,理由见 manifest);
    dry_run=false 走 ``execute_retention``(落审计 → 建 note_delete 任务),与自动轮次
    **完全同一套选篇代码**。
    """
    operator = current_operator()
    async with get_session() as session:
        if payload.account_id is not None:
            await assert_account_access(operator, payload.account_id, session)
            account = await session.get(XhsAccount, payload.account_id)
            if account is None:
                raise NotFoundError(f"账号 {payload.account_id} 不存在")
            if not account.managed:
                # 不静默跑:非代管号没有「上限」这个概念,跑出来的空结果会被误读成「没有超限」
                raise ValueError(
                    f"账号 {payload.account_id} 不是代管账号(managed=false),不参与笔记上限淘汰;"
                    f"要纳入请先 PUT /api/accounts/{payload.account_id}/managed"
                )
            accounts = [account]
        else:
            stmt = select(XhsAccount).where(XhsAccount.managed.is_(True))
            ids = await visible_account_ids(operator, session)
            if ids is not None:
                if not ids:
                    raise ValueError("你没有任何可见账号,无法触发淘汰")
                stmt = stmt.where(XhsAccount.id.in_(ids))
            accounts = list((await session.execute(
                stmt.order_by(XhsAccount.id)
            )).scalars().all())
            if not accounts:
                raise ValueError(
                    "没有任何可见的代管账号(managed=true),无处可跑;"
                    "请先 PUT /api/accounts/{account_id}/managed 把账号加入代管"
                )

        # 一个时钟贯穿闸与运行:闸算的"今天"与审计行的 run_date 必须是同一天,否则跨零点
        # 那一瞬会出现"闸按昨天放行、审计写今天"的错配。
        now = datetime.utcnow()
        quota = {} if payload.dry_run else await _real_run_quota(session, accounts, now)

        runs = []
        for account in accounts:
            if payload.dry_run:
                plan = await plan_account_retention(session, account, now=now)
                # 字段与 manifest 的 returns 逐个对齐(含 run_date):两条路径形状不一致的话,
                # 照 manifest 解析的调用方会在预演路径上拿不到 run_date。
                runs.append({
                    "account_id": account.id,
                    "run_date": now.strftime("%Y-%m-%d"),
                    "note_count": plan["note_count"],
                    "cap": plan["cap"],
                    "over_cap": plan["over_cap"],
                    "eligible_count": plan["eligible_count"],
                    "selected_count": len(plan["selected"]),
                    "deleted_count": 0,
                    "dry_run": True,
                    "details": plan["details"],
                })
            else:
                runs.append(await execute_retention(
                    session, account, dry_run=False, record=True, now=now,
                    daily_max=quota[account.id],
                ))
        return {"dry_run": payload.dry_run, "runs": runs}
