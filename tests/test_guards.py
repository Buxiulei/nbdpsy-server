"""guards.py RBAC 助手单测:admin 通吃 / operator 仅其 access 行 / 越权抛错。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。造
admin、operator、两个账号,以及一条 operator→account1 的 access 行,覆盖三个
函数的全部分支:
- require_admin:admin 过、operator 抛 PermissionError。
- assert_account_access:admin 对任意账号放行;operator 有 access 放行、无则抛。
- visible_account_ids:admin 返 None(全部);operator 返其 access 账号 id 列表。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import (
    assert_account_access,
    require_admin,
    visible_account_ids,
)
from app.models import Operator, OperatorAccountAccess, XhsAccount


async def _seed(db: AsyncSession):
    """造 admin / operator / 两账号 / operator→account1 的 access,返回相关对象。"""
    admin = Operator(name="admin", apikey_hash="ha", role="admin")
    operator = Operator(name="op", apikey_hash="ho", role="operator")
    acc1 = XhsAccount(name="号1")
    acc2 = XhsAccount(name="号2")
    db.add_all([admin, operator, acc1, acc2])
    await db.commit()
    # 仅授权 operator 对 account1,account2 无关系(用于验证越权拦截)
    db.add(OperatorAccountAccess(operator_id=operator.id, xhs_account_id=acc1.id))
    await db.commit()
    return admin, operator, acc1, acc2


# ---------------- require_admin(纯函数,无需 db) ----------------


def test_require_admin_passes_for_admin():
    """admin 通过 require_admin(不抛异常即通过)。"""
    require_admin(Operator(name="a", apikey_hash="h", role="admin"))


def test_require_admin_raises_for_operator():
    """operator 调 require_admin 抛 PermissionError。"""
    with pytest.raises(PermissionError):
        require_admin(Operator(name="o", apikey_hash="h", role="operator"))


# ---------------- assert_account_access ----------------


async def test_admin_access_all(db: AsyncSession):
    """admin 对任意账号放行(含与其无 access 关系的 account2)。"""
    admin, _operator, acc1, acc2 = await _seed(db)
    await assert_account_access(admin, acc1.id, db)
    await assert_account_access(admin, acc2.id, db)


async def test_operator_access_only_granted(db: AsyncSession):
    """operator:有 access 的 account1 放行,无 access 的 account2 抛 PermissionError。"""
    _admin, operator, acc1, acc2 = await _seed(db)
    await assert_account_access(operator, acc1.id, db)
    with pytest.raises(PermissionError):
        await assert_account_access(operator, acc2.id, db)


# ---------------- visible_account_ids ----------------


async def test_visible_admin_returns_none(db: AsyncSession):
    """admin 的可见账号为 None(表示全部)。"""
    admin, *_ = await _seed(db)
    assert await visible_account_ids(admin, db) is None


async def test_visible_operator_returns_granted_ids(db: AsyncSession):
    """operator 的可见账号恰为其 access 行对应的账号 id 列表。"""
    _admin, operator, acc1, _acc2 = await _seed(db)
    ids = await visible_account_ids(operator, db)
    assert ids == [acc1.id]
