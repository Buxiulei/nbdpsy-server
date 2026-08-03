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
    # 发布时刻:由 published_notes 台账补到 note_id 时一并回填(台账行自带本 job id,
    # 无需靠标题匹配)。取台账的 published_at,即发布成功那一刻的本机时钟。
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 最近一次失败原因
    error: Mapped[str | None] = mapped_column(Text, default=None)
    retries: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 笔记三组件(合集 / 引用笔记 / 关联活动),发布时在 step6 之后、step7 之前设置。
    # 全可空 = 不设置;存平台侧 id 字符串(活动 bizId 实测是字符串,合集/笔记 id 是 hex)。
    # ⚠️ activity_id 会让平台把该活动的话题**追加**进正文(注入的话题名与活动名可能不同)。
    collection_id: Mapped[str | None] = mapped_column(default=None)
    quoted_note_id: Mapped[str | None] = mapped_column(default=None)
    activity_id: Mapped[str | None] = mapped_column(default=None)
    # 发布结果回显(JSON):服务端实际应用的话题逐个成败 + 三组件逐项结果。
    # 参数被静默丢弃时调用方当场可见,不必等笔记发出去人工读正文(2026-08-03 运营教训)。
    result_json: Mapped[str | None] = mapped_column(Text, default=None)
    # 这篇笔记推介哪位咨询师(姓名)。建 job 时据它推导 quoted_note_id
    # (见 app/services/counselor_quote.py),推不出来就留空绝不猜;发布当场随
    # generated_at / operator_id 一起带进 published_notes 台账,便于事后按咨询师归集。
    related_counselor: Mapped[str | None] = mapped_column(default=None)
    # 这篇笔记的核心目的(推介咨询师 / 概念解读 / …,推荐词表见 app/services/note_purpose.py)。
    # 发布当场随 related_counselor / generated_at 一起带进 published_notes 台账,并在那边
    # 记 purpose_source='declared' —— 调用方亲口声明的目的比事后从正文推断的可信。
    note_purpose: Mapped[str | None] = mapped_column(default=None)
    # 创建该任务的 operator id
    created_by: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
