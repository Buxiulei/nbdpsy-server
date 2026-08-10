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

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, text
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

    # ── 可见性:平台原值(T2 同步纠正)+ 我们自己的切换留痕 ──
    # 存平台原值,**不自造 public/private 枚举**:只实测了 0=公开 / 1=仅自己可见两档,
    # 另外三档(仅互关好友/部分人可见/部分人不可见)语义未验证,自造映射遇第三态会丢信息。
    # ⚠️ NULL 表示**未知**,不等于公开 —— 今天就是因为台账缺这个字段,把一篇用户刻意
    # 隐藏的笔记误判成"低价值公开笔记"。
    permission_code: Mapped[int | None] = mapped_column(nullable=True)
    permission_msg: Mapped[str | None] = mapped_column(nullable=True)
    # **我们主动**切换成功的时刻与发起人;T2 同步不动这两列(那是平台侧事实之外的操作留痕)
    visibility_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    visibility_changed_by: Mapped[int | None] = mapped_column(nullable=True)

    # ── 内容侧字段:T0 发布当场写死 ──
    # 发布时先写我们自己的标题;T2 同步时用平台 display_title 纠正(运营可能改过标题)
    title: Mapped[str] = mapped_column(default="")
    # 发布成功那一刻的**本机时钟**,永不为空——这是我们唯一 100% 掌握的时刻
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 生成时间:取 publish_jobs.created_at(代理值 = 发布任务提交时刻,见设计 4.5)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成用户:取 publish_jobs.created_by(语义是"谁的 apikey 提交了发布")
    operator_id: Mapped[int | None] = mapped_column(nullable=True)
    # 这篇笔记推介哪位咨询师(姓名):取 publish_jobs.related_counselor,T0 当场带过来。
    # 非本系统发布的 orphan 行与存量行为 NULL —— NULL 是**未知**,不代表"没推介谁"。
    related_counselor: Mapped[str | None] = mapped_column(nullable=True)

    # ── 核心目的:给**调用这篇笔记的 agent** 读的意图标注 ──
    # 存字符串**不做枚举约束**:推荐词表(见 app/services/note_purpose.py)会扩,
    # 库层写死枚举会让新词入不了库。
    note_purpose: Mapped[str | None] = mapped_column(nullable=True)
    # 这个目的怎么来的:declared=发布时调用方声明(权威,可直接信)/
    # inferred=从正文推断(机器猜的,要留余地)/ NULL=未知。
    # **两者可信度不同**,agent 必须能区分,所以不能只存 note_purpose 一列。
    purpose_source: Mapped[str | None] = mapped_column(nullable=True)

    # ── 笔记正文:手工发布的 orphan 行本地一个字都没有,靠只读进编辑页抓回来 ──
    # 本系统发的(linked)正文在 content_archive 副本里,这一列可留空。
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 正文抓取时刻。**非空 = 已经开过一次编辑页**:哪怕抓回来是空串(纯图笔记),也不必
    # 再为这篇起一次浏览器会话——重开会话的代价远高于存一个空串。
    content_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ── 媒体清单:只存**归一化后的永久 URL**,不落文件(2026-08-05 实测定案) ──
    # 平台页面上的图 URL 带签发时间戳与签名,18 天就过期(实证 403);剥成
    # sns-img-qc/{路径段}/{file_id} 则永久有效**且是原图**。故台账只记清单
    # (几百字节/篇),要图时按需下载 —— 比落盘 1500 张省几十 GB,画质还更高。
    # 形如 [{"ordinal":1,"kind":"image","file_id":"...","segment":"spectrum","url":"..."}]
    media_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 媒体抓取时刻。非空 = 已为这篇开过一次详情页,别再为它起第二次会话(同 content_fetched_at)
    media_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_publish_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("publish_jobs.id"), nullable=True
    )
    content_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_archive.id"), nullable=True
    )

    # pending_id(已发布待补 id)/ linked(已补上)/ orphan(列表里有但本系统没发过)
    sync_status: Mapped[str] = mapped_column(default="pending_id")

    # 笔记数量上限淘汰把这篇真删掉的时刻(NULL = 还在)。**台账行不物理删** —— 台账是永久的,
    # "我们发过这篇"的事实要留着,这一列只是淘汰链的收敛标记:库存计数与淘汰候选都按它排行。
    # 没有它,已删掉的笔记仍在库存里计数,第二天照样被选中、照样再建一条删除任务(幽灵 job),
    # 而平台上早就没有这张卡片了 —— 淘汰永远不收敛。写入方见
    # app/services/retention_scheduler.py 的 reconcile_deletions。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 保护位:置 1 的笔记**永不进淘汰候选**,但**仍计入库存**。淘汰按五指标加权删最低的
    # 几篇,前提是"低互动 = 低价值" —— 这条假设对**功能位笔记**(全矩阵置顶的品牌片、
    # 二维码导流笔记)不成立:它们浏览量天然垫底却是门面与转化入口,被删且不可逆。
    # 仍计入库存是刻意的:平台上确实有这一篇,不算它等于把 note_cap 悄悄放大。
    # ⚠️ server_default 不可省(同 xhs_accounts.managed):note_ledger 建行走的是显式列名的
    # 裸 sqlite3 INSERT,新 NOT NULL 列在 DDL 里没有 DEFAULT 会让那条 INSERT 当场炸。
    protected: Mapped[bool] = mapped_column(default=False, server_default=text("0"))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 互动快照:对账用,**非权威指标源**(权威指标在 note_metrics 两表)
    likes: Mapped[int] = mapped_column(default=0)
    collects: Mapped[int] = mapped_column(default=0)
    comments: Mapped[int] = mapped_column(default=0)
    shares: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
