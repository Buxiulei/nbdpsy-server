"""受众公开行为库的原子事件流:谁(站内公开身份)在什么时候跟哪个自家号互动了一次。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 2.1 节。
数据来自主站通知页的 ``/you/likes`` 与 ``/you/connections``(UI 驱动被动监听采回来的,
不逆向签名直调),平台已经公开发给前端的字段原样落一行。

## 合规硬边界(不可协商,改这张表前先读)

本表存的是**受众公开行为**,不是个人档案:

- 只存平台在通知流里已经公开给我们的字段(userid / 昵称 / 头像 / 关系状态 /
  被互动的公开笔记),不采互动者主页,不做去匿名化;
- **绝不建立 ``actor_userid`` → 来访者真实身份(姓名 / 手机 / 预约记录 / 咨询关系)的
  任何关联**。这张表不设、不留、也不预留这类字段或外键 —— 想加之前先回来读这一段;
- 昵称 / 头像是**采集时快照**,平台上改了不追溯,本库不做历史画像比对;
- 自家矩阵号互刷产生的互动照常入库(它也是数据),但分析查询默认把它们剔除
  (自家 user_id 从 ``xhs_accounts`` 现查,不硬编码)。

## 为什么是"一次互动一行"而不是"一个人一行"

同一个 userid 会反复互动(赞了 A 又收藏了 B,先赞后取消再赞),每一次都是不同的平台事件
id。纵向轨迹要的正是这个序列本身,聚合成"一个人一行"就把时间维度压没了。人的画像是这张
表按 ``actor_userid`` 的聚合查询,**不另存一份会漂移的 actor 快照表**。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AudienceEvent(Base):
    """一条平台通知事件(某人对某个自家号的一次公开互动)。"""

    __tablename__ = "audience_events"
    __table_args__ = (
        # 平台事件 id 是去重键,但**带上 account_id 才保险**:多个自家号各自采各自的通知流,
        # 平台没承诺过事件 id 跨账号全局唯一。少了 account_id,两个号收到同 id 的事件会互相
        # 顶掉一条,而那是两条真实互动。
        UniqueConstraint(
            "account_id", "platform_event_id",
            name="uq_audience_events_account_event",
        ),
        # 纵向轨迹按 userid 聚合(单人时间线 / 跨号分布 / 打分全走它)
        Index("ix_audience_events_actor", "actor_userid"),
        # 增量采集游标与时间范围查询:每号按时间倒序取最新那条
        Index("ix_audience_events_account_time", "account_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 收到这条互动的自家号
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 平台事件 id(``message.id``)。重采同一段时间会原样再下发一遍,靠它幂等。
    platform_event_id: Mapped[str] = mapped_column()
    # 互动者的平台稳定唯一键(24 位 hex)。**这是本库唯一的人身标识,到此为止**,
    # 见模块 docstring 合规段。
    actor_userid: Mapped[str] = mapped_column()
    # 采集时的昵称快照(会变,不追溯)
    actor_nickname: Mapped[str] = mapped_column()
    # 采集时的头像 URL 快照;平台偶尔不给,故 nullable
    actor_image: Mapped[str | None] = mapped_column(nullable=True)
    # like_note / fav_note / like_comment / like_share / like_avatar / follow
    # (取值集合与 audience_events.EVENT_TYPES 同源)
    event_type: Mapped[str] = mapped_column()
    # 被互动的笔记 id。**不做外键**(同 note_interactions 的理由):被互动的笔记不一定在
    # published_notes 里(已删的、台账没同步的、甚至是别人的笔记 —— 赞评论那类事件记的是
    # 评论所在的那篇),台账缺行不该挡住记账。
    # follow 与 like_avatar 没有笔记 → NULL,这是语义不是缺数据。
    target_note_id: Mapped[str | None] = mapped_column(nullable=True)
    target_note_title: Mapped[str | None] = mapped_column(nullable=True)
    # 采集时的关系快照:both 互关 / fans 他关注我 / follows 我关注他 / none 陌生人。
    # 实采 922 条里 fstatus 全有值,但 indicator(中文标签)只有 462 条有 —— 所以口径按
    # fstatus 走,不认那个标签。仍设 nullable:平台哪天不给,少一格不该丢整条事件。
    fstatus: Mapped[str | None] = mapped_column(nullable=True)
    # 平台给的精确 epoch 秒(不是"3 天前"那种相对文案),增量游标与时间线都靠它
    event_time: Mapped[int] = mapped_column()
    # 原始 message 留档:解析口径以后改了,还能拿它把历史行重算一遍而不用重采真号
    raw_json: Mapped[str] = mapped_column(Text)
    # server_default 与 Python default 都给:本仓有若干路径用裸 sqlite3 显式列清单
    # INSERT(它们不知道这一列),只写 ORM default 会让那些语句当场 IntegrityError,
    # 而且**只在跑到那条路径时**才炸(与 xhs_accounts.managed 同一条教训)。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=func.current_timestamp()
    )
