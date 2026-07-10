"""GET /api/accounts + GET /api/accounts/{id}/cookies 端点测试:插件"我的账号"列表 + 注入用解密 cookie。

隔离手法与 test_cookies_import_http 一致:patch app.core.db 模块级 engine/async_session 指向
tmp sqlite,patch settings.ROOT_ADMIN_APIKEY,用真实 lifespan 驱动 init_db + bootstrap_admin
(root admin 拿到明文 apikey 做 Bearer 头)。

覆盖(brief 必测):
- GET /api/accounts 带 apikey → 200 且返回该运营者可见的号(admin 全见);无 apikey → 401。
- 造 operator + 两个号只 grant 一个 → 该 operator 的 /api/accounts 只见被 grant 的号(RBAC)。
- GET /api/accounts/{id}/cookies 有 access → 200 返回解密 cookies;无 access → 403;无 apikey → 401。
- 账号列表返回体绝不含 login_cookies(明文/密文)。
"""

from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.core import config as config_module
from app.core.security import hash_apikey
from app.models import Operator, XhsAccount
from app.server import create_app
from app.services import cookie_service, operator_service

ADMIN_KEY = "unit-root-admin-key-accounts-rest-xx"


@asynccontextmanager
async def isolated_client(tmp_path, monkeypatch, root_key=ADMIN_KEY):
    """隔离库 + 注入 ROOT_ADMIN_APIKEY,跑真实 lifespan,交出 (client, root_key)。

    必须以 `async with isolated_client(...)` 在测试体内使用,保证 MCP session
    manager 的 anyio 作用域在同一 task 内进出(与 test_cookies_import_http 同款)。
    """
    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)
    monkeypatch.setattr(config_module.settings, "ROOT_ADMIN_APIKEY", root_key)

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as c:
                yield c, root_key
    finally:
        await tmp_engine.dispose()


async def _admin_operator():
    """取 bootstrap 建好的 root 管理员(供 cookie_service.import_cookies 落号用)。"""
    async with db_module.async_session() as s:
        return (
            await s.execute(select(Operator).where(Operator.name == "root"))
        ).scalar_one()


async def _seed_account(name, user_id, cookies):
    """以 admin 身份灌一个带 cookie 的号;返回 account_id。"""
    admin = await _admin_operator()
    async with db_module.async_session() as s:
        account, _created = await cookie_service.import_cookies(
            s, admin, name, cookies, {"user_id": user_id}
        )
        return account.id


async def _make_operator(apikey_plain):
    """建一个启用中的非 admin operator(apikey 明文 → hash 入库);返回 operator_id。"""
    async with db_module.async_session() as s:
        op = Operator(
            name="op-rest",
            role="operator",
            apikey_hash=hash_apikey(apikey_plain),
            enabled=True,
        )
        s.add(op)
        await s.commit()
        return op.id


# ---------------- GET /api/accounts ----------------


async def test_list_accounts_admin_sees_all(tmp_path, monkeypatch):
    """带合法 apikey(admin)GET /api/accounts → 200,全见已入库的号;返回体不含 cookie。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        await _seed_account("号A", "uA", [{"name": "a1", "value": "x"}])
        await _seed_account("号B", "uB", [{"name": "a1", "value": "y"}])

        r = await c.get(
            "/api/accounts", headers={"Authorization": f"Bearer {key}"}
        )
        assert r.status_code == 200, r.text
        accounts = r.json()["accounts"]
        names = {a["name"] for a in accounts}
        assert names == {"号A", "号B"}
        # 列表视图绝不含 login_cookies(明文/密文)
        assert all("login_cookies" not in a for a in accounts)


async def test_list_accounts_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey GET /api/accounts → 401(中间件挡,不进业务层)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _key):
        r = await c.get("/api/accounts")
        assert r.status_code == 401


async def test_list_accounts_operator_sees_only_granted(tmp_path, monkeypatch):
    """非 admin operator 只见被 grant 的号(RBAC 收窄)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _key):
        acc1 = await _seed_account("号1", "u1", [{"name": "a1", "value": "x"}])
        await _seed_account("号2", "u2", [{"name": "a1", "value": "y"}])

        op_key = "operator-plain-key-rest-scope-01"
        op_id = await _make_operator(op_key)
        # 只授权 acc1
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc1, op_id)

        r = await c.get(
            "/api/accounts", headers={"Authorization": f"Bearer {op_key}"}
        )
        assert r.status_code == 200, r.text
        got = {a["id"] for a in r.json()["accounts"]}
        assert got == {acc1}


# ---------------- GET /api/accounts/{id}/cookies ----------------


async def test_get_cookies_with_access_returns_decrypted(tmp_path, monkeypatch):
    """admin 有 access:GET /api/accounts/{id}/cookies → 200 返回解密 cookies。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        acc = await _seed_account(
            "号C", "uC", [{"name": "a1", "value": "秘", "sameSite": "lax"}]
        )
        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc
        cookies = body["cookies"]
        assert cookies[0]["name"] == "a1"
        assert cookies[0]["value"] == "秘"


async def test_get_cookies_without_access_403(tmp_path, monkeypatch):
    """无 access 的 operator GET /api/accounts/{id}/cookies → 403(AccessDenied 映射)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _key):
        acc = await _seed_account("号D", "uD", [{"name": "a1", "value": "x"}])
        op_key = "operator-plain-key-rest-noaccess-1"
        await _make_operator(op_key)  # 不授权任何号

        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {op_key}"},
        )
        assert r.status_code == 403


async def test_get_cookies_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey GET /api/accounts/{id}/cookies → 401(中间件挡)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _key):
        acc = await _seed_account("号E", "uE", [{"name": "a1", "value": "x"}])
        r = await c.get(f"/api/accounts/{acc}/cookies")
        assert r.status_code == 401
