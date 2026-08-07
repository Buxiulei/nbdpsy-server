"""浏览器任务统一台账模型:四类内存台账(cookie_check/note_export/note_delete/op_images)收敛落库。

2026-07-24 API/Worker 进程拆分设计(docs/design/2026-07-24-api-worker-split-design.md 第三节):
任务台账全落库,任何进程重启不丢任务、不丢终态,轮询 id 永不因重启 404。

- id 即对外轮询 id:cookie_check/note_export/note_delete 用 uuid hex;
  op_images 用 "opimg_{session_id}_{ext_job_id}" 复合串(REST 契约的 (session_id, job_id)
  二元组编进主键,单表统一回查);
- 认领纪律:UPDATE ... WHERE status='queued' 且 rowcount=1 才算领到(乐观锁,
  单 worker 也保持该纪律,为多消费方留正确性);
- 僵死恢复:running 且心跳超时的行按 kind 语义处置——幂等类重置 queued 自动重跑,
  非幂等类置 error + 结果未知指引(见 services/browser_jobs_repo.recover_stale)。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class BrowserJob(Base):
    """一条浏览器任务台账行;主键即对外轮询 id。"""

    __tablename__ = "browser_jobs"

    # 轮询端点的 queue 段每次都按 (account_id, status) 取该号的待派队列 / 在跑行 / 窗口内
    # 会话行,轮询频率是 3-5s/任务;没有这个索引它们全是全表扫,台账越长越慢。
    __table_args__ = (Index("ix_browser_jobs_account_status", "account_id", "status"),)

    # 对外轮询 id(uuid hex;op_images 为 "opimg_{session_id}_{ext_job_id}" 复合串)
    id: Mapped[str] = mapped_column(primary_key=True)
    # 任务类型:cookie_check | note_export | note_delete | op_images
    kind: Mapped[str] = mapped_column()
    # 账号绑定类任务必填;op_images(API 调用型,无账号)为 NULL
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("xhs_accounts.id"), nullable=True
    )
    # 提交者 operator id(配额与 RBAC 依据;0 表示非请求上下文的进程内直调)
    operator_id: Mapped[int] = mapped_column()
    # 任务入参 JSON 串(title/count/prompts/...;不存明文 cookie,执行时按号重新解密)
    payload: Mapped[str] = mapped_column(Text)
    # 状态:queued | running | done | error
    status: Mapped[str] = mapped_column(default="queued")
    # 终态结果 JSON 串(note_count/urls/errors/deleted/...;error 时含 "error" 键)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 认领的 worker 实例标识(乐观认领写入;queued 时为 NULL)
    claimed_by: Mapped[str | None] = mapped_column(nullable=True)
    # 在跑心跳(执行方周期 touch;僵死判定见 recover_stale)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
