"""op 分组 REST(2 端点):一致性生图建任务(202)/ 轮询任务结果。

自薯营家(xhs.nbdpsy.com,2026-07-23 停机)迁移,路径与响应结构逐字段复刻原契约,
skill 侧 ``nbdpsy-xiaohongshu-creator/scripts/gen_images.py`` 零改动自动恢复:
- POST /api/op/consistent-images:{prompts, anchor_url?} → 202 {job_id, session_id}
- GET  /api/op/drafts/{session_id}/jobs/{job_id}:{status, result}
契约细节(下标对齐/done+errors 额度错语义/uploads 免鉴权直链/P1 锚点法)见
services/op_images.py 模块 docstring。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.errors import NotFoundError
from app.services import op_images

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/op/consistent-images",
        "summary": "异步触发 gpt-image-2 锚点法一致性批量生图(含去水印后处理)",
        "admin_only": False,
        "params": {"prompts": "body,list[str](每页绘图提示词,顺序即页序,1-100 条)",
                   "anchor_url": "body,str|None(已确认 P1 的 /uploads 直链,跨篇一致性锚点)"},
        "returns": '{"job_id": int, "session_id": str}',
        "errors": "400=prompts 为空/超限",
        "notes": "202 异步契约:拿 job_id+session_id 后每 10s 轮询 GET "
                 "/api/op/drafts/{session_id}/jobs/{job_id}。锚点法:anchor_url 为空则第 1 张"
                 "(P1)当锚点、其余页各自锚定它;非空则全部页锚定该已确认 P1(不重画 P1)。"
                 "产物自动过去水印工作流(截图重栅格化+元数据剥离)。批量出图耗时约每页 "
                 "30-60s,8 页 medium 质量约 $0.7。",
    },
    {
        "method": "GET", "path": "/api/op/drafts/{session_id}/jobs/{job_id}",
        "summary": "轮询一致性生图任务结果",
        "admin_only": False,
        "params": {"session_id": "path,str", "job_id": "path,int"},
        "returns": '{"status": "queued|running|done|failed", "result": {...}}',
        "errors": "404=任务不存在或已过期",
        "notes": "done 时 result.urls 与提交 prompts 按下标对齐(失败位空串),"
                 "result.errors 为等长消息数组(成功位空串);**额度错表现为 done+errors "
                 "有值**(不是整任务 failed),需逐页读 errors 判定。urls 是相对 /uploads/…"
                 "路径,拼 base 即公网直链(免鉴权,不可猜目录名即访问控制)。进程内存台账,"
                 "重启即丢(404 时重新发起);终态留存 2 小时。",
    },
]


class ConsistentImagesRequest(BaseModel):
    """一致性生图请求体;prompts 顺序即页序。"""

    # 上限 99:产物按页序落 01.png..99.png(/uploads 免鉴权路由白名单为两位数字名)
    prompts: list[str] = Field(min_length=1, max_length=99)
    anchor_url: str | None = Field(default=None, max_length=2000)


@router.post("/api/op/consistent-images", status_code=202)
async def start_consistent_images_endpoint(payload: ConsistentImagesRequest) -> dict:
    """异步触发锚点法一致性批量生图,立即返回 job_id + session_id。"""
    prompts = [str(p).strip() for p in payload.prompts if str(p).strip()]
    if not prompts:
        raise ValueError("prompts 为空(全部为空白串)")
    job_id, session_id = op_images.start_images_job(prompts, payload.anchor_url)
    return {"job_id": job_id, "session_id": session_id}


@router.get("/api/op/drafts/{session_id}/jobs/{job_id}")
async def get_consistent_images_job_endpoint(session_id: str, job_id: int) -> dict:
    """轮询生图任务:queued/running/done/failed + result。"""
    entry = op_images.get_images_job(session_id, job_id)
    if entry is None:
        raise NotFoundError(f"job {session_id}/{job_id} 不存在或已过期")
    return {"status": entry["status"], "result": entry.get("result") or {}}
