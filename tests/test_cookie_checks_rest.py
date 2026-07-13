"""POST /api/accounts/{id}/cookie-checks + GET /api/cookie-checks/{id} 端点测试:cookie 活性巡检异步对。

平移自 tests/test_publish_tools.py 的 check_cookies/get_cookie_check MCP 工具测试
(monkeypatch 点照旧——patch 的是 app.browser.sync_client.check_login_once,
cookie_check 服务本身不动,只是把工具层入口换成 REST)。

覆盖(brief 必测):
- POST 发起 → 202 {check_id, status:"checking"};轮询 GET 到 valid,附 user_info。
- POST 无该号 access → 403。
- POST 未知账号 → 404。
- 检测返回 error(基础设施失败)→ 轮询到 error 态,带 reason。
- GET 未知 check_id → 404。
- GET 跨 operator(无该号 access)→ 403。
"""

import asyncio

import app.core.db as db_module
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def _await_check(client, headers, check_id):
    """轮询 GET /api/cookie-checks/{check_id} 到非 checking 终态;带超时防死等后台任务。"""
    for _ in range(250):  # 250 * 0.02s = 5s 上限
        r = await client.get(f"/api/cookie-checks/{check_id}", headers=headers)
        if r.status_code != 200 or r.json()["status"] != "checking":
            return r
        await asyncio.sleep(0.02)
    raise AssertionError("异步 cookie 检测未在超时内落终态")


async def test_start_check_returns_202_check_id_then_valid(tmp_path, monkeypatch):
    """POST 立即 202 返 check_id/status=checking;GET 轮到 valid 附 user_info。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号1", "u1", _COOKIES)

        def fake_check_login_once(account_id, cookies):
            return {
                "status": "valid",
                "user_info": {"nickname": "小蓝", "user_id": "u1"},
            }

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        r = await c.post(
            f"/api/accounts/{acc1}/cookie-checks", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "checking"
        check_id = body["check_id"]
        assert isinstance(check_id, str) and check_id

        final = await _await_check(c, bearer(ADMIN_KEY), check_id)
        assert final.status_code == 200, final.text
        fb = final.json()
        assert fb["status"] == "valid"
        assert fb["user_info"]["nickname"] == "小蓝"


async def test_start_check_denied_without_access(tmp_path, monkeypatch):
    """无该号 access 的 operator POST → 403,且不触发后台检测。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号1", "u1", _COOKIES)
        called = {"n": 0}

        def fake_check_login_once(account_id, cookies):
            called["n"] += 1
            return {"status": "valid", "user_info": None}

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        op_key = "op-key-no-access"
        await make_operator(op_key)  # 未 grant 任何账号

        r = await c.post(
            f"/api/accounts/{acc1}/cookie-checks", headers=bearer(op_key)
        )
        assert r.status_code == 403
        assert called["n"] == 0  # 越权在起后台检测前就被拦


async def test_start_check_unknown_account_404(tmp_path, monkeypatch):
    """未知 account_id → 404(admin 越过 assert_account_access,落到账号存在性检查)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.post(
            "/api/accounts/999999/cookie-checks", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 404


async def test_check_error_state_carries_reason(tmp_path, monkeypatch):
    """检测返回 error(基础设施失败)→ 轮询到 error 态,带 reason(不代表 cookie 失效)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号1", "u1", _COOKIES)

        def fake_check_login_once(account_id, cookies):
            return {
                "status": "error",
                "user_info": None,
                "reason": "浏览器启动失败:boom",
            }

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        r = await c.post(
            f"/api/accounts/{acc1}/cookie-checks", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text
        check_id = r.json()["check_id"]

        final = await _await_check(c, bearer(ADMIN_KEY), check_id)
        assert final.status_code == 200, final.text
        fb = final.json()
        assert fb["status"] == "error"
        assert "浏览器启动失败" in fb["reason"]


async def test_get_check_unknown_id_404(tmp_path, monkeypatch):
    """未知 check_id → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.get(
            "/api/cookie-checks/does-not-exist", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 404


async def test_get_check_denied_cross_operator(tmp_path, monkeypatch):
    """A(有 access)发起检测,B(无该号 access)查结果 → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号1", "u1", _COOKIES)

        def fake_check_login_once(account_id, cookies):
            return {"status": "valid", "user_info": None}

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        op_a_key = "op-a-key"
        op_a_id = await make_operator(op_a_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_a_id, acc1, None)
            await s.commit()

        r = await c.post(
            f"/api/accounts/{acc1}/cookie-checks", headers=bearer(op_a_key)
        )
        assert r.status_code == 202, r.text
        check_id = r.json()["check_id"]
        # 等落终态(仍用 A 的 apikey 轮询)
        await _await_check(c, bearer(op_a_key), check_id)

        op_b_key = "op-b-key"
        await make_operator(op_b_key)  # 未 grant acc1

        r2 = await c.get(
            f"/api/cookie-checks/{check_id}", headers=bearer(op_b_key)
        )
        assert r2.status_code == 403
