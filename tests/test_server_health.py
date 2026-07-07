"""server 骨架冒烟测试:create_app() 能装配并响应 /healthz 探活。

说明:/healthz 是父 FastAPI 上独立的明文 REST 路由,不依赖 FastMCP
session manager 的 lifespan(挂载在 /mcp 的子 app)。因此用 httpx
ASGITransport 直打(不跑 lifespan 事件)即可稳定断言 200 + ok=True,
无需 LifespanManager;这也正是探活/鉴权白名单选中 /healthz 的原因。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.server import create_app


@pytest.mark.asyncio
async def test_app_boots():
    """create_app() 装配成功且 /healthz 返回 200 与 {"ok": True}。"""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_health_tool_registered():
    """register_all 把 MCP health 工具注册到实例,且返回 ok/version。"""
    from fastmcp import FastMCP

    from app.tools import register_all

    mcp = FastMCP("test")
    register_all(mcp)

    tools = await mcp.list_tools()
    assert "health" in {t.name for t in tools}

    result = await mcp.call_tool("health", {})
    payload = result.structured_content
    assert payload["ok"] is True
    assert "version" in payload


@pytest.mark.asyncio
async def test_mcp_endpoint_wired_via_real_lifespan(tmp_path, monkeypatch):
    """驱动 create_app() 的真实 lifespan 后,POST /mcp/ initialize 能打通。

    回归目标:test_app_boots 用 ASGITransport 直打,不会触发 lifespan 事件
    (combine_lifespans 被拆掉也测不出来);test_health_tool_registered 建的是
    裸 FastMCP,从不经过 create_app()/app.mount("/mcp", ...)(挂载退回没有
    path="/" 的 mcp.http_app(),重现 /mcp/mcp 双嵌套 bug,这里也测不出来)。
    本测试真正跑 app.router.lifespan_context(app)(等价 uvicorn 启停,驱动
    combine_lifespans 组合出的 init_db + MCP session manager task group 初始化),
    再对 /mcp/(注意结尾斜杠,POST /mcp 无斜杠会 307)发一个 JSON-RPC
    initialize,断言 200 且响应体含 protocolVersion——证明 lifespan 组合与
    "/mcp" 挂载确实布线通了,而不只是 create_app() 不报错。
    """
    # 与 tests/test_db.py 同样的隔离手法:把 init_db() 实际落地的模块级
    # engine/async_session 换成临时 sqlite,不碰生产库文件 ./data/nbdpsy.db。
    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)
    # /mcp/ 现在受 apikey 中间件保护:注入 ROOT_ADMIN_APIKEY 让 bootstrap 建 root,
    # 再用 Bearer 头通过校验(否则 initialize 直接被中间件 401 拦掉)。
    from app.core import config as config_module

    admin_key = "wired-test-admin-key"
    monkeypatch.setattr(config_module.settings, "ROOT_ADMIN_APIKEY", admin_key)

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as c:
                r = await c.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"},
                        },
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {admin_key}",
                    },
                )
    finally:
        await tmp_engine.dispose()

    # 实测返回是 Streamable HTTP 的 SSE 帧(text/event-stream),而非纯 JSON;
    # 按实际返回体断言 200 + 含 protocolVersion 字样即可,不强解析 SSE 格式。
    assert r.status_code == 200
    assert "protocolVersion" in r.text
