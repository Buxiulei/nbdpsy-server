"""note-collection-batches 分组 REST(2 端点):合集批量扫描 / 批量移出 + 轮询。

服务实现在 ``app.services.note_collection_batch``(一轮一会话 + 单轮预算 + 撞墙即停),
本模块只做入参校验、鉴权与结果映射。

**一次 POST = 一个号一轮 ≤ 单轮上限篇**,不是"一口气清完"。存量 ~100 篇要分几轮跑完,
这是设计意图不是性能问题 —— 每篇移出都是一次真「更新」提交,而同号一小时 5 次浏览器
会话就足以把账号打上验证墙(实测)。

``dry_run`` 分出两条代价完全不同的路,别混着用:

- ``dry_run=true``:**只读扫描**,报每篇在不在目标合集(运营需求 P1「列出合集内笔记」)。
  零点击零提交,可安全重跑;
- ``dry_run=false``:**真移出**(P2)。逐篇走与 ``POST /note-components`` 完全相同的
  代码路径,每篇一次全量覆盖提交。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_collection_batch
from app.services.quota import assert_operator_quota

router = APIRouter()

# 一次请求最多接受多少个 note_id(不是本轮做几篇 —— 本轮做几篇由单轮上限与时间预算定)。
# 给个上界只是不让请求体无限大,超出的部分调用方下次再传。
_MAX_NOTE_IDS = 200

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/collection-batches",
        "summary": "异步跑一轮合集批量清理:扫描名单(只读)或批量移出(**每轮篇数有硬上限**)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "collection_id": "body,str(目标合集 id,取自 GET /api/accounts/{id}/collections)",
            "collection_name": "body,str(**必填**:扫描靠它判定「这篇在不在这个合集里」,"
                               "移出靠它确认「当前所在合集就是目标」——比对不上一律拒绝动手)",
            "note_ids": "body,list[str](要处理的笔记 note_id,1-200 个;**本轮只会做前 "
                        "limit 篇**,剩下的在结果 remaining 里,再发一次请求接着做)",
            "dry_run": "body,bool(默认 false)。true=**只读扫描**,逐篇报 in_collection,"
                       "零点击零提交可安全重跑;false=**真移出**,每篇一次全量覆盖提交",
            "limit": "body,int|None(本轮最多做几篇;**只能往小压**,超过单轮上限按上限算)",
        },
        "returns": '{job_id, planned(本轮打算做几篇), status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;422=collection_id / collection_name / "
                  "note_ids 缺失或超长;429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器(headed 真屏)在**一次会话里**逐篇处理;拿 job_id 后"
                 "每 10-30s 轮询 GET /api/collection-batches/{job_id}。"
                 "⚠️ 五条必读:"
                 "① **一轮做不完是正常的**——单轮上限:移出 "
                 f"NOTE_COLLECTION_REMOVE_ROUND_LIMIT(默认 "
                 f"{settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT})篇 / 扫描 "
                 f"NOTE_COLLECTION_SCAN_ROUND_LIMIT(默认 "
                 f"{settings.NOTE_COLLECTION_SCAN_ROUND_LIMIT})篇;单轮还有时间预算,"
                 "预算用尽就停,没轮到的在 remaining 里,**别把它当失败**;"
                 "② **移出是幂等的**——本就不在该合集的笔记 → status='skipped' 且"
                 "**一次发布都不点**,所以拿一批 note_id 直接跑、重跑都安全;"
                 "③ **撞墙即停**——任一篇撞上验证墙立刻中止本轮、剩余一篇不碰,该号置 "
                 "cookie_status='restricted' 并落 risk_events;**已完成的部分照常记在 "
                 "notes 里不回滚**,撞墙那一篇不记账。看到这个 error **不要立刻重试**;"
                 "④ **非幂等 kind**——僵死不自动重跑(移出路每篇都是一次全量覆盖提交);"
                 "⑤ **P1 名单为什么是扫描**:平台的合集列表接口只给 note_num 总数、没有成员"
                 "列表,而合集详情页的成员接口尚未取证 —— 在拿到实证前我们不猜页面路径,"
                 "先用「逐篇进更新页只读合集区」这条已验证的路把名单扫出来。它慢,但它是真的。",
    },
    {
        "method": "GET", "path": "/api/collection-batches/{job_id}",
        "summary": "轮询合集批量清理结果(逐篇明细 + 本轮没轮到的 remaining)",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, dry_run?, collection_id?, collection_name?, picked?, handled?, "
                   "in_collection?, removed?, skipped?, failed?, remaining?:[note_id], "
                   "notes?:[{note_id, status, in_collection, label?, reason?, detail?}], "
                   "reason?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,做到哪一篇未知)**。"
                 "逐篇 status 三值:扫描路是 scanned / error;移出路是 removed(真移出并"
                 "回读确认)/ skipped(本就不在该合集,零点击零提交)/ error。"
                 "**in_collection 是 P1 名单的答案**:扫描路 true=这篇确实在目标合集里。"
                 "计数:picked=本轮挑了几篇;handled=实际做了几篇(预算用尽会少于 picked);"
                 "in_collection=其中几篇在合集里;removed / skipped / failed 见上;"
                 "remaining=**本轮没轮到的 note_id**,原样再发一次请求即可接着做。"
                 "⚠️ 逐篇 error 里若出现 collection_remove_unknown_modal,表示点移除的 × 之后"
                 "平台弹出了我们**尚未取证**的弹窗:系统绝不盲点、那一篇整单中止不提交,"
                 "请把 reason 里的弹窗原文回报给我们补取证,别自行重试。",
    },
]


class CollectionBatchRequest(BaseModel):
    """合集批量清理请求体。

    ``collection_name`` **必填**(与单篇端点的"强烈建议"不同):批量路一次要处理 N 篇,
    名字缺失会让每一篇都在浏览器层被拒绝动手 —— 整轮白开一次会话。在入口拦掉更便宜。
    """

    collection_id: str = Field(min_length=1, max_length=64)
    collection_name: str = Field(
        min_length=1, max_length=64,
        description="目标合集名:扫描靠它判成员,移出靠它确认「当前所在合集就是目标」",
    )
    note_ids: list[str] = Field(min_length=1, max_length=_MAX_NOTE_IDS)
    dry_run: bool = Field(
        default=False,
        description="true=只读扫描(零点击零提交,出名单);false=真移出(每篇一次全量覆盖提交)",
    )
    limit: int | None = Field(
        default=None, ge=1,
        description="本轮最多做几篇;**只能往小压**,超过单轮上限一律按上限算",
    )


@router.post("/api/accounts/{account_id}/collection-batches", status_code=202)
async def start_collection_batch_endpoint(
    account_id: int, payload: CollectionBatchRequest
) -> dict:
    """异步触发一轮合集批量扫描 / 移出,立即返回 job_id(**一轮 ≤ 单轮上限篇**)。"""
    operator = current_operator()
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    started = note_collection_batch.start_batch(
        account_id,
        payload.collection_id,
        payload.collection_name,
        payload.note_ids,
        dry_run=payload.dry_run,
        limit=payload.limit,
    )
    return {**started, "status": "queued"}


@router.get("/api/collection-batches/{job_id}")
async def get_collection_batch_endpoint(job_id: str) -> dict:
    """轮询合集批量清理结果:queued / running / done(逐篇明细)/ error / unknown。"""
    row = await load_job(job_id, note_collection_batch.JOB_KIND, "job_id")
    view = base_view(row)
    result = row.get("result") or {}
    # 终态(done / error)都下发逐篇详情:**失败时的逐篇原因比成功时更值钱**
    # (2026-08-03 运营上报教训:把详情藏在 done 分支里,error 的调用方一个字都拿不到)。
    if row["status"] in ("done", "error"):
        for key in (
            "dry_run", "collection_id", "collection_name", "picked", "handled",
            "in_collection", "removed", "skipped", "failed", "remaining", "notes",
        ):
            if key in result:
                view[key] = result[key]
    return view
