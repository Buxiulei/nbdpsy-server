"""组件状态只读查询 REST:契约 + 结果平铺 + 权限。

2026-08-04 运营 P0-1(该清单最核心):引用/合集在正文里零痕迹,applied:true 只是设置
当时的回读;事后无任何程序化自证手段。本端点开更新页只读快照,是唯一不开 App 的验证法。
"""

import pytest

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


def _api_role(monkeypatch):
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _seed_job(job_id, kind, account_id, status, result):
    import json
    from app.models.browser_job import BrowserJob
    import app.core.db as db_module

    async with db_module.async_session() as s:
        s.add(BrowserJob(id=job_id, kind=kind, account_id=account_id, operator_id=1,
                         payload="{}", status=status,
                         result=json.dumps(result, ensure_ascii=False) if result is not None else None))
        await s.commit()


@pytest.mark.asyncio
async def test_post_creates_job(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("读组件号", "uCr1", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-component-reads",
                         headers=bearer(ADMIN_KEY), json={"note_id": "6a" + "0" * 22})
        assert r.status_code == 202
        assert r.json()["status"] == "queued" and r.json()["job_id"]


@pytest.mark.asyncio
async def test_post_empty_note_id_422(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("读组件号2", "uCr2", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-component-reads",
                         headers=bearer(ADMIN_KEY), json={"note_id": ""})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_poll_flattens_snapshot(tmp_path, monkeypatch):
    """done 时平铺快照字段——quote_set/collection_set 判读布尔与入口存在性都要在。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("读组件号3", "uCr3", _COOKIES)
        await _seed_job("ncr-1", "note_components_read", acc, "done", {
            "title": "某笔记", "permission": "公开可见",
            "quote_text": "引用 @NBDpsy 的笔记", "quote_set": True,
            "collection_label": "选择合集", "collection_set": False,
            "collection_entry_present": True,
            "topics": ["亲职化", "原生家庭"], "image_count": 6, "body_head": "正文头",
        })
        body = (await c.get("/api/note-component-reads/ncr-1",
                            headers=bearer(ADMIN_KEY))).json()
        assert body["status"] == "done"
        assert body["quote_set"] is True and body["collection_set"] is False
        assert body["collection_entry_present"] is True
        assert body["topics"] == ["亲职化", "原生家庭"] and body["image_count"] == 6


@pytest.mark.asyncio
async def test_poll_error_passes_error_through(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("读组件号4", "uCr4", _COOKIES)
        await _seed_job("ncr-2", "note_components_read", acc, "error",
                        {"error": "browser_start_failed: boom"})
        body = (await c.get("/api/note-component-reads/ncr-2",
                            headers=bearer(ADMIN_KEY))).json()
        assert body["status"] == "error"


@pytest.mark.asyncio
async def test_kind_is_idempotent():
    """纯只读 → 必须在 _IDEMPOTENT_KINDS 里(僵死可自动重跑,零副作用)。"""
    from app.services.browser_jobs_repo import _IDEMPOTENT_KINDS
    assert "note_components_read" in _IDEMPOTENT_KINDS


def test_account_worker_resolves_kind():
    from app import account_worker
    assert account_worker._resolve_execute("note_components_read") is not None
