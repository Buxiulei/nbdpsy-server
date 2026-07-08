"""端到端冒烟:整个纯 MCP 后台的两条关键链路走通。

两条链:
1. RBAC 链(不标 slow,纯 DB/工具链,不起浏览器):
   create_operator → grant_account_access → 该 operator 只见被授权号。
   走真实 MCP 工具(register_all + call_tool),验证管理面 + 访问收窄端到端自洽。
2. 发布链(标 slow,需真 cookie + 浏览器,默认不在 CI 跑):
   import_cookies → check_cookies → publish_note → 轮询 get_publish_status。
   缺真账号素材(环境变量未配)时 skip,绝不阻塞 CI。

隔离手法与单测一致:tmp sqlite + patch db_module.async_session;set_current_operator
在同一 task 内注入运营者上下文(ContextVar 已实测穿透 mcp.call_tool 直调)。
"""

import json
import os
from contextlib import asynccontextmanager

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.auth.context import reset_current_operator, set_current_operator
from app.core.security import hash_apikey
from app.models import Operator, XhsAccount
from app.tools import register_all


@asynccontextmanager
async def isolated_full_mcp(tmp_path, monkeypatch):
    """建隔离库 + patch 模块级 async_session + 注册全部工具,交出 (mcp, sessionmaker)。"""
    from app.core.db import Base

    tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e2e.db", future=True)
    import app.models  # noqa: F401  触发模型注册后建表

    async with tmp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    smk = async_sessionmaker(tmp_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session", smk)

    mcp = FastMCP("e2e-smoke")
    register_all(mcp)
    try:
        yield mcp, smk
    finally:
        await tmp_engine.dispose()


def _ctx(op_id: int, role: str) -> Operator:
    """构造 detached Operator 供 set_current_operator(工具只读 id/role)。"""
    return Operator(id=op_id, name=f"op{op_id}", role=role, apikey_hash="x", enabled=True)


async def _seed_admin(smk) -> int:
    """入库一个 root 管理员,返回其 id(admin 上下文的锚点)。"""
    async with smk() as s:
        admin = Operator(
            name="root", role="admin", apikey_hash=hash_apikey("root-key"), enabled=True
        )
        s.add(admin)
        await s.commit()
        return admin.id


# ============ 链路 1:RBAC 端到端(不 slow)============


async def test_rbac_chain_operator_sees_only_granted(tmp_path, monkeypatch):
    """create_operator → 建两号 → grant 其一 → operator 只见被授权号,越权号被拒。"""
    async with isolated_full_mcp(tmp_path, monkeypatch) as (mcp, smk):
        admin_id = await _seed_admin(smk)
        admin_token = set_current_operator(_ctx(admin_id, "admin"))
        try:
            # 1. admin 建一个 operator(拿到一次性 apikey)
            created = await mcp.call_tool(
                "create_operator", {"name": "运营小张", "role": "operator"}
            )
            op = created.structured_content
            op_id = op["id"]
            assert op["role"] == "operator"
            assert op["apikey"]  # 一次性明文 apikey 已返回

            # 2. admin 建两个受托管账号(直接入库,避免起浏览器)
            async with smk() as s:
                acc1 = XhsAccount(name="号1")
                acc2 = XhsAccount(name="号2")
                s.add_all([acc1, acc2])
                await s.commit()
                acc1_id, acc2_id = acc1.id, acc2.id

            # 3. admin 只把 acc1 授权给该 operator
            granted = await mcp.call_tool(
                "grant_account_access",
                {"operator_id": op_id, "xhs_account_id": acc1_id},
            )
            assert granted.structured_content["xhs_account_id"] == acc1_id

            # 4. admin 视角:授权清单只含 acc1
            grants = await mcp.call_tool(
                "list_operator_grants", {"operator_id": op_id}
            )
            assert grants.structured_content["xhs_account_ids"] == [acc1_id]
        finally:
            reset_current_operator(admin_token)

        # 5. operator 视角:list_accounts 只见 acc1;取 acc2 被拒(越权)
        op_token = set_current_operator(_ctx(op_id, "operator"))
        try:
            listed = await mcp.call_tool("list_accounts", {})
            visible = {a["id"] for a in listed.structured_content["accounts"]}
            assert visible == {acc1_id}

            got1 = await mcp.call_tool("get_account", {"account_id": acc1_id})
            assert got1.structured_content["id"] == acc1_id

            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("get_account", {"account_id": acc2_id})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(op_token)


# ============ 链路 2:发布端到端(slow,需真账号)============

# 发布冒烟需真 cookie:环境变量 NBDPSY_E2E_COOKIES(cookies JSON 字符串)+
# NBDPSY_E2E_ACCOUNT_NAME(可选,默认 e2e-号)。未配则 skip,绝不阻塞 CI。
_E2E_COOKIES = os.getenv("NBDPSY_E2E_COOKIES")


@pytest.mark.slow
@pytest.mark.skipif(
    not _E2E_COOKIES,
    reason="需真小红书 cookie:设 NBDPSY_E2E_COOKIES(+可选 NBDPSY_E2E_ACCOUNT_NAME)后手动跑",
)
async def test_publish_chain_real_account(tmp_path, monkeypatch):
    """import_cookies → check_cookies → publish_note → 轮询 get_publish_status(需真号真浏览器)。

    默认 skip;手动跑时需 Xvfb(:99)可用 + 真 cookie。发布内容用明确的测试标记,
    跑完请自行到小红书后台删除产出的测试笔记(本冒烟不自动删远端笔记)。
    """
    import asyncio

    account_name = os.getenv("NBDPSY_E2E_ACCOUNT_NAME", "e2e-号")

    async with isolated_full_mcp(tmp_path, monkeypatch) as (mcp, smk):
        # 该链需要真实运行发布调度器(publish_note 立即入队走 get_active_scheduler)
        from app.publish.runtime import set_active_scheduler
        from app.publish.scheduler import PublishScheduler

        scheduler = PublishScheduler(smk, poll_interval=1.0)
        scheduler.start()
        set_active_scheduler(scheduler)

        admin_id = await _seed_admin(smk)
        token = set_current_operator(_ctx(admin_id, "admin"))
        try:
            # 1. 灌 cookie(admin 建号即拥有 access)
            imported = await mcp.call_tool(
                "import_cookies",
                {"account_name": account_name, "cookies_json": _E2E_COOKIES},
            )
            account_id = imported.structured_content["account_id"]

            # 2. 活性巡检(异步):check_cookies 返 check_id → 轮询 get_cookie_check 到终态
            started = await mcp.call_tool("check_cookies", {"account_id": account_id})
            check_id = started.structured_content["check_id"]
            check_status = None
            for _ in range(30):  # 最多 ~60s 等浏览器检测
                got = await mcp.call_tool("get_cookie_check", {"check_id": check_id})
                check_status = got.structured_content["status"]
                if check_status != "checking":
                    break
                await asyncio.sleep(2)
            assert check_status == "valid", "cookie 已失效,先重新导出再跑"

            # 3. 建发布任务(立即入队)
            published = await mcp.call_tool(
                "publish_note",
                {
                    "account_id": account_id,
                    "title": "e2e 冒烟测试笔记",
                    "content": "这是一条自动化端到端冒烟测试,请忽略。",
                    "images": [],
                    "topics": [],
                },
            )
            job_id = published.structured_content["job_id"]

            # 4. 轮询终态(最多 ~2 分钟),断言落到 published
            final_status = None
            for _ in range(60):
                status = await mcp.call_tool("get_publish_status", {"job_id": job_id})
                final_status = status.structured_content["status"]
                if final_status in ("published", "failed"):
                    break
                await asyncio.sleep(2)
            assert final_status == "published", f"发布未成功,终态={final_status}"
        finally:
            reset_current_operator(token)
            set_active_scheduler(None)
            await scheduler.stop()
