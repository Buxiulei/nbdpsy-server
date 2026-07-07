"""小红书账号模型:账号资料 + 登录态 / cookie 巡检态 + 加密 cookie。"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class XhsAccount(Base):
    """一个受托管的小红书账号。

    login_cookies 存 Fernet 加密串(见 app.core.security),不落明文;
    status / cookie_status 由巡检任务更新,初始均为 'unknown'。
    """

    __tablename__ = "xhs_accounts"

    # user_id 部分唯一索引:非 NULL 时全库唯一(同一小红书号只允许一行);
    # user_id 为 NULL(仅 name 建的号)不受约束,可多行并存。
    __table_args__ = (
        Index(
            "uq_xhs_account_user_id",
            "user_id",
            unique=True,
            sqlite_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 内部展示名(运营者可读),必填
    name: Mapped[str] = mapped_column()
    # 小红书昵称 / 平台侧标识,登录后回填
    nickname: Mapped[str | None] = mapped_column(default=None)
    user_id: Mapped[str | None] = mapped_column(default=None)
    red_id: Mapped[str | None] = mapped_column(default=None)
    avatar: Mapped[str | None] = mapped_column(default=None)
    # 账号在线/登录态:'unknown' | 具体状态由巡检写入
    status: Mapped[str] = mapped_column(default="unknown")
    # cookie 有效性:'unknown' | 'valid' | 'invalid' 等,由 cookie 巡检写入
    cookie_status: Mapped[str] = mapped_column(default="unknown")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # Fernet 加密后的 cookie 串,可能较长,用 Text
    login_cookies: Mapped[str | None] = mapped_column(Text, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
