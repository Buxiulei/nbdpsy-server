"""笔记导出服务(app.services.note_export)单测,不起真浏览器。

P1 台账落库后重写:旧版测的内存 _registry/_tasks/驱逐机制已整体移除(台账进
browser_jobs 表,持久化在 tests/test_browser_jobs_repo.py 锁定;REST 全链在
tests/test_notes_rest.py 锁定)。本文件锁四件事:

- start_export:enqueue 台账 + 派进程内执行,返回 export_id;
- get_export:台账行 → 既有 entry 形状的映射(running/done+note_count/error+reason);
- execute 成功:导出行数经 upsert_notes 落库并返回 note_count;
- execute 失败:CreatorExportError 收敛为 {"error": reason},不抛出、不落半截数据。
"""

import app.core.db as db_module
from sqlalchemy import select

from app.browser.creator_export import CreatorExportError
from app.models.note_metric import NoteMetric
from app.services import note_export


# ---------------- start_export / get_export(台账接线) ----------------


def test_start_export_enqueues_and_spawns_inline(monkeypatch):
    """start_export = 台账 enqueue + spawn_inline 派执行,原样返回 export_id。"""
    calls = {}

    def fake_enqueue(kind, payload, account_id=None, job_id=None):
        calls["enqueue"] = (kind, account_id)
        return "exp-fixed"

    def fake_spawn(job_id, execute_call):
        calls["spawn"] = job_id

    monkeypatch.setattr(note_export.browser_jobs_repo, "enqueue_from_request", fake_enqueue)
    monkeypatch.setattr(note_export.browser_jobs_repo, "spawn_inline", fake_spawn)

    assert note_export.start_export(7, [{"name": "a1"}]) == "exp-fixed"
    assert calls["enqueue"] == ("note_export", 7)
    assert calls["spawn"] == "exp-fixed"


def test_get_export_maps_row_to_legacy_entry(monkeypatch):
    """get_export 把台账行映射回既有 entry 形状(REST 读的键一个不缺)。"""
    rows = {
        "e-run": {"kind": "note_export", "account_id": 5, "status": "running", "result": None},
        "e-done": {"kind": "note_export", "account_id": 5, "status": "done",
                   "result": {"note_count": 24}},
        "e-err": {"kind": "note_export", "account_id": 5, "status": "error",
                  "result": {"error": "need_manual_login"}},
        "e-alien": {"kind": "note_delete", "account_id": 5, "status": "done", "result": {}},
    }
    monkeypatch.setattr(
        note_export.browser_jobs_repo, "get_job_sync", lambda db, jid: rows.get(jid))
    monkeypatch.setattr(
        note_export.browser_jobs_repo, "current_db_path", lambda: ":memory:")

    assert note_export.get_export("e-run")["status"] == "running"
    done = note_export.get_export("e-done")
    assert done["status"] == "done" and done["note_count"] == 24
    err = note_export.get_export("e-err")
    assert err["status"] == "error" and err["reason"] == "need_manual_login"
    assert note_export.get_export("e-alien") is None  # 异 kind 不冒充导出台账
    assert note_export.get_export("nope") is None


# ---------------- execute(执行契约) ----------------


async def test_execute_success_stores_and_returns_count(db_factory, monkeypatch):
    """execute 成功:导出行经 upsert_notes 落库并返回 {"note_count": N}。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)

    async def fake_load(account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    monkeypatch.setattr(note_export, "load_account_cookies", fake_load)

    rows = [{
        "account_id": 42, "title": "标题A", "publish_time": "2026年07月01日10时00分00秒",
        "likes": 3, "collects": 1, "comments": 0, "danmu": 0, "shares": 1,
        "reposts": 0, "follows": 0, "exposure": 65, "views": 12,
        "cover_ctr": 13.8, "avg_view_duration": 37.0,
    }]

    def fake_export_sync(account_id, cookies, download_dir, ts):
        return rows

    monkeypatch.setattr(note_export, "_export_sync", fake_export_sync)

    result = await note_export.execute(42, {})
    assert result == {"note_count": 1}
    async with db_factory() as session:
        stored = (await session.execute(select(NoteMetric))).scalars().all()
    assert len(stored) == 1 and stored[0].title == "标题A"


async def test_execute_error_returns_error_dict(db_factory, monkeypatch):
    """execute 失败:CreatorExportError 收敛为 {"error": reason},不抛出、不落库。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)

    async def fake_load(account_id):
        return []

    monkeypatch.setattr(note_export, "load_account_cookies", fake_load)

    def boom(account_id, cookies, download_dir, ts):
        raise CreatorExportError("need_manual_login")

    monkeypatch.setattr(note_export, "_export_sync", boom)

    result = await note_export.execute(42, {})
    assert result["error"] == "need_manual_login"
    async with db_factory() as session:
        stored = (await session.execute(select(NoteMetric))).scalars().all()
    assert stored == []  # 失败不落半截数据
