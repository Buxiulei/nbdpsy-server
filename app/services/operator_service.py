"""运营者与账号授权的服务层:建/列/改/删运营者、轮换 apikey、授权/回收账号访问。

RBAC 管理面的业务核心。约定:
- 纯业务逻辑,使用调用方传入的 AsyncSession——会话与事务边界由调用者(MCP 工具/
  引导流程)掌握,本模块只 add/query/commit,不自开引擎、不管理连接。**唯一例外**是
  下面那条硬保护:判定不通过时会 rollback **整个事务**(不只是本次改动),故不要把
  update/delete_operator 串在一个已有未提交改动的会话里——那些改动会被一并丢弃。
- apikey 只在 create/rotate 时返回一次明文;库内仅存 SHA256 hash
  (见 app.core.security),明文永不落库。
- grant_access 幂等:命中既有授权即返回,不违反 (operator_id, xhs_account_id) 唯一约束。
- delete_operator 先清该运营者的全部 access 行与风格档案(当前档案 + 历史版本)再删本体
  (应用层级联,不依赖 DB 外键)。
- update_operator 与 delete_operator 带"最后一个管理员"硬保护:操作完不能一个启用中的
  管理员都不剩,否则回滚并抛 HTTPException(409)——判定见 _ensure_admin_remains。
"""

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.security import generate_apikey, hash_apikey
from app.models.operator import Operator, OperatorAccountAccess
from app.models.style_profile import StyleProfile, StyleProfileVersion


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


async def _ensure_admin_remains(session: AsyncSession) -> None:
    """"最后一个管理员"硬保护的唯一判定:改动 flush 进事务后统计剩余"有效管理员"
    (role='admin' 且 enabled=true),为 0 则回滚整个事务并抛 HTTPException(409)。

    update_operator(降级/停用)与 delete_operator(删除)共用本函数,口径只有一份。
    管理端点(建/列/改/删运营者)全是 admin_only,一旦系统里 0 个管理员,再没有任何人
    能改回来,只能直接改数据库——降级、停用、删除最后一位管理员后果完全一样,都得拦。

    判定放在同一事务里、且**先应用改动再统计**:统计看到的是改动后的真实状态,
    这样两个并发请求不会双双通过(SQLite 写事务串行,后者的 flush 要等前者提交后
    才拿到写锁,届时统计已能看到前者的结果)。"先数再改"则有 TOCTOU:两边都数到
    2 个管理员,各自放行,落库后剩 0 个。

    409 用 fastapi.HTTPException 直抛,与 app/services/quota.py 的 429 同源
    (FastAPI 默认 handler 返 {"detail": ...},manifest error_contract 已声明该形状),
    不在 server.py 增专用异常映射。回滚会按 SQLAlchemy 语义 expire 该会话里的 ORM
    对象,调用方接到 409 后不要再读旧对象,要读请重新查询。
    """
    await session.flush()  # 改动落到事务内(未提交),下面的统计才看得见
    remaining_admins = (
        await session.execute(
            select(func.count())
            .select_from(Operator)
            .where(Operator.role == "admin", Operator.enabled.is_(True))
        )
    ).scalar()
    if remaining_admins == 0:
        await session.rollback()  # 整个事务回滚,库里保持原样
        raise HTTPException(
            status_code=409,
            detail=(
                "系统必须保留至少一个启用中的管理员;"
                "请先把其他人设为管理员,再来降级/停用/删除这一位"
            ),
        )


async def update_operator(
    session: AsyncSession,
    id: int,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    name: str | None = None,
) -> Operator:
    """局部更新运营者 role/enabled/name(None 表示不改);运营者不存在抛 NotFoundError。

    硬保护:改完后系统若一个有效管理员都不剩(降级 role→operator 或停用
    enabled→false 最后一位),回滚并抛 409——判定见 _ensure_admin_remains。
    """
    op = await session.get(Operator, id)
    if op is None:
        raise NotFoundError(f"运营者 {id} 不存在")
    if role is not None:
        op.role = role
    if enabled is not None:
        op.enabled = enabled
    if name is not None:
        op.name = name
    await _ensure_admin_remains(session)
    await session.commit()
    return op


async def delete_operator(session: AsyncSession, id: int) -> None:
    """删除运营者并级联清除其全部账号授权行 + 风格档案(当前档案与全部历史版本)。

    运营者不存在时静默(幂等)。风格档案的 FK 虽已声明 ondelete=CASCADE,但 SQLite 只有
    在连接上执行 PRAGMA foreign_keys=ON 才真正执行级联,而本仓 app/core/db.py 的连接
    pragma 只设 journal_mode/busy_timeout——**不能只靠 DB 约束**,否则删号后档案变孤儿行。

    硬保护:删掉的若是最后一位有效管理员,回滚并抛 409——与 update_operator 的降级/
    停用后果一样(管理端点全 admin_only,0 个管理员 = 管理面永久瘫痪),判定共用
    _ensure_admin_remains。

    **判定条件只能是"这次真删掉了一行",绝不能是"被删者(读那一刻)是不是管理员"**:
    上面 ``session.get`` 是写事务之外的陈旧读,拿它的 role 当门,就给出了这条真实攻击路径——
    DELETE 读到 bob 还是 operator → 另一请求把 bob 提成 admin → 第三个请求降级 root(此刻
    统计看到 bob 是 admin,放行)→ DELETE 才拿到写锁,删掉 bob 且**跳过判定** → 0 管理员。
    三条普通并发请求即可触发(实测时序网格 35/120 命中,最差配置 75%)。故删到行就无条件判定,
    多花一次 COUNT 换掉整类竞态。删不存在的 id 没删任何行、不判定,幂等语义原样保留。
    """
    op = await session.get(Operator, id)
    await session.execute(
        delete(OperatorAccountAccess).where(
            OperatorAccountAccess.operator_id == id
        )
    )
    await session.execute(
        delete(StyleProfileVersion).where(StyleProfileVersion.operator_id == id)
    )
    await session.execute(delete(StyleProfile).where(StyleProfile.operator_id == id))
    if op is not None:
        await session.delete(op)
        await _ensure_admin_remains(session)
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
