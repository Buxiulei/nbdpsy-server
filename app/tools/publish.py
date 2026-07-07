"""publish 分组 MCP 工具:建发布任务 / 查状态 / 列任务 / 取消(RBAC 收窄到 caller 有权的号)。

register_publish(mcp) 注册 4 个工具。每个工具取 current_operator() 后按访问权收窄:
- publish_note:assert_account_access → 建 PublishJob(pending)→ 无 schedule_time 时立即投入
  调度器内部队列(get_active_scheduler().submit),否则由调度器 scan 循环到期后自取。
- get_publish_status:读某 job;caller 须对该 job 的账号有 access。
- list_publish_jobs:按 caller 的 visible_account_ids 过滤(admin 全见),可再按 account_id/status 筛。
- cancel_publish_job:仅 pending 可取消(置 canceled);越权账号抛 AccessDenied。

images/topics 序列化成 images_json/topics_json 落库;images 每项为 URL/base64(远程 agent 供图),
到发布 runner 里再由 materialize_images 落成本地文件,本工具不碰浏览器。
"""

import json
from datetime import datetime

from fastmcp import FastMCP
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
from app.models.publish_job import PublishJob
from app.publish.runtime import get_active_scheduler


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
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def register_publish(mcp: FastMCP) -> None:
    """把 publish 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def publish_note(
        account_id: int,
        title: str,
        content: str,
        images: list,
        topics: list,
        schedule_time: str | None = None,
    ) -> dict:
        """建一条发布任务并入队(需 access);返回 {job_id, status:'queued'}。

        images 每项为 http(s) URL / data URI / {b64,ext};schedule_time 传 ISO8601 字符串
        表示定时发布(交调度器 scan 循环到期自取),不传则立即入队。
        """
        operator = current_operator()
        scheduled_at = datetime.fromisoformat(schedule_time) if schedule_time else None
        async with get_session() as session:
            await assert_account_access(operator, account_id, session)
            job = PublishJob(
                account_id=account_id,
                title=title,
                content=content,
                images_json=json.dumps(images or [], ensure_ascii=False),
                topics_json=json.dumps(topics or [], ensure_ascii=False),
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
        return {"job_id": job_id, "status": "queued"}

    @mcp.tool
    async def get_publish_status(job_id: int) -> dict:
        """查发布任务状态(caller 须对该 job 的账号有 access,否则抛 AccessDenied)。"""
        operator = current_operator()
        async with get_session() as session:
            job = await session.get(PublishJob, job_id)
            if job is None:
                raise ValueError(f"发布任务 {job_id} 不存在")
            await assert_account_access(operator, job.account_id, session)
            return {
                "status": job.status,
                "note_id": job.note_id,
                "note_url": job.note_url,
                "error": job.error,
                "retries": job.retries,
            }

    @mcp.tool
    async def list_publish_jobs(
        account_id: int | None = None, status: str | None = None
    ) -> dict:
        """列发布任务:按 caller 可见账号过滤(admin 全见),可选再按 account_id/status 筛。"""
        operator = current_operator()
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
            stmt = stmt.order_by(PublishJob.id.desc())
            jobs = (await session.execute(stmt)).scalars().all()
            return {"jobs": [_job_view(j) for j in jobs]}

    @mcp.tool
    async def cancel_publish_job(job_id: int) -> dict:
        """取消发布任务(仅 pending 可取消,置 canceled);越权账号抛 AccessDenied。

        返回 {ok}:成功取消 True;非 pending(已在发布 / 已终态)为 False。
        """
        operator = current_operator()
        async with get_session() as session:
            job = await session.get(PublishJob, job_id)
            if job is None:
                raise ValueError(f"发布任务 {job_id} 不存在")
            await assert_account_access(operator, job.account_id, session)
            if job.status != "pending":
                return {"ok": False}
            job.status = "canceled"
            await session.commit()
            return {"ok": True}
