"""accounts + cookies 分组 MCP 工具的 RBAC 与行为测试。

隔离手法(与 test_admin_tools 一致):patch app.core.db 的模块级 async_session 指向
tmp sqlite,使工具内 get_session() 落隔离库;set_current_operator 在同一 task 内注入
上下文(已实测 ContextVar 穿透 mcp.call_tool 直调)。

覆盖(brief 必测):
- account_service.list_accounts:operator 只见被 grant 的号,admin 全见。
- update_account 拒敏感字段(user_id 等)→ ValueError(service 级白名单)。
- 工具级越权:非授权 operator 调 get/update/delete → ToolError(含"无权操作账号")。
- 账号工具返回体不含 login_cookies(明文/密文)。
- import_cookies 工具:解析 cookies_json 字符串建号,返回 {account_id, created}。
- get_cookies 工具:有 access 解密回读;无 access → ToolError。
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.auth.context import AccessDenied, reset_current_operator, set_current_operator
from app.core.security import hash_apikey
from app.models import Operator, OperatorAccountAccess, XhsAccount
from app.services import account_service
from app.tools.accounts import register_accounts
from app.tools.cookies import register_cookies


@asynccontextmanager
async def isolated_mcp(tmp_path, monkeypatch):
    """建隔离库 + patch 模块级 async_session + 注册 accounts/cookies 工具,交出 (mcp, sessionmaker)。"""
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

    mcp = FastMCP("account-test")
    register_accounts(mcp)
    register_cookies(mcp)
    try:
        yield mcp, smk
    finally:
        await tmp_engine.dispose()


async def _seed(smk):
    """造 admin + 两个 operator + 三个账号,并给 op1 授权 acc1/acc2;返回各 id。"""
    async with smk() as s:
        admin = Operator(name="root", role="admin", apikey_hash="h0", enabled=True)
        op1 = Operator(name="op1", role="operator", apikey_hash="h1", enabled=True)
        op2 = Operator(name="op2", role="operator", apikey_hash="h2", enabled=True)
        acc1 = XhsAccount(name="号1")
        acc2 = XhsAccount(name="号2")
        acc3 = XhsAccount(name="号3")
        s.add_all([admin, op1, op2, acc1, acc2, acc3])
        await s.commit()
        ids = {
            "admin": admin.id,
            "op1": op1.id,
            "op2": op2.id,
            "acc1": acc1.id,
            "acc2": acc2.id,
            "acc3": acc3.id,
        }
        s.add_all(
            [
                OperatorAccountAccess(operator_id=op1.id, xhs_account_id=acc1.id),
                OperatorAccountAccess(operator_id=op1.id, xhs_account_id=acc2.id),
            ]
        )
        await s.commit()
    return ids


def _ctx(op_id, role):
    """构造一个 detached Operator 供 set_current_operator(工具只读 id/role)。"""
    return Operator(id=op_id, name=f"op{op_id}", role=role, apikey_hash="x", enabled=True)


# ---------------- list_accounts:可见范围过滤 ----------------


async def test_list_accounts_operator_sees_only_granted(tmp_path, monkeypatch):
    """operator 调 list_accounts 只见被 grant 的号;admin 全见。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("list_accounts", {})
        finally:
            reset_current_operator(token)
        got = {a["id"] for a in res.structured_content["accounts"]}
        assert got == {ids["acc1"], ids["acc2"]}

        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            res_admin = await mcp.call_tool("list_accounts", {})
        finally:
            reset_current_operator(token)
        got_admin = {a["id"] for a in res_admin.structured_content["accounts"]}
        assert got_admin == {ids["acc1"], ids["acc2"], ids["acc3"]}


async def test_account_view_has_no_cookie_field(tmp_path, monkeypatch):
    """账号工具返回体绝不含 login_cookies(明文/密文)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)
        # 先给 acc1 灌一份 cookie(经 admin import 工具)
        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            await mcp.call_tool(
                "import_cookies",
                {
                    "account_name": "号1",
                    "cookies_json": json.dumps([{"name": "a", "value": "x"}]),
                    "user_info": {"user_id": "u1"},
                },
            )
            res = await mcp.call_tool("get_account", {"account_id": ids["acc1"]})
        finally:
            reset_current_operator(token)
        assert "login_cookies" not in res.structured_content
        assert res.structured_content["id"] == ids["acc1"]


# ---------------- update/delete:越权与安全字段 ----------------


async def test_update_delete_denied_without_access(tmp_path, monkeypatch):
    """op2 无 access:update/delete 任一账号 → ToolError(含"无权操作账号")。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei_u:
                await mcp.call_tool(
                    "update_account", {"account_id": ids["acc1"], "name": "黑"}
                )
            assert "无权操作账号" in str(ei_u.value)

            with pytest.raises(ToolError) as ei_d:
                await mcp.call_tool("delete_account", {"account_id": ids["acc1"]})
            assert "无权操作账号" in str(ei_d.value)
        finally:
            reset_current_operator(token)


async def test_update_account_rejects_sensitive_fields(tmp_path, monkeypatch):
    """service 级:update_account 传敏感字段(user_id)→ ValueError,name 正常改。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (_mcp, smk):
        ids = await _seed(smk)
        admin = _ctx(ids["admin"], "admin")
        async with smk() as s:
            with pytest.raises(ValueError):
                await account_service.update_account(
                    s, admin, ids["acc1"], user_id="hacked"
                )
        # 合法字段 name 能改
        async with smk() as s:
            acc = await account_service.update_account(
                s, admin, ids["acc1"], name="新名"
            )
            assert acc.name == "新名"


async def test_update_delete_happy_path(tmp_path, monkeypatch):
    """授权 operator 改名成功;admin 删账号后连带清 access 行。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)

        # op1 有 acc1 的 access,改名成功
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool(
                "update_account", {"account_id": ids["acc1"], "name": "改后"}
            )
            assert res.structured_content["name"] == "改后"
        finally:
            reset_current_operator(token)

        # admin 删 acc1
        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            res_d = await mcp.call_tool(
                "delete_account", {"account_id": ids["acc1"]}
            )
            assert res_d.structured_content["deleted"] == ids["acc1"]
        finally:
            reset_current_operator(token)

        # 账号与其 access 行都没了
        async with smk() as s:
            assert await s.get(XhsAccount, ids["acc1"]) is None
            cnt = (
                await s.execute(
                    select(func.count())
                    .select_from(OperatorAccountAccess)
                    .where(OperatorAccountAccess.xhs_account_id == ids["acc1"])
                )
            ).scalar()
            assert cnt == 0


# ---------------- cookies 工具:import / get ----------------


async def test_import_cookies_tool_parses_json_and_creates(tmp_path, monkeypatch):
    """import_cookies 工具解析 cookies_json 字符串建号,返回 {account_id, created}。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        # 用一个真实入库的 operator(import 新建时给它建 access)
        async with smk() as s:
            op = Operator(
                name="importer", role="operator", apikey_hash=hash_apikey("k"),
                enabled=True,
            )
            s.add(op)
            await s.commit()
            op_id = op.id

        token = set_current_operator(_ctx(op_id, "operator"))
        try:
            res = await mcp.call_tool(
                "import_cookies",
                {
                    "account_name": "新号",
                    "cookies_json": json.dumps(
                        [{"name": "a1", "value": "x", "sameSite": "lax"}]
                    ),
                    "user_info": {"user_id": "u9", "nickname": "N"},
                },
            )
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["created"] is True
        assert isinstance(data["account_id"], int)

        # 库内确有该号,且导入 operator 拿到 access
        async with smk() as s:
            acc = await s.get(XhsAccount, data["account_id"])
            assert acc is not None
            assert acc.user_id == "u9"
            cnt = (
                await s.execute(
                    select(func.count())
                    .select_from(OperatorAccountAccess)
                    .where(
                        OperatorAccountAccess.operator_id == op_id,
                        OperatorAccountAccess.xhs_account_id == data["account_id"],
                    )
                )
            ).scalar()
            assert cnt == 1


async def test_import_cookies_tool_accepts_list(tmp_path, monkeypatch):
    """B3:cookies_json 直接传 list(已解析数组)也能建号,不必先 JSON 序列化。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        async with smk() as s:
            op = Operator(
                name="importer", role="operator", apikey_hash=hash_apikey("k"),
                enabled=True,
            )
            s.add(op)
            await s.commit()
            op_id = op.id

        token = set_current_operator(_ctx(op_id, "operator"))
        try:
            res = await mcp.call_tool(
                "import_cookies",
                {
                    "account_name": "list号",
                    # 直接传数组(非 JSON 字符串)
                    "cookies_json": [{"name": "a1", "value": "x", "sameSite": "lax"}],
                    "user_info": {"user_id": "ulist"},
                },
            )
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["created"] is True
        async with smk() as s:
            acc = await s.get(XhsAccount, data["account_id"])
            assert acc.user_id == "ulist"


async def test_import_cookies_tool_rejects_bad_input(tmp_path, monkeypatch):
    """D3:cookies_json 非法 JSON 字符串 / 非 cookie 数组 → 明确中文报错,不建坏号。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        async with smk() as s:
            op = Operator(
                name="importer", role="operator", apikey_hash=hash_apikey("k"),
                enabled=True,
            )
            s.add(op)
            await s.commit()
            op_id = op.id

        token = set_current_operator(_ctx(op_id, "operator"))
        try:
            # 非法 JSON 字符串
            with pytest.raises(ToolError) as ei_json:
                await mcp.call_tool(
                    "import_cookies",
                    {"account_name": "坏号", "cookies_json": "{不是合法json"},
                )
            assert "cookie 对象数组" in str(ei_json.value)

            # 合法 JSON 但不是数组(是对象)
            with pytest.raises(ToolError) as ei_shape:
                await mcp.call_tool(
                    "import_cookies",
                    {"account_name": "坏号", "cookies_json": json.dumps({"name": "a"})},
                )
            assert "cookie 对象数组" in str(ei_shape.value)

            # 数组元素不是 dict
            with pytest.raises(ToolError) as ei_elem:
                await mcp.call_tool(
                    "import_cookies",
                    {"account_name": "坏号", "cookies_json": json.dumps(["a", "b"])},
                )
            assert "cookie 对象数组" in str(ei_elem.value)
        finally:
            reset_current_operator(token)

        # 三次非法输入都没建出账号
        async with smk() as s:
            cnt = (
                await s.execute(select(func.count()).select_from(XhsAccount))
            ).scalar()
            assert cnt == 0


async def test_get_cookies_tool_access_control(tmp_path, monkeypatch):
    """get_cookies 工具:导入者能解密回读;无 access 的 operator → ToolError。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        async with smk() as s:
            importer = Operator(
                name="importer", role="operator",
                apikey_hash=hash_apikey("k1"), enabled=True,
            )
            other = Operator(
                name="other", role="operator",
                apikey_hash=hash_apikey("k2"), enabled=True,
            )
            s.add_all([importer, other])
            await s.commit()
            importer_id, other_id = importer.id, other.id

        # importer 建号(拿到 access)
        token = set_current_operator(_ctx(importer_id, "operator"))
        try:
            imp = await mcp.call_tool(
                "import_cookies",
                {
                    "account_name": "号X",
                    "cookies_json": json.dumps(
                        [{"name": "a1", "value": "秘", "sameSite": "lax"}]
                    ),
                    "user_info": {"user_id": "uX"},
                },
            )
            acc_id = imp.structured_content["account_id"]

            got = await mcp.call_tool("get_cookies", {"account_id": acc_id})
            cookies = got.structured_content["cookies"]
            assert cookies[0]["name"] == "a1"
            assert cookies[0]["value"] == "秘"
            assert cookies[0]["sameSite"] == "Lax"
        finally:
            reset_current_operator(token)

        # other 无 access → ToolError
        token = set_current_operator(_ctx(other_id, "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("get_cookies", {"account_id": acc_id})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


# ---------------- service 级:get_account 不存在/越权 ----------------


async def test_get_account_not_found_and_denied(tmp_path, monkeypatch):
    """admin 取不存在账号 → ValueError;operator 取无 access 账号 → AccessDenied。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (_mcp, smk):
        ids = await _seed(smk)
        admin = _ctx(ids["admin"], "admin")
        async with smk() as s:
            with pytest.raises(ValueError):
                await account_service.get_account(s, admin, 999999)
        op2 = _ctx(ids["op2"], "operator")
        async with smk() as s:
            with pytest.raises(AccessDenied):
                await account_service.get_account(s, op2, ids["acc1"])


# ---------------- poll_login:等登录完成信号 ----------------

# 固定基准时刻:早于测试运行的真实 now,便于用 base±delta 造 since 前/后的号,
# 与真实 created_at(约等于 now)拉开距离,断言不受运行时钟抖动影响。
_BASE = datetime(2026, 1, 1, 0, 0, 0)


def test_parse_since_normalizes_to_naive_utc():
    """_parse_since:tz-aware 归一到 naive UTC(+08:00 的 08:00 == UTC 00:00);naive 原样。"""
    from app.tools.accounts import _parse_since

    aware = _parse_since("2026-01-01T08:00:00+08:00")
    assert aware == datetime(2026, 1, 1, 0, 0, 0)
    assert aware.tzinfo is None

    naive = _parse_since("2026-01-01T00:00:00")
    assert naive == datetime(2026, 1, 1, 0, 0, 0)


async def test_poll_login_new_account_done(tmp_path, monkeypatch):
    """登新号(不传 account_id):since 之后登录的号 done=true 且返回;since 之前的号不算。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)  # op1 有 acc1/acc2
        # 两号 created_at 拨到 since 之前;acc1 无登录,acc2 在 since 之后登录
        async with smk() as s:
            acc1 = await s.get(XhsAccount, ids["acc1"])
            acc1.created_at = _BASE - timedelta(hours=1)
            acc1.last_login_at = None
            acc2 = await s.get(XhsAccount, ids["acc2"])
            acc2.created_at = _BASE - timedelta(hours=1)
            acc2.last_login_at = _BASE + timedelta(minutes=5)
            await s.commit()

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool(
                "poll_login", {"since": _BASE.isoformat()}
            )
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["done"] is True
        # 只 since 之后登录的 acc2 命中;acc1(since 之前)不算
        assert {a["id"] for a in data["accounts"]} == {ids["acc2"]}


async def test_poll_login_by_account_id_reflects_last_login(tmp_path, monkeypatch):
    """重登旧号(传 account_id):done 反映该号 last_login_at 是否刷新到 since 之后。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)
        since = _BASE.isoformat()

        # 情形A:last_login 在 since 之前 → done=false
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            acc.last_login_at = _BASE - timedelta(minutes=1)
            await s.commit()

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            before = await mcp.call_tool(
                "poll_login", {"since": since, "account_id": ids["acc1"]}
            )
            assert before.structured_content["done"] is False
            assert before.structured_content["account"]["id"] == ids["acc1"]

            # 情形B:重新登录后 last_login 刷新到 since 之后 → done=true
            async with smk() as s:
                acc = await s.get(XhsAccount, ids["acc1"])
                acc.last_login_at = _BASE + timedelta(minutes=1)
                await s.commit()

            after = await mcp.call_tool(
                "poll_login", {"since": since, "account_id": ids["acc1"]}
            )
            assert after.structured_content["done"] is True
            assert after.structured_content["account"]["id"] == ids["acc1"]
        finally:
            reset_current_operator(token)


async def test_poll_login_rbac(tmp_path, monkeypatch):
    """RBAC:operator 只见自己的号;查别人的号(account_id)→ ToolError。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)
        since = _BASE.isoformat()
        # acc1(op1 的号)在 since 之后登录
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            acc.last_login_at = _BASE + timedelta(minutes=5)
            await s.commit()

        # op2 无任何授权号:登新号视角看不到别人号的登录 → done=false 空列表
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            res = await mcp.call_tool("poll_login", {"since": since})
            assert res.structured_content["done"] is False
            assert res.structured_content["accounts"] == []

            # 传 account_id 查别人的号 → 越权 ToolError
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "poll_login", {"since": since, "account_id": ids["acc1"]}
                )
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


async def test_poll_login_admin_sees_all_scope(tmp_path, monkeypatch):
    """admin 全见(visible_account_ids=None 分支):仍只匹配 since 之后登录的号。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk):
        ids = await _seed(smk)
        since = _BASE.isoformat()
        # 三号 created_at 都拨到 since 之前;仅 acc1 在 since 之后登录
        async with smk() as s:
            for aid, last_login in (
                (ids["acc1"], _BASE + timedelta(minutes=5)),
                (ids["acc2"], _BASE - timedelta(hours=1)),
                (ids["acc3"], None),
            ):
                acc = await s.get(XhsAccount, aid)
                acc.created_at = _BASE - timedelta(hours=1)
                acc.last_login_at = last_login
            await s.commit()

        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            res = await mcp.call_tool("poll_login", {"since": since})
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["done"] is True
        assert {a["id"] for a in data["accounts"]} == {ids["acc1"]}
