"""apikey 中间件 + Operator 上下文 + 引导 admin 的测试。

隔离手法与 tests/test_db.py 一致:monkeypatch app.core.db 的模块级 engine/
async_session 指向 tmp sqlite,并 patch settings.ROOT_ADMIN_APIKEY,用真实
lifespan(app.router.lifespan_context)驱动 init_db + bootstrap_admin,绝不碰
生产库 ./data/nbdpsy.db。

lifespan 必须在测试协程体内 `async with`(而非 yielding async fixture):MCP
StreamableHTTP session manager 内部用 anyio cancel scope,要求进入/退出在同一
task;pytest-asyncio 会把 async fixture 的 setup/teardown 拆到不同 task,导致
"Attempted to exit cancel scope in a different task"。故用 @asynccontextmanager
helper,在测试体内单 task 内 enter/exit(与 test_server_health 的写法一致)。

覆盖(brief 必测):
- 无 key 打受保护端点 → 401
- 非法 key → 401
- 带 ROOT_ADMIN_APIKEY → 200 且 whoami 返回 admin(REST 路由)
- 白名单 /healthz 无 key 仍 200
另加:X-API-Key 头、context.py 单测、bootstrap 幂等、以及关键风险实测——
ContextVar 能否穿透到挂载在 /mcp 的 FastMCP 工具执行(见文末测试与断言注释)。
"""

import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.core import config as config_module
from app.server import create_app

ADMIN_KEY = "unit-root-admin-key-xxxxxxxxxxxx"


@asynccontextmanager
async def isolated_client(tmp_path, monkeypatch, root_key=ADMIN_KEY):
    """隔离库 + 注入 ROOT_ADMIN_APIKEY,跑真实 lifespan,交出 (client, root_key)。

    必须以 `async with isolated_client(...)` 在测试体内使用,保证 MCP session
    manager 的 anyio 作用域在同一 task 内进出。
    """
    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)
    # bootstrap.py 用的是同一 settings 单例,patch 属性即生效。
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


# ---------------- context.py 单测 ----------------


def test_current_operator_raises_when_unset():
    """无上下文时 current_operator() 抛 AuthError。"""
    from app.auth.context import AuthError, current_operator

    with pytest.raises(AuthError):
        current_operator()


def test_set_reset_current_operator_roundtrip():
    """set → get 命中,reset 后回到未认证。"""
    from app.auth.context import (
        AuthError,
        current_operator,
        reset_current_operator,
        set_current_operator,
    )
    from app.models.operator import Operator

    op = Operator(name="root", role="admin", apikey_hash="h", enabled=True)
    token = set_current_operator(op)
    try:
        assert current_operator() is op
    finally:
        reset_current_operator(token)
    with pytest.raises(AuthError):
        current_operator()


# ---------------- 中间件 REST 行为(brief 必测) ----------------


async def test_healthz_whitelisted_without_key(tmp_path, monkeypatch):
    """白名单 /healthz 无 key 仍 200。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _):
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


async def test_whoami_without_key_401(tmp_path, monkeypatch):
    """受保护 /api/whoami 无 key → 401。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _):
        r = await c.get("/api/whoami")
        assert r.status_code == 401


async def test_whoami_bad_key_401(tmp_path, monkeypatch):
    """非法 key → 401。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, _):
        r = await c.get(
            "/api/whoami", headers={"Authorization": "Bearer totally-wrong"}
        )
        assert r.status_code == 401


async def test_whoami_admin_key_200(tmp_path, monkeypatch):
    """带 ROOT_ADMIN_APIKEY(Bearer)→ 200 且返回 admin/root。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        r = await c.get("/api/whoami", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert body["name"] == "root"


async def test_whoami_x_api_key_header_200(tmp_path, monkeypatch):
    """X-API-Key 头同样可通过校验。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        r = await c.get("/api/whoami", headers={"X-API-Key": key})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"


async def test_disabled_operator_rejected(tmp_path, monkeypatch):
    """enabled=False 的运营者即便 key 正确也 401(校验带 enabled 过滤)。"""
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        # 直接在隔离库里把 root 禁用
        from app.models.operator import Operator

        async with db_module.async_session() as s:
            op = (
                await s.execute(select(Operator).where(Operator.name == "root"))
            ).scalar_one()
            op.enabled = False
            await s.commit()

        r = await c.get("/api/whoami", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 401


# ---------------- bootstrap_admin 幂等 ----------------


async def test_bootstrap_upsert_idempotent_with_key(tmp_path, monkeypatch):
    """配置了 ROOT_ADMIN_APIKEY:多次 bootstrap 只保留一个 root,不产生重复。"""
    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/b.db", future=True
    )
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)
    monkeypatch.setattr(config_module.settings, "ROOT_ADMIN_APIKEY", ADMIN_KEY)

    from app.auth.bootstrap import bootstrap_admin
    from app.core.db import init_db
    from app.core.security import hash_apikey
    from app.models.operator import Operator

    await init_db()
    await bootstrap_admin()
    await bootstrap_admin()  # 第二次:必须幂等

    async with tmp_sessionmaker() as s:
        cnt = (
            await s.execute(
                select(func.count())
                .select_from(Operator)
                .where(Operator.name == "root")
            )
        ).scalar()
        assert cnt == 1
        op = (
            await s.execute(select(Operator).where(Operator.name == "root"))
        ).scalar_one()
        assert op.role == "admin"
        assert op.apikey_hash == hash_apikey(ADMIN_KEY)
    await tmp_engine.dispose()


async def test_bootstrap_generates_when_key_empty(tmp_path, monkeypatch):
    """未配置 ROOT_ADMIN_APIKEY:生成 admin;二次调用不重复生成。"""
    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/e.db", future=True
    )
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)
    monkeypatch.setattr(config_module.settings, "ROOT_ADMIN_APIKEY", "")

    from app.auth.bootstrap import bootstrap_admin
    from app.core.db import init_db
    from app.models.operator import Operator

    await init_db()
    await bootstrap_admin()
    await bootstrap_admin()

    async with tmp_sessionmaker() as s:
        cnt = (
            await s.execute(select(func.count()).select_from(Operator))
        ).scalar()
        assert cnt == 1
        op = (await s.execute(select(Operator))).scalar_one()
        assert op.role == "admin"
        assert op.name == "root"
        assert op.enabled is True
    await tmp_engine.dispose()


# ---------------- 关键风险实测:ContextVar 是否穿透 /mcp 工具 ----------------


def _sse_json(text: str):
    """从 Streamable HTTP 的 SSE 响应体里取第一段 data: 的 JSON。"""
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return None


async def _mcp_call(c, sid, auth, method, params=None, msg_id=None):
    """向 /mcp/ 发一条 JSON-RPC(带 session id 与鉴权头)。"""
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    headers = {**auth, "Accept": "application/json, text/event-stream"}
    if sid:
        headers["mcp-session-id"] = sid
    return await c.post("/mcp/", json=body, headers=headers)


async def test_contextvar_propagation_into_mcp_tool(tmp_path, monkeypatch):
    """实测:中间件 set 的 Operator 上下文能否被 /mcp 的 whoami 工具读到。

    完整 Streamable HTTP 握手:initialize(取 mcp-session-id)→ initialized 通知
    → tools/call whoami。

    实测结论(fastmcp 3.4.3 + starlette 1.3.1 + 进程内 ASGITransport):
    **能穿透**。父 FastAPI 的 BaseHTTPMiddleware 在 dispatch 里 set 的 ContextVar,
    经 call_next 的 copy_context() 被下游(含挂载在 /mcp 的工具执行)同 task 链继承,
    whoami 返回 authenticated=True 且身份为 root/admin。故后续工具可直接用
    current_operator() 取运营者(仍保留 get_http_headers() 作兜底,见 task 报告)。
    """
    async with isolated_client(tmp_path, monkeypatch) as (c, key):
        auth = {"Authorization": f"Bearer {key}"}

        init = await _mcp_call(
            c,
            None,
            auth,
            "initialize",
            params={
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
            msg_id=1,
        )
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        assert sid, "initialize 未返回 mcp-session-id"

        notified = await _mcp_call(c, sid, auth, "notifications/initialized")
        assert notified.status_code in (200, 202)

        called = await _mcp_call(
            c,
            sid,
            auth,
            "tools/call",
            params={"name": "whoami", "arguments": {}},
            msg_id=2,
        )
        assert called.status_code == 200
        payload = _sse_json(called.text)
        assert payload is not None, f"whoami 无法解析 SSE 响应: {called.text!r}"
        result = payload["result"]
        structured = result.get("structuredContent") or json.loads(
            result["content"][0]["text"]
        )

    # 固化实测结论:ContextVar 穿透成功,身份正确。
    assert structured["authenticated"] is True
    assert structured["name"] == "root"
    assert structured["role"] == "admin"
