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

from app.auth.context import current_operator
from app.core.errors import NotFoundError
from app.services import op_images
from app.services.quota import assert_operator_quota

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/op/consistent-images",
        "summary": "异步触发 gpt-image-2 锚点法一致性批量生图(含去水印后处理)",
        "admin_only": False,
        "params": {"prompts": "body,list[str](每页绘图提示词,顺序即页序,1-100 条)",
                   "anchor_url": "body,str|None(已确认 P1 的 /uploads 直链,跨篇一致性锚点)",
                   "aspect_ratio": "body,str(默认 3:4 竖版;公众号传 16:9 出横版 1536x1024)"},
        "returns": '{"job_id": int, "session_id": str}',
        "errors": "400=prompts 为空/超限",
        "notes": "202 异步契约:拿 job_id+session_id 后每 10s 轮询 GET "
                 "/api/op/drafts/{session_id}/jobs/{job_id}。锚点法:anchor_url 为空则第 1 张"
                 "(P1)当锚点、其余页各自锚定它;非空则全部页锚定该已确认 P1(不重画 P1)。"
                 "产物自动过去水印工作流(非整数缩小 0.855 + PNG 重编码,同时丢弃 C2PA/EXIF);"
                 "该步失败即判该页失败(不返回带水印图),原图仍可从 orig_urls 取。"
                 "锚点图喂上游前会自动瘦身(两条独立规则:长边 >1536 等比缩到 1536;体积 >500KB "
                 "转 JPEG q90 且**尺寸不变**——上传耗时取决于体积不是像素数)。调用方无需自己压;"
                 "拿 orig_urls 的出图原件(1.6-2.4MB)当 anchor_url 时收益最大,实测降约 88%。"
                 "批量出图耗时约每页 30-60s,8 页 medium 质量约 $0.7;遇上游超时服务端会内部"
                 "重试(单页最多 3 次尝试,退避 5s/15s),个别页因此可能拖到数分钟——**轮询窗口"
                 "建议给到 600s**。",
    },
    {
        "method": "GET", "path": "/api/op/drafts/{session_id}/jobs/{job_id}",
        "summary": "轮询一致性生图任务结果",
        "admin_only": False,
        "params": {"session_id": "path,str", "job_id": "path,int"},
        "returns": '{"status": "queued|running|done|failed", "result": {...}}',
        "errors": "404=任务不存在或已过期",
        "notes": "done 时 result.urls / result.errors / result.orig_urls / result.attempts "
                 "四者等长且与提交 "
                 "prompts 按下标对齐(urls 失败位空串、errors 成功位空串);**额度错表现为 "
                 "done+errors 有值**(不是整任务 failed),需逐页读 errors 判定。"
                 "**去水印失败也算该页失败**(urls 空 + errors 写明),绝不返回带水印的图。"
                 "orig_urls 是去水印前的 provider 原图(NN.orig.png),即便该页去水印失败也可取。"
                 "attempts[i] 是该页打到上游的**实际尝试次数**(含 429/超时重试;没打到上游的位为 0),"
                 "1 表示一次过、>1 表示上游当时不稳——据此判断慢是不是常态、失败页值不值得人工重跑;"
                 "注意 attempts 计的是**服务端这层**的尝试(上限 3),OpenAI SDK 内部每次尝试还会"
                 "自动重试 1 次,故真实打到上游的请求数最多为 attempts×2。"
                 "**计费口径**:上游超时抛的是 SDK APITimeoutError,即我们没拿到响应,这类请求"
                 "一般不计费,但服务端拿不到 usage、**无法从代码层证实**;故不提供 billed 字段"
                 "(证实不了的事不给字段,给了就是假承诺),请以 OpenAI 账单为准。"
                 "urls/orig_urls 都是相对 /uploads/… 路径,拼 base 即公网直链(免鉴权,"
                 "不可猜目录名即访问控制)。任务台账落库(browser_jobs 表),**服务重启不丢**,"
                 "终态与 /uploads 产物目前无 TTL 清理、长期可查——拿到 404 说明 session_id/job_id "
                 "写错或该任务从未建成,并非过期失效,重发同一批前请先确认。"
                 "另:执行进程若被中断(重启/崩溃),该任务在心跳超 900s 后被判 failed 且 "
                 "result.unknown=true,意思是**结果未知、服务端不自动重跑**(生图烧钱),"
                 "请先核对产物目录再决定是否重新发起。",
    },
]


class ConsistentImagesRequest(BaseModel):
    """一致性生图请求体;prompts 顺序即页序。"""

    # 上限 99:产物按页序落 01.png..99.png(/uploads 免鉴权路由白名单为两位数字名)
    prompts: list[str] = Field(min_length=1, max_length=99)
    anchor_url: str | None = Field(default=None, max_length=2000)
    # 出图宽高比。gpt-image-2 只有三种物理尺寸,provider 内按此归一:竖版类
    # (3:4/4:5/2:3/9:16)→1024x1536、方版 1:1→1024x1024、横版类(4:3/3:2/16:9)→1536x1024。
    # 小红书轮播用默认 3:4;公众号封面与正文插图传 16:9(横版)。缺省保持 3:4 向后兼容。
    aspect_ratio: str = Field(default="3:4", max_length=16)


@router.post("/api/op/consistent-images", status_code=202)
async def start_consistent_images_endpoint(payload: ConsistentImagesRequest) -> dict:
    """异步触发锚点法一致性批量生图,立即返回 job_id + session_id。"""
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建生图任务。
    await assert_operator_quota(current_operator())
    prompts = [str(p).strip() for p in payload.prompts if str(p).strip()]
    if not prompts:
        raise ValueError("prompts 为空(全部为空白串)")
    job_id, session_id = op_images.start_images_job(
        prompts, payload.anchor_url, aspect_ratio=payload.aspect_ratio
    )
    return {"job_id": job_id, "session_id": session_id}


@router.get("/api/op/drafts/{session_id}/jobs/{job_id}")
async def get_consistent_images_job_endpoint(session_id: str, job_id: int) -> dict:
    """轮询生图任务:queued/running/done/failed + result。"""
    entry = op_images.get_images_job(session_id, job_id)
    if entry is None:
        raise NotFoundError(f"job {session_id}/{job_id} 不存在或已过期")
    return {"status": entry["status"], "result": entry.get("result") or {}}
