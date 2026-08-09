"""播客合集创建端点:入参校验 + 建 job + 轮询(不起浏览器)。

隔离手法与 tests/test_note_components_rest.py 同源(rest_client 真 lifespan);
浏览器执行体整段 mock 掉——这里验的是 REST 契约与台账串接,不是拟人层。
"""

import app.core.db as db_module
from app.services import browser_jobs_repo, operator_service, podcast_collection
from tests.rest_helpers import bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


async def _account_with_operator(name: str, uid: str, key: str) -> int:
    acc = await seed_account(name, uid, _COOKIES)
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, acc, op_id)
    return acc


def _cover(tmp_path, name: str = "c.png", size: int = 128) -> str:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return str(p)


def _no_browser(monkeypatch):
    """把 spawn_inline 掐掉:建了 job 就停,别真去起 camoufox。"""
    monkeypatch.setattr(browser_jobs_repo, "spawn_inline", lambda job_id, call: None)


async def test_create_collection_enqueues_job(tmp_path, monkeypatch):
    """三项合法 → 202 + job_id;payload 三个字段一字不差地落进台账。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C1", "uC1", "op-coll-ok")
        cover = _cover(tmp_path)
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "心理急救包", "description": "每周一集", "cover": cover},
            headers=bearer("op-coll-ok"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert r.json()["status"] == "queued"
        row = await browser_jobs_repo.get_job(job_id)
        assert row["kind"] == podcast_collection.KIND
        assert row["account_id"] == acc
        assert row["payload"] == {
            "name": "心理急救包", "description": "每周一集", "cover": cover
        }


async def test_name_too_long_422(tmp_path, monkeypatch):
    """名称超 20 字 → 422(实拍确认 input 的 maxlength=20),不建 job。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C2", "uC2", "op-coll-long")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "超" * 21, "cover": _cover(tmp_path)},
            headers=bearer("op-coll-long"),
        )
        assert r.status_code == 422, r.text


async def test_blank_name_422(tmp_path, monkeypatch):
    """纯空白名称 → 422(pydantic 的 min_length 拦不住 "   ")。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C3", "uC3", "op-coll-blank")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "   ", "cover": _cover(tmp_path)},
            headers=bearer("op-coll-blank"),
        )
        assert r.status_code == 422, r.text


async def test_desc_too_long_422(tmp_path, monkeypatch):
    """简介超 100 字 → 422(实拍确认 textarea 的 maxlength=100)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C4", "uC4", "op-coll-desc")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "description": "长" * 101, "cover": _cover(tmp_path)},
            headers=bearer("op-coll-desc"),
        )
        assert r.status_code == 422, r.text


async def test_cover_missing_file_422(tmp_path, monkeypatch):
    """封面路径不存在 → 422,不白烧一次浏览器会话。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C5", "uC5", "op-coll-404")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "cover": str(tmp_path / "没有.png")},
            headers=bearer("op-coll-404"),
        )
        assert r.status_code == 422, r.text
        assert "不存在" in r.text


async def test_cover_bad_ext_422(tmp_path, monkeypatch):
    """封面扩展名不支持(.gif)→ 422;而 .webp **放行**(实测 accept 含 webp)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C6", "uC6", "op-coll-ext")
        bad = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "cover": _cover(tmp_path, "c.gif")},
            headers=bearer("op-coll-ext"),
        )
        assert bad.status_code == 422 and "格式不支持" in bad.text
        ok = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "cover": _cover(tmp_path, "c.webp")},
            headers=bearer("op-coll-ext"),
        )
        assert ok.status_code == 202, ok.text


async def test_cover_oversize_422(tmp_path, monkeypatch):
    """封面超 5MB → 422(压常量验判据,不真造 5MB 文件)。"""
    _no_browser(monkeypatch)
    monkeypatch.setattr(
        "app.publish.policy.PODCAST_COLLECTION_COVER_MAX_BYTES", 16
    )
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C7", "uC7", "op-coll-big")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "cover": _cover(tmp_path)},
            headers=bearer("op-coll-big"),
        )
        assert r.status_code == 422 and "大小" in r.text


async def test_account_not_found_404(tmp_path, monkeypatch):
    """账号不存在 → 404(admin 也不给建)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.post(
            "/api/accounts/9999/podcast-collections",
            json={"name": "X", "cover": _cover(tmp_path)},
            headers=bearer("test-root-admin-key"),
        )
        assert r.status_code == 404, r.text


async def test_poll_returns_result_fields(tmp_path, monkeypatch):
    """轮询把 name / collection_id / confirmed_by 等结果字段透传出来。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C8", "uC8", "op-coll-poll")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "心理急救包", "cover": _cover(tmp_path)},
            headers=bearer("op-coll-poll"),
        )
        job_id = r.json()["job_id"]
        # 终态写入有 C1 守卫:必须先认领(queued→running)才落得进去
        await browser_jobs_repo.claim_job(job_id, "test")
        await browser_jobs_repo.finish_job(
            job_id, "done",
            {"status": "done", "name": "心理急救包", "collection_id": None,
             "confirmed_by": "create_page_closed",
             "name_shown_after_close": True, "name_preexisted": False},
        )
        got = await c.get(f"/api/podcast-collections/{job_id}",
                          headers=bearer("op-coll-poll"))
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["status"] == "done"
        assert body["name"] == "心理急救包"
        assert body["collection_id"] is None
        assert body["confirmed_by"] == "create_page_closed"
        # 透传是**白名单**制:新字段没进名单就静默丢掉,而 manifest 已经把它们写进 returns
        # —— 字段级漂移没有端点级测试兜得住,只能在这里逐个钉死。
        assert body["name_shown_after_close"] is True
        assert body["name_preexisted"] is False


async def test_poll_error_carries_evidence(tmp_path, monkeypatch):
    """失败时**也**下发当场取证 —— 失败的逐项原因比成功时更值钱,不许藏起来。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号C9", "uC9", "op-coll-err")
        r = await c.post(
            f"/api/accounts/{acc}/podcast-collections",
            json={"name": "X", "cover": _cover(tmp_path)},
            headers=bearer("op-coll-err"),
        )
        job_id = r.json()["job_id"]
        await browser_jobs_repo.claim_job(job_id, "test")
        await browser_jobs_repo.finish_job(
            job_id, "error",
            {"error": "create_button_never_enabled: …",
             "observed": {"create_button": {"found": True, "enabled": False}}},
        )
        body = (await c.get(f"/api/podcast-collections/{job_id}",
                            headers=bearer("op-coll-err"))).json()
        assert body["status"] == "error"
        assert body["reason"].startswith("create_button_never_enabled")
        assert body["observed"]["create_button"]["enabled"] is False


async def test_poll_wrong_kind_404(tmp_path, monkeypatch):
    """拿别的 kind 的 job_id 来查 → 404(而不是返回另一个任务的状态)。"""
    _no_browser(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await _account_with_operator("号CA", "uCA", "op-coll-kind")
        other = browser_jobs_repo.enqueue_sync(
            browser_jobs_repo.current_db_path(), "draft_clean", {}, 0, account_id=acc
        )
        got = await c.get(f"/api/podcast-collections/{other}",
                          headers=bearer("op-coll-kind"))
        assert got.status_code == 404, got.text
