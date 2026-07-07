"""RBAC 鉴权助手:把工具/端点的操作收窄到 caller 有权的小红书账号。

三个入口(接口稳定,后续工具大量依赖):
- require_admin:非 admin 抛 PermissionError(仅管理员可建运营者/授权等操作)。
- assert_account_access:admin 放行;否则须存在 (operator_id, account_id) 的
  access 行,无则抛 PermissionError。
- visible_account_ids:admin 返 None(表示全部账号);否则返其 access 账号 id 列表。

约束:均为纯查询,使用调用方传入的 session(不自开会话、不管理事务边界)。
入参 op 通常是 current_operator() 交出的 detached Operator——本模块只读其已加载的
简单列(id / role),不触发懒加载,detached 亦安全。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operator import Operator, OperatorAccountAccess


def require_admin(op: Operator) -> None:
    """要求 op 为管理员;非 admin 抛 PermissionError。"""
    if op.role != "admin":
        raise PermissionError("需要管理员权限")


async def assert_account_access(
    op: Operator, account_id: int, session: AsyncSession
) -> None:
    """断言 op 对 account_id 有操作权;admin 直接放行,否则须存在对应 access 行。"""
    if op.role == "admin":
        return
    row = (
        await session.execute(
            select(OperatorAccountAccess.id).where(
                OperatorAccountAccess.operator_id == op.id,
                OperatorAccountAccess.xhs_account_id == account_id,
            )
        )
    ).first()
    if row is None:
        raise PermissionError(f"无权操作账号 {account_id}")


async def visible_account_ids(
    op: Operator, session: AsyncSession
) -> list[int] | None:
    """返回 op 可见的小红书账号 id 列表;admin 返 None 表示全部。"""
    if op.role == "admin":
        return None
    result = await session.execute(
        select(OperatorAccountAccess.xhs_account_id).where(
            OperatorAccountAccess.operator_id == op.id
        )
    )
    return list(result.scalars().all())
