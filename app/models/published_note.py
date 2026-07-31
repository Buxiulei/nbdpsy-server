"""发布笔记永久台账模型:我们发布过的每一篇笔记都在库里留一行,永不清理。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第 4.1 节。

**为什么不复用 content_archive**(设计已定,两点硬冲突):
1. content_archive 有 90 天滑动 TTL(ArchiveReaper 删行 + 删媒体目录),永久台账不能
   建在会被清理的表上;
2. content_archive 只覆盖本系统发出去的笔记,而创作中心列表里还有非本系统发布的存量
   笔记(实测 NBDpsy-夕夕 26 篇),那些永远不会有归档行。

台账是小行、永久;归档是大内容 + 媒体文件、可 TTL。台账用可空外键指向归档与发布任务,
**不重复存**正文与媒体;关联不上就留 NULL(标题重复/为空的笔记本就无法区分,绝不猜)。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PublishedNote(Base):
    """创作中心笔记列表里的一篇已发布笔记(每账号每 note_id 一行)。"""

    __tablename__ = "published_notes"
    # 幂等键:同一账号同一 note_id 只有一行,重复同步走 upsert 刷新而非加行
    __table_args__ = (
        UniqueConstraint("account_id", "note_id", name="uq_published_notes_account_note"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 真实 note_id(24 位 hex),来自创作中心笔记列表接口的 notes[i].id
    note_id: Mapped[str] = mapped_column()

    # 拼完整可访问链接的两件套 + 拼好的链接。⚠️ xsec_token 时效未实测,使用方必须容忍
    # 失效,**不得假设存下的链接永远可打开**(设计第六节风险 3)。
    xsec_token: Mapped[str | None] = mapped_column(nullable=True)
    xsec_source: Mapped[str | None] = mapped_column(nullable=True)
    note_url: Mapped[str | None] = mapped_column(nullable=True)

    # display_title 原样落,可为空串(空标题笔记照样有 id)
    title: Mapped[str] = mapped_column(default="")
    # 接口 type 原样落,**不做映射**——真实取值集合尚未穷举(实测只见过 normal)
    note_type: Mapped[str | None] = mapped_column(nullable=True)
    # 权威发布时间:由接口 visible_time(unix 秒)转;拿不到留 NULL
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 回连:非本系统发布的笔记、或标题无法唯一区分的,一律留 NULL
    source_publish_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("publish_jobs.id"), nullable=True
    )
    content_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_archive.id"), nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 互动快照:对账用,**非权威指标源**(权威指标在 note_metrics 两表)
    likes: Mapped[int] = mapped_column(default=0)
    collects: Mapped[int] = mapped_column(default=0)
    comments: Mapped[int] = mapped_column(default=0)
    shares: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
