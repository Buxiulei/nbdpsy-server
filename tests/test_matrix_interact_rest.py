"""matrix-interact 分组 REST(2 端点)测试:手工触发互动(202)+ 轮询结果。

隔离手法同 test_published_notes_rest.py:rest_client 真 lifespan(隔离库)+
``NBDPSY_ROLE=api``(只登记台账不派执行,不起浏览器)。

覆盖:
- 鉴权缺失 → 401;越权 operator → 403;未知账号 → 404(都不登记任务)。
- 请求体校验:publisher_user_id/title 必填非空、comment 超长 → 422。
- 登记内容:payload 三件套齐全,operator_id 记到发起人名下(配额依据)。
- 轮询:queued 可查;done 带 note_url + 逐动作 actions(含 not_requested / 单动作 error);
  error → reason;僵死 unknown 标记 → unknown;跨 kind 的 id → 404。
- 防漂移:2 条新路由在 manifest 与实际注册路由里双向全等。
"""

import json

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

_NEW_ROUTES = {
    ("POST", "/api/accounts/{account_id}/matrix-interactions"),
    ("GET", "/api/matrix-interactions/{interaction_id}"),
}


def _api_role(monkeypatch) -> None:
    """置 NBDPSY_ROLE=api:start_interact 只登记台账,不在本进程起浏览器。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _grant(op_id: int, account_id: int) -> None:
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, account_id, None)
        await s.commit()


async def _seed_job(job_id: str, kind: str, account_id: int, status: str, result) -> None:
    """直接经 ORM 造一条终态 browser_jobs 行(模拟 worker 已跑完)。"""
    async with db_module.async_session() as s:
        s.add(
            BrowserJob(
                id=job_id,
                kind=kind,
                account_id=account_id,
                operator_id=0,
                payload="{}",
                status=status,
                result=json.dumps(result, ensure_ascii=False) if result else None,
            )
        )
        await s.commit()


# ---------------- POST /api/accounts/{id}/matrix-interactions ----------------


async def test_missing_apikey_401(tmp_path, monkeypatch):
    """无 apikey → 401。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动鉴权号", "uMxAuth", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/matrix-interactions",
            json={"publisher_user_id": "pub1", "title": "标题"},
        )
        assert r.status_code == 401


async def test_start_interaction_202_and_payload(tmp_path, monkeypatch):
    """202 回 interaction_id;payload 带发布者 user_id / 标题 / 评论文案,记发起人名下。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动号", "uMx", _COOKIES)
        op_key = "op-mx-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/matrix-interactions",
            json={
                "publisher_user_id": "5f8a1b2c",
                "title": "目标笔记标题",
                "comment": "这条讲得真好,收藏了",
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["interaction_id"]

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, body["interaction_id"])
        assert row.kind == "matrix_interact"
        # account_id 是**去互动的号**(鉴权对象),不是发布者
        assert row.account_id == acc and row.operator_id == op_id
        payload = json.loads(row.payload)
        assert payload["publisher_user_id"] == "5f8a1b2c"
        assert payload["title"] == "目标笔记标题"
        assert payload["comment"] == "这条讲得真好,收藏了"

        # 轮询立刻可查到 queued
        poll = await c.get(
            f"/api/matrix-interactions/{body['interaction_id']}", headers=bearer(op_key)
        )
        assert poll.status_code == 200
        assert poll.json() == {"status": "queued"}


async def test_comment_optional_defaults_empty(tmp_path, monkeypatch):
    """comment 可省略,默认空串——效果即"只点赞收藏"(评论那步记 not_requested)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("无评论号", "uMxNoC", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/matrix-interactions",
            json={"publisher_user_id": "pub9", "title": "只赞不评"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, r.json()["interaction_id"])
        assert json.loads(row.payload)["comment"] == ""


async def test_interaction_request_validation(tmp_path, monkeypatch):
    """publisher_user_id / title 必填非空;comment 超 200 字 → 422。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动校验号", "uMxValid", _COOKIES)
        url = f"/api/accounts/{acc}/matrix-interactions"

        bad_bodies = [
            {"publisher_user_id": "", "title": "标题"},
            {"publisher_user_id": "pub", "title": ""},
            {"title": "缺发布者"},
            {"publisher_user_id": "pub"},
            {"publisher_user_id": "pub", "title": "标题", "comment": "字" * 201},
        ]
        for body in bad_bodies:
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, f"未被拒: {body!r} -> {r.text}"

        # 刚好 200 字放行(上限是闭区间)
        r = await c.post(
            url,
            json={"publisher_user_id": "pub", "title": "标题", "comment": "字" * 200},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text


async def test_start_interaction_denied_and_unknown(tmp_path, monkeypatch):
    """越权 → 403;未知账号 → 404;两者都不登记任务。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动越权号", "uMxDeny", _COOKIES)
        body = {"publisher_user_id": "pub", "title": "标题"}
        other_key = "op-mx-denied-01"
        await make_operator(other_key)

        assert (await c.post(
            f"/api/accounts/{acc}/matrix-interactions",
            json=body, headers=bearer(other_key),
        )).status_code == 403
        assert (await c.post(
            "/api/accounts/999999/matrix-interactions",
            json=body, headers=bearer(ADMIN_KEY),
        )).status_code == 404

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            count = await s.scalar(select(func.count()).select_from(BrowserJob))
        assert count == 0


# ---------------- GET /api/matrix-interactions/{id} ----------------


async def test_poll_done_actions(tmp_path, monkeypatch):
    """done:note_url + 逐动作 actions 原样下发(含 not_requested 与单动作 error)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动终态号", "uMxDone", _COOKIES)
        await _seed_job(
            "mx-done-1", "matrix_interact", acc, "done",
            {
                "note_url": "https://www.xiaohongshu.com/explore/abc",
                "actions": {
                    "like": {"status": "done"},
                    "collect": {"status": "skipped", "reason": "已收藏"},
                    "comment": {"status": "not_requested", "reason": "无评论文案"},
                },
            },
        )
        body = (await c.get(
            "/api/matrix-interactions/mx-done-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert body["status"] == "done"
        assert body["note_url"].endswith("/abc")
        # 动作级状态四种语义各自保留,不被压平成一个整体成败
        assert body["actions"]["like"]["status"] == "done"
        assert body["actions"]["collect"]["status"] == "skipped"
        assert body["actions"]["comment"]["status"] == "not_requested"


async def test_poll_error_unknown_and_cross_kind(tmp_path, monkeypatch):
    """error → reason;僵死 unknown 标记 → unknown;跨 kind / 不存在的 id → 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动失败号", "uMxErr", _COOKIES)
        await _seed_job(
            "mx-err-1", "matrix_interact", acc, "error", {"error": "note_not_found"}
        )
        await _seed_job(
            "mx-unknown-1", "matrix_interact", acc, "error",
            {"error": "执行进程中断,任务结果未知(unknown):请人工核对", "unknown": True},
        )
        await _seed_job("sync-1", "note_ledger_sync", acc, "done", {"note_count": 1})

        err = (await c.get(
            "/api/matrix-interactions/mx-err-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert err["status"] == "error" and err["reason"] == "note_not_found"

        unk = (await c.get(
            "/api/matrix-interactions/mx-unknown-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert unk["status"] == "unknown"  # 非幂等,绝不冒充普通失败诱导重试

        # 别的 kind 的 id 在本端点必须 404,不能返回那条任务的状态
        assert (await c.get(
            "/api/matrix-interactions/sync-1", headers=bearer(ADMIN_KEY)
        )).status_code == 404
        assert (await c.get(
            "/api/matrix-interactions/nope", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_poll_denied_403(tmp_path, monkeypatch):
    """轮询按台账行的 account_id 收窄:无该号授权 → 403。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("互动轮询越权号", "uMxPollDeny", _COOKIES)
        await _seed_job("mx-other-1", "matrix_interact", acc, "done", {"actions": {}})
        other_key = "op-mx-poll-denied-01"
        await make_operator(other_key)
        r = await c.get(
            "/api/matrix-interactions/mx-other-1", headers=bearer(other_key)
        )
        assert r.status_code == 403


# ---------------- 防漂移(局部子集) ----------------


def test_manifest_covers_new_routes():
    """2 条新路由在 manifest 与实际注册路由里双向全等。"""
    from app.http import ALL_MANIFEST_ENTRIES
    from app.server import create_app

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
    assert _NEW_ROUTES <= actual, f"未注册: {sorted(_NEW_ROUTES - actual)}"
    assert _NEW_ROUTES <= declared, f"manifest 漏写: {sorted(_NEW_ROUTES - declared)}"
