"""notes 分组 REST(3 端点)测试:触发导出(202)/ 轮询导出结果 / 读快照与日趋势。

隔离手法与 test_cookie_checks_rest.py / test_publish_rest.py 一致:rest_client 跑真实
lifespan(隔离库);note_export.start_export/get_export 是进程级内存台账 + 后台浏览器导出,
测试 monkeypatch 这两个入口(不真起浏览器),笔记快照/趋势数据直接经 upsert_notes 造。

覆盖(brief 6 用例):
- POST /api/accounts/{id}/note-exports:授权号 → 202 {export_id, status:"running"}。
- POST 越权 → 403;未知账号 → 404(均不触发后台导出)。
- GET /api/note-exports/{id}:轮到 done → 200 附 note_count;不存在 → 404;跨 operator → 403。
- GET /api/accounts/{id}/notes:读最新快照;越权 → 403。
- GET …/notes?title=&publish_time=&trend=daily:返某笔记的日趋势升序序列。
- 防漂移:manifest 声明与实际注册的 3 条新路由双向全等(局部子集校验)。
"""

from datetime import datetime

import app.core.db as db_module
from app.services import operator_service
from app.services.note_metrics_service import upsert_notes
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def _grant(op_id: int, account_id: int) -> None:
    """给 operator 授权某号(幂等)。"""
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, account_id, None)
        await s.commit()


async def _seed_notes(account_id: int, rows: list[dict], snapshot_date: str) -> None:
    """直接经 upsert_notes 造该号笔记快照/当天趋势数据(绕过浏览器导出)。"""
    async with db_module.async_session() as s:
        await upsert_notes(s, account_id, rows, snapshot_date, datetime.utcnow())


# ---------------- POST /api/accounts/{id}/note-exports ----------------


async def test_start_export_202(tmp_path, monkeypatch):
    """授权号 POST → 202 {export_id, status:"running"}(monkeypatch start_export)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        calls = {"args": None}

        def fake_start_export(account_id, cookies):
            calls["args"] = (account_id, cookies)
            return "exp-fixed-id"

        monkeypatch.setattr(
            "app.services.note_export.start_export", fake_start_export
        )

        r = await c.post(
            f"/api/accounts/{acc}/note-exports", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body == {"export_id": "exp-fixed-id", "status": "running"}
        # 已解密 cookie 传给导出器:account_id 正确、cookies 非空
        assert calls["args"][0] == acc
        assert isinstance(calls["args"][1], list) and calls["args"][1]


async def test_start_export_denied_403_and_unknown_404(tmp_path, monkeypatch):
    """越权 operator → 403;未知账号(admin)→ 404;两者都不触发后台导出。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号B", "uB", _COOKIES)
        called = {"n": 0}

        def fake_start_export(account_id, cookies):
            called["n"] += 1
            return "exp-x"

        monkeypatch.setattr(
            "app.services.note_export.start_export", fake_start_export
        )

        # 越权:未授权任何号的 operator
        op_key = "op-notes-denied-01"
        await make_operator(op_key)
        r = await c.post(
            f"/api/accounts/{acc}/note-exports", headers=bearer(op_key)
        )
        assert r.status_code == 403
        assert called["n"] == 0  # 越权在起导出前就被拦

        # 未知账号:admin 越过 access 校验,落到账号存在性检查 → 404
        r2 = await c.post(
            "/api/accounts/999999/note-exports", headers=bearer(ADMIN_KEY)
        )
        assert r2.status_code == 404
        assert called["n"] == 0  # 账号不存在也不触发导出


# ---------------- GET /api/note-exports/{export_id} ----------------


async def test_get_export_poll(tmp_path, monkeypatch):
    """轮到 done → 200 附 note_count;不存在 → 404;跨 operator(无该号 access)→ 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号C", "uC", _COOKIES)

        # 进程级内存台账的假实现:按 export_id 查
        fake_registry = {
            "exp-done": {
                "status": "done",
                "account_id": acc,
                "note_count": 7,
                "reason": None,
                "created_at": datetime.utcnow(),
            }
        }

        def fake_get_export(export_id):
            return fake_registry.get(export_id)

        monkeypatch.setattr(
            "app.services.note_export.get_export", fake_get_export
        )

        # done:200 + status/note_count
        r = await c.get("/api/note-exports/exp-done", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "done"
        assert body["note_count"] == 7
        assert "reason" not in body  # reason 为 None 不带键

        # 不存在的 export_id → 404
        r2 = await c.get("/api/note-exports/nope", headers=bearer(ADMIN_KEY))
        assert r2.status_code == 404

        # 跨 operator:B 无 acc 的 access → 403
        op_b_key = "op-notes-poll-b-01"
        await make_operator(op_b_key)  # 未授权 acc
        r3 = await c.get("/api/note-exports/exp-done", headers=bearer(op_b_key))
        assert r3.status_code == 403


# ---------------- GET /api/accounts/{id}/notes ----------------


async def test_list_notes(tmp_path, monkeypatch):
    """seed 快照 → 授权 operator GET 读到;无该号 access 的 operator → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号D", "uD", _COOKIES)
        await _seed_notes(
            acc,
            [
                {"title": "笔记一", "publish_time": "2026年05月22日10时", "likes": 12},
                {"title": "笔记二", "publish_time": "2026年05月23日11时", "likes": 3},
            ],
            snapshot_date="2026-07-13",
        )

        op_key = "op-notes-list-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.get(f"/api/accounts/{acc}/notes", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        notes = r.json()["notes"]
        assert {n["title"] for n in notes} == {"笔记一", "笔记二"}
        by_title = {n["title"]: n for n in notes}
        assert by_title["笔记一"]["likes"] == 12
        assert by_title["笔记一"]["account_id"] == acc

        # 越权:未授权的 operator → 403(service 层 RBAC)
        other_key = "op-notes-list-other-01"
        await make_operator(other_key)
        r2 = await c.get(f"/api/accounts/{acc}/notes", headers=bearer(other_key))
        assert r2.status_code == 403


async def test_notes_trend(tmp_path, monkeypatch):
    """?title=&publish_time=&trend=daily → 返某笔记的每日趋势升序序列。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号E", "uE", _COOKIES)
        title = "趋势笔记"
        publish_time = "2026年05月22日10时"
        # 两天导出 → daily 两行(跨天加行)
        await _seed_notes(
            acc, [{"title": title, "publish_time": publish_time, "likes": 10}],
            snapshot_date="2026-07-12",
        )
        await _seed_notes(
            acc, [{"title": title, "publish_time": publish_time, "likes": 25}],
            snapshot_date="2026-07-13",
        )

        r = await c.get(
            f"/api/accounts/{acc}/notes",
            params={"title": title, "publish_time": publish_time, "trend": "daily"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "trend" in body and "notes" not in body
        trend = body["trend"]
        assert [d["snapshot_date"] for d in trend] == ["2026-07-12", "2026-07-13"]
        assert [d["likes"] for d in trend] == [10, 25]


async def test_notes_ship_field_meta(tmp_path, monkeypatch):
    """口径随数据下发:两种形态都带 meta.field_meta,且 notes/trend 键结构不变。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号F", "uF", _COOKIES)
        title = "带口径的笔记"
        publish_time = "2026年05月22日10时"
        await _seed_notes(
            acc, [{"title": title, "publish_time": publish_time, "likes": 10}],
            snapshot_date="2026-07-13",
        )

        r = await c.get(f"/api/accounts/{acc}/notes", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["notes"][0]["title"] == title  # 原有键结构不动
        fm = body["meta"]["field_meta"]
        # 裸数字不再没口径:窗口不同的两个代表字段都能查到
        assert fm["views"]["window"] == "T"
        assert fm["follows"]["window"] == "T-1"
        assert body["meta"]["field_notes"]

        # trend 形态同样带 meta
        r2 = await c.get(
            f"/api/accounts/{acc}/notes",
            params={"title": title, "publish_time": publish_time, "trend": "daily"},
            headers=bearer(ADMIN_KEY),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["meta"]["field_meta"]["views"]["window"] == "T"


# ---------------- 防漂移(局部子集) ----------------


def test_manifest_covers_new_routes():
    """notes 3 端点在 manifest 与实际注册路由里双向全等(全局防漂移在 test_manifest.py)。"""
    from app.http import ALL_MANIFEST_ENTRIES
    from app.server import create_app

    new_routes = {
        ("POST", "/api/accounts/{account_id}/note-exports"),
        ("GET", "/api/note-exports/{export_id}"),
        ("GET", "/api/accounts/{account_id}/notes"),
    }
    _HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    app = create_app()
    actual = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in ops
        if method.upper() in _HTTP_METHODS
    }
    declared = {(e["method"], e["path"]) for e in ALL_MANIFEST_ENTRIES}
    # 3 条新路由既在实际注册里,也在 manifest 声明里(双向)
    assert new_routes <= actual, f"未注册: {sorted(new_routes - actual)}"
    assert new_routes <= declared, f"manifest 漏写: {sorted(new_routes - declared)}"
