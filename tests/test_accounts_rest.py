"""GET /api/accounts + 单号 CRUD + /api/accounts/{id}/cookies + GET /api/login/poll 端点测试。

隔离手法与 test_cookies_import_http 一致:patch app.core.db 模块级 engine/async_session 指向
tmp sqlite,patch settings.ROOT_ADMIN_APIKEY,用真实 lifespan 驱动 init_db + bootstrap_admin
(root admin 拿到明文 apikey 做 Bearer 头)。

覆盖(brief 必测):
- GET /api/accounts 带 apikey → 200 且返回该运营者可见的号(admin 全见);无 apikey → 401。
- 造 operator + 两个号只 grant 一个 → 该 operator 的 /api/accounts 只见被 grant 的号(RBAC)。
- GET /api/accounts/{id}/cookies 有 access → 200 返回解密 cookies;无 access → 403;无 apikey → 401。
- 账号列表返回体绝不含 login_cookies(明文/密文)。
- GET/PATCH/DELETE /api/accounts/{account_id} 单号 CRUD:授权/未授权/不存在的 403、404 分支。
- GET /api/login/poll 登录闭环轮询:登新号、重登旧号、RBAC 收窄、since 非法、account_id 不存在。
"""

from datetime import datetime, timedelta

import app.core.db as db_module
from app.models.xhs_account import XhsAccount
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator as _make_operator,
    rest_client as isolated_client,
    seed_account as _seed_account,
)

# poll_login 测试的固定基准时刻:早于测试运行的真实 now,便于用 base±delta 造 since 前/后的
# 号,与真实 created_at(约等于 now)拉开距离,断言不受运行时钟抖动影响。
_BASE = datetime(2026, 1, 1, 0, 0, 0)


# ---------------- GET /api/accounts ----------------


async def test_list_accounts_admin_sees_all(tmp_path, monkeypatch):
    """带合法 apikey(admin)GET /api/accounts → 200,全见已入库的号;返回体不含 cookie。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        await _seed_account("号A", "uA", [{"name": "a1", "value": "x"}])
        await _seed_account("号B", "uB", [{"name": "a1", "value": "y"}])

        r = await c.get(
            "/api/accounts", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert r.status_code == 200, r.text
        accounts = r.json()["accounts"]
        names = {a["name"] for a in accounts}
        assert names == {"号A", "号B"}
        # 列表视图绝不含 login_cookies(明文/密文)
        assert all("login_cookies" not in a for a in accounts)


async def test_list_accounts_without_apikey_401(tmp_path, monkeypatch):
    """无 apikey GET /api/accounts → 401(中间件挡,不进业务层)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get("/api/accounts")
        assert r.status_code == 401


async def test_list_accounts_invalid_apikey_401(tmp_path, monkeypatch):
    """带一个库里不存在的 Bearer token GET /api/accounts → 401
    (覆盖中间件"apikey 存在但查不到 operator"分支,即 op is None,与缺失 apikey 是不同分支)。
    """
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get(
            "/api/accounts",
            headers={"Authorization": "Bearer this-apikey-does-not-exist-in-db"},
        )
        assert r.status_code == 401


async def test_list_accounts_operator_sees_only_granted(tmp_path, monkeypatch):
    """非 admin operator 只见被 grant 的号(RBAC 收窄)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
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
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account(
            "号C", "uC", [{"name": "a1", "value": "秘", "sameSite": "lax"}]
        )
        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc
        cookies = body["cookies"]
        assert cookies[0]["name"] == "a1"
        assert cookies[0]["value"] == "秘"


async def test_get_cookies_operator_with_access_returns_decrypted(
    tmp_path, monkeypatch
):
    """operator 有 access:自己的 apikey GET /api/accounts/{id}/cookies → 200 返回解密 cookies
    (现有测试只覆盖 admin 正向 + operator 负向,这里补 operator 正向)。
    """
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account(
            "号F", "uF", [{"name": "a1", "value": "秘F", "sameSite": "lax"}]
        )
        op_key = "operator-plain-key-rest-access-ok-01"
        op_id = await _make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc, op_id)

        r = await c.get(
            f"/api/accounts/{acc}/cookies",
            headers={"Authorization": f"Bearer {op_key}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc
        cookies = body["cookies"]
        assert cookies[0]["name"] == "a1"
        assert cookies[0]["value"] == "秘F"


async def test_get_cookies_without_access_403(tmp_path, monkeypatch):
    """无 access 的 operator GET /api/accounts/{id}/cookies → 403(AccessDenied 映射)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
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
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号E", "uE", [{"name": "a1", "value": "x"}])
        r = await c.get(f"/api/accounts/{acc}/cookies")
        assert r.status_code == 401


# ---------------- GET/PATCH/DELETE /api/accounts/{account_id} ----------------


async def test_get_account_view_and_denied(tmp_path, monkeypatch):
    """授权号 GET → 200 account_view 键全(且无 login_cookies);未授权号 → 403;不存在 → 404。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号G", "uG", [{"name": "a1", "value": "x"}])
        op_key = "operator-plain-key-rest-get-acc-01"
        op_id = await _make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc, op_id)

        r = await c.get(f"/api/accounts/{acc}", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == acc
        assert set(body.keys()) == {
            "id", "name", "nickname", "user_id", "red_id", "avatar", "status",
            "cookie_status", "last_check_at", "last_login_at", "created_at",
        }
        assert "login_cookies" not in body

        other_key = "operator-plain-key-rest-get-acc-noaccess"
        await _make_operator(other_key)  # 不授权任何号
        r_denied = await c.get(f"/api/accounts/{acc}", headers=bearer(other_key))
        assert r_denied.status_code == 403

        # 不存在的账号:非 admin 无 access 行时得 403(不泄露存在性),故用 admin 判 404。
        r_404 = await c.get("/api/accounts/999999", headers=bearer(ADMIN_KEY))
        assert r_404.status_code == 404


async def test_update_account_name_happy_and_denied(tmp_path, monkeypatch):
    """PATCH {"name":"新名"} → 200 name 变;未授权 → 403。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号H", "uH", [{"name": "a1", "value": "x"}])
        op_key = "operator-plain-key-rest-patch-acc-01"
        op_id = await _make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc, op_id)

        r = await c.patch(
            f"/api/accounts/{acc}", json={"name": "新名"}, headers=bearer(op_key)
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "新名"

        other_key = "operator-plain-key-rest-patch-acc-noaccess"
        await _make_operator(other_key)
        r_denied = await c.patch(
            f"/api/accounts/{acc}", json={"name": "黑"}, headers=bearer(other_key)
        )
        assert r_denied.status_code == 403


async def test_update_account_rejects_extra_fields(tmp_path, monkeypatch):
    """PATCH {"status":"x"} → 422(Pydantic 模型只收 name;与旧工具 ValueError 语义的合理收严)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号I", "uI", [{"name": "a1", "value": "x"}])
        r = await c.patch(
            f"/api/accounts/{acc}", json={"status": "x"}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 422


async def test_delete_account_happy_and_denied(tmp_path, monkeypatch):
    """DELETE → {deleted:id} 后 GET → 404;未授权 → 403。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号J", "uJ", [{"name": "a1", "value": "x"}])
        op_key = "operator-plain-key-rest-del-acc-01"
        op_id = await _make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc, op_id)

        r = await c.delete(f"/api/accounts/{acc}", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": acc}

        r_gone = await c.get(f"/api/accounts/{acc}", headers=bearer(ADMIN_KEY))
        assert r_gone.status_code == 404

        acc2 = await _seed_account("号K", "uK", [{"name": "a1", "value": "x"}])
        other_key = "operator-plain-key-rest-del-acc-noaccess"
        await _make_operator(other_key)
        r_denied = await c.delete(f"/api/accounts/{acc2}", headers=bearer(other_key))
        assert r_denied.status_code == 403


# ---------------- GET /api/login/poll:登录闭环轮询 ----------------


def test_parse_since_normalizes_to_naive_utc():
    """_parse_since:tz-aware 归一到 naive UTC(+08:00 的 08:00 == UTC 00:00);naive 原样。"""
    from app.http.accounts_rest import _parse_since

    aware = _parse_since("2026-01-01T08:00:00+08:00")
    assert aware == datetime(2026, 1, 1, 0, 0, 0)
    assert aware.tzinfo is None

    naive = _parse_since("2026-01-01T00:00:00")
    assert naive == datetime(2026, 1, 1, 0, 0, 0)


async def test_poll_login_new_account_done(tmp_path, monkeypatch):
    """seed 前记 since=utcnow iso;seed_account 后 GET /api/login/poll?since=... → done True。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        since = datetime.utcnow().isoformat()
        acc = await _seed_account("号L", "uL", [{"name": "a1", "value": "x"}])

        r = await c.get(
            "/api/login/poll", params={"since": since}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["done"] is True
        assert {a["id"] for a in body["accounts"]} == {acc}


async def test_poll_login_by_account_id(tmp_path, monkeypatch):
    """对既有号,since 晚于 last_login_at → done False;刷新到 since 之后 → done True,键 account。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        acc = await _seed_account("号M", "uM", [{"name": "a1", "value": "x"}])
        since = _BASE.isoformat()

        async with db_module.async_session() as s:
            account = await s.get(XhsAccount, acc)
            account.last_login_at = _BASE - timedelta(minutes=1)
            await s.commit()

        r_before = await c.get(
            "/api/login/poll",
            params={"since": since, "account_id": acc},
            headers=bearer(ADMIN_KEY),
        )
        assert r_before.status_code == 200, r_before.text
        body_before = r_before.json()
        assert body_before["done"] is False
        assert body_before["account"]["id"] == acc

        async with db_module.async_session() as s:
            account = await s.get(XhsAccount, acc)
            account.last_login_at = _BASE + timedelta(minutes=1)
            await s.commit()

        r_after = await c.get(
            "/api/login/poll",
            params={"since": since, "account_id": acc},
            headers=bearer(ADMIN_KEY),
        )
        assert r_after.status_code == 200, r_after.text
        body_after = r_after.json()
        assert body_after["done"] is True
        assert body_after["account"]["id"] == acc


async def test_poll_login_rbac_narrowed(tmp_path, monkeypatch):
    """operator 无授权 → done False accounts 空;admin 全见。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        since = _BASE.isoformat()
        acc = await _seed_account("号N", "uN", [{"name": "a1", "value": "x"}])
        async with db_module.async_session() as s:
            account = await s.get(XhsAccount, acc)
            account.last_login_at = _BASE + timedelta(minutes=5)
            await s.commit()

        op_key = "operator-plain-key-rest-poll-noaccess"
        await _make_operator(op_key)  # 不授权任何号

        r_op = await c.get(
            "/api/login/poll", params={"since": since}, headers=bearer(op_key)
        )
        assert r_op.status_code == 200, r_op.text
        body_op = r_op.json()
        assert body_op["done"] is False
        assert body_op["accounts"] == []

        r_admin = await c.get(
            "/api/login/poll", params={"since": since}, headers=bearer(ADMIN_KEY)
        )
        assert r_admin.status_code == 200, r_admin.text
        body_admin = r_admin.json()
        assert body_admin["done"] is True
        assert {a["id"] for a in body_admin["accounts"]} == {acc}


async def test_poll_new_account_excludes_cleaned_placeholder(tmp_path, monkeypatch):
    """§7.6:A 清理占位后,登新号 poll 返回的 accounts 只含真账号(占位被删不再出现)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        auth = bearer(ADMIN_KEY)
        since = datetime.utcnow().isoformat()

        # 占位推送(无 user_info)→ 落 xhs_account_ 占位行
        r1 = await c.post(
            "/api/cookies/import",
            headers=auth,
            json={
                "account_name": "xhs_account_9990001",
                "cookies": [{"name": "web_session", "value": "x"}],
            },
        )
        assert r1.status_code == 200, r1.text
        placeholder_id = r1.json()["account_id"]

        # 真登录推送 → 清掉占位
        r2 = await c.post(
            "/api/cookies/import",
            headers=auth,
            json={
                "account_name": "生意经",
                "cookies": [{"name": "web_session", "value": "y"}],
                "user_info": {"user_id": "real-poll", "nickname": "生意经"},
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["cleaned_placeholders"] == 1
        real_id = r2.json()["account_id"]

        # 登新号 poll:accounts 只含真账号,占位 id 不在列表里
        r = await c.get(
            "/api/login/poll", params={"since": since}, headers=auth
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["done"] is True
        ids = {a["id"] for a in body["accounts"]}
        assert ids == {real_id}
        assert placeholder_id not in ids


async def test_poll_login_bad_since_400(tmp_path, monkeypatch):
    """since="garbage" → 400。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get(
            "/api/login/poll", params={"since": "garbage"}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 400


async def test_poll_login_unknown_account_404(tmp_path, monkeypatch):
    """account_id 指定的账号不存在 → 404。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        since = _BASE.isoformat()
        r = await c.get(
            "/api/login/poll",
            params={"since": since, "account_id": 999999},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404
