"""publish 分组 MCP 工具 + check_cookies + runner 图片物料化的 RBAC 与行为测试。

隔离手法(与 test_account_tools 一致):patch app.core.db 的模块级 async_session 指向
tmp sqlite,使工具内 get_session() 落隔离库;set_current_operator 在同一 task 内注入上下文。
publish_note 的入队走 runtime 单例注入的假调度器(只记录 submit,不起真实后台循环/浏览器);
check_cookies 的浏览器调用 monkeypatch sync_client.check_login_once(不起浏览器)。

覆盖(brief 必测):
- publish_note:建 pending job 返 job_id;无 schedule_time → 入队(submit);有 schedule_time → 不入队;越权 → ToolError。
- get_publish_status:读 DB;越权账号的 job → ToolError。
- list_publish_jobs:按 caller 可见账号过滤(admin 全见);指定越权 account_id → ToolError。
- cancel_publish_job:仅 pending 可取消(置 canceled);非 pending → ok=False;越权 → ToolError。
- check_cookies:monkeypatch check_login_once 返回 valid → 写回 cookie_status/last_check_at + 回填资料;越权 → ToolError。
- runner 图片物料化:monkeypatch materialize_images + publish_once,断言用物料化后的本地路径发布且发布后清理 workdir。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.auth.context import reset_current_operator, set_current_operator
from app.core.security import encrypt_cookies
from app.models import Operator, OperatorAccountAccess, PublishJob, XhsAccount
from app.publish import runtime as runtime_mod
from app.publish.queue import AccountLocks
from app.publish.scheduler import PublishScheduler, make_publish_runner
from app.tools.cookies import register_cookies
from app.tools.publish import _parse_schedule_time, register_publish


class _FakeScheduler:
    """只记录 submit 的假调度器(publish_note 入队断言用,不起真实队列/循环)。"""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


@asynccontextmanager
async def isolated_mcp(tmp_path, monkeypatch):
    """建隔离库 + patch 模块级 async_session + 注入假调度器 + 注册 publish/cookies 工具。

    交出 (mcp, sessionmaker, fake_scheduler)。fake_scheduler 记录 publish_note 的 submit。
    """
    from app.core.db import Base

    tmp_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True
    )
    import app.models  # noqa: F401  触发模型注册后建表

    async with tmp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    smk = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "async_session", smk)

    fake_scheduler = _FakeScheduler()
    runtime_mod.set_active_scheduler(fake_scheduler)

    mcp = FastMCP("publish-test")
    register_publish(mcp)
    register_cookies(mcp)
    try:
        yield mcp, smk, fake_scheduler
    finally:
        runtime_mod.set_active_scheduler(None)
        await tmp_engine.dispose()


async def _seed(smk):
    """造 admin + op1/op2 + acc1/acc2/acc3,给 op1 授权 acc1/acc2;返回各 id。"""
    async with smk() as s:
        admin = Operator(name="root", role="admin", apikey_hash="h0", enabled=True)
        op1 = Operator(name="op1", role="operator", apikey_hash="h1", enabled=True)
        op2 = Operator(name="op2", role="operator", apikey_hash="h2", enabled=True)
        acc1 = XhsAccount(name="号1")
        acc2 = XhsAccount(name="号2")
        acc3 = XhsAccount(name="号3")
        s.add_all([admin, op1, op2, acc1, acc2, acc3])
        await s.commit()
        ids = {
            "admin": admin.id, "op1": op1.id, "op2": op2.id,
            "acc1": acc1.id, "acc2": acc2.id, "acc3": acc3.id,
        }
        s.add_all([
            OperatorAccountAccess(operator_id=op1.id, xhs_account_id=acc1.id),
            OperatorAccountAccess(operator_id=op1.id, xhs_account_id=acc2.id),
        ])
        await s.commit()
    return ids


def _ctx(op_id, role):
    """构造一个 detached Operator 供 set_current_operator(工具只读 id/role)。"""
    return Operator(id=op_id, name=f"op{op_id}", role=role, apikey_hash="x", enabled=True)


async def _make_job(smk, account_id, **overrides) -> int:
    """建一条 PublishJob,返回 id。"""
    defaults = dict(
        account_id=account_id, title="标题", content="正文",
        images_json="[]", topics_json="[]", status="pending",
    )
    defaults.update(overrides)
    async with smk() as s:
        job = PublishJob(**defaults)
        s.add(job)
        await s.commit()
        return job.id


# ---------------- N3:schedule_time 时区归一 ----------------


def test_parse_schedule_time_tzaware_to_naive_utc():
    """N3:+08:00 输入 → 存成 naive UTC(早 8 小时),去掉 tzinfo。"""
    dt = _parse_schedule_time("2026-01-01T09:00:00+08:00")
    assert dt == datetime(2026, 1, 1, 1, 0, 0)
    assert dt.tzinfo is None


def test_parse_schedule_time_naive_unchanged():
    """N3:naive 输入原样返回(不平移、不加 tzinfo)。"""
    dt = _parse_schedule_time("2026-01-01T09:00:00")
    assert dt == datetime(2026, 1, 1, 9, 0, 0)
    assert dt.tzinfo is None


def test_parse_schedule_time_none():
    """N3:None/空 → None(立即入队路径)。"""
    assert _parse_schedule_time(None) is None
    assert _parse_schedule_time("") is None


# ---------------- publish_note ----------------


async def test_publish_note_creates_pending_and_enqueues(tmp_path, monkeypatch):
    """无 schedule_time:建 pending job 返 job_id 且立即入队(fake_scheduler.submit 收到)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, sched):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool(
                "publish_note",
                {
                    "account_id": ids["acc1"],
                    "title": "T",
                    "content": "C",
                    "images": ["https://cdn/a.png"],
                    "topics": ["#心理"],
                },
            )
        finally:
            reset_current_operator(token)

        data = res.structured_content
        assert data["status"] == "pending"
        job_id = data["job_id"]
        assert isinstance(job_id, int)
        # 立即入队
        assert sched.submitted == [job_id]

        # 库内确有该 pending job,字段正确序列化
        async with smk() as s:
            job = await s.get(PublishJob, job_id)
            assert job.status == "pending"
            assert job.account_id == ids["acc1"]
            assert job.created_by == ids["op1"]
            assert json.loads(job.images_json) == ["https://cdn/a.png"]
            assert json.loads(job.topics_json) == ["#心理"]
            assert job.schedule_time is None


async def test_publish_note_scheduled_not_enqueued(tmp_path, monkeypatch):
    """有 schedule_time:落 schedule_time 且不立即入队(交 scan 循环到期自取)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, sched):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool(
                "publish_note",
                {
                    "account_id": ids["acc1"],
                    "title": "T", "content": "C",
                    "images": ["https://cdn/x.png"], "topics": [],
                    "schedule_time": "2099-01-01T08:00:00",
                },
            )
        finally:
            reset_current_operator(token)

        job_id = res.structured_content["job_id"]
        assert sched.submitted == []  # 未入队
        async with smk() as s:
            job = await s.get(PublishJob, job_id)
            assert job.schedule_time is not None
            assert job.schedule_time.year == 2099


async def test_publish_note_denied_without_access(tmp_path, monkeypatch):
    """op2 无 acc1 的 access → publish_note 抛 ToolError(含"无权操作账号")。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, sched):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "publish_note",
                    {
                        "account_id": ids["acc1"],
                        "title": "T", "content": "C",
                        "images": [], "topics": [],
                    },
                )
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)
        # 越权不落库、不入队
        assert sched.submitted == []
        async with smk() as s:
            cnt = (await s.execute(select(func.count()).select_from(PublishJob))).scalar()
            assert cnt == 0


# ---------------- D1:publish_note 图片张数校验(建 job 前早失败) ----------------


async def test_publish_note_rejects_empty_images(tmp_path, monkeypatch):
    """D1:images 为空 → 立即报错,不建 pending job、不入队。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, sched):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "publish_note",
                    {
                        "account_id": ids["acc1"],
                        "title": "T", "content": "C",
                        "images": [], "topics": [],
                    },
                )
            assert "至少需要 1 张图片" in str(ei.value)
        finally:
            reset_current_operator(token)
        assert sched.submitted == []
        async with smk() as s:
            cnt = (await s.execute(select(func.count()).select_from(PublishJob))).scalar()
            assert cnt == 0


async def test_publish_note_rejects_too_many_images(tmp_path, monkeypatch):
    """D1:images 超 18 张 → 立即报错,不建 pending job。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, sched):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "publish_note",
                    {
                        "account_id": ids["acc1"],
                        "title": "T", "content": "C",
                        "images": [f"https://cdn/{i}.png" for i in range(19)],
                        "topics": [],
                    },
                )
            assert "最多 18 张图片" in str(ei.value)
        finally:
            reset_current_operator(token)
        assert sched.submitted == []
        async with smk() as s:
            cnt = (await s.execute(select(func.count()).select_from(PublishJob))).scalar()
            assert cnt == 0


# ---------------- get_publish_status ----------------


async def test_get_publish_status_reads_and_access(tmp_path, monkeypatch):
    """有 access 读到状态;越权账号的 job → ToolError。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        job_id = await _make_job(
            smk, ids["acc1"], status="published",
            note_id="nid", note_url="https://xhs/1", retries=2,
        )

        # op1 有 acc1 access
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("get_publish_status", {"job_id": job_id})
        finally:
            reset_current_operator(token)
        data = res.structured_content
        assert data["status"] == "published"
        assert data["note_id"] == "nid"
        assert data["note_url"] == "https://xhs/1"
        assert data["retries"] == 2

        # op2 无 access → ToolError
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("get_publish_status", {"job_id": job_id})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


async def test_get_publish_status_returns_enriched_fields(tmp_path, monkeypatch):
    """C2:返回体补全 job_id/account_id/schedule_time/next_retry_at,既有字段不删。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        job_id = await _make_job(
            smk, ids["acc1"], status="failed", error="boom", retries=1,
            schedule_time=datetime(2099, 1, 1, 0, 0, 0),
            next_retry_at=datetime(2099, 1, 1, 0, 5, 0),
        )
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("get_publish_status", {"job_id": job_id})
        finally:
            reset_current_operator(token)
        data = res.structured_content
        # 新增字段
        assert data["job_id"] == job_id
        assert data["account_id"] == ids["acc1"]
        assert data["schedule_time"] == "2099-01-01T00:00:00"
        assert data["next_retry_at"] == "2099-01-01T00:05:00"
        # 既有字段仍在(向后兼容)
        assert data["status"] == "failed"
        assert data["error"] == "boom"
        assert data["retries"] == 1


# ---------------- list_publish_jobs ----------------


async def test_list_publish_jobs_access_filter(tmp_path, monkeypatch):
    """operator 只见可见账号的 job;admin 全见;指定越权 account_id → ToolError。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        j1 = await _make_job(smk, ids["acc1"])
        j2 = await _make_job(smk, ids["acc2"])
        j3 = await _make_job(smk, ids["acc3"])  # op1 无 acc3 access

        # op1 只见 acc1/acc2 的 job
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("list_publish_jobs", {})
        finally:
            reset_current_operator(token)
        got = {j["job_id"] for j in res.structured_content["jobs"]}
        assert got == {j1, j2}

        # admin 全见
        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            res_a = await mcp.call_tool("list_publish_jobs", {})
        finally:
            reset_current_operator(token)
        got_a = {j["job_id"] for j in res_a.structured_content["jobs"]}
        assert got_a == {j1, j2, j3}

        # op1 指定越权 account_id=acc3 → ToolError
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("list_publish_jobs", {"account_id": ids["acc3"]})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


async def test_list_publish_jobs_status_filter(tmp_path, monkeypatch):
    """status 过滤:仅返回该状态的 job(在可见范围内)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        pending = await _make_job(smk, ids["acc1"], status="pending")
        await _make_job(smk, ids["acc1"], status="published")

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("list_publish_jobs", {"status": "pending"})
        finally:
            reset_current_operator(token)
        got = {j["job_id"] for j in res.structured_content["jobs"]}
        assert got == {pending}


async def test_list_publish_jobs_rejects_bad_status(tmp_path, monkeypatch):
    """D2:status 传非法值 → 明确报错(列出合法值),而非静默返回空。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        await _make_job(smk, ids["acc1"], status="pending")
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "list_publish_jobs", {"status": "done"}
                )
            msg = str(ei.value)
            assert "status 非法" in msg
            assert "pending" in msg  # 报错里带上合法值
        finally:
            reset_current_operator(token)


async def test_list_publish_jobs_limit(tmp_path, monkeypatch):
    """D2:limit 限制返回条数(按新→旧取前 N)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        job_ids = [await _make_job(smk, ids["acc1"]) for _ in range(5)]
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            res = await mcp.call_tool("list_publish_jobs", {"limit": 2})
        finally:
            reset_current_operator(token)
        jobs = res.structured_content["jobs"]
        assert len(jobs) == 2
        # 新→旧:取最后建的两条
        assert [j["job_id"] for j in jobs] == sorted(job_ids, reverse=True)[:2]


# ---------------- cancel_publish_job ----------------


async def test_cancel_only_pending(tmp_path, monkeypatch):
    """pending 可取消(置 canceled);非 pending → ok=False;越权 → ToolError。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        pending = await _make_job(smk, ids["acc1"], status="pending")
        publishing = await _make_job(smk, ids["acc1"], status="publishing")

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            ok = await mcp.call_tool("cancel_publish_job", {"job_id": pending})
            assert ok.structured_content["ok"] is True

            not_ok = await mcp.call_tool("cancel_publish_job", {"job_id": publishing})
            assert not_ok.structured_content["ok"] is False
            # C3:非 pending 时带上当前状态,让 caller 一眼看出为何取消不了
            assert not_ok.structured_content["status"] == "publishing"
        finally:
            reset_current_operator(token)

        async with smk() as s:
            assert (await s.get(PublishJob, pending)).status == "canceled"
            assert (await s.get(PublishJob, publishing)).status == "publishing"

        # op2 越权取消 → ToolError
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("cancel_publish_job", {"job_id": publishing})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


# ---------------- check_cookies(异步)+ get_cookie_check ----------------


async def _await_cookie_check(mcp, check_id: str) -> dict:
    """轮询 get_cookie_check 到非 checking 终态并返回;带超时防死等后台任务。

    需在已 set_current_operator 的上下文内调用(get_cookie_check 走 access 鉴权)。
    """
    for _ in range(250):  # 250 * 0.02 = 5s 上限
        res = await mcp.call_tool("get_cookie_check", {"check_id": check_id})
        if res.structured_content["status"] != "checking":
            return res.structured_content
        await asyncio.sleep(0.02)
    raise AssertionError("异步 cookie 检测未在超时内落终态")


async def test_check_cookies_async_returns_check_id_and_valid(tmp_path, monkeypatch):
    """check_cookies 立即返 check_id/status=checking;get_cookie_check 轮到 valid 并写回。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        # 给 acc1 灌一份加密 cookie(check_cookies 会解密后传入 check_login_once)
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            acc.login_cookies = encrypt_cookies(
                json.dumps([{"name": "a", "value": "x"}])
            )
            await s.commit()

        captured = {}

        def fake_check_login_once(account_id, cookies):
            captured["args"] = (account_id, cookies)
            return {"status": "valid", "user_info": {"nickname": "小蓝", "user_id": "u1"}}

        # 后台检测在 cookie_check 服务里调 sync_client.check_login_once,pat 该模块属性
        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            started = await mcp.call_tool("check_cookies", {"account_id": ids["acc1"]})
            sc = started.structured_content
            assert sc["status"] == "checking"
            assert isinstance(sc["check_id"], str) and sc["check_id"]

            final = await _await_cookie_check(mcp, sc["check_id"])
        finally:
            reset_current_operator(token)

        assert final["status"] == "valid"
        assert final["user_info"]["nickname"] == "小蓝"
        # 解密后的 cookie 传给了 check_login_once
        assert captured["args"][0] == ids["acc1"]
        assert captured["args"][1] == [{"name": "a", "value": "x"}]

        # 写回巡检态 + 回填资料
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            assert acc.cookie_status == "valid"
            assert acc.last_check_at is not None
            assert acc.nickname == "小蓝"
            assert acc.user_id == "u1"


async def test_check_cookies_denied_without_access(tmp_path, monkeypatch):
    """op2 无 acc1 的 access → check_cookies 抛 ToolError,且不触发 check_login_once。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)

        called = {"n": 0}

        def fake_check_login_once(account_id, cookies):
            called["n"] += 1
            return {"status": "valid", "user_info": None}

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("check_cookies", {"account_id": ids["acc1"]})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)
        assert called["n"] == 0  # 越权在起后台检测前就被拦


async def test_check_cookies_async_error_preserves_status(tmp_path, monkeypatch):
    """D4:后台检测返回 error(基础设施失败)→ 不覆盖 cookie_status,原值保留;结果仍可轮询。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        # acc1 原本是 valid 号,带一份加密 cookie
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            acc.cookie_status = "valid"
            acc.login_cookies = encrypt_cookies(
                json.dumps([{"name": "a", "value": "x"}])
            )
            await s.commit()

        def fake_check_login_once(account_id, cookies):
            return {"status": "error", "user_info": None, "reason": "浏览器启动失败:boom"}

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            started = await mcp.call_tool("check_cookies", {"account_id": ids["acc1"]})
            final = await _await_cookie_check(
                mcp, started.structured_content["check_id"]
            )
        finally:
            reset_current_operator(token)

        assert final["status"] == "error"
        assert "浏览器启动失败" in final["reason"]

        # 关键:好号未被误标 —— cookie_status 保留原 valid,last_check_at 未被写
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            assert acc.cookie_status == "valid"
            assert acc.last_check_at is None


async def test_get_cookie_check_unknown_id(tmp_path, monkeypatch):
    """get_cookie_check 传未知 check_id → ToolError(不存在或已过期)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        token = set_current_operator(_ctx(ids["admin"], "admin"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool(
                    "get_cookie_check", {"check_id": "does-not-exist"}
                )
            assert "不存在或已过期" in str(ei.value)
        finally:
            reset_current_operator(token)


async def test_get_cookie_check_denied_cross_operator(tmp_path, monkeypatch):
    """越权:op1 发起的 check,op2(无 acc1 access)查其结果 → ToolError(无权操作账号)。"""
    async with isolated_mcp(tmp_path, monkeypatch) as (mcp, smk, _s):
        ids = await _seed(smk)
        async with smk() as s:
            acc = await s.get(XhsAccount, ids["acc1"])
            acc.login_cookies = encrypt_cookies(
                json.dumps([{"name": "a", "value": "x"}])
            )
            await s.commit()

        def fake_check_login_once(account_id, cookies):
            return {"status": "valid", "user_info": None}

        monkeypatch.setattr(
            "app.browser.sync_client.check_login_once", fake_check_login_once
        )

        # op1 有 acc1 access,发起检测并等后台落终态(仍在 op1 上下文)
        token = set_current_operator(_ctx(ids["op1"], "operator"))
        try:
            started = await mcp.call_tool("check_cookies", {"account_id": ids["acc1"]})
            check_id = started.structured_content["check_id"]
            await _await_cookie_check(mcp, check_id)
        finally:
            reset_current_operator(token)

        # op2 无 acc1 access,查该 check_id → 越权 ToolError
        token = set_current_operator(_ctx(ids["op2"], "operator"))
        try:
            with pytest.raises(ToolError) as ei:
                await mcp.call_tool("get_cookie_check", {"check_id": check_id})
            assert "无权操作账号" in str(ei.value)
        finally:
            reset_current_operator(token)


# ---------------- runner 图片物料化(接线洞修复) ----------------


async def test_runner_materializes_images_and_cleans_workdir(
    db_factory, monkeypatch, tmp_path
):
    """runner:URL/base64 图片先 materialize_images 落本地 → 用本地路径调 publish_once → 发布后清理 workdir。"""
    from app.publish import scheduler as scheduler_mod

    # workdir 落到隔离临时目录(而非真实 ./data/uploads)
    monkeypatch.setattr(scheduler_mod.settings, "UPLOAD_DIR", str(tmp_path))

    async with db_factory() as s:
        acc = XhsAccount(name="acc")
        s.add(acc)
        await s.commit()
        account_id = acc.id
    async with db_factory() as s:
        job = PublishJob(
            account_id=account_id, title="T", content="C",
            images_json=json.dumps(["https://cdn/a.png", "https://cdn/b.png"]),
            topics_json="[]", status="pending",
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    captured = {}

    def fake_materialize(images, workdir):
        # 真建 workdir + 落一个文件,供之后断言清理
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        f = workdir / "img_00.png"
        f.write_bytes(b"fake")
        captured["workdir"] = workdir
        captured["images"] = list(images)
        return [f]

    monkeypatch.setattr(scheduler_mod, "materialize_images", fake_materialize)

    def fake_publish_once(acc_id, cookies, title, content, image_paths, topics):
        captured["image_paths"] = image_paths
        # 发布时 workdir 及物料文件仍在
        captured["exists_during"] = captured["workdir"].exists()
        from app.browser.sync_client import PublishResult
        return PublishResult(success=True, note_url="https://xhs/ok")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    # 物料化收到原始 URL 列表;publish_once 用的是物料化后的本地路径
    assert captured["images"] == ["https://cdn/a.png", "https://cdn/b.png"]
    assert captured["image_paths"] == [str(captured["workdir"] / "img_00.png")]
    assert captured["exists_during"] is True
    # 发布后 workdir 被清理
    assert not captured["workdir"].exists()

    async with db_factory() as s:
        assert (await s.get(PublishJob, job_id)).status == "published"
