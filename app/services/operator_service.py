"""运营者与账号授权的服务层:建/列/改/删运营者、轮换 apikey、授权/回收账号访问。

RBAC 管理面的业务核心。约定:
- 纯业务逻辑,使用调用方传入的 AsyncSession——会话与事务边界由调用者(MCP 工具/
  引导流程)掌握,本模块只 add/query/commit,不自开引擎、不管理连接。
- apikey 只在 create/rotate 时返回一次明文;库内仅存 SHA256 hash
  (见 app.core.security),明文永不落库。
- grant_access 幂等:命中既有授权即返回,不违反 (operator_id, xhs_account_id) 唯一约束。
- delete_operator 先清该运营者的全部 access 行再删本体(应用层级联,不依赖 DB 外键)。
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.security import generate_apikey, hash_apikey
from app.models.operator import Operator, OperatorAccountAccess


async def create_operator(
    session: AsyncSession, name: str, role: str = "operator"
) -> tuple[Operator, str]:
    """新建运营者:生成 apikey,库存其 hash,返回 (Operator, 明文 apikey)。

    明文仅此一次返回,调用方须立即交付使用者;库内永不留明文。
    """
    plain = generate_apikey()
    op = Operator(
        name=name, role=role, apikey_hash=hash_apikey(plain), enabled=True
    )
    session.add(op)
    await session.commit()
    return op, plain


async def list_operators(session: AsyncSession) -> list[Operator]:
    """按 id 升序返回全部运营者。"""
    result = await session.execute(select(Operator).order_by(Operator.id))
    return list(result.scalars().all())


async def update_operator(
    session: AsyncSession,
    id: int,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    name: str | None = None,
) -> Operator:
    """局部更新运营者 role/enabled/name(None 表示不改);运营者不存在抛 NotFoundError。"""
    op = await session.get(Operator, id)
    if op is None:
        raise NotFoundError(f"运营者 {id} 不存在")
    if role is not None:
        op.role = role
    if enabled is not None:
        op.enabled = enabled
    if name is not None:
        op.name = name
    await session.commit()
    return op


async def delete_operator(session: AsyncSession, id: int) -> None:
    """删除运营者并级联清除其全部账号授权行;运营者不存在时静默(幂等)。"""
    await session.execute(
        delete(OperatorAccountAccess).where(
            OperatorAccountAccess.operator_id == id
        )
    )
    op = await session.get(Operator, id)
    if op is not None:
        await session.delete(op)
    await session.commit()


async def rotate_apikey(session: AsyncSession, id: int) -> str:
    """重置运营者 apikey:生成新 key,更新 hash(旧 key 立即失效),返回一次性明文。

    运营者不存在抛 NotFoundError。
    """
    op = await session.get(Operator, id)
    if op is None:
        raise NotFoundError(f"运营者 {id} 不存在")
    plain = generate_apikey()
    op.apikey_hash = hash_apikey(plain)
    await session.commit()
    return plain


async def grant_access(
    session: AsyncSession,
    operator_id: int,
    xhs_account_id: int,
    granted_by: int | None,
) -> OperatorAccountAccess:
    """授予运营者对某小红书账号的操作权;幂等:已存在则返回既有行,不违反唯一约束。"""
    existing = (
        await session.execute(
            select(OperatorAccountAccess).where(
                OperatorAccountAccess.operator_id == operator_id,
                OperatorAccountAccess.xhs_account_id == xhs_account_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    access = OperatorAccountAccess(
        operator_id=operator_id,
        xhs_account_id=xhs_account_id,
        granted_by=granted_by,
    )
    session.add(access)
    await session.commit()
    return access


async def revoke_access(
    session: AsyncSession, operator_id: int, xhs_account_id: int
) -> None:
    """回收运营者对某账号的授权;不存在时静默(幂等)。"""
    await session.execute(
        delete(OperatorAccountAccess).where(
            OperatorAccountAccess.operator_id == operator_id,
            OperatorAccountAccess.xhs_account_id == xhs_account_id,
        )
    )
    await session.commit()


async def list_grants(session: AsyncSession, operator_id: int) -> list[int]:
    """返回运营者已授权的小红书账号 id 列表。"""
    result = await session.execute(
        select(OperatorAccountAccess.xhs_account_id).where(
            OperatorAccountAccess.operator_id == operator_id
        )
    )
    return list(result.scalars().all())
