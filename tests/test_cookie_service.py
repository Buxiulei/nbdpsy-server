"""cookie_service 单测:sameSite 规范化 + 共享 upsert + 加密落库 + import/get。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。核心断言:
- normalize:'unspecified'/小写 → 'Lax';缺 sameSite 补 'Lax';其余字段原样保留。
- import 新号:建 XhsAccount + created=True + 导入 operator 拿到 access + cookie 可解密回读。
- import 幂等:同 user_id / 同 account_name 二次 import → created=False、同一行 id、cookie 被更新。
- get_cookies:有 access 解密回读;无 access 的 operator 取 → 抛 AccessDenied。
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AccessDenied
from app.core.security import decrypt_cookies, hash_apikey
from app.models import Operator, OperatorAccountAccess, XhsAccount
from app.services import cookie_service


async def _make_operator(
    db: AsyncSession, name: str = "op", role: str = "operator"
) -> Operator:
    """造一个已提交的运营者(apikey_hash 随便给,测试只用 id/role)。"""
    op = Operator(
        name=name, apikey_hash=hash_apikey(name), role=role, enabled=True
    )
    db.add(op)
    await db.commit()
    return op


# ---------------- normalize_cookies ----------------


def test_normalize_samesite_unspecified():
    """'unspecified' → 'Lax'。"""
    out = cookie_service.normalize_cookies(
        [{"name": "a", "value": "1", "sameSite": "unspecified"}]
    )
    assert out[0]["sameSite"] == "Lax"


def test_normalize_samesite_lowercase():
    """小写 'lax' → 'Lax';'strict' → 'Strict';'none' → 'None'。"""
    out = cookie_service.normalize_cookies(
        [
            {"name": "a", "value": "1", "sameSite": "lax"},
            {"name": "b", "value": "2", "sameSite": "strict"},
            {"name": "c", "value": "3", "sameSite": "none"},
        ]
    )
    assert [c["sameSite"] for c in out] == ["Lax", "Strict", "None"]


def test_normalize_samesite_missing_defaults_lax():
    """缺 sameSite → 补 'Lax'。"""
    out = cookie_service.normalize_cookies([{"name": "a", "value": "1"}])
    assert out[0]["sameSite"] == "Lax"


def test_normalize_preserves_other_fields():
    """name/value/domain/path/httpOnly/secure/expires 原样保留,不改动入参。"""
    raw = [
        {
            "name": "a1",
            "value": "x",
            "domain": ".xiaohongshu.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "expires": 123.0,
            "sameSite": "lax",
        }
    ]
    out = cookie_service.normalize_cookies(raw)
    c = out[0]
    assert c["name"] == "a1"
    assert c["value"] == "x"
    assert c["domain"] == ".xiaohongshu.com"
    assert c["path"] == "/"
    assert c["httpOnly"] is True
    assert c["secure"] is True
    assert c["expires"] == 123.0
    assert c["sameSite"] == "Lax"
    # 不得就地修改入参
    assert raw[0]["sameSite"] == "lax"


# ---------------- import_cookies ----------------


async def test_import_creates_account_and_access(db: AsyncSession):
    """新号:created=True、回填 user_info、导入 operator 拿到 access、cookie 可解密回读。"""
    op = await _make_operator(db)
    cookies = [
        {"name": "a1", "value": "x", "domain": ".xiaohongshu.com", "sameSite": "lax"}
    ]
    acc, created = await cookie_service.import_cookies(
        db, op, "号1", cookies, {"nickname": "N", "user_id": "u1"}
    )
    assert created is True
    assert acc.id is not None
    assert acc.name == "号1"
    assert acc.nickname == "N"
    assert acc.user_id == "u1"
    assert acc.last_login_at is not None
    # login_cookies 非空且解密回得来(sameSite 已规范化)
    assert acc.login_cookies
    decrypted = json.loads(decrypt_cookies(acc.login_cookies))
    assert decrypted[0]["sameSite"] == "Lax"
    # 导入 operator 拿到 access
    cnt = (
        await db.execute(
            select(func.count())
            .select_from(OperatorAccountAccess)
            .where(
                OperatorAccountAccess.operator_id == op.id,
                OperatorAccountAccess.xhs_account_id == acc.id,
            )
        )
    ).scalar()
    assert cnt == 1


async def test_import_idempotent_by_user_id(db: AsyncSession):
    """同 user_id 二次 import → created=False、同一行 id、cookie 被更新、不新增号。"""
    op = await _make_operator(db)
    acc, created = await cookie_service.import_cookies(
        db, op, "号1", [{"name": "a1", "value": "old"}], {"user_id": "u1", "nickname": "N"}
    )
    assert created is True

    acc2, created2 = await cookie_service.import_cookies(
        db, op, "号1改名", [{"name": "a1", "value": "new"}], {"user_id": "u1"}
    )
    assert created2 is False
    assert acc2.id == acc.id
    # cookie 被更新
    decrypted = json.loads(decrypt_cookies(acc2.login_cookies))
    assert decrypted[0]["value"] == "new"
    # nickname 不被空 user_info 覆盖(保留首次的 N)
    assert acc2.nickname == "N"
    # 全库只有一个号
    total = (
        await db.execute(select(func.count()).select_from(XhsAccount))
    ).scalar()
    assert total == 1


async def test_import_update_refreshes_last_login_at(db: AsyncSession):
    """更新既有号(二次 import)也刷新 last_login_at —— 重登旧号 poll_login 才检测得到。"""
    op = await _make_operator(db)
    acc, created = await cookie_service.import_cookies(
        db, op, "号1", [{"name": "a1", "value": "old"}], {"user_id": "u1"}
    )
    assert created is True
    assert acc.last_login_at is not None

    # 人为把 last_login_at 拨回过去,模拟"上一次登录"
    old_time = datetime(2000, 1, 1, 0, 0, 0)
    acc.last_login_at = old_time
    await db.commit()

    # 二次 import 走更新路径,应把 last_login_at 刷新到 old_time 之后
    acc2, created2 = await cookie_service.import_cookies(
        db, op, "号1", [{"name": "a1", "value": "new"}], {"user_id": "u1"}
    )
    assert created2 is False
    assert acc2.id == acc.id
    assert acc2.last_login_at is not None
    assert acc2.last_login_at > old_time


async def test_import_idempotent_by_account_name(db: AsyncSession):
    """无 user_id 时按 account_name 匹配:同名二次 import → created=False、同一行。"""
    op = await _make_operator(db)
    acc, created = await cookie_service.import_cookies(
        db, op, "号1", [{"name": "a1", "value": "old"}], None
    )
    assert created is True

    acc2, created2 = await cookie_service.import_cookies(
        db, op, "号1", [{"name": "a1", "value": "new"}], None
    )
    assert created2 is False
    assert acc2.id == acc.id
    total = (
        await db.execute(select(func.count()).select_from(XhsAccount))
    ).scalar()
    assert total == 1


async def test_user_id_partial_unique_index_enforced(db: AsyncSession):
    """user_id 部分唯一索引:同一非空 user_id 第二行 IntegrityError;多个 NULL 允许并存。"""
    a1 = XhsAccount(name="号1")
    a1.user_id = "dup"
    db.add(a1)
    await db.commit()

    # 同一非空 user_id 再插一行 → 撞唯一索引
    a2 = XhsAccount(name="号2")
    a2.user_id = "dup"
    db.add(a2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 部分索引:user_id 为 NULL(仅 name 建的号)不受约束,可多行并存
    n1 = XhsAccount(name="空号A")
    n2 = XhsAccount(name="空号B")
    db.add_all([n1, n2])
    await db.commit()

    total = (
        await db.execute(select(func.count()).select_from(XhsAccount))
    ).scalar()
    # a1(dup) + n1 + n2;a2 已回滚
    assert total == 3


async def test_import_no_false_merge_across_user_ids(db: AsyncSession):
    """account_name 兜底防误并:同名但不同 user_id 不并入,新建独立行。"""
    op = await _make_operator(db)

    # 1) 仅 account_name、无 user_id → 新建(user_id 为空)
    acc0, c0 = await cookie_service.import_cookies(
        db, op, "同名号", [{"name": "a1", "value": "0"}], None
    )
    assert c0 is True
    assert acc0.user_id is None

    # 2) 相同 account_name 带 user_id A → 允许并入该行(把 A 盖上,合理)
    accA, cA = await cookie_service.import_cookies(
        db, op, "同名号", [{"name": "a1", "value": "A"}], {"user_id": "A"}
    )
    assert cA is False
    assert accA.id == acc0.id
    assert accA.user_id == "A"

    # 3) 相同 account_name 带不同 user_id B → 不并入(现在该行 user_id=A),新建独立行
    accB, cB = await cookie_service.import_cookies(
        db, op, "同名号", [{"name": "a1", "value": "B"}], {"user_id": "B"}
    )
    assert cB is True
    assert accB.id != accA.id
    assert accB.user_id == "B"

    # 两行,id 不同
    total = (
        await db.execute(select(func.count()).select_from(XhsAccount))
    ).scalar()
    assert total == 2


# ---------------- S1:更新路径鉴权 ----------------


async def test_import_update_denied_without_access(db: AsyncSession):
    """S1:无 access 的 operator import 命中既有号(按 user_id)→ 抛 AccessDenied 且 cookie 未变。"""
    importer = await _make_operator(db, name="importer")
    acc, _ = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "orig"}], {"user_id": "u1"}
    )

    other = await _make_operator(db, name="other")
    try:
        await cookie_service.import_cookies(
            db, other, "号1改名", [{"name": "a1", "value": "hijack"}], {"user_id": "u1"}
        )
        assert False, "应抛 AccessDenied"
    except AccessDenied:
        pass
    # cookie 未变(仍为 importer 导入的原值,未被越权覆盖)
    fresh = await db.get(XhsAccount, acc.id)
    decrypted = json.loads(decrypt_cookies(fresh.login_cookies))
    assert decrypted[0]["value"] == "orig"


async def test_import_update_allowed_with_access(db: AsyncSession):
    """S1:有 access 的 operator(导入者本人)命中既有号 → 正常更新 cookie。"""
    importer = await _make_operator(db, name="importer")
    acc, _ = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "orig"}], {"user_id": "u1"}
    )
    acc2, created2 = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "updated"}], {"user_id": "u1"}
    )
    assert created2 is False
    assert acc2.id == acc.id
    decrypted = json.loads(decrypt_cookies(acc2.login_cookies))
    assert decrypted[0]["value"] == "updated"


async def test_import_update_admin_allowed(db: AsyncSession):
    """S1:admin 无 access 行也能更新既有号(assert 对 admin 放行)。"""
    importer = await _make_operator(db, name="importer")
    acc, _ = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "orig"}], {"user_id": "u1"}
    )
    admin = await _make_operator(db, name="boss", role="admin")
    acc2, created2 = await cookie_service.import_cookies(
        db, admin, "号1", [{"name": "a1", "value": "byadmin"}], {"user_id": "u1"}
    )
    assert created2 is False
    assert acc2.id == acc.id
    decrypted = json.loads(decrypt_cookies(acc2.login_cookies))
    assert decrypted[0]["value"] == "byadmin"


# ---------------- get_cookies ----------------


async def test_get_cookies_with_access(db: AsyncSession):
    """有 access 的 operator 能取回解密后的 cookies。"""
    op = await _make_operator(db)
    cookies = [{"name": "a1", "value": "x", "sameSite": "lax"}]
    acc, _ = await cookie_service.import_cookies(db, op, "号1", cookies, {"user_id": "u1"})

    got = await cookie_service.get_cookies(db, op, acc.id)
    assert got[0]["name"] == "a1"
    assert got[0]["value"] == "x"
    assert got[0]["sameSite"] == "Lax"


async def test_get_cookies_denied_without_access(db: AsyncSession):
    """无 access 的 operator 取 cookies → 抛 AccessDenied。"""
    importer = await _make_operator(db, name="importer")
    acc, _ = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "x"}], {"user_id": "u1"}
    )
    # 另一个未获授权的普通运营者
    other = await _make_operator(db, name="other")
    try:
        await cookie_service.get_cookies(db, other, acc.id)
        assert False, "应抛 AccessDenied"
    except AccessDenied:
        pass


async def test_get_cookies_admin_bypasses_access(db: AsyncSession):
    """admin 无需 access 行也能取(assert_account_access 对 admin 放行)。"""
    importer = await _make_operator(db, name="importer")
    acc, _ = await cookie_service.import_cookies(
        db, importer, "号1", [{"name": "a1", "value": "x"}], {"user_id": "u1"}
    )
    admin = await _make_operator(db, name="boss", role="admin")
    got = await cookie_service.get_cookies(db, admin, acc.id)
    assert got[0]["name"] == "a1"
