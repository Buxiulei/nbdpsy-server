"""发布笔记永久台账模型:我们发布过的每一篇笔记都在库里留一行,永不清理。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第 4.1 / 4.1.1 节。

**为什么不复用 content_archive**(设计已定,两点硬冲突):
1. content_archive 有 90 天滑动 TTL(ArchiveReaper 删行 + 删媒体目录),永久台账不能
   建在会被清理的表上;
2. content_archive 只覆盖本系统发出去的笔记,而创作中心列表里还有非本系统发布的存量
   笔记(实测 NBDpsy-夕夕 26 篇),那些永远不会有归档行。

**写入时序(核心)**:发布成功那一刻(T0)就把内容侧字段全写死——纯 DB 写入,不依赖
任何浏览器操作;平台侧字段(note_id / xsec / platform_published_at / note_type)当场
拿不到,由事后的列表同步补(T1 发布后一次、T2 定时全量)。哪怕同步永远失败,
"我们发过这篇、什么内容、谁发的、什么时候发的"也已经完整落库。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PublishedNote(Base):
    """一篇已发布笔记的永久台账行。"""

    __tablename__ = "published_notes"
    __table_args__ = (
        # 平台侧幂等键:同账号同 note_id 只有一行。note_id 在 T0 时为 NULL,SQLite 下
        # 多个 NULL 互不冲突,故一个账号可以同时有多行待补 id 的 pending_id。
        UniqueConstraint("account_id", "note_id", name="uq_published_notes_account_note"),
        # 本系统侧幂等键:一条发布任务只对应一行台账(T0 重复调用不产生第二行),
        # 同时从库层面杜绝"一个 job 被两行台账认领"。orphan 行该列为 NULL,不受约束。
        UniqueConstraint("source_publish_job_id", name="uq_published_notes_pubjob"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))

    # ── 平台侧字段:T0 拿不到,由 T1/T2 列表同步后补 ──
    # 真实 note_id(24 位 hex)。⚠️ 未补上前是 NULL,不是空串——空串会撞联合唯一键
    note_id: Mapped[str | None] = mapped_column(nullable=True)
    # 拼完整可访问链接的两件套 + 拼好的链接。⚠️ xsec_token 时效未实测,使用方必须容忍
    # 失效,**不得假设存下的链接永远可打开**(设计第六节风险 3)。
    xsec_token: Mapped[str | None] = mapped_column(nullable=True)
    xsec_source: Mapped[str | None] = mapped_column(nullable=True)
    note_url: Mapped[str | None] = mapped_column(nullable=True)
    # 接口 type 原样落,**不做映射**——真实取值集合尚未穷举(实测只见过 normal)
    note_type: Mapped[str | None] = mapped_column(nullable=True)
    # 平台权威发布时间(接口 visible_time unix 秒转);与 published_at 有分钟级差异属正常
    platform_published_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # ── 内容侧字段:T0 发布当场写死 ──
    # 发布时先写我们自己的标题;T2 同步时用平台 display_title 纠正(运营可能改过标题)
    title: Mapped[str] = mapped_column(default="")
    # 发布成功那一刻的**本机时钟**,永不为空——这是我们唯一 100% 掌握的时刻
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 生成时间:取 publish_jobs.created_at(代理值 = 发布任务提交时刻,见设计 4.5)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成用户:取 publish_jobs.created_by(语义是"谁的 apikey 提交了发布")
    operator_id: Mapped[int | None] = mapped_column(nullable=True)

    source_publish_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("publish_jobs.id"), nullable=True
    )
    content_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_archive.id"), nullable=True
    )

    # pending_id(已发布待补 id)/ linked(已补上)/ orphan(列表里有但本系统没发过)
    sync_status: Mapped[str] = mapped_column(default="pending_id")

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 互动快照:对账用,**非权威指标源**(权威指标在 note_metrics 两表)
    likes: Mapped[int] = mapped_column(default=0)
    collects: Mapped[int] = mapped_column(default=0)
    comments: Mapped[int] = mapped_column(default=0)
    shares: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
