"""note-comments 分组 REST(2 端点)测试:触发单篇评论(202)+ 轮询结果。

隔离手法同 test_published_notes_rest.py:rest_client 真 lifespan(隔离库)+
``NBDPSY_ROLE=api``(只登记台账不派执行,不起浏览器)。

覆盖:
- 鉴权缺失 → 401;越权 operator → 403;未知账号 → 404(都不登记任务)。
- 请求体校验:publisher_user_id / title / **text 均必填非空**(评论文案是本端点唯一
  的动作,空文案没有可执行的动作);text 超 200 字 → 422。
- 登记内容:kind=note_comment、payload 三件套齐全、记到发起人名下(配额依据)。
- 轮询:queued 可查;done 带 note_url + commented;error 带 reason 且仍给 note_url
  (非幂等链路,人工核对要用);僵死 unknown 标记 → unknown;跨 kind 的 id → 404。
- note_comment 非幂等:不得进 _IDEMPOTENT_KINDS(重复执行会发出重复评论)。
- 防漂移:2 条新路由在 manifest 与实际注册路由里双向全等。
"""

import json

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.services import browser_jobs_repo, operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

_NEW_ROUTES = {
    ("POST", "/api/accounts/{account_id}/note-comments"),
    ("GET", "/api/note-comments/{comment_id}"),
}


def _api_role(monkeypatch) -> None:
    """置 NBDPSY_ROLE=api:start_comment 只登记台账,不在本进程起浏览器。"""
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


# ---------------- 非幂等纪律 ----------------


def test_note_comment_is_not_idempotent_kind():
    """note_comment 绝不能进 _IDEMPOTENT_KINDS:重复执行会发出**重复评论**。

    点赞是开关(重跑最多来回切),评论是追加——僵死自动重跑会在别人笔记下留下两条
    一模一样的评论,轻则刷屏重则触发风控。
    """
    assert "note_comment" not in browser_jobs_repo._IDEMPOTENT_KINDS


# ---------------- POST /api/accounts/{id}/note-comments ----------------


async def test_missing_apikey_401(tmp_path, monkeypatch):
    """无 apikey → 401。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论鉴权号", "uCmAuth", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/note-comments",
            json={"publisher_user_id": "pub1", "title": "标题", "text": "好文"},
        )
        assert r.status_code == 401


async def test_start_comment_202_and_payload(tmp_path, monkeypatch):
    """202 回 comment_id;payload 带发布者 user_id / 标题 / 文案,记发起人名下。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论号", "uCm", _COOKIES)
        op_key = "op-cm-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-comments",
            json={
                "publisher_user_id": "5f8a1b2c",
                "title": "目标笔记标题",
                "text": "这条讲得真好,收藏了",
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["comment_id"]

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, body["comment_id"])
        assert row.kind == "note_comment"
        # account_id 是**发评论的号**(鉴权对象),不是被评论笔记的作者
        assert row.account_id == acc and row.operator_id == op_id
        payload = json.loads(row.payload)
        assert payload["publisher_user_id"] == "5f8a1b2c"
        assert payload["title"] == "目标笔记标题"
        assert payload["text"] == "这条讲得真好,收藏了"

        poll = await c.get(
            f"/api/note-comments/{body['comment_id']}", headers=bearer(op_key)
        )
        assert poll.status_code == 200
        # queue 段随排队态一起下发(排队可见性):这里只钉形状,细节见 tests/test_queue_status*.py
        body_poll = poll.json()
        assert body_poll["status"] == "queued"
        assert body_poll["queue"]["position"] == 1


async def test_comment_request_validation(tmp_path, monkeypatch):
    """三个字段全必填非空;text 超 200 字 → 422;刚好 200 字放行。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论校验号", "uCmValid", _COOKIES)
        url = f"/api/accounts/{acc}/note-comments"

        bad_bodies = [
            {"publisher_user_id": "", "title": "标题", "text": "好文"},
            {"publisher_user_id": "pub", "title": "", "text": "好文"},
            # 文案必填:本端点唯一的动作就是评论,空文案没有可执行的动作
            {"publisher_user_id": "pub", "title": "标题", "text": ""},
            {"publisher_user_id": "pub", "title": "标题"},
            {"title": "缺发布者", "text": "好文"},
            {"publisher_user_id": "pub", "title": "标题", "text": "字" * 201},
        ]
        for body in bad_bodies:
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, f"未被拒: {body!r} -> {r.text}"

        r = await c.post(
            url,
            json={"publisher_user_id": "pub", "title": "标题", "text": "字" * 200},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text


async def test_start_comment_denied_and_unknown(tmp_path, monkeypatch):
    """越权 → 403;未知账号 → 404;两者都不登记任务。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论越权号", "uCmDeny", _COOKIES)
        body = {"publisher_user_id": "pub", "title": "标题", "text": "好文"}
        other_key = "op-cm-denied-01"
        await make_operator(other_key)

        assert (await c.post(
            f"/api/accounts/{acc}/note-comments", json=body, headers=bearer(other_key)
        )).status_code == 403
        assert (await c.post(
            "/api/accounts/999999/note-comments", json=body, headers=bearer(ADMIN_KEY)
        )).status_code == 404

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            count = await s.scalar(select(func.count()).select_from(BrowserJob))
        assert count == 0


# ---------------- GET /api/note-comments/{id} ----------------


async def test_poll_done(tmp_path, monkeypatch):
    """done:note_url + commented:true。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论终态号", "uCmDone", _COOKIES)
        await _seed_job(
            "cm-done-1", "note_comment", acc, "done",
            {"note_url": "https://www.xiaohongshu.com/explore/abc", "commented": True},
        )
        body = (await c.get(
            "/api/note-comments/cm-done-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert body["status"] == "done"
        assert body["commented"] is True
        assert body["note_url"].endswith("/abc")


async def test_poll_error_keeps_note_url_for_manual_check(tmp_path, monkeypatch):
    """error 时仍下发 note_url:非幂等链路,重试前必须人工去评论区核对。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论失败号", "uCmErr", _COOKIES)
        await _seed_job(
            "cm-err-1", "note_comment", acc, "error",
            {
                "note_url": "https://www.xiaohongshu.com/explore/zzz",
                "error": "comment_unverified: 发送后未复核到评论",
            },
        )
        body = (await c.get(
            "/api/note-comments/cm-err-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert body["status"] == "error"
        assert "comment_unverified" in body["reason"]
        # 这条恰恰是"可能已经发出去了"的情况,链接必须交出来
        assert body["note_url"].endswith("/zzz")
        assert "commented" not in body


async def test_poll_unknown_and_cross_kind(tmp_path, monkeypatch):
    """僵死 unknown 标记 → unknown;跨 kind / 不存在的 id → 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论未知号", "uCmUnk", _COOKIES)
        await _seed_job(
            "cm-unknown-1", "note_comment", acc, "error",
            {"error": "执行进程中断,任务结果未知(unknown):请人工核对", "unknown": True},
        )
        await _seed_job("mx-1", "matrix_interact", acc, "done", {"actions": {}})

        unk = (await c.get(
            "/api/note-comments/cm-unknown-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert unk["status"] == "unknown"  # 绝不冒充普通失败诱导重试

        assert (await c.get(
            "/api/note-comments/mx-1", headers=bearer(ADMIN_KEY)
        )).status_code == 404
        assert (await c.get(
            "/api/note-comments/nope", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_poll_denied_403(tmp_path, monkeypatch):
    """轮询按台账行的 account_id 收窄:无该号授权 → 403。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("评论轮询越权号", "uCmPollDeny", _COOKIES)
        await _seed_job("cm-other-1", "note_comment", acc, "done", {"commented": True})
        other_key = "op-cm-poll-denied-01"
        await make_operator(other_key)
        r = await c.get("/api/note-comments/cm-other-1", headers=bearer(other_key))
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
