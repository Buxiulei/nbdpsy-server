"""内容资产库模型:发布成功的笔记内容归档,供跨账号复用/借鉴/复盘。

设计 docs/superpowers/specs/2026-07-25-content-archive-design.md。每篇发布成功自动归档
一条(source_publish_job_id 唯一保证幂等),媒体独立存一份副本(脱离 uploads 7 天清理);
取详情即刷新 last_used_at,距最后使用超 ARCHIVE_TTL_DAYS(90)天由 ArchiveReaper 删除。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ContentArchive(Base):
    """一条已发布内容的归档快照(全局共享,不绑消费账号)。"""

    __tablename__ = "content_archive"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column()
    content: Mapped[str] = mapped_column()
    topics_json: Mapped[str] = mapped_column(default="[]")  # 标签(#tag)列表 JSON
    # 归档目录内媒体清单 JSON:[{"type":"image"|"video","name":"01.jpg"}]
    media_json: Mapped[str] = mapped_column(default="[]")
    kind: Mapped[str] = mapped_column(default="image_note")  # image_note / video_note

    source_account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    source_note_url: Mapped[str | None] = mapped_column(nullable=True)
    # 源发布运营(publish_jobs.created_by,与其一致无 FK)
    source_operator_id: Mapped[int | None] = mapped_column(nullable=True)
    # 源发布任务 id:唯一 → 同一发布只归档一条(自动归档幂等)
    source_publish_job_id: Mapped[int | None] = mapped_column(unique=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 取详情即刷新;距今超 TTL 由 ArchiveReaper 删除
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    use_count: Mapped[int] = mapped_column(default=0)
