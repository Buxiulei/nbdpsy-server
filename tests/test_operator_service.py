"""operator_service 服务层单测:建/列/改/删运营者、轮换 apikey、授权幂等/级联清理。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。核心断言:
- create 返回明文且库内只存 hash(明文 != 库值,且 hash 对得上)。
- rotate 后 hash 变、旧 key 失效、新 key 生效。
- grant 幂等:重复授权返回既有行、不新增行、不撞唯一约束。
- delete 级联清空该 operator 的全部 access 行。
- list_grants 返回正确账号 id 列表;revoke 生效。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_apikey, verify_apikey
from app.models import Operator, OperatorAccountAccess, XhsAccount
from app.services import operator_service as svc


async def _make_accounts(db: AsyncSession, n: int = 2) -> list[XhsAccount]:
    """造 n 个小红书账号并提交,返回对象列表(供授权测试引用真实 id)。"""
    accs = [XhsAccount(name=f"号{i}") for i in range(1, n + 1)]
    db.add_all(accs)
    await db.commit()
    return accs


# ---------------- create ----------------


async def test_create_returns_plaintext_stores_hash(db: AsyncSession):
    """create 返回明文 apikey,库内只存其 hash(默认 role=operator, enabled=True)。"""
    op, key = await svc.create_operator(db, "alice")
    assert key  # 明文非空
    assert op.id is not None
    assert op.name == "alice"
    assert op.role == "operator"  # 默认角色
    assert op.enabled is True
    # 关键:库里存 hash 而非明文
    assert op.apikey_hash != key
    assert op.apikey_hash == hash_apikey(key)
    assert verify_apikey(key, op.apikey_hash)


async def test_create_admin_role(db: AsyncSession):
    """显式 role='admin' 建管理员。"""
    op, _ = await svc.create_operator(db, "boss", role="admin")
    assert op.role == "admin"


# ---------------- list / update ----------------


async def test_list_operators(db: AsyncSession):
    """list_operators 返回已建的全部运营者。"""
    await svc.create_operator(db, "a")
    await svc.create_operator(db, "b")
    names = {o.name for o in await svc.list_operators(db)}
    assert {"a", "b"} <= names


async def test_update_operator(db: AsyncSession):
    """update 局部改 role/enabled/name。"""
    op, _ = await svc.create_operator(db, "u")
    updated = await svc.update_operator(
        db, op.id, role="admin", enabled=False, name="u2"
    )
    assert updated.role == "admin"
    assert updated.enabled is False
    assert updated.name == "u2"


# ---------------- rotate ----------------


async def test_rotate_changes_hash_and_invalidates_old(db: AsyncSession):
    """rotate 后 hash 变更;旧 key 失效、新 key 生效。"""
    op, old_key = await svc.create_operator(db, "r")
    old_hash = op.apikey_hash
    new_key = await svc.rotate_apikey(db, op.id)
    assert new_key != old_key
    assert op.apikey_hash != old_hash
    assert not verify_apikey(old_key, op.apikey_hash)  # 旧 key 失效
    assert verify_apikey(new_key, op.apikey_hash)  # 新 key 生效


# ---------------- grant / list_grants / revoke ----------------


async def test_grant_idempotent(db: AsyncSession):
    """重复 grant 幂等:返回既有行,不新增记录,不撞唯一约束。"""
    op, _ = await svc.create_operator(db, "g")
    acc1, _acc2 = await _make_accounts(db)
    a1 = await svc.grant_access(db, op.id, acc1.id, granted_by=None)
    a1_again = await svc.grant_access(db, op.id, acc1.id, granted_by=None)
    assert a1_again.id == a1.id  # 返回既有行
    cnt = (
        await db.execute(
            select(func.count())
            .select_from(OperatorAccountAccess)
            .where(OperatorAccountAccess.operator_id == op.id)
        )
    ).scalar()
    assert cnt == 1  # 未新增


async def test_list_grants(db: AsyncSession):
    """list_grants 返回该 operator 授权的全部账号 id。"""
    op, _ = await svc.create_operator(db, "lg")
    acc1, acc2 = await _make_accounts(db)
    await svc.grant_access(db, op.id, acc1.id, granted_by=None)
    await svc.grant_access(db, op.id, acc2.id, granted_by=None)
    ids = await svc.list_grants(db, op.id)
    assert sorted(ids) == sorted([acc1.id, acc2.id])


async def test_revoke_access(db: AsyncSession):
    """revoke 后该授权消失。"""
    op, _ = await svc.create_operator(db, "rv")
    acc1, _ = await _make_accounts(db)
    await svc.grant_access(db, op.id, acc1.id, granted_by=None)
    await svc.revoke_access(db, op.id, acc1.id)
    assert await svc.list_grants(db, op.id) == []


# ---------------- delete 级联 ----------------


async def test_delete_operator_cascades_access(db: AsyncSession):
    """delete_operator 删运营者并级联清空其全部 access 行。"""
    op, _ = await svc.create_operator(db, "d")
    acc1, acc2 = await _make_accounts(db)
    await svc.grant_access(db, op.id, acc1.id, granted_by=None)
    await svc.grant_access(db, op.id, acc2.id, granted_by=None)
    await svc.delete_operator(db, op.id)
    assert await db.get(Operator, op.id) is None  # 运营者已删
    cnt = (
        await db.execute(
            select(func.count())
            .select_from(OperatorAccountAccess)
            .where(OperatorAccountAccess.operator_id == op.id)
        )
    ).scalar()
    assert cnt == 0  # access 行级联清空
