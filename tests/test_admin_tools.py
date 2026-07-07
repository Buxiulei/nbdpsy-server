"""admin 分组 MCP 工具的鉴权与行为测试。

隔离手法(与 test_auth_middleware 一致):monkeypatch app.core.db 的模块级
async_session 指向 tmp sqlite,使工具内 get_session() 落到隔离库,绝不碰生产库。
工具鉴权用 set_current_operator 在同一 task 内注入上下文——已实测 ContextVar 能穿透
mcp.call_tool 直调(见 test_auth_middleware 的穿透结论)。

覆盖:
- 8 个工具全部对非 admin 抛 ToolError(内含 require_admin 的"需要管理员权限")。
- admin 调 create_operator:返回一次性明文 apikey,且库内只存 hash。
- admin 调 grant→list_grants→revoke 全链路,granted_by 落当前 admin id。
"""

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
from app.models import Operator, OperatorAccountAccess, XhsAccount
from app.tools.admin import register_admin


@asynccontextmanager
async def isolated_admin_mcp(tmp_path, monkeypatch):
    """建隔离库 + patch 模块级 async_session + 注册 admin 工具,交出 (mcp, sessionmaker)。"""
    from app.core.db import Base

    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    import app.models  # noqa: F401  触发模型注册后建表

    async with tmp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    smk = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "async_session", smk)

    mcp = FastMCP("admin-test")
    register_admin(mcp)
    try:
        yield mcp, smk
    finally:
        await tmp_engine.dispose()


# ---------------- 工具级鉴权:非 admin 全拦 ----------------

# 每个工具的最小合法入参;require_admin 在任何 DB 访问前先抛,故 id 可随意占位。
_NON_ADMIN_CALLS = {
    "create_operator": {"name": "x"},
    "list_operators": {},
    "update_operator": {"operator_id": 1},
    "delete_operator": {"operator_id": 1},
    "rotate_operator_apikey": {"operator_id": 1},
    "grant_account_access": {"operator_id": 1, "xhs_account_id": 1},
    "revoke_account_access": {"operator_id": 1, "xhs_account_id": 1},
    "list_operator_grants": {"operator_id": 1},
}


async def test_all_admin_tools_block_non_admin(tmp_path, monkeypatch):
    """非 admin 运营者调任一管理员工具都被 require_admin 拦下(ToolError 含中文原因)。"""
    async with isolated_admin_mcp(tmp_path, monkeypatch) as (mcp, _smk):
        op = Operator(name="op", role="operator", apikey_hash="h", enabled=True)
        token = set_current_operator(op)
        try:
            for tool, args in _NON_ADMIN_CALLS.items():
                with pytest.raises(ToolError) as ei:
                    await mcp.call_tool(tool, args)
                assert "需要管理员权限" in str(ei.value), f"{tool} 未被拦"
        finally:
            reset_current_operator(token)


# ---------------- admin 正常路径 ----------------


async def test_admin_create_operator_returns_plaintext_and_stores_hash(
    tmp_path, monkeypatch
):
    """admin 建运营者:工具返回一次性明文 apikey,库内只存 hash。"""
    async with isolated_admin_mcp(tmp_path, monkeypatch) as (mcp, smk):
        admin = Operator(name="root", role="admin", apikey_hash="h", enabled=True)
        token = set_current_operator(admin)
        try:
            res = await mcp.call_tool("create_operator", {"name": "alice"})
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["apikey"]  # 一次性明文
        assert data["role"] == "operator"
        assert data["name"] == "alice"
        assert "note" in data  # 含"只显示一次"提示

        # 库内确有该运营者,且存 hash 而非明文
        async with smk() as s:
            op = await s.get(Operator, data["id"])
            assert op is not None
            assert op.apikey_hash == hash_apikey(data["apikey"])
            assert op.apikey_hash != data["apikey"]


async def test_admin_grant_list_revoke_roundtrip(tmp_path, monkeypatch):
    """admin 授权→列出→回收全链路;granted_by 记为当前 admin 的 id。"""
    async with isolated_admin_mcp(tmp_path, monkeypatch) as (mcp, smk):
        # 先在隔离库造一个 admin(取回真实 id 供 granted_by 断言)与一个账号、一个被授权运营者
        async with smk() as s:
            admin = Operator(
                name="root", role="admin", apikey_hash="h", enabled=True
            )
            target = Operator(
                name="t", role="operator", apikey_hash="h2", enabled=True
            )
            acc = XhsAccount(name="号1")
            s.add_all([admin, target, acc])
            await s.commit()
            admin_id, target_id, acc_id = admin.id, target.id, acc.id

        ctx_admin = Operator(
            id=admin_id, name="root", role="admin", apikey_hash="h", enabled=True
        )
        token = set_current_operator(ctx_admin)
        try:
            granted = await mcp.call_tool(
                "grant_account_access",
                {"operator_id": target_id, "xhs_account_id": acc_id},
            )
            assert granted.structured_content["xhs_account_id"] == acc_id

            # granted_by 落当前 admin id
            async with smk() as s:
                row = await s.get(
                    OperatorAccountAccess,
                    granted.structured_content["id"],
                )
                assert row is not None
                assert row.granted_by == admin_id

            listed = await mcp.call_tool(
                "list_operator_grants", {"operator_id": target_id}
            )
            assert listed.structured_content["xhs_account_ids"] == [acc_id]

            await mcp.call_tool(
                "revoke_account_access",
                {"operator_id": target_id, "xhs_account_id": acc_id},
            )
            listed2 = await mcp.call_tool(
                "list_operator_grants", {"operator_id": target_id}
            )
            assert listed2.structured_content["xhs_account_ids"] == []
        finally:
            reset_current_operator(token)
