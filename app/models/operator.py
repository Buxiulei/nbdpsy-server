"""运营者与账号授权关系模型。

- Operator:运营者账号,apikey_hash 唯一,role 区分 admin/operator。
- OperatorAccountAccess:运营者↔小红书账号的授权关系,同一对唯一。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Operator(Base):
    """运营者账号:登录凭据仅存 apikey 的 SHA256 hash,不存明文。"""

    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    # apikey 的 SHA256 十六进制摘要,全局唯一(用于鉴权反查)
    apikey_hash: Mapped[str] = mapped_column(unique=True)
    # 角色:'admin'(可管所有账号/建运营者) 或 'operator'(仅授权账号)
    role: Mapped[str] = mapped_column()
    # 是否启用:禁用后鉴权即拒绝
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 创建者 operator id;根管理员由系统初始化时为 None
    created_by: Mapped[int | None] = mapped_column(default=None)


class OperatorAccountAccess(Base):
    """运营者对某小红书账号的授权关系;(operator_id, xhs_account_id) 唯一。"""

    __tablename__ = "operator_account_access"
    __table_args__ = (
        UniqueConstraint("operator_id", "xhs_account_id", name="uq_operator_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    xhs_account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 授予者 operator id;系统预置授权时为 None
    granted_by: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
