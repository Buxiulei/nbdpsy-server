"""账号服务层:列/查/改/删受托管小红书账号,全部经 RBAC 收窄到 caller 有权的账号。

约定(与 operator_service / cookie_service 一致):
- 纯业务逻辑,使用调用方传入的 AsyncSession——只 add/query/commit,不自开引擎/事务边界。
- list_accounts 用 visible_account_ids 过滤:admin 返全部,operator 仅其被 grant 的号。
- get/update/delete 均先 assert_account_access:admin 放行,operator 无 access 抛 AccessDenied
  (账号不存在时 operator 亦得 AccessDenied,不泄露存在性)。
- update 只允许改安全字段(白名单,当前仅 name);login_cookies / user_id 等敏感字段禁止
  经此改动(须走 cookie_service 的 import 才落 cookie/身份),越界字段抛 ValueError。
- delete 先清该账号的全部 OperatorAccountAccess 行再删本体(应用层级联,不依赖 DB 外键)。
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import assert_account_access, visible_account_ids
from app.models.operator import Operator, OperatorAccountAccess
from app.models.xhs_account import XhsAccount

# update_account 允许改写的安全字段白名单;其余(login_cookies/user_id/status 等)禁止经此改动。
_UPDATABLE_FIELDS = frozenset({"name"})


async def list_accounts(
    session: AsyncSession, operator: Operator
) -> list[XhsAccount]:
    """按 id 升序返回 operator 可见的账号;admin 全见,operator 仅其被 grant 的号。"""
    ids = await visible_account_ids(operator, session)
    stmt = select(XhsAccount).order_by(XhsAccount.id)
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(XhsAccount.id.in_(ids))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_account(
    session: AsyncSession, operator: Operator, account_id: int
) -> XhsAccount:
    """鉴权后返回单个账号;无 access 抛 AccessDenied,账号不存在抛 ValueError。"""
    await assert_account_access(operator, account_id, session)
    account = await session.get(XhsAccount, account_id)
    if account is None:
        raise ValueError(f"账号 {account_id} 不存在")
    return account


async def update_account(
    session: AsyncSession, operator: Operator, account_id: int, **fields
) -> XhsAccount:
    """鉴权后局部更新账号安全字段(当前仅 name);越界字段/不存在均抛 ValueError。

    fields 只允许 _UPDATABLE_FIELDS 内的键——传入 login_cookies/user_id 等敏感字段直接
    拒绝(ValueError),避免绕过 cookie_service 篡改登录态与身份。值为 None 的字段跳过不改。
    """
    await assert_account_access(operator, account_id, session)
    illegal = set(fields) - _UPDATABLE_FIELDS
    if illegal:
        raise ValueError(f"不允许更新字段: {', '.join(sorted(illegal))}")
    account = await session.get(XhsAccount, account_id)
    if account is None:
        raise ValueError(f"账号 {account_id} 不存在")
    for key, value in fields.items():
        if value is not None:
            setattr(account, key, value)
    await session.commit()
    return account


async def delete_account(
    session: AsyncSession, operator: Operator, account_id: int
) -> None:
    """鉴权后删账号并级联清其全部授权行;无 access 抛 AccessDenied,账号不存在静默(幂等)。"""
    await assert_account_access(operator, account_id, session)
    await session.execute(
        delete(OperatorAccountAccess).where(
            OperatorAccountAccess.xhs_account_id == account_id
        )
    )
    account = await session.get(XhsAccount, account_id)
    if account is not None:
        await session.delete(account)
    await session.commit()
