"""运营者配额闸测试(设计第五节:enqueue 未完成任务数超限 429,admin 豁免)。

两层覆盖:
- 单元:直接调 app.services.quota.assert_operator_quota,monkeypatch 模块内
  browser_jobs_repo 为假实现(契约签名 count_unfinished_for_operator)。
- REST:rest_client 起真实 app,验证五个 enqueue 端点(publish-jobs / cookie-checks /
  note-exports / note-deletions / op consistent-images)超额一律 429 且无副作用;
  admin 豁免、未超额放行走 publish-jobs(配假调度器,不触真浏览器)。
"""

import pytest
from fastapi import HTTPException

import app.core.db as db_module
from app.core.config import settings
from app.models import Operator, PublishJob
from app.publish import runtime as runtime_mod
from app.services import operator_service
from app.services import quota as quota_module
from app.services.quota import assert_operator_quota
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeRepo:
    """假 browser_jobs_repo:按契约签名返回固定计数,并记录被数过的 operator_id。"""

    def __init__(self, count: int) -> None:
        self.count = count
        self.counted: list[int] = []

    async def count_unfinished_for_operator(self, operator_id: int) -> int:
        self.counted.append(operator_id)
        return self.count


def _operator(role: str = "operator", op_id: int = 7) -> Operator:
    """构造内存 Operator(不落库;quota 只读 id/role 两列)。"""
    return Operator(
        id=op_id, name="配额测试", apikey_hash=f"h{op_id}", role=role, enabled=True
    )


class _FakeScheduler:
    """只记录 submit 的假调度器(避免 202 用例触发真发布链)。"""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


# ---------------- 单元:assert_operator_quota ----------------


async def test_quota_exceeded_raises_429(monkeypatch):
    """count 达上限 → HTTPException 429,文案含 N/上限与"配额"。"""
    fake = _FakeRepo(settings.OPERATOR_PENDING_QUOTA)
    monkeypatch.setattr(quota_module, "browser_jobs_repo", fake)
    with pytest.raises(HTTPException) as exc_info:
        await assert_operator_quota(_operator())
    assert exc_info.value.status_code == 429
    detail = exc_info.value.detail
    assert "配额" in detail
    assert f"{settings.OPERATOR_PENDING_QUOTA}/{settings.OPERATOR_PENDING_QUOTA}" in detail
    assert fake.counted == [7]


async def test_quota_under_limit_passes(monkeypatch):
    """count = 上限-1 → 放行不抛。"""
    fake = _FakeRepo(settings.OPERATOR_PENDING_QUOTA - 1)
    monkeypatch.setattr(quota_module, "browser_jobs_repo", fake)
    await assert_operator_quota(_operator())  # 不抛
    assert fake.counted == [7]


async def test_quota_admin_exempt(monkeypatch):
    """admin 豁免:不计数(repo 不被调用)、不抛。"""
    fake = _FakeRepo(999)
    monkeypatch.setattr(quota_module, "browser_jobs_repo", fake)
    await assert_operator_quota(_operator(role="admin"))  # 不抛
    assert fake.counted == []


async def test_quota_skips_when_repo_missing(monkeypatch):
    """并行开发窗口:browser_jobs_repo 未落地(None)→ fail-open 放行。"""
    monkeypatch.setattr(quota_module, "browser_jobs_repo", None)
    await assert_operator_quota(_operator())  # 不抛


# ---------------- REST:五个 enqueue 端点接线 ----------------


async def _make_operator_with_access(*account_ids: int, key: str) -> int:
    """建一个 operator 并授权给定账号,返回 operator_id。"""
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        for acc_id in account_ids:
            await operator_service.grant_access(s, op_id, acc_id, op_id)
    return op_id


async def test_all_enqueue_endpoints_return_429_when_exceeded(tmp_path, monkeypatch):
    """超额 operator 打五个 enqueue 端点一律 429,且不产生任何任务副作用。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake_sched = _FakeScheduler()
        runtime_mod.set_active_scheduler(fake_sched)
        acc = await seed_account("号Q1", "uQ1", _COOKIES)
        op_key = "op-quota-429-01"
        await _make_operator_with_access(acc, key=op_key)
        monkeypatch.setattr(
            quota_module, "browser_jobs_repo", _FakeRepo(settings.OPERATOR_PENDING_QUOTA)
        )

        requests = [
            ("post", "/api/publish-jobs",
             {"account_id": acc, "title": "T", "content": "C",
              "images": ["https://cdn/a.png"], "topics": []}),
            ("post", f"/api/accounts/{acc}/cookie-checks", None),
            ("post", f"/api/accounts/{acc}/note-exports", None),
            ("post", f"/api/accounts/{acc}/note-deletions", {"title": "标题", "count": 1}),
            ("post", "/api/op/consistent-images", {"prompts": ["画一张海报"]}),
        ]
        for method, path, body in requests:
            r = await c.request(
                method.upper(), path, json=body, headers=bearer(op_key)
            )
            assert r.status_code == 429, f"{path} 应 429,实得 {r.status_code}: {r.text}"
            detail = r.json()["detail"]
            assert "配额" in detail and str(settings.OPERATOR_PENDING_QUOTA) in detail

        # 副作用复核:发布 job 未落库、未入队。
        assert fake_sched.submitted == []
        async with db_module.async_session() as s:
            jobs = (await s.execute(
                PublishJob.__table__.select()
            )).fetchall()
            assert jobs == []


async def test_admin_exempt_via_rest(tmp_path, monkeypatch):
    """admin 即便计数爆表也放行:publish-jobs 202(配假调度器)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake_sched = _FakeScheduler()
        runtime_mod.set_active_scheduler(fake_sched)
        acc = await seed_account("号Q2", "uQ2", _COOKIES)
        monkeypatch.setattr(quota_module, "browser_jobs_repo", _FakeRepo(999))

        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/a.png"], "topics": []},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        assert fake_sched.submitted == [r.json()["job_id"]]


async def test_under_quota_operator_passes_via_rest(tmp_path, monkeypatch):
    """未超额的普通 operator 正常入队:publish-jobs 202。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake_sched = _FakeScheduler()
        runtime_mod.set_active_scheduler(fake_sched)
        acc = await seed_account("号Q3", "uQ3", _COOKIES)
        op_key = "op-quota-pass-01"
        await _make_operator_with_access(acc, key=op_key)
        monkeypatch.setattr(
            quota_module,
            "browser_jobs_repo",
            _FakeRepo(settings.OPERATOR_PENDING_QUOTA - 1),
        )

        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/a.png"], "topics": []},
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        assert fake_sched.submitted == [r.json()["job_id"]]
