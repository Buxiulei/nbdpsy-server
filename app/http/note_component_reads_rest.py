"""笔记组件状态**只读查询** REST:引用/合集/话题/图数当前真值,可程序化自证。

为什么存在(2026-08-04 运营 P0-1,该清单最核心一条):引用与合集在正文里零痕迹,
`applied: true` 只是**设置当时**的回读确认;事后任意时刻"现在到底挂着没有"没有任何
程序化查询手段,而台账 published_notes 压根没有这两个字段(运营误读的 None 来自
publish_jobs 的请求参数列)。360 篇每篇要挂,人工开 App 逐条看不现实。

心智与其它浏览器任务一致:POST 202+job_id → GET 轮询(要开真浏览器读更新页,
无法做成同步 GET)。**纯只读零点击**,kind 幂等。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.core.errors import NotFoundError
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.http.job_polling import QUEUE_MANIFEST_NOTE, base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_components_read
from app.services.quota import assert_operator_quota

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST",
        "path": "/api/accounts/{account_id}/note-component-reads",
        "summary": "异步读取一篇已发布笔记的组件当前状态(引用/合集/话题/图数/权限)",
        "admin_only": False,
        "params": {"note_id": "body,str(平台笔记 id,24 位 hex)"},
        "returns": '{job_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;422=note_id 为空;429=配额已满",
        "notes": "**纯只读零点击**,用于程序化自证组件是否真挂上(引用/合集在正文里零痕迹,"
                 "这是唯一不开 App 的验证手段)。挂完组件后调它复核;批量挂载抽检也用它。"
                 "注意:quote_set 只能判「有没有引用」,判不了「引的是不是那一篇」——"
                 "平台引用区只显示「引用 @作者 的笔记」不含标题;引对与否由设置时的"
                 "note_id 定位+标题交叉校验保证。",
    },
    {
        "method": "GET",
        "path": "/api/note-component-reads/{job_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询组件状态读取结果",
        "admin_only": False,
        "params": {"job_id": "path,str"},
        "returns": "{status, title?, permission?, quote_text?, quote_set?, collection_label?, "
                   "collection_set?, collection_entry_present?, topics?, image_count?, body_head?}",
        "notes": "quote_set/collection_set 为判读布尔(空态文案=未设置);"
                 "collection_entry_present=false 表示该账号更新页上「加入合集」入口本身"
                 "不存在(2026-08-04 起两账号实测消失,与合集是否已挂无关,用它区分"
                 "「没挂上」与「根本没入口」);topics 为正文话题实体名列表。",
    },
]

_RESULT_KEYS = (
    "title", "permission", "quote_text", "quote_set", "collection_label",
    "collection_set", "collection_entry_present", "topics", "image_count", "body_head",
)


class NoteComponentReadRequest(BaseModel):
    note_id: str = Field(min_length=1, description="平台笔记 id")


@router.post("/api/accounts/{account_id}/note-component-reads", status_code=202)
async def start_note_component_read_endpoint(
    account_id: int, payload: NoteComponentReadRequest
) -> dict:
    """异步触发一次组件状态只读,立即返回 job_id。"""
    operator = current_operator()
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    job_id = note_components_read.start_read(account_id, payload.note_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/note-component-reads/{job_id}")
async def get_note_component_read_endpoint(job_id: str) -> dict:
    """轮询组件状态读取结果;done 时平铺快照字段。"""
    row = await load_job(job_id, "note_components_read", "job_id")
    view = await base_view(row)
    result = row.get("result") or {}
    if row["status"] in ("done", "error"):
        for key in _RESULT_KEYS:
            if key in result:
                view[key] = result[key]
    return view
