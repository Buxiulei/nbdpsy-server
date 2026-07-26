"""admin 分组 REST 测试:仅 admin 可调 / apikey 生命周期 / 授权往返。"""

from tests.rest_helpers import (
    ADMIN_KEY, bearer, get_root_admin, make_operator, rest_client, seed_account,
)

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


async def test_update_operator_demote_last_admin_409(tmp_path, monkeypatch):
    """降级唯一管理员(bootstrap 的 root)→ 409 {"detail": ...},且 root 仍在岗。

    管理端点全 admin_only,若放行这一改动系统就是 0 个管理员,谁都改不回来。
    """
    async with rest_client(tmp_path, monkeypatch) as client:
        root = await get_root_admin()
        r = await client.patch(
            f"/api/operators/{root.id}",
            json={"role": "operator"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 409, r.text
        assert "至少一个启用中的管理员" in r.json()["detail"]

        # root 的 apikey 依旧管用、角色依旧是 admin → 改动确实没落库
        r2 = await client.get("/api/operators", headers=bearer(ADMIN_KEY))
        assert r2.status_code == 200, r2.text
        me = next(o for o in r2.json()["operators"] if o["id"] == root.id)
        assert me["role"] == "admin"
        assert me["enabled"] is True


async def test_update_operator_disable_last_admin_409(tmp_path, monkeypatch):
    """停用唯一管理员 → 同样 409:停用与降级后果完全一样,两条路径都得堵。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        root = await get_root_admin()
        r = await client.patch(
            f"/api/operators/{root.id}",
            json={"enabled": False},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 409, r.text

        r2 = await client.get("/api/operators", headers=bearer(ADMIN_KEY))
        me = next(o for o in r2.json()["operators"] if o["id"] == root.id)
        assert me["enabled"] is True


async def test_update_operator_demote_admin_ok_when_another_admin_exists(
    tmp_path, monkeypatch
):
    """先把新人提成管理员,再降级 root → 放行(系统仍有一位在岗管理员)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        root = await get_root_admin()
        created = (
            await client.post(
                "/api/operators", json={"name": "gina", "role": "admin"},
                headers=bearer(ADMIN_KEY),
            )
        ).json()
        r = await client.patch(
            f"/api/operators/{root.id}",
            json={"role": "operator"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator"

        # 降级后 root 已非 admin,改用新管理员的 key 复核
        r2 = await client.get("/api/operators", headers=bearer(created["apikey"]))
        assert r2.status_code == 200, r2.text
        me = next(o for o in r2.json()["operators"] if o["id"] == root.id)
        assert me["role"] == "operator"


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


async def test_delete_last_admin_409(tmp_path, monkeypatch):
    """删唯一管理员(bootstrap 的 root)→ 409,且 root 的 key 与角色都原样可用。

    与 PATCH 的降级/停用后果完全一致:放行就是 0 个管理员,而管理端点全 admin_only。
    """
    async with rest_client(tmp_path, monkeypatch) as client:
        root = await get_root_admin()
        r = await client.delete(
            f"/api/operators/{root.id}", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 409, r.text
        assert "至少一个启用中的管理员" in r.json()["detail"]

        # root 的 apikey 依旧管用、角色/启用位依旧原样 → 事务真回滚
        r2 = await client.get("/api/operators", headers=bearer(ADMIN_KEY))
        assert r2.status_code == 200, r2.text
        me = next(o for o in r2.json()["operators"] if o["id"] == root.id)
        assert me["role"] == "admin"
        assert me["enabled"] is True


async def test_delete_admin_ok_when_another_admin_exists(tmp_path, monkeypatch):
    """先建第二位管理员,再删 root → 放行(系统仍有一位在岗管理员)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        root = await get_root_admin()
        created = (
            await client.post(
                "/api/operators", json={"name": "hana", "role": "admin"},
                headers=bearer(ADMIN_KEY),
            )
        ).json()
        r = await client.delete(
            f"/api/operators/{root.id}", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": root.id}

        # root 已删,改用新管理员的 key 复核名单里确实没有它了
        r2 = await client.get("/api/operators", headers=bearer(created["apikey"]))
        assert r2.status_code == 200, r2.text
        assert all(o["id"] != root.id for o in r2.json()["operators"])


async def test_delete_unknown_id_still_200(tmp_path, monkeypatch):
    """删不存在的运营者仍幂等返 200,新增的管理员保护不改这一语义。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.delete("/api/operators/9999", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": 9999}
