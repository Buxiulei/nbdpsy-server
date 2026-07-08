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
from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
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


def register_publish(mcp: FastMCP) -> None:
    """把 publish 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def publish_note(
        account_id: int,
        title: str,
        content: str,
        images: list,
        topics: list[str],
        schedule_time: str | None = None,
    ) -> dict:
        """发布一条小红书图文笔记(异步入队,需对该账号有 access)。

        仅支持图文,不支持视频。images 至少 1 张、最多 18 张(为空或超 18 张会立即报错,
        不会建出注定失败的任务)。images 每项为下列三种形态之一(agent 在别的机器上也能发,
        服务端会自行下载/解码):
          - http(s) URL 字符串:``"https://example.com/a.png"``
          - data URI 字符串:``"data:image/png;base64,<base64>"``
          - dict(键必须是 b64/ext):``{"b64": "<base64>", "ext": "png"}``
        topics 是话题标签列表(自动去重后**静默截断至 ≤10**,不报错)。

        长度限制均为**静默硬截断、不报错**,请自行控长:标题按显示长度截断 ≤20、正文截断
        ≤900、话题去重后截断 ≤10。

        schedule_time 传 ISO8601 表示定时发布,不传则立即入队。**务必带时区偏移**(如
        ``2026-01-01T09:00:00+08:00``);不带时区偏移的时刻按 UTC 解释,会早/晚 8 小时发布。

        **异步契约**:返回 {job_id, status:'pending'} 后,每 5-10s 调 get_publish_status(job_id)
        轮询,直到 published/failed。publishing 常态耗时 1-3 分钟;失败会自动重试(最多 3 次,
        退避约 2/10/30 分钟),单条任务最长约 40 分钟才会落 failed。同一账号的发布自动串行。
        """
        operator = current_operator()
        scheduled_at = _parse_schedule_time(schedule_time)
        async with get_session() as session:
            await assert_account_access(operator, account_id, session)
            # D1:建 job 前先校验图片张数,避免造出注定失败的 pending 任务。
            if not images:
                raise ValueError("图文笔记至少需要 1 张图片")
            if len(images) > _MAX_IMAGES:
                raise ValueError(f"最多 {_MAX_IMAGES} 张图片")
            job = PublishJob(
                account_id=account_id,
                title=title,
                content=content,
                images_json=json.dumps(images, ensure_ascii=False),
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
        return {"job_id": job_id, "status": "pending"}

    @mcp.tool
    async def get_publish_status(job_id: int) -> dict:
        """轮询发布任务状态(caller 须对该 job 的账号有 access,否则抛 AccessDenied)。

        status 枚举:
          - pending:排队中(含定时未到期、失败后等待重试)
          - publishing:发布中(常态 1-3 分钟)
          - published:成功,返回 note_url(note_id 可能为空,只保证有 note_url)
          - failed:重试耗尽(最多 3 次)后的终态,error 给最后一次失败原因
          - canceled:被 cancel_publish_job 取消
        轮询节奏:每 5-10s 调一次直到 published/failed。next_retry_at 表示失败后回 pending
        的**下次重试时刻**(未安排重试则为 null);retries 是已重试次数。
        返回体含 job_id/account_id/title/status/note_id/note_url/error/retries/
        schedule_time/next_retry_at/created_at。
        """
        operator = current_operator()
        async with get_session() as session:
            job = await session.get(PublishJob, job_id)
            if job is None:
                raise ValueError(f"发布任务 {job_id} 不存在")
            await assert_account_access(operator, job.account_id, session)
            return _job_view(job)

    @mcp.tool
    async def list_publish_jobs(
        account_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict:
        """列发布任务:按 caller 可见账号过滤(admin 全见),可选再按 account_id/status 筛。

        status 若指定,必须是合法枚举:pending|publishing|published|failed|canceled
        (传非法值会报错,而非静默返回空)。limit 限制返回条数(默认 50,按新→旧取前 N)。
        """
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

    @mcp.tool
    async def cancel_publish_job(job_id: int) -> dict:
        """取消发布任务(仅 pending 可取消,置 canceled);越权账号抛 AccessDenied。

        返回 {ok}:成功取消时 {ok: True}。非 pending(已在发布 / 已终态)时返回
        {ok: False, status: <当前状态>},让 caller 一眼看出为何取消不了。
        """
        operator = current_operator()
        async with get_session() as session:
            job = await session.get(PublishJob, job_id)
            if job is None:
                raise ValueError(f"发布任务 {job_id} 不存在")
            await assert_account_access(operator, job.account_id, session)
            if job.status != "pending":
                return {"ok": False, "status": job.status}
            job.status = "canceled"
            await session.commit()
            return {"ok": True}
