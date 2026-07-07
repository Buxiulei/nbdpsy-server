"""POST /api/cookies/import 端点测试:插件推 cookie 的 HTTP 灌入。

隔离手法与 test_auth_middleware 一致:patch app.core.db 模块级 engine/async_session 指向
tmp sqlite,patch settings.ROOT_ADMIN_APIKEY,用真实 lifespan(app.router.lifespan_context)
驱动 init_db + bootstrap_admin(root admin 拿到明文 apikey 做 Bearer 头)。

覆盖(brief 必测):
- 带合法 apikey POST → 200 + account_id(created=True),库内确有该号。
- 无 apikey → 401(中间件挡,不进业务层)。
- 请求体缺字段 → 422(Pydantic 校验)。
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
from app.models import XhsAccount
from app.server import create_app

ADMIN_KEY = "unit-root-admin-key-cookies-import-xx"


@asynccontextmanager
async def isolated_client(tmp_path, monkeypatch, root_key=ADMIN_KEY):
    """隔离库 + 注入 ROOT_ADMIN_APIKEY,跑真实 lifespan,交出 (client, root_key)。

    必须以 `async with isolated_client(...)` 在测试体内使用,保证 MCP session
    manager 的 anyio 作用域在同一 task 内进出(与 test_auth_middleware 同款)。
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


async def test_import_with_apikey_creates_account(tmp_path, monkeypatch):
    """带合法 apikey POST → 200 + account_id;库内确有该号(user_id 回填)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        r = await c.post(
            "/api/cookies/import",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "account_name": "插件号",
                "cookies": [{"name": "a1", "value": "x", "sameSite": "lax"}],
                "user_info": {"user_id": "u-http", "nickname": "N"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["account_id"], int)
        assert body["created"] is True

        async with db_module.async_session() as s:
            acc = await s.get(XhsAccount, body["account_id"])
            assert acc is not None
            assert acc.name == "插件号"
            assert acc.user_id == "u-http"
            assert acc.login_cookies  # 已加密落库


async def test_import_second_time_updates_not_duplicates(tmp_path, monkeypatch):
    """同 user_id 二次 POST → created=False、同一 account_id,不新增号。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        auth = {"Authorization": f"Bearer {key}"}
        payload = {
            "account_name": "插件号",
            "cookies": [{"name": "a1", "value": "old"}],
            "user_info": {"user_id": "u-dup"},
        }
        r1 = await c.post("/api/cookies/import", headers=auth, json=payload)
        assert r1.status_code == 200
        first_id = r1.json()["account_id"]

        payload["cookies"] = [{"name": "a1", "value": "new"}]
        r2 = await c.post("/api/cookies/import", headers=auth, json=payload)
        assert r2.status_code == 200
        assert r2.json()["created"] is False
        assert r2.json()["account_id"] == first_id

        async with db_module.async_session() as s:
            total = (
                await s.execute(select(XhsAccount).where(XhsAccount.user_id == "u-dup"))
            ).scalars().all()
            assert len(total) == 1


async def test_import_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey → 401(中间件挡,不进业务层)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _key):
        r = await c.post(
            "/api/cookies/import",
            json={
                "account_name": "插件号",
                "cookies": [{"name": "a1", "value": "x"}],
            },
        )
        assert r.status_code == 401


async def test_import_missing_field_422(tmp_path, monkeypatch):
    """带合法 apikey 但缺 cookies 字段 → 422(Pydantic 校验)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        r = await c.post(
            "/api/cookies/import",
            headers={"Authorization": f"Bearer {key}"},
            json={"account_name": "插件号"},
        )
        assert r.status_code == 422
