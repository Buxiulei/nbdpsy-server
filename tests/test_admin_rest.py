"""admin 分组 REST 测试:仅 admin 可调 / apikey 生命周期 / 授权往返。"""

from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

# 8 端点 (method, path 构造器) 清单,用于逐一打非 admin 拦截
_ADMIN_CALLS = [
    ("POST", "/api/operators", {"name": "x"}),
    ("GET", "/api/operators", None),
    ("PATCH", "/api/operators/1", {"enabled": False}),
    ("DELETE", "/api/operators/1", None),
    ("POST", "/api/operators/1/rotate-apikey", None),
    ("POST", "/api/operators/1/grants", {"xhs_account_id": 1}),
    ("DELETE", "/api/operators/1/grants/1", None),
    ("GET", "/api/operators/1/grants", None),
]


async def test_all_admin_endpoints_block_non_admin(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "plain-operator-key"
        await make_operator(op_key)
        for method, path, body in _ADMIN_CALLS:
            r = await client.request(method, path, json=body, headers=bearer(op_key))
            assert r.status_code == 403, f"{method} {path} 应 403,得 {r.status_code}"
            assert "需要管理员权限" in r.json()["error"]


async def test_create_operator_returns_plaintext_and_new_key_works(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/operators", json={"name": "alice"}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["role"] == "operator"
        assert data["enabled"] is True
        assert data["apikey"]
        assert data["note"]
        assert data["id"]
        assert data["name"] == "alice"

        r2 = await client.get("/api/whoami", headers=bearer(data["apikey"]))
        assert r2.status_code == 200, r2.text
        assert r2.json()["name"] == "alice"


async def test_create_operator_missing_name_422(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/operators", json={}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 422


async def test_list_operators_contains_root_and_created(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await client.post(
            "/api/operators", json={"name": "bob"}, headers=bearer(ADMIN_KEY)
        )
        r = await client.get("/api/operators", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        ops = r.json()["operators"]
        names = {o["name"] for o in ops}
        assert names == {"root", "bob"}
        for o in ops:
            assert set(o.keys()) == {"id", "name", "role", "enabled", "created_at"}


async def test_update_operator_disable_then_key_rejected(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/operators", json={"name": "carl"}, headers=bearer(ADMIN_KEY)
        )
        created = r.json()
        r2 = await client.patch(
            f"/api/operators/{created['id']}",
            json={"enabled": False},
            headers=bearer(ADMIN_KEY),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["enabled"] is False

        r3 = await client.get("/api/whoami", headers=bearer(created["apikey"]))
        assert r3.status_code == 401


async def test_update_operator_unknown_id_404(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.patch(
            "/api/operators/9999", json={"enabled": False}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 404
        assert "error" in r.json()


async def test_rotate_apikey_old_dies_new_works(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/operators", json={"name": "dana"}, headers=bearer(ADMIN_KEY)
        )
        created = r.json()
        r2 = await client.post(
            f"/api/operators/{created['id']}/rotate-apikey", headers=bearer(ADMIN_KEY)
        )
        assert r2.status_code == 200, r2.text
        rotated = r2.json()
        assert rotated["apikey"]
        assert rotated["apikey"] != created["apikey"]
        assert rotated["note"]

        r_old = await client.get("/api/whoami", headers=bearer(created["apikey"]))
        assert r_old.status_code == 401
        r_new = await client.get("/api/whoami", headers=bearer(rotated["apikey"]))
        assert r_new.status_code == 200


async def test_grant_list_revoke_roundtrip(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        acc = await seed_account("号A", "uA", _COOKIES)
        r = await client.post(
            "/api/operators", json={"name": "erin"}, headers=bearer(ADMIN_KEY)
        )
        created = r.json()
        op_id = created["id"]
        op_key = created["apikey"]

        r2 = await client.post(
            f"/api/operators/{op_id}/grants",
            json={"xhs_account_id": acc},
            headers=bearer(ADMIN_KEY),
        )
        assert r2.status_code == 200, r2.text
        granted = r2.json()
        assert granted["operator_id"] == op_id
        assert granted["xhs_account_id"] == acc
        assert granted["id"]

        r3 = await client.get(
            f"/api/operators/{op_id}/grants", headers=bearer(ADMIN_KEY)
        )
        assert r3.status_code == 200, r3.text
        assert r3.json() == {"operator_id": op_id, "xhs_account_ids": [acc]}

        r4 = await client.get(
            f"/api/accounts/{acc}/cookies", headers=bearer(op_key)
        )
        assert r4.status_code == 200, r4.text

        r5 = await client.delete(
            f"/api/operators/{op_id}/grants/{acc}", headers=bearer(ADMIN_KEY)
        )
        assert r5.status_code == 200, r5.text
        assert r5.json() == {"operator_id": op_id, "xhs_account_id": acc, "revoked": True}

        r6 = await client.get(
            f"/api/operators/{op_id}/grants", headers=bearer(ADMIN_KEY)
        )
        assert r6.json() == {"operator_id": op_id, "xhs_account_ids": []}

        r7 = await client.get(
            f"/api/accounts/{acc}/cookies", headers=bearer(op_key)
        )
        assert r7.status_code == 403


async def test_delete_operator(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/operators", json={"name": "frank"}, headers=bearer(ADMIN_KEY)
        )
        created = r.json()
        r2 = await client.delete(
            f"/api/operators/{created['id']}", headers=bearer(ADMIN_KEY)
        )
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"deleted": created["id"]}

        r3 = await client.get("/api/whoami", headers=bearer(created["apikey"]))
        assert r3.status_code == 401
