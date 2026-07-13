"""server 骨架冒烟测试:create_app() 能装配并响应 /healthz 探活。

说明:/healthz 是 FastAPI 上独立的明文 REST 路由,不依赖 lifespan(建表/引导
admin/起调度器都在 lifespan 里)。因此用 httpx ASGITransport 直打(不跑
lifespan 事件)即可稳定断言 200 + ok=True,无需 LifespanManager;这也正是
探活/鉴权白名单选中 /healthz 的原因。
"""

import pytest
from httpx import ASGITransport, AsyncClient

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
