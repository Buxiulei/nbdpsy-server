"""REST 测试公共件:隔离库 client + 种子数据 helper。

从 tests/test_accounts_rest.py 原样提炼(isolated_client → rest_client、
_seed_account → seed_account、_make_operator → make_operator、
_admin_operator → get_root_admin),供各 REST 测试文件复用,避免重复定义。
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
from app.models import Operator
from app.server import create_app
from app.services import cookie_service

ADMIN_KEY = "test-root-admin-key"


def bearer(key: str) -> dict:
    """构造 Authorization 头。"""
    return {"Authorization": f"Bearer {key}"}


@asynccontextmanager
async def rest_client(tmp_path, monkeypatch, root_key=ADMIN_KEY):
    """隔离库 + 注入 ROOT_ADMIN_APIKEY,跑真实 lifespan,交出 AsyncClient。

    必须以 `async with rest_client(...)` 在测试体内使用,保证 lifespan 里起的
    后台任务(发布调度器等)的 anyio 作用域在同一 task 内进出。
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
                yield c
    finally:
        await tmp_engine.dispose()


async def get_root_admin() -> Operator:
    """取 bootstrap 建好的 root 管理员(供 cookie_service.import_cookies 落号用)。"""
    async with db_module.async_session() as s:
        return (
            await s.execute(select(Operator).where(Operator.name == "root"))
        ).scalar_one()


async def seed_account(name, user_id, cookies):
    """以 admin 身份灌一个带 cookie 的号;返回 account_id。"""
    admin = await get_root_admin()
    async with db_module.async_session() as s:
        account, _created, _cleaned = await cookie_service.import_cookies(
            s, admin, name, cookies, {"user_id": user_id}
        )
        return account.id


async def make_operator(apikey_plain):
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
