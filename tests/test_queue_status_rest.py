"""排队可见性在 REST 面上的落地:queue 段真的出现在轮询响应里 + manifest 契约防漂移。

单测 queue_status 本身在 tests/test_queue_status.py;这里管的是"端点有没有把它端出去"
——2026-08-07 运营看不到排队信息的直接原因就是端点不给,判据算得再对也白搭。
"""

import json
from datetime import datetime, timedelta

import pytest

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

# 全部带 queue 段的轮询端点(与 manifest 声明双向对齐,见下面的防漂移测试)
_POLLING_PATHS = {
    "/api/cookie-checks/{check_id}",
    "/api/note-exports/{export_id}",
    "/api/note-deletions/{deletion_id}",
    "/api/publish-jobs/{job_id}",
    "/api/note-ledger-syncs/{sync_id}",
    "/api/note-purpose-backfills/{backfill_id}",
    "/api/note-visibility-changes/{change_id}",
    "/api/note-comments/{comment_id}",
    "/api/note-component-reads/{job_id}",
    "/api/note-components/{job_id}",
    "/api/collection-batches/{job_id}",
    "/api/note-extracts/{job_id}",
    "/api/interaction-backfills/{job_id}",
    "/api/podcast-collections/{job_id}",
}


def _api_role(monkeypatch):
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _seed_browser(job_id, kind, account_id, status="queued", *, created=None,
                        operator_id=1, payload=None, updated=None):
    now = datetime.utcnow()
    async with db_module.async_session() as s:
        s.add(
            BrowserJob(
                id=job_id, kind=kind, account_id=account_id, operator_id=operator_id,
                payload=json.dumps(payload or {}, ensure_ascii=False), status=status,
                created_at=created or now, updated_at=updated or created or now,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_browser_poll_carries_queue_section(tmp_path, monkeypatch):
    """走 base_view 的轮询端点(这里取 note-component-reads)带 queue 段与位次。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("排队号", "uQ1", _COOKIES)
        base = datetime.utcnow() - timedelta(minutes=5)
        await _seed_browser("q-first", "note_components_read", acc, created=base)
        await _seed_browser(
            "q-mine", "note_components_read", acc, created=base + timedelta(seconds=1)
        )
        r = await c.get("/api/note-component-reads/q-mine", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "queued"
        assert body["queue"]["position"] == 2
        assert body["queue"]["ahead"] == 1
        assert body["queue"]["account_queue_depth"] == 2


@pytest.mark.asyncio
async def test_browser_poll_queue_null_on_terminal(tmp_path, monkeypatch):
    """终态 queue 为 null(字段仍在,调用方不必判 key 存不存在)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("终态号", "uQ2", _COOKIES)
        async with db_module.async_session() as s:
            s.add(
                BrowserJob(
                    id="q-done", kind="note_components_read", account_id=acc,
                    operator_id=1, payload="{}", status="done",
                    result=json.dumps({"title": "x"}, ensure_ascii=False),
                )
            )
            await s.commit()
        r = await c.get("/api/note-component-reads/q-done", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json()["queue"] is None


@pytest.mark.asyncio
async def test_cookie_check_poll_carries_queue_section(tmp_path, monkeypatch):
    """cookie-checks 把 queued/running 都译成 checking,queue 段是唯一能分辨排队的手段。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("巡检号", "uQ3", _COOKIES)
        await _seed_browser("chk-1", "cookie_check", acc)
        r = await c.get("/api/cookie-checks/chk-1", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "checking"
        assert body["queue"]["position"] == 1 and body["queue"]["ahead"] == 0


@pytest.mark.asyncio
async def test_note_export_poll_carries_queue_section(tmp_path, monkeypatch):
    """note-exports 同理(它把 queued 译成 running)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("导出号", "uQ4", _COOKIES)
        await _seed_browser("exp-1", "note_export", acc)
        r = await c.get("/api/note-exports/exp-1", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json()["queue"]["position"] == 1


@pytest.mark.asyncio
async def test_publish_job_poll_carries_queue_section(tmp_path, monkeypatch):
    """发布任务的 pending 也带 queue 段,且与同号 browser 任务合成一条队列。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("发布号", "uQ5", _COOKIES)
        base = datetime.utcnow() - timedelta(minutes=5)
        await _seed_browser("pub-ahead", "note_export", acc, created=base)
        async with db_module.async_session() as s:
            job = PublishJob(
                account_id=acc, title="t", content="c", images_json="[]",
                topics_json="[]", status="pending",
                created_at=base + timedelta(seconds=1),
            )
            s.add(job)
            await s.commit()
            job_id = job.id
        r = await c.get(f"/api/publish-jobs/{job_id}", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["queue"]["position"] == 2, body["queue"]
        assert body["queue"]["account_queue_depth"] == 2


@pytest.mark.asyncio
async def test_publish_job_queue_null_on_terminal(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("已发号", "uQ6", _COOKIES)
        async with db_module.async_session() as s:
            job = PublishJob(
                account_id=acc, title="t", content="c", images_json="[]",
                topics_json="[]", status="published", started_at=datetime.utcnow(),
            )
            s.add(job)
            await s.commit()
            job_id = job.id
        r = await c.get(f"/api/publish-jobs/{job_id}", headers=bearer(ADMIN_KEY))
        assert r.json()["queue"] is None


@pytest.mark.asyncio
async def test_blocked_by_session_cap_visible_through_rest(tmp_path, monkeypatch):
    """端到端:满帽时轮询直接告诉运营在等什么、等到几点。"""
    _api_role(monkeypatch)
    from app.core.config import settings

    monkeypatch.setattr(settings, "ACCOUNT_HOURLY_SESSION_CAP", 1, raising=False)
    monkeypatch.setattr(settings, "ACCOUNT_HOURLY_OPERATOR_SESSION_CAP", 1, raising=False)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("满帽号", "uQ7", _COOKIES)
        oldest = datetime.utcnow() - timedelta(minutes=40)
        await _seed_browser("cap-done", "note_export", acc, status="done", updated=oldest)
        await _seed_browser("cap-mine", "note_components_read", acc)
        r = await c.get("/api/note-component-reads/cap-mine", headers=bearer(ADMIN_KEY))
        q = r.json()["queue"]
        assert q["blocked_by"] == "session_cap", q
        assert q["detail"]["used"] == 1 and q["detail"]["cap"] == 1
        assert q["detail"]["kind_of_cap"] == "operator"
        expected = (oldest + timedelta(hours=1)).replace(microsecond=0).isoformat()[:16]
        assert q["detail"]["window_resets_at"].startswith(expected)


# ---------------- manifest 字段级防漂移 ----------------


def test_manifest_declares_queue_on_every_polling_endpoint():
    """每个轮询端点的 manifest 条目都挂同一份 queue 说明。

    字段级漂移没有端点级防漂移测试能发现(端点集合不变,少一个字段照样全绿),所以这条
    把"哪些端点该有 queue"写死成集合双向比对:新增轮询端点忘了带 queue 段,这里先红。
    """
    from app.http import ALL_MANIFEST_ENTRIES
    from app.http.job_polling import QUEUE_MANIFEST_NOTE

    declared = {e["path"] for e in ALL_MANIFEST_ENTRIES if "queue" in e}
    assert declared == _POLLING_PATHS, (
        f"漏声明: {sorted(_POLLING_PATHS - declared)}; 多声明: {sorted(declared - _POLLING_PATHS)}"
    )
    for e in ALL_MANIFEST_ENTRIES:
        if "queue" in e:
            assert e["queue"] == QUEUE_MANIFEST_NOTE, e["path"]


def test_queue_manifest_note_tells_caller_not_to_retry():
    """说明里必须讲清三种 blocked_by、window_resets_at,以及"看到 queued 别重试"。

    最后这句不是客套:2026-08-07 运营看到 queued 以为卡死就重发,重发只会再灌一条进同
    一个队列,让所有人等更久。契约不写,下次照犯。
    """
    from app.http.job_polling import QUEUE_MANIFEST_NOTE

    for token in (
        "position", "ahead", "account_queue_depth", "running", "blocked_by",
        "session_cap", "account_busy", "global_concurrency", "window_resets_at",
        "不要重试",
    ):
        assert token in QUEUE_MANIFEST_NOTE, token
