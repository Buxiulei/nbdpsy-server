"""笔记合集创建端点:入参校验 + 建 job + 轮询(不起浏览器)。

隔离手法与 tests/test_podcast_collection_rest.py 同源(rest_client 真 lifespan);
浏览器执行体整段掐掉——这里验的是 REST 契约与台账串接,不是拟人层。
"""

import app.core.db as db_module
from app.services import browser_jobs_repo, note_collection_create, operator_service
from tests.rest_helpers import bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def _account_with_operator(name: str, uid: str, key: str) -> int:
    acc = await seed_account(name, uid, _COOKIES)
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, acc, op_id)
    return acc


def _no_browser(monkeypatch):
    """把 spawn_inline 掐掉:建了 job 就停,别真去起 camoufox。"""
    monkeypatch.setattr(browser_jobs_repo, "spawn_inline", lambda job_id, call: None)


async def test_create_enqueues_job(tmp_path, monkeypatch):
    """三项合法 → 202 + job_id;payload 三个字段一字不差地落进台账。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N1", "uN1", "op-nc-ok")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "读懂复杂性创伤", "description": "看懂它如何影响日常",
                  "carrier_note_id": "n-carrier"},
            headers=bearer("op-nc-ok"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert r.json()["status"] == "queued"
        row = await browser_jobs_repo.get_job(job_id)
        assert row["kind"] == note_collection_create.KIND
        assert row["account_id"] == acc
        assert row["payload"] == {
            "name": "读懂复杂性创伤", "description": "看懂它如何影响日常",
            "carrier_note_id": "n-carrier",
        }


async def test_cover_is_explicitly_rejected(tmp_path, monkeypatch):
    """传 cover → 422 且**说清为什么** —— 笔记合集平台没有封面字段(2026-08-09 实拍)。

    静默忽略是更坏的选择:调用方会以为封面设上了,而平台上那个合集根本没有封面。
    """
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N2", "uN2", "op-nc-cover")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X", "carrier_note_id": "n1",
                  "cover": "/data/uploads/kepu-cover.png"},
            headers=bearer("op-nc-cover"),
        )
        assert r.status_code == 422, r.text
        assert "无封面字段" in r.text
        assert "播客合集" in r.text, "要指路到真正有封面的那个端点"


async def test_name_too_long_422(tmp_path, monkeypatch):
    """名称超 20 字 → 422(实拍确认输入框 0/20 计数),不建 job。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N3", "uN3", "op-nc-long")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "超" * 21, "carrier_note_id": "n1"},
            headers=bearer("op-nc-long"),
        )
        assert r.status_code == 422, r.text


async def test_desc_limit_is_50_not_100(tmp_path, monkeypatch):
    """简介 50 字放行、51 字 422 —— 上限与播客合集(100)**不同**,别照抄。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N4", "uN4", "op-nc-desc")
        ok = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X", "description": "长" * 50, "carrier_note_id": "n1"},
            headers=bearer("op-nc-desc"),
        )
        assert ok.status_code == 202, ok.text
        bad = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X", "description": "长" * 51, "carrier_note_id": "n1"},
            headers=bearer("op-nc-desc"),
        )
        assert bad.status_code == 422, bad.text


async def test_blank_name_or_carrier_422(tmp_path, monkeypatch):
    """纯空白名称 / 纯空白载体笔记 → 422(pydantic 的 min_length 拦不住 "   ")。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N5", "uN5", "op-nc-blank")
        blank_name = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "   ", "carrier_note_id": "n1"},
            headers=bearer("op-nc-blank"),
        )
        assert blank_name.status_code == 422, blank_name.text
        blank_carrier = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X", "carrier_note_id": "  "},
            headers=bearer("op-nc-blank"),
        )
        assert blank_carrier.status_code == 422, blank_carrier.text


async def test_carrier_note_id_is_required(tmp_path, monkeypatch):
    """不传载体笔记 → 422:创建入口只在笔记编辑器里,没有载体就打不开编辑器。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N6", "uN6", "op-nc-nocarrier")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X"},
            headers=bearer("op-nc-nocarrier"),
        )
        assert r.status_code == 422, r.text


async def test_account_not_found_404(tmp_path, monkeypatch):
    """账号不存在 → 404(admin 也不给建)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.post(
            "/api/accounts/9999/note-collections",
            json={"name": "X", "carrier_note_id": "n1"},
            headers=bearer("test-root-admin-key"),
        )
        assert r.status_code == 404, r.text


async def test_poll_returns_result_fields(tmp_path, monkeypatch):
    """轮询把结果字段透传出来(白名单制,逐个钉死 —— 字段级漂移没别的东西兜得住)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N7", "uN7", "op-nc-poll")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "读懂复杂性创伤", "carrier_note_id": "n1"},
            headers=bearer("op-nc-poll"),
        )
        job_id = r.json()["job_id"]
        # 终态写入有 C1 守卫:必须先认领(queued→running)才落得进去
        await browser_jobs_repo.claim_job(job_id, "test")
        await browser_jobs_repo.finish_job(
            job_id, "done",
            {"status": "done", "name": "读懂复杂性创伤", "collection_id": "cid9",
             "confirmed_by": "modal_closed_and_in_fresh_list",
             "name_preexisted": False, "joined_carrier": 0, "modal_closed": True,
             "carrier_collection_label": None,
             "created_api_capture": [{"url": "…/collection/create", "status": 200}]},
        )
        body = (await c.get(f"/api/note-collections/{job_id}",
                            headers=bearer("op-nc-poll"))).json()
        assert body["status"] == "done"
        assert body["collection_id"] == "cid9"
        assert body["confirmed_by"] == "modal_closed_and_in_fresh_list"
        assert body["name_preexisted"] is False
        assert body["joined_carrier"] == 0
        assert body["modal_closed"] is True
        assert body["created_api_capture"][0]["status"] == 200


async def test_poll_duplicate_error_carries_existing_id(tmp_path, monkeypatch):
    """同名撞车时轮询必须把**现有那条的 id** 交出去 —— 调用方直接拿它挂笔记,不用重建。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N8", "uN8", "op-nc-dup")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "读懂复杂性创伤", "carrier_note_id": "n1"},
            headers=bearer("op-nc-dup"),
        )
        job_id = r.json()["job_id"]
        await browser_jobs_repo.claim_job(job_id, "test")
        await browser_jobs_repo.finish_job(
            job_id, "error",
            {"error": "collection_name_already_exists: 该号已有同名合集",
             "collection_id": "c1", "note_num": 7, "name_preexisted": True},
        )
        body = (await c.get(f"/api/note-collections/{job_id}",
                            headers=bearer("op-nc-dup"))).json()
        assert body["status"] == "error"
        assert body["reason"].startswith("collection_name_already_exists")
        assert body["collection_id"] == "c1" and body["note_num"] == 7


async def test_poll_error_carries_evidence(tmp_path, monkeypatch):
    """失败时**也**下发当场取证 —— 失败的逐项原因比成功时更值钱,不许藏起来。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号N9", "uN9", "op-nc-err")
        r = await c.post(
            f"/api/accounts/{acc}/note-collections",
            json={"name": "X", "carrier_note_id": "n1"},
            headers=bearer("op-nc-err"),
        )
        job_id = r.json()["job_id"]
        await browser_jobs_repo.claim_job(job_id, "test")
        await browser_jobs_repo.finish_job(
            job_id, "error",
            {"error": "create_modal_still_open: …", "modal_closed": False,
             "create_submit_state": {"found": True, "enabled": False,
                                     "cls": "d-button disabled"},
             "modal_html": "<div class='d-modal'>创建合集</div>",
             "observed": {"create_modal_present": True}},
        )
        body = (await c.get(f"/api/note-collections/{job_id}",
                            headers=bearer("op-nc-err"))).json()
        assert body["status"] == "error"
        assert body["create_submit_state"]["enabled"] is False
        assert body["modal_html"].startswith("<div")
        assert body["observed"]["create_modal_present"] is True


async def test_poll_wrong_kind_404(tmp_path, monkeypatch):
    """拿别的 kind 的 job_id 来查 → 404(而不是返回另一个任务的状态)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号NA", "uNA", "op-nc-kind")
        other = browser_jobs_repo.enqueue_sync(
            browser_jobs_repo.current_db_path(), "draft_clean", {}, 0, account_id=acc
        )
        got = await c.get(f"/api/note-collections/{other}",
                          headers=bearer("op-nc-kind"))
        assert got.status_code == 404, got.text
