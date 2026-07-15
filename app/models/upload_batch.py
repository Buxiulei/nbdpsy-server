"""图片上传批次模型:一次上传落盘的一组图片对应一行,承载归属与 TTL。

batch_id 全局唯一(token_urlsafe 生成),既是落盘子目录名也是 URL 路径段;
operator_id 记归属(谁传的),file_count 记张数;created_at/expires_at 支撑懒清理
(expires_at < now 的批次连目录带行一起删)。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UploadBatch(Base):
    """一次图片上传的批次记录;batch_id 唯一,归属某 operator,到期后懒清理。"""

    __tablename__ = "upload_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 落盘目录名 + URL 路径段,全局唯一(secrets.token_urlsafe 生成)
    batch_id: Mapped[str] = mapped_column(unique=True)
    # 归属:创建该批次的 operator id
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    # 该批次落盘的图片张数
    file_count: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 过期时刻:懒清理据此删目录 + 行
    expires_at: Mapped[datetime] = mapped_column(DateTime)
