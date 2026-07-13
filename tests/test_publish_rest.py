"""publish 分组 REST(4 端点)测试:建任务(202)/ 查状态 / 列表 / 取消。

隔离手法与 test_accounts_rest.py / test_manifest.py 一致:rest_client 跑真实 lifespan
(隔离库 + 真调度器起停);publish_note 的入队断言需要假调度器,故 client 起来后用
runtime.set_active_scheduler(fake) 覆盖 lifespan 装好的真调度器(与 test_publish_tools.py
的 isolated_mcp 手法对齐,只是覆盖时机挪到 client 建好之后)。

覆盖(brief 必测):
- POST /api/publish-jobs:无 schedule_time → 202 + 立即入队;有 schedule_time → 202 + 不入队
  (DB 落 naive UTC);越权 → 403;images 为空/超 18 张 → 400。
- GET /api/publish-jobs/{job_id}:200 返回 _job_view 全字段;越权 → 403;不存在 → 404。
- GET /api/publish-jobs:按 caller 可见账号过滤(admin 全见);status 过滤;status 非法 → 400;
  limit 限制条数。
- POST /api/publish-jobs/{job_id}/cancel:仅 pending 可取消;非 pending → {ok:false,status};
  不存在 → 404。
"""

import json
from datetime import datetime

import app.core.db as db_module
from app.models import Operator, PublishJob, XhsAccount
from app.publish import runtime as runtime_mod
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    get_root_admin,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeScheduler:
    """只记录 submit 的假调度器(与 test_publish_tools.py 的 _FakeScheduler 一致)。"""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


def _install_fake_scheduler() -> _FakeScheduler:
    """在 rest_client 起来(真 lifespan 已装真调度器)后覆盖为假调度器。"""
    fake = _FakeScheduler()
    runtime_mod.set_active_scheduler(fake)
    return fake


async def _make_operator_with_access(*account_ids: int, key: str) -> int:
    """建一个 operator 并授权给定账号,返回 operator_id。"""
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        for acc_id in account_ids:
            await operator_service.grant_access(s, op_id, acc_id, op_id)
    return op_id


async def _make_job(account_id: int, **overrides) -> int:
    """直接建一条 PublishJob(绕过 REST,供状态/查询类测试铺数据)。"""
    defaults = dict(
        account_id=account_id, title="标题", content="正文",
        images_json="[]", topics_json="[]", status="pending",
    )
    defaults.update(overrides)
    async with db_module.async_session() as s:
        job = PublishJob(**defaults)
        s.add(job)
        await s.commit()
        return job.id


# ---------------- _parse_schedule_time ----------------


def test_parse_schedule_time_tzaware_naive_none():
    """三态:tz-aware → naive UTC;naive → 原样;None/空 → None。"""
    from app.http.publish_rest import _parse_schedule_time

    dt = _parse_schedule_time("2026-01-01T09:00:00+08:00")
    assert dt == datetime(2026, 1, 1, 1, 0, 0)
    assert dt.tzinfo is None

    dt2 = _parse_schedule_time("2026-01-01T09:00:00")
    assert dt2 == datetime(2026, 1, 1, 9, 0, 0)
    assert dt2.tzinfo is None

    assert _parse_schedule_time(None) is None
    assert _parse_schedule_time("") is None


# ---------------- POST /api/publish-jobs ----------------


async def test_publish_note_creates_pending_and_enqueues(tmp_path, monkeypatch):
    """无 schedule_time:202 + {job_id, status:pending} + 立即入队(fake.submitted 收到)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await seed_account("号A", "uA", _COOKIES)
        op_key = "op-publish-create-01"
        await _make_operator_with_access(acc, key=op_key)

        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T",
                "content": "C",
                "images": ["https://cdn/a.png"],
                "topics": ["#心理"],
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        data = r.json()
        assert data["status"] == "pending"
        job_id = data["job_id"]
        assert isinstance(job_id, int)
        assert fake.submitted == [job_id]

        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.status == "pending"
            assert job.account_id == acc
            assert json.loads(job.images_json) == ["https://cdn/a.png"]
            assert json.loads(job.topics_json) == ["#心理"]
            assert job.schedule_time is None


async def test_publish_scheduled_not_enqueued(tmp_path, monkeypatch):
    """带 schedule_time(+08:00) → 202 且未入队;DB 落 naive UTC(早 8 小时)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await seed_account("号B", "uB", _COOKIES)
        op_key = "op-publish-sched-01"
        await _make_operator_with_access(acc, key=op_key)

        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T",
                "content": "C",
                "images": ["https://cdn/b.png"],
                "topics": [],
                "schedule_time": "2030-01-01T09:00:00+08:00",
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert fake.submitted == []

        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.schedule_time == datetime(2030, 1, 1, 1, 0, 0)


async def test_publish_denied_without_access(tmp_path, monkeypatch):
    """operator 无该号 access → 403;不建 job、不入队。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await seed_account("号C", "uC", _COOKIES)
        op_key = "op-publish-denied-01"
        await make_operator(op_key)  # 不授权任何号

        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T", "content": "C",
                "images": ["https://cdn/c.png"], "topics": [],
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 403
        assert fake.submitted == []


async def test_publish_rejects_empty_images(tmp_path, monkeypatch):
    """images 为空 → 400,error 文案含"图片"。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号D", "uD", _COOKIES)
        op_key = "op-publish-empty-01"
        await _make_operator_with_access(acc, key=op_key)

        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T", "content": "C",
                "images": [], "topics": [],
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 400, r.text
        assert "图片" in r.json()["error"]


async def test_publish_rejects_19_images(tmp_path, monkeypatch):
    """images 超 18 张 → 400,error 文案含"图片"。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号E", "uE", _COOKIES)
        op_key = "op-publish-19-01"
        await _make_operator_with_access(acc, key=op_key)

        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T", "content": "C",
                "images": [f"https://cdn/{i}.png" for i in range(19)],
                "topics": [],
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 400, r.text
        assert "图片" in r.json()["error"]


# ---------------- GET /api/publish-jobs/{job_id} ----------------


async def test_get_status_reads_and_denied(tmp_path, monkeypatch):
    """建后 GET → 200 全字段;他人(无 access)→ 403;不存在 → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号F", "uF", _COOKIES)
        op_key = "op-publish-status-01"
        await _make_operator_with_access(acc, key=op_key)
        other_key = "op-publish-status-other-01"
        await make_operator(other_key)  # 无 access

        job_id = await _make_job(
            acc, status="published", note_id="nid", note_url="https://xhs/1",
            retries=2,
        )

        r = await c.get(f"/api/publish-jobs/{job_id}", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        data = r.json()
        assert set(data.keys()) == {
            "job_id", "account_id", "title", "status", "note_id", "note_url",
            "error", "retries", "schedule_time", "next_retry_at", "created_at",
        }
        assert data["job_id"] == job_id
        assert data["account_id"] == acc
        assert data["status"] == "published"
        assert data["note_id"] == "nid"
        assert data["note_url"] == "https://xhs/1"
        assert data["retries"] == 2

        r2 = await c.get(f"/api/publish-jobs/{job_id}", headers=bearer(other_key))
        assert r2.status_code == 403

        r3 = await c.get("/api/publish-jobs/9999", headers=bearer(ADMIN_KEY))
        assert r3.status_code == 404


# ---------------- GET /api/publish-jobs ----------------


async def test_list_jobs_access_filter(tmp_path, monkeypatch):
    """operator 只见授权号的 job;admin 全见。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc1 = await seed_account("号G1", "uG1", _COOKIES)
        acc2 = await seed_account("号G2", "uG2", _COOKIES)
        acc3 = await seed_account("号G3", "uG3", _COOKIES)
        op_key = "op-publish-list-access-01"
        await _make_operator_with_access(acc1, acc2, key=op_key)

        j1 = await _make_job(acc1)
        j2 = await _make_job(acc2)
        j3 = await _make_job(acc3)

        r = await c.get("/api/publish-jobs", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        got = {j["job_id"] for j in r.json()["jobs"]}
        assert got == {j1, j2}

        r_admin = await c.get("/api/publish-jobs", headers=bearer(ADMIN_KEY))
        assert r_admin.status_code == 200
        got_admin = {j["job_id"] for j in r_admin.json()["jobs"]}
        assert got_admin == {j1, j2, j3}


async def test_list_jobs_status_filter_and_bad_status_400(tmp_path, monkeypatch):
    """?status=pending 生效;?status=xx → 400。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号H", "uH", _COOKIES)
        op_key = "op-publish-list-status-01"
        await _make_operator_with_access(acc, key=op_key)

        pending = await _make_job(acc, status="pending")
        await _make_job(acc, status="published")

        r = await c.get(
            "/api/publish-jobs", params={"status": "pending"}, headers=bearer(op_key)
        )
        assert r.status_code == 200, r.text
        got = {j["job_id"] for j in r.json()["jobs"]}
        assert got == {pending}

        r_bad = await c.get(
            "/api/publish-jobs", params={"status": "xx"}, headers=bearer(op_key)
        )
        assert r_bad.status_code == 400
        assert "status 非法" in r_bad.json()["error"]


async def test_list_jobs_limit(tmp_path, monkeypatch):
    """?limit=1 只回最新 1 条。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号I", "uI", _COOKIES)
        op_key = "op-publish-list-limit-01"
        await _make_operator_with_access(acc, key=op_key)

        job_ids = [await _make_job(acc) for _ in range(3)]

        r = await c.get(
            "/api/publish-jobs", params={"limit": 1}, headers=bearer(op_key)
        )
        assert r.status_code == 200, r.text
        jobs = r.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == max(job_ids)


# ---------------- POST /api/publish-jobs/{job_id}/cancel ----------------


async def test_cancel_only_pending(tmp_path, monkeypatch):
    """pending → {ok:true} 且状态 canceled;再取消 → {ok:false,status:canceled};不存在 → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号J", "uJ", _COOKIES)
        op_key = "op-publish-cancel-01"
        await _make_operator_with_access(acc, key=op_key)

        job_id = await _make_job(acc, status="pending")

        r = await c.post(
            f"/api/publish-jobs/{job_id}/cancel", headers=bearer(op_key)
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

        async with db_module.async_session() as s:
            assert (await s.get(PublishJob, job_id)).status == "canceled"

        r2 = await c.post(
            f"/api/publish-jobs/{job_id}/cancel", headers=bearer(op_key)
        )
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"ok": False, "status": "canceled"}

        r3 = await c.post(
            "/api/publish-jobs/9999/cancel", headers=bearer(ADMIN_KEY)
        )
        assert r3.status_code == 404
