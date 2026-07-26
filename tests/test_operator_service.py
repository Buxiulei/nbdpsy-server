"""operator_service 服务层单测:建/列/改/删运营者、轮换 apikey、授权幂等/级联清理。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。核心断言:
- create 返回明文且库内只存 hash(明文 != 库值,且 hash 对得上)。
- rotate 后 hash 变、旧 key 失效、新 key 生效。
- grant 幂等:重复授权返回既有行、不新增行、不撞唯一约束。
- delete 级联清空该 operator 的全部 access 行。
- list_grants 返回正确账号 id 列表;revoke 生效。
- "最后一个管理员"硬保护:update 的降级/停用与 delete 的删除三条等价路径都拒 409
  且事务回滚;删普通运营者、删不存在的 id 不受影响(幂等语义不变)。
"""

import pytest
from fastapi import HTTPException
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
    # 另起一位在岗管理员:否则把 u 改成"停用的 admin"会让系统一个有效管理员都不剩,
    # 撞上 update_operator 的最后一个管理员硬保护。
    await svc.create_operator(db, "在岗管理员", role="admin")
    op, _ = await svc.create_operator(db, "u")
    updated = await svc.update_operator(
        db, op.id, role="admin", enabled=False, name="u2"
    )
    assert updated.role == "admin"
    assert updated.enabled is False
    assert updated.name == "u2"


# ---------------- update:最后一个管理员硬保护 ----------------


async def _read_back(db: AsyncSession, op_id: int) -> tuple[str, bool]:
    """绕开 identity map 直读库内 (role, enabled),用于证明被拒后确实没落库。"""
    row = (
        await db.execute(
            select(Operator.role, Operator.enabled).where(Operator.id == op_id)
        )
    ).one()
    return row.role, row.enabled


async def test_demote_last_admin_rejected_and_rolled_back(db: AsyncSession):
    """降级唯一管理员 → 409,且库内 role/enabled 原样未动(事务真回滚,无半吊子状态)。"""
    boss, _ = await svc.create_operator(db, "boss", role="admin")
    boss_id = boss.id  # 回滚会 expire ORM 对象,id 先取出来
    await svc.create_operator(db, "小兵")  # 普通运营不算管理员,救不了场
    with pytest.raises(HTTPException) as exc:
        await svc.update_operator(db, boss_id, role="operator")
    assert exc.value.status_code == 409
    assert "至少一个启用中的管理员" in exc.value.detail
    assert await _read_back(db, boss_id) == ("admin", True)


async def test_disable_last_admin_rejected_and_rolled_back(db: AsyncSession):
    """停用唯一管理员 → 409(与降级等价的第二条路径),库内同样不留改动。"""
    boss, _ = await svc.create_operator(db, "boss", role="admin")
    boss_id = boss.id
    with pytest.raises(HTTPException) as exc:
        await svc.update_operator(db, boss_id, enabled=False)
    assert exc.value.status_code == 409
    assert await _read_back(db, boss_id) == ("admin", True)


async def test_demote_one_of_two_admins_ok(db: AsyncSession):
    """有两位有效管理员时降级其一放行:改动落库,另一位仍在岗。"""
    one, _ = await svc.create_operator(db, "boss1", role="admin")
    two, _ = await svc.create_operator(db, "boss2", role="admin")
    updated = await svc.update_operator(db, one.id, role="operator")
    assert updated.role == "operator"
    assert await _read_back(db, one.id) == ("operator", True)
    assert await _read_back(db, two.id) == ("admin", True)


async def test_demote_when_the_other_admin_is_disabled_rejected(db: AsyncSession):
    """两位 admin 但其中一位 enabled=false(有效管理员只剩一位)→ 降级另一位被拒。"""
    boss, _ = await svc.create_operator(db, "boss", role="admin")
    boss_id = boss.id
    sleeping, _ = await svc.create_operator(db, "休假的管理员", role="admin")
    # 此刻 boss 仍在岗,停用 sleeping 合法
    await svc.update_operator(db, sleeping.id, enabled=False)
    with pytest.raises(HTTPException) as exc:
        await svc.update_operator(db, boss_id, role="operator")
    assert exc.value.status_code == 409
    assert await _read_back(db, boss_id) == ("admin", True)


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


async def test_delete_last_admin_rejected_and_rolled_back(db: AsyncSession):
    """删唯一有效管理员 → 409,且该运营者仍在库里、role/enabled 原样(事务真回滚)。

    降级/停用被拦住而删除放行,系统照样落到 0 个管理员——管理端点全 admin_only,
    没人能改回来。第三条路径同样得堵。
    """
    boss, _ = await svc.create_operator(db, "boss", role="admin")
    boss_id = boss.id  # 回滚会 expire ORM 对象,id 先取出来
    await svc.create_operator(db, "小兵")  # 普通运营不算管理员,救不了场
    with pytest.raises(HTTPException) as exc:
        await svc.delete_operator(db, boss_id)
    assert exc.value.status_code == 409
    assert "至少一个启用中的管理员" in exc.value.detail
    assert await db.get(Operator, boss_id) is not None  # 人还在
    assert await _read_back(db, boss_id) == ("admin", True)  # 且没被改动


async def test_delete_plain_operator_ok(db: AsyncSession):
    """删普通运营者正常成功:被删者不是管理员,减不了管理员数量,不该触发保护。"""
    # 背景管理员:硬保护判的是"删完系统还剩几个有效管理员",而不是"被删的这位是不是管理员"
    # (后者要靠写事务外的陈旧读,正是被并发击穿过的那个门)。故无管理员的库删任何行都会 409。
    await svc.create_operator(db, "在岗boss", role="admin")
    op, _ = await svc.create_operator(db, "小兵")
    await svc.delete_operator(db, op.id)
    assert await db.get(Operator, op.id) is None


async def test_delete_one_of_two_admins_ok(db: AsyncSession):
    """有两位有效管理员时删其一放行,另一位仍在岗。"""
    one, _ = await svc.create_operator(db, "boss1", role="admin")
    two, _ = await svc.create_operator(db, "boss2", role="admin")
    await svc.delete_operator(db, one.id)
    assert await db.get(Operator, one.id) is None
    assert await _read_back(db, two.id) == ("admin", True)


async def test_delete_when_the_other_admin_is_disabled_rejected(db: AsyncSession):
    """两位 admin 但其一 enabled=false(有效管理员只剩一位)→ 删在岗那位被拒。"""
    boss, _ = await svc.create_operator(db, "boss", role="admin")
    boss_id = boss.id
    sleeping, _ = await svc.create_operator(db, "休假的管理员", role="admin")
    await svc.update_operator(db, sleeping.id, enabled=False)  # 此刻 boss 在岗,合法
    with pytest.raises(HTTPException) as exc:
        await svc.delete_operator(db, boss_id)
    assert exc.value.status_code == 409
    assert await _read_back(db, boss_id) == ("admin", True)


async def test_delete_unknown_id_is_silent(db: AsyncSession):
    """删不存在的 id 仍幂等静默成功——新判定不能把幂等语义变成 409。"""
    await svc.delete_operator(db, 9999)


async def test_delete_operator_cascades_access(db: AsyncSession):
    """delete_operator 删运营者并级联清空其全部 access 行。"""
    await svc.create_operator(db, "在岗boss", role="admin")  # 见上:删到行就判定,库里须有管理员
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


async def test_delete_checks_unconditionally_not_by_deleted_role(db: AsyncSession):
    """回归钉:判定条件必须是"这次真删到了行",不能是"被删者(读那一刻)是不是管理员"。

    这条洞被并发实测击穿过:``session.get`` 是写事务之外的陈旧读,拿它的 role 当门 →
    DELETE 读到 bob 还是 operator → 另一请求把 bob 提成 admin → 第三个请求降级 root
    (此刻统计看到 bob 是 admin,放行)→ DELETE 才拿到写锁,删掉 bob 且跳过判定 → 0 管理员。
    三条普通并发请求即可触发(时序网格 35/120 命中,最差配置 75%)。

    并发复现是时序相关的,做成测试必然 flaky;这里改钉**等价的确定性性质**:
    在一个没有有效管理员的库里删一个**普通运营者**——若判定无条件执行就会 409,
    若有人把 ``if was_effective_admin`` 那道门加回来,这里会静默成功,测试变红。
    """
    plain, _ = await svc.create_operator(db, "路人甲")
    plain_id = plain.id  # 回滚会 expire ORM 对象,id 先取出来(否则再读触发同步懒加载报错)
    with pytest.raises(HTTPException) as exc:
        await svc.delete_operator(db, plain_id)
    assert exc.value.status_code == 409
    assert await _read_back(db, plain_id) == ("operator", True)  # 事务回滚,人还在且原样
