"""published-notes 分组 REST(6 端点):发布笔记永久台账查询 + 台账同步 + 可见性切换。

三块能力此前只有服务端实现(``app.services.note_ledger`` / ``app.services.note_visibility``),
外部调用方一个都调不到,本模块把它们开出去:

- 台账查询(同步读):``GET /api/accounts/{id}/published-notes`` 列该号台账(分页)、
  ``GET /api/published-notes/{note_id}`` 取单条。
- 台账同步(异步 202 + 轮询):``POST /api/accounts/{id}/note-ledger-syncs`` 手工触发
  一次创作中心列表抓取并纠正台账,补 note_id / 平台时间 / 可见性 / 互动快照。
- 可见性切换(异步 202 + 轮询):``POST /api/accounts/{id}/note-visibility-changes``
  把某篇笔记切成公开可见或仅自己可见。

**三个字段的语义歧义已经害人踩过坑,响应文档里逐条写死**(见 MANIFEST_ENTRIES):
``permission_code`` 的 ``null`` 是**未知**不是公开;``sync_status`` 三态的分别;
``published_at``(本机记录,永不为空)与 ``platform_published_at``(平台权威,可能为空)。

可见性切换是**会改变线上内容可见性**的敏感操作,且**非幂等**(失败不自动重跑):
见 ``POST /api/accounts/{id}/note-visibility-changes`` 的 notes。
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount
from app.services import note_ledger, note_visibility
from app.services.quota import assert_operator_quota

router = APIRouter()

# 台账列表单页上限:台账永不清理,单号规模是几十到几百篇,200 一页足够翻完。
_MAX_LIMIT = 200

# 台账三态字段的口径说明,随列表/单条一起下发——这三条正是设计文档里点名"已经害人
# 踩过坑"的歧义点,写在 meta 里比只写 manifest 更难被忽略。
_FIELD_NOTES = {
    "permission_code": (
        "平台原值,**不是**我们自造的 public/private 枚举:0=公开可见,1=仅自己可见,"
        "**null=未知(不等于公开)**——台账行还没同步到平台可见性时就是 null。"
        "另有三档(仅互关好友可见/部分人可见/部分人不可见)语义未验证,真出现会原样落这里。"
        "判断一篇笔记是否公开必须写 `permission_code == 0`,写 `not permission_code` "
        "会把 null(未知)误判成公开。"
    ),
    "sync_status": (
        "pending_id=已发布、待补 note_id(发布当场就落了台账行,平台侧字段还没同步回来);"
        "linked=已关联到发布任务与内容归档(source_publish_job_id / content_archive_id 可用);"
        "orphan=平台上有但本系统没发过(如人工在 App 里发的老帖),没有发布任务可连。"
    ),
    "published_at": (
        "本机记录的发布完成时刻,**永不为空**——这是我们 100% 掌握的时刻;"
        "platform_published_at 是平台权威时间,**可能为空**(靠台账同步补),"
        "两者有分钟级差异属正常。做时间序分析优先用 platform_published_at,"
        "为空再退回 published_at。"
    ),
    "interaction": (
        "对账用的互动快照(台账同步时刷新),**非权威指标源**;权威指标与逐日趋势走 "
        "GET /api/accounts/{account_id}/note-trends。"
    ),
    "ordering": "按 published_at 降序(最新发布在前),同刻按台账行 id 降序。",
}

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts/{account_id}/published-notes",
        "summary": "列该号发布笔记永久台账(分页)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "limit": "query,int=50(1-200)",
            "offset": "query,int=0",
        },
        "returns": "{notes:[{id,account_id,note_id,title,note_url,note_type,published_at,"
                   "platform_published_at,generated_at,permission_code,permission_msg,"
                   "visibility_changed_at,visibility_changed_by,sync_status,"
                   "source_publish_job_id,content_archive_id,operator_id,last_synced_at,"
                   "interaction:{likes,collects,comments,shares,views}}, ...], "
                   "total:int(该号台账总行数,与 limit 无关), limit, offset, "
                   "meta:{field_notes(三个歧义字段的口径 + 排序口径)}}",
        "errors": "403=无该号授权",
        "notes": "台账**永不清理**:我们发过的每一篇都在,平台上被删/被限流的也留着(只记录不删)。"
                 "按 published_at 降序。⚠️ permission_code 的 null 是**未知不是公开**;"
                 "sync_status 三态 pending_id/linked/orphan 含义见 meta.field_notes;"
                 "published_at 永不为空(本机记录)而 platform_published_at 可能为空(平台权威)。"
                 "note_url 带 xsec 时效参数,**不保证永远可打开**,失效即重新同步。"
                 "interaction 是对账快照非权威指标,要指标走 note-trends。",
    },
    {
        "method": "GET", "path": "/api/published-notes/{note_id}",
        "summary": "按平台 note_id 取单条台账",
        "admin_only": False, "params": {"note_id": "path,str(24 位 hex 平台笔记 id)"},
        "returns": "{note:{同列表单条视图}, meta:{field_notes}}",
        "errors": "403=无该笔记所属账号的授权;404=台账里没有该 note_id",
        "notes": "只能按**平台 note_id** 取。sync_status=pending_id 的行还没有 note_id"
                 "(是 null),**本端点查不到它们**——那些只能从列表端点按账号翻。"
                 "note_id 已同步补上后即可查。",
    },
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-ledger-syncs",
        "summary": "异步触发一次该号台账同步(补 note_id / 刷可见性与互动快照)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": '{sync_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器进创作中心笔记管理页抓全量列表并纠正台账"
                 "(约 1-3 分钟);拿 sync_id 后每 5-10s 轮询 GET /api/note-ledger-syncs/{sync_id}。"
                 "同步会:给 pending_id 行补 note_id / note_url / 平台时间、刷 permission_code "
                 "与互动快照、用平台标题纠正 title、给平台上有而台账没有的笔记建 orphan 行;"
                 "**台账有而平台列表查不到的行只记日志不删**。本操作**幂等**(纯只读抓取 + upsert),"
                 "失败或僵死可放心重发。同号浏览器操作(发布/cookie 检测/导出/切可见性)"
                 "共享 per-account 锁串行,别对同号并发发起。",
    },
    {
        "method": "GET", "path": "/api/note-ledger-syncs/{sync_id}",
        "summary": "轮询台账同步结果",
        "admin_only": False, "params": {"sync_id": "path,str"},
        "returns": "{status, note_count?, refreshed?, linked?, linked_by_title?, orphan?, "
                   "ambiguous?, pending_remaining?, missing?, reason?}",
        "errors": "403=无该号授权;404=sync_id 不存在",
        "notes": "status 四态:queued(待派发)/running(同步中)/done(附计数)/error(附 reason,"
                 "如账号无可用 cookie、浏览器起不来;不代表下次必失败,可直接重发)。"
                 "done 的计数含义:note_count=平台列表抓到几篇;refreshed=刷新了几条已有台账行;"
                 "linked=几条 pending_id 补上了 note_id;linked_by_title=几条存量行按标题回连上了"
                 "发布任务;orphan=新建了几条"
                 "非本系统发布的行;ambiguous=几篇因同标题多条待补而**认不准**(既不认也不建行,"
                 "留着下次);pending_remaining=还剩几条没补上 id;missing=台账有但这次列表里没见到"
                 "(可能被删/被限流,行保留)。本 kind 幂等,僵死会自动重跑,故**不会**出现 unknown。",
    },
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-visibility-changes",
        "summary": "异步触发把某篇笔记切成公开可见 / 仅自己可见(改变线上可见性,慎用)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "body,str(平台笔记 id,**定位主键 + 回读校验**,必填)",
            "title": "body,str|None(**兜底用**,平台列表里查不到该 note_id 时才拿它匹配卡片)",
            "target_privacy": "body,int(**只接受 0=公开可见 / 1=仅自己可见**,其余值 422)",
        },
        "returns": '{change_id, status:"queued"}',
        "errors": "400=账号无 user_id 等入参前置不满足;403=无该号授权;404=账号不存在;"
                  "422=target_privacy 不是整数 0/1(布尔 true/false 也拒,别指望它当 1/0),"
                  "或 note_id 为空;"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器进笔记管理页 → 定位卡片 → 开权限弹窗 → 选档位 → 提交 → "
                 "**回读校验生效**(约 1-2 分钟);拿 change_id 后每 5-10s 轮询 "
                 "GET /api/note-visibility-changes/{change_id}。"
                 "⚠️ 三条硬约束:"
                 "① **只支持这两档**——平台还有「仅互关好友可见」「部分人可见」「部分人不可见」,"
                 "接口参数完全未验证,一律不开放,传别的值直接 422;"
                 "② **定位优先 note_id**——先拿 note_id 去平台列表里翻译出**当前**标题再匹配卡片"
                 "(台账 title 会过期:实测平台显示「粤语咨询师-…」而台账是「心理咨询师-…」),"
                 "body 里的 title 只在平台列表查不到该 note_id 时兜底。平台标题为空、或同一账号下"
                 "重复的笔记仍**定位不了**(error + reason 含 note_not_locatable),这是已知限制;"
                 "③ **非幂等,失败不自动重跑**——调用方看到 error/unknown 必须先核对该笔记**当前**"
                 "可见性(重新同步台账或去创作中心看)再决定,**不要盲目重试**:僵死任务重跑可能把"
                 "运营刚手工改回公开的笔记再次藏起来。"
                 "同号浏览器操作共享 per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/note-visibility-changes/{change_id}",
        "summary": "轮询可见性切换结果",
        "admin_only": False, "params": {"change_id": "path,str"},
        "returns": "{status, result_status?, permission_code?, permission_msg?, reason?}",
        "errors": "403=无该号授权;404=change_id 不存在",
        "notes": "status 五态:queued / running / done(切换已生效并回读确认)/ "
                 "error(附 reason,如 note_not_locatable=标题定位不到、unsupported_privacy、"
                 "账号无可用 cookie)/ **unknown(执行进程中断,改没改成未知)**。"
                 "done 时 result_status 再分两种:`done`=真改了档位;`skipped`=本就是目标档位,"
                 "点了取消没提交(什么都没改,台账也不留 visibility_changed_at 痕迹)。"
                 "permission_code 是切换后的平台原值(0=公开/1=仅自己可见)。"
                 "⚠️ error 与 unknown **都不要自动重试**(非幂等,见 POST 端点约束③);"
                 "unknown 尤其要人工核对当前可见性——它连「提没提交」都不确定。",
    },
]


def _utc(dt: datetime | None) -> str | None:
    """库内 naive UTC → 带 ``+00:00`` 显式偏移的 ISO 串(与 publish-jobs 读回口径一致)。

    数值不变,只把这个 naive datetime 本就代表的 UTC 时区标注出来,消除"裸串被当本地
    时间"的歧义;不转 +08:00,避免把 Beijing-only 假设硬编码进通用 REST 契约。
    """
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt is not None else None


def _note_view(row: PublishedNote) -> dict:
    """台账行 → 对外视图。

    ``permission_code`` / ``permission_msg`` **原样透传**(None → null,0 → 0):这两个值
    的 falsy 陷阱正是本模块反复强调的坑,视图层绝不做 ``or`` 兜底把 0 变成别的东西。
    互动快照收在 ``interaction`` 子对象里,与 note-trends 的权威指标在形状上就分开。
    """
    return {
        "id": row.id,
        "account_id": row.account_id,
        "note_id": row.note_id,
        "title": row.title,
        "note_url": row.note_url,
        "note_type": row.note_type,
        "published_at": _utc(row.published_at),
        "platform_published_at": _utc(row.platform_published_at),
        "generated_at": _utc(row.generated_at),
        "permission_code": row.permission_code,
        "permission_msg": row.permission_msg,
        "visibility_changed_at": _utc(row.visibility_changed_at),
        "visibility_changed_by": row.visibility_changed_by,
        "sync_status": row.sync_status,
        "source_publish_job_id": row.source_publish_job_id,
        "content_archive_id": row.content_archive_id,
        "operator_id": row.operator_id,
        "last_synced_at": _utc(row.last_synced_at),
        "interaction": {
            "likes": row.likes,
            "collects": row.collects,
            "comments": row.comments,
            "shares": row.shares,
            "views": row.views,
        },
    }


# ---------------- 台账查询(同步读)----------------


@router.get("/api/accounts/{account_id}/published-notes")
async def list_published_notes_endpoint(
    account_id: int,
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """列该号发布笔记台账(published_at 降序,分页);越权 403。

    一并下发 ``total``(该号台账总行数,不受 limit 影响)让调用方能翻完页,以及
    ``meta.field_notes``——三个歧义字段的口径随数据一起走,不指望调用方去读 manifest。
    """
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        total = await session.scalar(
            select(func.count())
            .select_from(PublishedNote)
            .where(PublishedNote.account_id == account_id)
        )
        rows = (
            (
                await session.execute(
                    select(PublishedNote)
                    .where(PublishedNote.account_id == account_id)
                    # 同刻(存量行常共用同一个同步时刻)按 id 降序兜底,保证翻页稳定
                    .order_by(
                        PublishedNote.published_at.desc(), PublishedNote.id.desc()
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
    return {
        "notes": [_note_view(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "meta": {"field_notes": _FIELD_NOTES},
    }


@router.get("/api/published-notes/{note_id}")
async def get_published_note_endpoint(note_id: str) -> dict:
    """按平台 note_id 取单条台账;不存在 404,无该行所属账号授权 403。

    note_id 在平台侧全局唯一(台账里也有 (account_id, note_id) 唯一约束),故一个
    note_id 至多命中一行。待补 id 的 pending_id 行 note_id 为 NULL,本端点查不到它们。
    """
    operator = current_operator()
    async with get_session() as session:
        row = await session.scalar(
            select(PublishedNote).where(PublishedNote.note_id == note_id)
        )
        if row is None:
            raise NotFoundError(f"台账里没有笔记 {note_id}")
        await assert_account_access(operator, row.account_id, session)
    return {"note": _note_view(row), "meta": {"field_notes": _FIELD_NOTES}}


# ---------------- 台账同步(异步 202 + 轮询)----------------


@router.post("/api/accounts/{account_id}/note-ledger-syncs", status_code=202)
async def start_note_ledger_sync_endpoint(account_id: int) -> dict:
    """异步触发该号一次台账同步,立即返回 sync_id(同步后台跑,不阻塞)。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不发起同步。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    return {"sync_id": note_ledger.start_sync(account_id), "status": "queued"}


@router.get("/api/note-ledger-syncs/{sync_id}")
async def get_note_ledger_sync_endpoint(sync_id: str) -> dict:
    """轮询台账同步结果:queued / running / done(附各项计数)/ error(附 reason)。"""
    row = await load_job(sync_id, "note_ledger_sync", "sync_id")
    view = base_view(row)
    if row["status"] == "done":
        view.update(row.get("result") or {})
    return view


# ---------------- 可见性切换(异步 202 + 轮询)----------------


class NoteVisibilityChangeRequest(BaseModel):
    """可见性切换请求体。

    ``target_privacy`` 用 ``Literal[0, 1]`` 而非 int:**只支持这两档**是平台侧未验证
    带来的硬约束(另外三档与其 user_ids 格式完全没测过),让非法档位在入口就 422,
    而不是排队一两分钟后才在浏览器层拿 unsupported_privacy 失败。
    """

    note_id: str = Field(
        min_length=1, max_length=64,
        description="平台笔记 id(定位主键 + 回读校验切换是否生效)",
    )
    title: str | None = Field(
        default=None, max_length=100,
        description="笔记标题,**兜底用**(平台列表里查不到该 note_id 时才拿它匹配卡片)",
    )
    target_privacy: Literal[0, 1] = Field(
        description="目标档位:0=公开可见 / 1=仅自己可见(本期只做这两档)"
    )

    @field_validator("target_privacy", mode="before")
    @classmethod
    def _reject_bool(cls, value):
        """布尔值一律拒。

        pydantic 会把 JSON 的 ``true``/``false`` 收进 ``Literal[0, 1]``(True→1),于是
        误传 ``"target_privacy": true`` 会被静默读成「仅自己可见」把笔记藏起来。
        ``note_visibility.execute`` 里有同款 ``isinstance(target, bool)`` 闸,
        这里在入口就拦住,别让它排队一两分钟后才失败。
        """
        if isinstance(value, bool):
            raise ValueError("target_privacy 必须是整数 0 或 1,不接受布尔值")
        return value


@router.post("/api/accounts/{account_id}/note-visibility-changes", status_code=202)
async def start_note_visibility_change_endpoint(
    account_id: int, payload: NoteVisibilityChangeRequest
) -> dict:
    """异步触发可见性切换,立即返回 change_id。

    过配额闸的理由与 note-exports / note-deletions 一致:切换要起一个真 camoufox 会话,
    配额闸护的正是这条浏览器流水线,不闸就能一口气排上几百条把别的任务饿死。
    """
    operator = current_operator()
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    change_id = note_visibility.start_change(
        account_id,
        payload.note_id,
        payload.title or "",
        payload.target_privacy,
        operator.id,
    )
    return {"change_id": change_id, "status": "queued"}


@router.get("/api/note-visibility-changes/{change_id}")
async def get_note_visibility_change_endpoint(change_id: str) -> dict:
    """轮询切换结果:queued / running / done / error / unknown。

    done 时把服务侧的 ``status``(done=真改了 / skipped=本就是目标档位)另开一个键
    ``result_status`` 下发——外层 status 是任务生命周期,内层是这次切换到底动没动,
    两者含义不同,合成一个键会让"什么都没改"被读成"已改成功"。
    """
    row = await load_job(change_id, "note_visibility", "change_id")
    view = base_view(row)
    if row["status"] == "done":
        result = row.get("result") or {}
        view["result_status"] = result.get("status")
        view["permission_code"] = result.get("permission_code")
        view["permission_msg"] = result.get("permission_msg")
    return view
