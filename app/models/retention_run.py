"""笔记淘汰运行审计模型:一次「按上限淘汰」跑完就留一行,记清楚删了谁、凭什么。

设计 docs/design/2026-08-10-managed-accounts-design.md 第 3.2 节。

**为什么必须有这张表**:淘汰是不可逆删除。没有它,「今天删了哪几篇、它们当时几个赞、
凭什么是它们而不是别的」在删除动作发生后就永远查不回来了 —— 而这恰恰是运营第一时间
会问的三个问题。所以 ``details_json`` 存的是**全量**得分明细(不只被删的那几篇):
每篇的五指标原值、归一化加权得分、是否入淘汰名单、真建了删除 job 的话 job_id 是多少。

写入时序:**先落本表再建 note_delete job**。反过来的话,审计写失败就会出现"删除任务
已经在跑、库里却查不到任何依据"的黑箱。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RetentionRun(Base):
    """一次淘汰运行的审计行(每号每次一行,不清理——量极低,一天最多几行)。"""

    __tablename__ = "retention_runs"
    # 调度器每 tick 都要问「这号今天跑过没」,按 (account_id, run_date) 查
    __table_args__ = (Index("ix_retention_runs_account_date", "account_id", "run_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 运行日 "YYYY-MM-DD"(UTC 日界,与 note_metrics_daily.snapshot_date 同口径)
    run_date: Mapped[str] = mapped_column()
    # 当时的笔记库存数。⚠️ 列名是历史称呼,**它存的是 published_notes 台账计数**,
    # 不是平台真实笔记数 —— 两者会双向漂移(人工在 App 里删过 / 手工发的还没同步进台账),
    # 漂移的影响面与护栏见设计第七节。事后复盘时别把它当平台真值。
    platform_note_count: Mapped[int] = mapped_column(default=0)
    # 当时生效的 note_cap(账号改过上限后,老审计行仍能解释当时为什么删)
    cap: Mapped[int] = mapped_column(default=0)
    # 过了宽限期**且**能 join 上指标的候选篇数(即真正参与打分的那批)
    eligible_count: Mapped[int] = mapped_column(default=0)
    # 本次真建了几条 note_delete job(dry_run 行恒为 0)
    deleted_count: Mapped[int] = mapped_column(default=0)
    # 1 = 只算不删(RETENTION_ENABLED=0 的 kill switch 态);此时 details 里仍有完整
    # 淘汰名单(selected=true),只是没有 job_id —— 正是"关着跑几天看它会选谁"的用法。
    dry_run: Mapped[bool] = mapped_column(default=False)
    # 全量得分明细 JSON 串:[{note_id,title,published_at,eligible,skip_reason,
    # metrics,score,selected,job_id}, ...]
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
