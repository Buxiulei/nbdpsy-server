"""发布任务模型:一条待发布/发布中/已发布/失败/取消的小红书笔记任务。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PublishJob(Base):
    """一条发布任务。

    images_json / topics_json 存 JSON 序列化字符串(图片路径列表 / 话题列表);
    status 生命周期:pending → publishing → published / failed / canceled。
    重试相关:retries 累计次数,next_retry_at 为下次重试时刻。
    """

    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    title: Mapped[str] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    # 图片路径列表的 JSON 串
    images_json: Mapped[str] = mapped_column(Text)
    # 话题(#tag)列表的 JSON 串
    topics_json: Mapped[str] = mapped_column(Text)
    # 定时发布时刻;None 表示立即入队
    schedule_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 状态:pending | publishing | published | failed | canceled
    status: Mapped[str] = mapped_column(default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 发布成功后回填的笔记 id / url
    note_id: Mapped[str | None] = mapped_column(default=None)
    note_url: Mapped[str | None] = mapped_column(default=None)
    # 最近一次失败原因
    error: Mapped[str | None] = mapped_column(Text, default=None)
    retries: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 创建该任务的 operator id
    created_by: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
