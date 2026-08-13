"""interaction-backfills 分组 REST(2 端点):手工触发历史笔记互动补量 + 轮询。

服务实现在 ``app.services.interaction_backfill``(选篇 + 四层节流 + 撞墙即停 + 记账),
本模块只做入参校验、鉴权与结果映射。

**为什么是 admin_only**:补量是**矩阵级**运维动作 —— 一次 POST 派出去的活由系统按
"今天用得最少的那个号"挑互动方,挑中的号未必是调用者有权的那个,轮询端点又按任务行的
账号做 RBAC 收窄。若开给普通 operator,会出现"自己提交的任务自己 403 查不到"的坑;
更要紧的是,这条链路每一次调用都在消耗全矩阵的风控预算,不该是人人可点的按钮。

**一次 POST = 一个号一轮 ≤ M 篇**,不是"一口气补完"。要补完存量就隔一段时间再点一次
(或等台账同步的自动路径慢慢补),别连着刷 —— 理由见 POST 端点的 notes。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.config import settings
from app.http.job_polling import QUEUE_MANIFEST_NOTE, base_view, load_job
from app.services import interaction_backfill
from app.services.quota import assert_operator_quota

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/interaction-backfills",
        "summary": "异步触发一轮历史笔记互动补量(点赞 + 收藏,**每轮篇数有硬上限**)",
        "admin_only": True,
        "params": {
            "scope": 'body,str("account"|"all"|"newcomer")',
            "target_account_id": "body,int|None(scope=account 必填:**被互动**的那个号)",
            "actor_account_id": "body,int|None(scope=newcomer 必填:**去互动**的那个新号)",
            "limit": "body,int|None(本轮最多做几篇;只能往小压,超过单轮上限按上限算)",
        },
        "returns": '{job_id, actor_account_id, status:"queued", suppressed_notes:[note_id]} '
                   '—— 挑不出可做的活时 job_id=null、status="skipped" 并附 reason(不建任务)。'
                   'suppressed_notes = 被**笔记熔断**永久移出候选的篇(见 notes),'
                   '**这是要人去核实的清单**,不是错误;里面既可能是被平台屏蔽的,'
                   '也可能是在主页里排太靠后翻不到的,系统区分不了',
        "errors": "403=非管理员;422=scope 非法或 scope 与必填 id 不匹配;"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器(headed 真屏)在**一次会话里**逐篇进笔记详情点赞 + 收藏;"
                 "拿 job_id 后每 10-30s 轮询 GET /api/interaction-backfills/{job_id}。"
                 "⚠️ **节流是这个功能的主体,不是附加项**:全矩阵补一遍 ≈ 139 篇 × 6 个号 "
                 "≈ 834 次互动、约 9.3 小时纯浏览器时间,而且全是对老笔记的集中互动——"
                 "平台眼里最典型的补量特征。四层闸:①每号每天最多 "
                 f"NOTE_INTERACTION_DAILY_LIMIT(默认 {settings.NOTE_INTERACTION_DAILY_LIMIT})篇;"
                 "②一轮最多 NOTE_INTERACTION_ROUND_LIMIT(默认 "
                 f"{settings.NOTE_INTERACTION_ROUND_LIMIT})篇,超出留给下一轮;"
                 "③两篇之间随机停 60~240 秒;④与其它浏览器任务共用并发闸不额外起并发。"
                 "**全量补完要好几天,这是设计意图不是性能问题**——一口气刷完几乎必然触发风控,"
                 "而账号被挂墙的代价(需人工手机扫码、期间该号所有任务失败)远高于慢几天。"
                 "选篇口径:只互动 **permission_code=0 的公开笔记**(null=未知同样不碰,"
                 "私密笔记访客根本看不到);定位优先 note_id(台账 title 会过期);"
                 "已做完的篇直接跳过**不开浏览器**;优先新发现的笔记、其次最近发布的。"
                 "互动方由系统挑(今天用得最少的那个 valid 号),不是调用方指定——"
                 "scope=newcomer 除外,那正是指定新号去补别人的历史。"
                 "**非幂等**:僵死不自动重跑(重跑会重复开页、吃掉当日配额、放大风控暴露)。"
                 "**两个断路器**(2026-08-13 事故驱动,专治「白开注定失败的会话」):"
                 "①**actor 熔断** —— 最近 "
                 f"INTERACTION_ACTOR_BREAKER_N(默认 {settings.INTERACTION_ACTOR_BREAKER_N})"
                 "条互动台账行全是 error 的号本轮不派活(实据:一个号会话半死后 96 连败,"
                 "全是同一个 profile_not_loaded,且因为没撞验证墙,「撞墙即停」那套一次都没触发);"
                 f"熔断 INTERACTION_ACTOR_BREAKER_COOLDOWN_H(默认 "
                 f"{settings.INTERACTION_ACTOR_BREAKER_COOLDOWN_H})小时后**半开探测**放行一轮"
                 "(那一轮只做 1 篇),成功即自动复位。②**笔记熔断** —— 一篇被 ≥"
                 f"INTERACTION_NOTE_BREAKER_ACTORS(默认 {settings.INTERACTION_NOTE_BREAKER_ACTORS})"
                 "个**有资格**(valid 且未被 ① 熔断)的不同号在**发布者主页里翻不到**它,"
                 "即**永久**移出候选并出现在 suppressed_notes 里。⚠️ 翻不到有两种成因、台账区分不了:"
                 "被平台屏蔽/限流(实据:三篇 views=0 的笔记被全部 9 个号报找不到),或它在主页里"
                 "排得太靠后、超出了定位的滚动预算。两种情况下继续每轮重试都同样徒劳,所以一律停调度,"
                 "**成因交给人判**:请人工去平台看那几篇还在不在、在主页第几屏;确认它又该补了之后,"
                 "删掉 note_interactions 里该 note_id 的 error 行即可重新入池(系统不自动恢复)。",
    },
    {
        "method": "GET", "path": "/api/interaction-backfills/{job_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询互动补量结果",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, picked?, handled?, liked?, collected?, failed?, notes?, "
                   "reason?}",
        "errors": "403=无该互动方账号的授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done(附计数)/ error(附 reason)/ "
                 "**unknown(执行进程中断,做到哪一篇未知)**。"
                 "done 的计数含义:picked=本轮挑了几篇;handled=实际处理了几篇"
                 "(预算用尽会少于 picked,剩下的留给下一轮);liked / collected=几篇的赞/藏"
                 "**到位**了(含平台上本来就已点过的 skipped——重复点等于取消,那不是失败);"
                 "failed=几篇没成;notes=[{note_id, like, collect, error}] 逐篇明细。"
                 "picked=0 且带 reason 属正常终态:该号今天配额用完了、或没有可补的笔记了。"
                 "⚠️ error 里出现「撞风控墙」= 本轮被中止且该号已置 cookie_status=restricted "
                 "并落 risk_events:**不要重试**,先按墙的类型处置(scan_qr=拿手机小红书 App "
                 "扫码;rate_limit=晾着别再碰这个号)。已完成的那几篇照常记账,不会重做。"
                 "error 与 unknown 都不自动重跑(本 kind 非幂等)。",
    },
]


class InteractionBackfillRequest(BaseModel):
    """补量请求体。

    ``scope`` 用 ``Literal`` 让非法值在入口就 422,而不是排队后才在服务层拿 reason 失败;
    必填 id 的配对关系在 ``model_validator`` 里校验 —— scope=account 却不给
    ``target_account_id`` 是**入参错误**,不该被静默当成"没得可做"。
    """

    scope: Literal["account", "all", "newcomer"] = Field(
        description="account=给某号的历史笔记互动 / all=给所有号的 / newcomer=某新号去补别人的"
    )
    target_account_id: int | None = Field(
        default=None, description="被互动的账号(scope=account 必填)"
    )
    actor_account_id: int | None = Field(
        default=None, description="去互动的那个新号(scope=newcomer 必填)"
    )
    limit: int | None = Field(
        default=None, ge=1,
        description="本轮最多做几篇;**只能往小压**,超过单轮上限一律按上限算",
    )

    @model_validator(mode="after")
    def _check_ids(self) -> "InteractionBackfillRequest":
        if self.scope == "account" and self.target_account_id is None:
            raise ValueError("scope=account 必须给 target_account_id(被互动的那个号)")
        if self.scope == "newcomer" and self.actor_account_id is None:
            raise ValueError("scope=newcomer 必须给 actor_account_id(去互动的那个新号)")
        return self


@router.post("/api/interaction-backfills", status_code=202)
async def start_interaction_backfill_endpoint(
    payload: InteractionBackfillRequest,
) -> dict:
    """异步触发一轮互动补量,立即返回 job_id(**一轮 ≤ 单轮上限篇**,见 manifest)。

    过配额闸的理由与其余浏览器端点一致:一轮要起一个真 camoufox 会话并占住浏览器闸十几
    分钟,不闸就能一口气排上几十条把别的任务饿死。

    挑不出可做的活(所有号今天都到量 / 没有可补的公开笔记)时**不建任务**,返回
    ``job_id=null`` + ``status="skipped"`` + reason:开一个注定空转的浏览器任务毫无意义。
    """
    operator = current_operator()
    require_admin(operator)
    await assert_operator_quota(operator)
    result = await interaction_backfill.start_backfill(
        payload.scope,
        payload.target_account_id,
        payload.actor_account_id,
        payload.limit,
    )
    return {
        "job_id": result["job_id"],
        "actor_account_id": result["actor_account_id"],
        "status": "queued" if result["job_id"] else "skipped",
        "reason": result["reason"],
        # 被笔记熔断永久移出候选的篇:成因只能人判,不露出来就没人会去核实
        "suppressed_notes": result["suppressed_notes"],
    }


@router.get("/api/interaction-backfills/{job_id}")
async def get_interaction_backfill_endpoint(job_id: str) -> dict:
    """轮询补量结果:queued / running / done(附计数与逐篇明细)/ error / unknown。"""
    row = await load_job(job_id, interaction_backfill.JOB_KIND, "job_id")
    view = await base_view(row)
    if row["status"] == "done":
        view.update(row.get("result") or {})
    return view
