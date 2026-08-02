"""admin_only 端点全量权限扫描:manifest 里每个 admin_only 端点,operator key 调必须 403。

这条测试是给未来的防线——新增 admin_only 端点忘了写 `require_admin`,或忘了在下面
配 body,都会直接红。两条设计约束不可放宽:

- **必须带合法 body**。POST/PUT/PATCH 不带 body 会先撞 FastAPI 的 422(body 校验在
  handler 之前),而 422 **证明不了守卫存在**——曾有消费方据此误判 admin-default
  没守卫。所以断言**只接受 403**,写成 `in (403, 422)` 等于扔掉判别力;反过来说,
  只收 403 也就自动逼着下面的 body 必须合法(body 不合法 → 422 → 测试红)。
- **映射键集合与 manifest 的 admin_only 集合必须全等**(见完备性断言)。新增端点
  不配 body 就红,这才是"以后也能兜住"。

与 tests/test_admin_rest.py 的手写 8 端点清单互补:那边是 admin 分组自己的行为测试,
这里是跨模块、由 manifest 驱动的全量扫描。
"""

import re

from app.http import ALL_MANIFEST_ENTRIES
from tests.rest_helpers import bearer, make_operator, rest_client

# (method, manifest 里的原始 path) → 最小合法 body(GET/DELETE 无 body 用 None)。
# path 里的 {xxx} 占位符统一替换成整数 1;守卫是 handler 首行,先于查库,id 真假不影响判定。
_ADMIN_ONLY_BODIES: dict[tuple[str, str], dict | None] = {
    ("POST", "/api/operators"): {"name": "扫描-新建"},
    ("GET", "/api/operators"): None,
    ("PATCH", "/api/operators/{operator_id}"): {"enabled": False},
    ("DELETE", "/api/operators/{operator_id}"): None,
    ("POST", "/api/operators/{operator_id}/rotate-apikey"): None,
    ("POST", "/api/operators/{operator_id}/grants"): {"xhs_account_id": 1},
    ("DELETE", "/api/operators/{operator_id}/grants/{xhs_account_id}"): None,
    ("GET", "/api/operators/{operator_id}/grants"): None,
    ("PUT", "/api/style-profile/admin-default"): {
        "profile": {"visual": {"text_color": "#222"}},
        "note": "扫描用,不应写入",
    },
    ("DELETE", "/api/content-archive/{archive_id}"): None,
    ("POST", "/api/interaction-backfills"): {"scope": "all"},
}


def _admin_only_endpoints() -> set[tuple[str, str]]:
    """从 manifest 聚合表取全部 admin_only 端点。"""
    return {
        (e["method"], e["path"]) for e in ALL_MANIFEST_ENTRIES if e["admin_only"]
    }


def test_body_map_covers_every_admin_only_endpoint():
    """完备性:body 映射的键集合与 manifest 的 admin_only 集合全等。

    新增 admin_only 端点却没在 _ADMIN_ONLY_BODIES 里配 body,这里立刻红——
    否则下面的扫描会静默漏掉它,防线就形同虚设。
    """
    declared = _admin_only_endpoints()
    mapped = set(_ADMIN_ONLY_BODIES)
    assert mapped == declared, (
        f"未配 body 的 admin_only 端点: {sorted(declared - mapped)}; "
        f"已不是 admin_only 却还留在映射里: {sorted(mapped - declared)}"
    )


async def test_all_admin_only_endpoints_reject_operator(tmp_path, monkeypatch):
    """operator 角色的 key 打遍所有 admin_only 端点,必须全部 403(且是管理员守卫拦的)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "sweep-operator-key"
        await make_operator(op_key)  # role="operator"
        for (method, path), body in sorted(_ADMIN_ONLY_BODIES.items()):
            url = re.sub(r"\{[^}]+\}", "1", path)
            r = await client.request(method, url, json=body, headers=bearer(op_key))
            assert r.status_code == 403, (
                f"{method} {path} 应 403,实得 {r.status_code}:{r.text}"
                "(422 说明 body 不合法,须修上面的映射;200/404 说明守卫缺失,是真漏洞)"
            )
            # 区分"管理员守卫拦下"与"账号越权守卫拦下"——两者都是 403,只有前者才算数
            assert "需要管理员权限" in r.json()["error"], f"{method} {path} 403 原因不对:{r.text}"
