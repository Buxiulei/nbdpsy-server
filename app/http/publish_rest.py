"""publish 分组 REST:建发布任务(202)/ 查状态 / 列任务 / 取消。

端点体与 app/tools/publish.py 的 4 个 MCP 工具逐行对齐(平移自那里),仅两处改动:
①"发布任务 … 不存在"从裸 ValueError 改为 NotFoundError(→ 404,而非 400);
②入参从工具签名改为请求体 Pydantic 模型 / query 参数。

images/topics 序列化成 images_json/topics_json 落库;images 每项为 URL/base64(远程 agent
供图),到发布 runner 里再由 materialize_images 落成本地文件,本端点不碰浏览器。
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.publish_job import PublishJob
from app.publish.runtime import get_active_scheduler

# 发布任务状态枚举(与 DB / 调度器生命周期一致):校验 list_publish_jobs 的 status 入参用。
_JOB_STATUSES = ("pending", "publishing", "published", "failed", "canceled")
# 图文笔记图片张数硬上限(小红书图文最多 18 张);下限为 1(纯图文,无图不成立)。
_MAX_IMAGES = 18


def _parse_schedule_time(raw: str | None) -> datetime | None:
    """把 ISO8601 schedule_time 解析为 **naive UTC**(与模型/调度器统一的 utcnow 基准一致)。

    tz-aware 输入(如 ``2026-01-01T09:00:00+08:00``)先 astimezone(UTC) 再去掉 tzinfo,存成
    naive UTC(此例 → 01:00);naive 输入原样返回。否则带 +08:00 的定时时刻会被 scan_once
    的 ``utcnow()`` 当 UTC 直接比较,早/晚 8 小时发布。
    """
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _job_view(job: PublishJob) -> dict:
    """把发布任务序列化为对外视图(不含图片/正文等大字段,只给调度可读的元信息)。"""
    return {
        "job_id": job.id,
        "account_id": job.account_id,
        "title": job.title,
        "status": job.status,
        "note_id": job.note_id,
        "note_url": job.note_url,
        "error": job.error,
        "retries": job.retries,
        "schedule_time": (
            job.schedule_time.isoformat() if job.schedule_time else None
        ),
        "next_retry_at": (
            job.next_retry_at.isoformat() if job.next_retry_at else None
        ),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/publish-jobs",
        "summary": "发布一条小红书图文笔记(异步入队,需对该账号有 access)",
        "admin_only": False,
        "params": {
            "account_id": "body,int",
            "title": "body,str(显示长度截断 ≤20,静默不报错)",
            "content": "body,str(截断 ≤900,静默不报错)",
            "images": "body,list(1-18 项,越界立即 400);每项三形态之一:"
                       "http(s) URL 字符串 / data URI 字符串 / {b64, ext} 对象",
            "topics": "body,list[str]|None(默认[];去重后截断 ≤10,静默不报错)",
            "schedule_time": "body,str|None(ISO8601,务必带时区偏移,如 "
                              "2026-01-01T09:00:00+08:00;不传则立即入队;不带偏移按 UTC 解释)",
        },
        "returns": "{job_id, status:'pending'}",
        "errors": "400=images 为空或超 18 张;403=无该账号 access",
        "notes": "异步契约:拿到 job_id 后每 5-10s 调 GET /api/publish-jobs/{job_id} 轮询,直到 "
                 "published/failed;publishing 常态耗时 1-3 分钟;失败自动重试(最多 3 次,退避约 "
                 "2/10/30 分钟),单条任务最长约 40 分钟才会落 failed。同一账号的发布自动串行。",
    },
    {
        "method": "GET", "path": "/api/publish-jobs/{job_id}",
        "summary": "轮询发布任务状态(caller 须对该 job 的账号有 access)",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{job_id, account_id, title, status, note_id, note_url, error, "
                    "retries, schedule_time, next_retry_at, created_at}",
        "errors": "403=无该账号 access;404=job 不存在",
        "notes": "status 枚举五态:pending(排队中,含定时未到期/失败等待重试)、publishing"
                 "(发布中,常态 1-3 分钟)、published(成功,保证有 note_url,note_id 可能为空)、"
                 "failed(重试耗尽后的终态,error 给最后一次失败原因)、canceled(被 cancel 取消)。"
                 "next_retry_at 是失败后回 pending 的下次重试时刻(未安排重试则为 null);"
                 "retries 是已重试次数。轮询节奏建议每 5-10s 一次直到 published/failed。",
    },
    {
        "method": "GET", "path": "/api/publish-jobs",
        "summary": "列发布任务(按 caller 可见账号过滤,admin 全见)",
        "admin_only": False,
        "params": {
            "account_id": "query,int|None(显式鉴权;越权 403)",
            "status": "query,str|None(pending|publishing|published|failed|canceled;"
                      "非法值 400,而非静默返回空)",
            "limit": "query,int(默认 50,按新→旧取前 N)",
        },
        "returns": "{jobs: [同 GET /api/publish-jobs/{job_id} 的单条视图, ...]}",
        "errors": "400=status 非法;403=account_id 越权",
        "notes": "",
    },
    {
        "method": "POST", "path": "/api/publish-jobs/{job_id}/cancel",
        "summary": "取消发布任务(仅 pending 可取消,置 canceled)",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{ok:true} 成功取消;{ok:false, status:<当前状态>} 非 pending 取消不了",
        "errors": "403=无该账号 access;404=job 不存在",
        "notes": "",
    },
]


class PublishNoteRequest(BaseModel):
    account_id: int
    title: str
    content: str
    images: list
    topics: list[str] = []
    schedule_time: str | None = None


@router.post("/api/publish-jobs", status_code=202)
async def publish_note_endpoint(payload: PublishNoteRequest) -> dict:
    """发布图文笔记(异步入队):函数体与 app/tools/publish.py::publish_note 逐行对齐。"""
    operator = current_operator()
    scheduled_at = _parse_schedule_time(payload.schedule_time)
    async with get_session() as session:
        await assert_account_access(operator, payload.account_id, session)
        # D1:建 job 前先校验图片张数,避免造出注定失败的 pending 任务。
        if not payload.images:
            raise ValueError("图文笔记至少需要 1 张图片")
        if len(payload.images) > _MAX_IMAGES:
            raise ValueError(f"最多 {_MAX_IMAGES} 张图片")
        job = PublishJob(
            account_id=payload.account_id,
            title=payload.title,
            content=payload.content,
            images_json=json.dumps(payload.images, ensure_ascii=False),
            topics_json=json.dumps(payload.topics or [], ensure_ascii=False),
            schedule_time=scheduled_at,
            status="pending",
            created_by=operator.id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    # 立即发布:投入调度器队列免等下个 scan 周期;定时发布由 scan 循环到期自取。
    if scheduled_at is None:
        get_active_scheduler().submit(job_id)
    return {"job_id": job_id, "status": "pending"}


@router.get("/api/publish-jobs/{job_id}")
async def get_publish_status_endpoint(job_id: int) -> dict:
    """job 不存在 → NotFoundError(404);越权 → 403;返回 _job_view。"""
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        return _job_view(job)


@router.get("/api/publish-jobs")
async def list_publish_jobs_endpoint(
    account_id: int | None = None, status: str | None = None, limit: int = 50
) -> dict:
    """与 list_publish_jobs 工具逐行对齐;status 非法 → 裸 ValueError(400)。"""
    operator = current_operator()
    # D2:status 传了就必须合法,否则明确报错(避免"筛错拼写→静默空列表"的误导)。
    if status is not None and status not in _JOB_STATUSES:
        raise ValueError(
            f"status 非法:{status};合法值为 {'/'.join(_JOB_STATUSES)}"
        )
    async with get_session() as session:
        visible = await visible_account_ids(operator, session)
        stmt = select(PublishJob)
        # 非 admin:收窄到可见账号(空列表 → 无结果)
        if visible is not None:
            stmt = stmt.where(PublishJob.account_id.in_(visible))
        # 指定 account_id:显式鉴权(越权抛),再按其筛
        if account_id is not None:
            await assert_account_access(operator, account_id, session)
            stmt = stmt.where(PublishJob.account_id == account_id)
        if status is not None:
            stmt = stmt.where(PublishJob.status == status)
        stmt = stmt.order_by(PublishJob.id.desc()).limit(limit)
        jobs = (await session.execute(stmt)).scalars().all()
        return {"jobs": [_job_view(j) for j in jobs]}


@router.post("/api/publish-jobs/{job_id}/cancel")
async def cancel_publish_job_endpoint(job_id: int) -> dict:
    """仅 pending 可取消;job 不存在 → 404。"""
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        if job.status != "pending":
            return {"ok": False, "status": job.status}
        job.status = "canceled"
        await session.commit()
        return {"ok": True}
