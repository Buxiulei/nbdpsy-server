"""受众事件采集的增量游标:每个自家号、每条通知 channel 采到哪儿了。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 2.2 节。

增量策略靠这一行:通知流是**新事件在最前**的倒序流,所以从第一页往下翻,一旦翻到
``event_time <= last_event_time`` 就说明后面全是上次采过的,当场停 —— 号1 实采到底要
47 页滚 40 轮,每小时全量重翻一次纯粹是拿真号会话去换早就有的数据。

``last_event_time`` 为空 = 这个 channel 还没采过,首轮走全量(翻到 ``has_more=false``)。

``updated_at`` 是**调度器的到期判据**(早于 ``now - AUDIENCE_SYNC_INTERVAL`` 才轮到它),
所以每轮采集无论有没有新事件都要刷它 —— 不刷的话"这个号没人互动"会被读成"这个号还没采",
调度器于是每轮都挑中它,把会话额度全烧在最冷清的号上。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AudienceSyncState(Base):
    """(自家号, channel) 一行:上次采到的最新事件时刻 + 上次全量回采时刻。"""

    __tablename__ = "audience_sync_state"

    # 复合主键:一个号的 likes 与 connections 各自独立推进(两条流深度差一个数量级,
    # 实采 922 条 vs 98 条,共用一个游标会让浅的那条被深的拖着反复重翻)。
    account_id: Mapped[int] = mapped_column(
        ForeignKey("xhs_accounts.id"), primary_key=True
    )
    # likes / connections(与 audience_collect.CHANNELS 同源)
    channel: Mapped[str] = mapped_column(primary_key=True)
    # 上次采到的最新 event_time(epoch 秒);NULL = 还没采过,下一轮走全量
    last_event_time: Mapped[int | None] = mapped_column(nullable=True)
    # 上次**全量**回采(翻到底)的时刻;增量轮不动它。用来回答"这个号的历史补齐过没有"
    last_full_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # server_default 与 Python default 都给(同 audience_events.created_at 的理由)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=func.current_timestamp()
    )
