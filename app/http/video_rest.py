"""视频管线 REST（transport/remake/revise）：建任务 / 查 / 列 / 重试 / 修订 / 删。

平移自小红书运营工具 ``app/api/endpoints/video_transport.py`` 的 6 端点，语义逐条保真，
仅换三处面：

1. **celery 派发 → 方案 C DB 轮询**：源建 job 后 ``enqueue_stage(id, stage)`` 推 celery
   broker；本项目视频 worker 是**独立进程**（``python -m app.video.worker``，见设计 §1），
   与 API 进程隔离且**不共享内存里的调度器实例**——API 进程无法 ``submit`` 到 worker 的内部
   队列。故 REST 只需把 job 落成 ``status=queued`` + 刷心跳，worker 的轮询主循环
   （``scan_queued`` 每 poll_interval）自会取走从 ``first_incomplete_stage`` 续跑。
   ``create_job`` 出厂即 queued（无需额外动作）；retry / revise 需把 job 复位回 queued
   （见 ``_requeue``：等价 ``scheduler.enqueue`` 的 DB 半，去掉进程内 submit）。
2. **鉴权 = 宿主 apikey 中间件**：不自建 JWT。``current_operator()`` 读中间件写入的运营者；
   归属 ``_can_access``：admin（role=='admin'）全量，否则仅本人 ``created_by``。404/403/400
   经宿主异常处理器统一成错误契约（NotFoundError→404 / AccessDenied→403 / ValueError→400），
   状态冲突用 HTTPException(409)（源同义）。
3. **AsyncSession 由调用方注入**：宿主 services 惯例，端点用 ``get_session()`` 开会话，
   job_store 语义走 ``app.video.scheduler`` 的 async 函数族（收 session）。

``_enrich_inherited_stats`` / ``_INHERITED_STAGES`` 落在本层（源亦在 API 层）：revision
集成测试此前内联同名 helper，M4 落地后改回从本模块导入。

router + MANIFEST_ENTRIES 接线 ``app.http.__init__`` 的 ALL_ROUTERS / ALL_MANIFEST_ENTRIES，
一致性由 tests/test_manifest.py 防漂移测试钉死。
"""

import json
import logging
import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.auth.context import AccessDenied, current_operator
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.operator import Operator
from app.models.video_job import VideoJob
from app.video import paths, scheduler
from app.video.pipeline.remake import revision as remake_revision
from app.video.pipeline.remake.inherit import inherit_artifacts

logger = logging.getLogger(__name__)
router = APIRouter()

# revision job 继承的前五阶段（最贵段：下载/分析/转写/重分段/翻译），从父 job 拷贝产物直接标 done
_INHERITED_STAGES = ["download", "analyze", "transcript", "resegment", "translate"]

# 域名白名单锚在开头（^）——只认 youtube.com/watch?v= 与 youtu.be/，杜绝
# youtube.com.evil.com 之类伪装（SSRF 防线）。video id 至少一位字符（[\w-]+），长度本身不是安全边界。
_YT_RE = re.compile(r"^https://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+")

# ── /uploads/video 静态取图路由的防路径穿越白名单（仿 uploads_rest）─────────────────
# 产物目录段 = {job_id}-{hmac16}（paths._job_root），子目录 ∈ {raw,tts,out}，文件名允许
# 字母数字/点/下划线/连字符且不含 .. 与路径分隔符。三段均为单路径段（FastAPI 路径参数不跨 /），
# 叠加正则白名单 + resolve/is_relative_to 纵深防御，杜绝 ../ 逃逸。
_TOKEN_DIR_RE = re.compile(r"^\d+-[0-9a-f]{16}$")
_SUB_RE = re.compile(r"^(raw|tts|out)$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/video/jobs",
        "summary": "建一条视频管线任务（transport 搬运 / remake 分镜级再制作，异步入队）",
        "admin_only": False,
        "params": {
            "url": "body,str(仅 YouTube：youtube.com/watch?v= 或 youtu.be/，其余立即 400)",
            "mode": "body,str(transport|remake，默认 transport)",
            "burn_subtitles": "body,bool(默认 true，烧录中文字幕)",
            "voice": "body,str|None(豆包音色 id，默认服务端配置音色)",
            "max_resolution": "body,int(默认 1080)",
        },
        "returns": "{job_id}",
        "errors": "422=url 非 YouTube（请求体格式校验）；401=apikey 无效",
        "notes": "异步契约：拿 job_id 后每 10-30s 调 GET /api/video/jobs/{job_id} 轮询到 "
                 "completed/failed；视频链路 CPU 密集，transport 常态数分钟、remake 更久。"
                 "独立 worker 进程串行处理（单机并发 1），入队后最迟一个轮询周期开跑。",
    },
    {
        "method": "GET", "path": "/api/video/jobs",
        "summary": "列视频任务（按 caller 归属过滤，admin 全见）",
        "admin_only": False,
        "params": {
            "limit": "query,int(默认 20，上限 100，新→旧)",
            "offset": "query,int(默认 0)",
            "status": "query,str|None(queued|running|completed|failed)",
        },
        "returns": "{items:[同 GET 单条视图], offset}",
        "errors": "401=apikey 无效",
        "notes": "非 admin 只列本人建的任务。",
    },
    {
        "method": "GET", "path": "/api/video/jobs/{job_id}",
        "summary": "查单条视频任务状态 + 阶段进度 + 产物直链",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{id, url, title, mode, status, stage, error, duration_seconds, "
                    "stages:[{name,status,...}], products, created_at}",
        "errors": "403=非本人任务且非 admin；404=job 不存在",
        "notes": "status 四态：queued/running/completed/failed。products 是各产物的 "
                 "/uploads/video/... 直链（成片 video_url、中英/双语字幕、meta），completed 才齐全。",
    },
    {
        "method": "POST", "path": "/api/video/jobs/{job_id}/retry",
        "summary": "重试失败/已完成的任务（从首个未完成阶段续跑）",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{job_id, resume_stage}",
        "errors": "403=非本人任务且非 admin；404=job 不存在；409=仍在运行中",
        "notes": "仅 failed/completed 可重试；复位回 queued 后由 worker 从 resume_stage 续跑。",
    },
    {
        "method": "POST", "path": "/api/video/jobs/{job_id}/revise",
        "summary": "成片修订（自然语言意见 → 解析编辑清单 → 派生 revision 子任务增量重制）",
        "admin_only": False,
        "params": {
            "job_id": "path,int(被修订的父任务，须 mode=remake 且已 completed)",
            "instructions": "body,str(自然语言修改意见，如「第二句再细腻些」「片头改短」)",
        },
        "returns": "{job_id(子任务), parent_job_id, edit_plan(解析出的编辑清单)}",
        "errors": "400=仅 remake 可修订 / 意见解析失败或空清单（带 LLM 原始说明）；"
                  "403=非本人任务且非 admin；404=job 不存在；409=父片未完成 / 父产物缺失",
        "notes": "子任务继承父的下载/分析/转写/重分段/翻译产物（不重跑最贵段），仅从 rewrite "
                 "起链应用编辑清单。轮询子 job_id 到 completed 即得修订成片；可对成片再修订（多层链）。",
    },
    {
        "method": "DELETE", "path": "/api/video/jobs/{job_id}",
        "summary": "删任务及其全部产物目录（运行中不可删）",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{deleted: job_id}",
        "errors": "403=非本人任务且非 admin；404=job 不存在；409=运行中（先等失败或完成）",
        "notes": "级联删 DATA_DIR/uploads/video/ 下该 job 的产物目录（不可恢复）。",
    },
]


class CreateJobRequest(BaseModel):
    url: str
    burn_subtitles: bool = True
    voice: str | None = None
    max_resolution: int = 1080
    mode: Literal["transport", "remake"] = "transport"

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not _YT_RE.match(v):
            raise ValueError("仅支持 YouTube 视频链接")
        return v


class ReviseJobRequest(BaseModel):
    instructions: str


def _can_access(job: VideoJob, op: Operator) -> bool:
    """归属校验：admin 全量，否则仅本人 created_by（等价源 super_admin / created_by 判定）。"""
    return op.role == "admin" or job.created_by == op.id


def _job_payload(job: VideoJob) -> dict:
    """对外单条视图（与源 _job_payload 同构）：阶段展开成有序 name+状态列表，含产物索引。"""
    stages = job.stages or {}
    return {
        "id": job.id, "url": job.url, "title": job.title,
        "mode": getattr(job, "mode", "transport"),
        "status": job.status, "stage": job.stage, "error": job.error,
        "duration_seconds": job.duration_seconds,
        "stages": [{"name": s, **(stages.get(s) or {"status": "pending"})}
                   for s in scheduler.stage_order(job)],
        "products": job.products or {},
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


async def _requeue(session, job: VideoJob) -> None:
    """把 job 复位回 queued + 刷心跳，让独立 worker 进程的轮询主循环取走续跑。

    等价 ``scheduler.enqueue`` 的 DB 半（置 queued + touch_heartbeat），去掉进程内 submit——
    方案 C 下 worker 是独立进程，API 进程内无调度器实例可 submit。尤其解 revision 子 job 的
    「running + 心跳 NULL」死局（mark_stages_inherited 经 update_stage 把子 job 翻成 running，
    mark_running 占不到、recover 对 NULL 心跳永不命中）：复位 queued 后 mark_running 可原子占用。
    """
    job.status = "queued"
    job.updated_at = datetime.utcnow()
    await session.commit()
    await scheduler.touch_heartbeat(session, job.id)


def _load_raw_json(job_id: int, name: str):
    """读父 job raw 产物（rewritten/storyboard），缺失返 None。"""
    p = paths.raw_dir(job_id) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


async def _enrich_inherited_stats(session, child: VideoJob, parent: VideoJob) -> None:
    """给继承阶段补下游消费的真实路径 stats（指向子 raw 目录），补 mark_stages_inherited
    只写 inherited_from 的断链：_handle_storyboard 读 analyze.facts_path、deliver 读
    download.info。用父 stats 重建——*_path 键重指到子 raw 已拷入的同名文件（不存在则丢弃
    陈旧父路径，如 transcript/resegment 中间产物未继承），info/计数等标量原样保留。

    平移自源 API 层同名 helper，改 async + AsyncSession（revision 集成测试改回从本模块导入）。
    """
    child_raw = paths.raw_dir(child.id)
    for name in _INHERITED_STAGES:
        parent_stats = ((parent.stages or {}).get(name) or {}).get("stats") or {}
        stats = {"inherited_from": parent.id}
        for k, v in parent_stats.items():
            if k == "inherited_from":
                continue
            if k.endswith("_path") and isinstance(v, str):
                cand = child_raw / Path(v).name
                if cand.exists():                # 子目录有拷贝才重指
                    stats[k] = str(cand)
            else:
                stats[k] = v                     # info / 计数 / source 等标量保留
        await scheduler.update_stage(session, child, name, status="done", stats=stats)


@router.post("/api/video/jobs", status_code=202)
async def create_video_job(req: CreateJobRequest) -> dict:
    """建视频任务（202 异步入队）：落 queued，worker 轮询取走从 download 起跑。"""
    op = current_operator()
    options = {"burn_subtitles": req.burn_subtitles, "max_resolution": req.max_resolution}
    if req.voice:
        options["voice"] = req.voice
    async with get_session() as session:
        job = await scheduler.create_job(
            session, url=req.url, options=options, user_id=op.id, mode=req.mode
        )
        return {"job_id": job.id}


@router.get("/api/video/jobs")
async def list_video_jobs(
    limit: int = 20, offset: int = 0, status: str | None = None
) -> dict:
    """列视频任务：非 admin 收窄到本人 created_by；可选 status 过滤，新→旧分页。"""
    op = current_operator()
    async with get_session() as session:
        stmt = select(VideoJob)
        if op.role != "admin":
            stmt = stmt.where(VideoJob.created_by == op.id)
        if status:
            stmt = stmt.where(VideoJob.status == status)
        stmt = (stmt.order_by(VideoJob.id.desc())
                .offset(offset).limit(min(limit, 100)))
        rows = (await session.execute(stmt)).scalars().all()
        return {"items": [_job_payload(j) for j in rows], "offset": offset}


@router.get("/api/video/jobs/{job_id}")
async def get_video_job(job_id: int) -> dict:
    """查单条：job 不存在 → 404；非本人且非 admin → 403。"""
    op = current_operator()
    async with get_session() as session:
        job = await scheduler.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"视频任务 {job_id} 不存在")
        if not _can_access(job, op):
            raise AccessDenied("无权访问该视频任务")
        return _job_payload(job)


@router.post("/api/video/jobs/{job_id}/retry", status_code=202)
async def retry_video_job(job_id: int) -> dict:
    """重试：仅 failed/completed 可重试（运行中 → 409），复位 queued 后从首个未完成阶段续跑。"""
    op = current_operator()
    async with get_session() as session:
        job = await scheduler.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"视频任务 {job_id} 不存在")
        if not _can_access(job, op):
            raise AccessDenied("无权访问该视频任务")
        if job.status not in ("failed", "completed"):
            raise HTTPException(409, "job 仍在运行")
        job.error = None
        resume_stage = scheduler.first_incomplete_stage(job)
        await _requeue(session, job)
        return {"job_id": job.id, "resume_stage": resume_stage}


@router.post("/api/video/jobs/{job_id}/revise", status_code=202)
async def revise_video_job(job_id: int, req: ReviseJobRequest) -> dict:
    """成片修订：解析意见 → 校验 → 派生 revision 子 job → 继承产物 → 从 rewrite 起链。

    仅 mode=remake 且 status=completed 可修订（否则 400/409）；解析失败/空清单 → 400 带
    LLM 原始说明不建 job。响应 202 回显解析出的 edit_plan（调用方可展示；v1 直接执行）。
    """
    op = current_operator()
    async with get_session() as session:
        job = await scheduler.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"视频任务 {job_id} 不存在")
        if not _can_access(job, op):
            raise AccessDenied("无权访问该视频任务")
        if getattr(job, "mode", None) != "remake":
            raise ValueError("仅 remake 模式的成片可修订")
        if job.status != "completed":
            raise HTTPException(409, "仅已完成的成片可修订")
        # 父产物做意见解析基底 + 校验上下文（下标/场景 id 都相对父台词与分镜）
        rewritten = _load_raw_json(job.id, "rewritten.json")
        storyboard = _load_raw_json(job.id, "storyboard.json")
        if rewritten is None or storyboard is None:
            raise HTTPException(409, "父 job 产物缺失，无法修订")
        # 依次 parse→validate（apply 的字段防御不完整，跳 validate 会 KeyError）；EditPlanError→400
        try:
            edit_plan = await remake_revision.parse_instructions(
                req.instructions, rewritten, storyboard)
            remake_revision.validate_edit_plan(edit_plan, rewritten, storyboard)
        except remake_revision.EditPlanError as exc:
            # exc.detail = LLM 原始说明 / 具体违规描述（源同义，ValueError→400 带明细）
            raise ValueError(exc.detail)
        child = await scheduler.create_revision_job(
            session, job, instructions=req.instructions, edit_plan=edit_plan)
        inherit_artifacts(job.id, child.id)
        await scheduler.mark_stages_inherited(
            session, child, _INHERITED_STAGES, parent_id=job.id)
        await _enrich_inherited_stats(session, child, job)  # 补路径 stats，修复继承阶段断链
        # mark_stages_inherited 已把 child 翻成 running 且心跳 NULL——若复位失败，child 会卡
        # running 让 recovery 捞不到、retry 双 409 死局。故复位失败即 fail_job（child 落 failed
        # 可 retry/delete），再抛 500 让调用方知晓。
        try:
            await _requeue(session, child)  # 复位 queued → worker 从 rewrite 续跑
        except Exception as exc:
            logger.exception("revision job %s 复位入队失败", child.id)
            await scheduler.fail_job(session, child, f"入队失败: {exc}")
            raise HTTPException(500, "revision job 入队失败，可对该 job 执行 retry")
        return {"job_id": child.id, "parent_job_id": job.id, "edit_plan": edit_plan}


@router.delete("/api/video/jobs/{job_id}")
async def delete_video_job(job_id: int) -> dict:
    """删任务：运行中不可删（409）；级联删产物目录 + DB 行。"""
    op = current_operator()
    async with get_session() as session:
        job = await scheduler.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"视频任务 {job_id} 不存在")
        if not _can_access(job, op):
            raise AccessDenied("无权访问该视频任务")
        if job.status == "running":
            raise HTTPException(409, "运行中不可删，先等失败或完成")
        shutil.rmtree(paths.job_dir(job.id), ignore_errors=True)
        await session.delete(job)
        await session.commit()
        return {"deleted": job_id}


@router.get("/uploads/video/{token_dir}/{sub}/{name}")
async def serve_video_product(token_dir: str, sub: str, name: str) -> FileResponse:
    """取回视频产物（白名单免鉴权：HMAC token 目录即访问控制，与源一致）。

    ``/uploads`` 前缀在鉴权中间件白名单内（uploads_rest 同款），故本路由免 apikey——不可猜的
    ``{job_id}-{hmac16}`` 目录名（SECRET_KEY 派生，攻击者无从枚举他人成片）承担访问控制。
    正则白名单 + resolve/is_relative_to 双保险挡路径穿越；非文件 404。media_type 按扩展名猜
    （成片 mp4 / 字幕 srt / meta json 等各异），未知回退 application/octet-stream。
    请求时读 settings.DATA_DIR（不在 import 期绑定），与 paths / uploads_rest 同惯例，使测试
    对 DATA_DIR 的 monkeypatch 生效。
    """
    if (not _TOKEN_DIR_RE.fullmatch(token_dir)
            or not _SUB_RE.fullmatch(sub)
            or not _FILE_RE.fullmatch(name)):
        raise HTTPException(status_code=404, detail="资源不存在")
    video_root = (Path(settings.DATA_DIR) / "uploads" / "video").resolve()
    file_path = (video_root / token_dir / sub / name).resolve()
    # 纵深防御：正则已结构性排除逃逸字符，这里再确认最终路径确在 uploads/video 根内（双保险）。
    if not file_path.is_relative_to(video_root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)
