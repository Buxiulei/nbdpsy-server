"""CookieChecker 行为 + lifespan 开关测试(不起真浏览器)。

隔离手法与其它工具测试一致:tmp sqlite 引擎 + async_sessionmaker;
sync_client.check_login_once 被 monkeypatch 成假实现,断言状态写回。

覆盖:
- check_once:只检 cookie_status='valid' 的号,写回三态 + last_check_at + 回填资料;
  无 cookie 的 valid 号跳过(不误改状态)。
- account_gap=0 时不引入号间隔延时(测试可秒级跑完)。
- start/stop 生命周期:起循环 → 至少跑一轮 → 干净 stop(无遗留 task)。
- lifespan 开关:COOKIE_CHECK_INTERVAL=0 不起 checker;>0 起 + shutdown 干净 stop。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
import app.server as server_mod
from app.browser import cookie_checker as checker_mod
from app.browser.cookie_checker import CookieChecker
from app.core.security import encrypt_cookies
from app.models.xhs_account import XhsAccount


@pytest.fixture
async def smk(tmp_path):
    """独立 tmp sqlite 会话工厂 + 建表。"""
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    import app.models  # noqa: F401  触发模型注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _enc(cookies: list[dict]) -> str:
    """加密 cookie 串(与生产落库一致,checker 会解密回读)。"""
    return encrypt_cookies(json.dumps(cookies, ensure_ascii=False))


async def _add_account(factory, name, cookie_status, cookies=None) -> int:
    """造一个账号(可带加密 cookie),返回 id。"""
    async with factory() as s:
        acc = XhsAccount(name=name, cookie_status=cookie_status)
        if cookies is not None:
            acc.login_cookies = _enc(cookies)
        s.add(acc)
        await s.commit()
        return acc.id


async def test_check_once_only_valid_and_writes_back(smk, monkeypatch):
    """只检 valid 号:写回 check_login_once 的三态 + 回填 user_info;invalid 号不检。"""
    valid_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])
    invalid_id = await _add_account(smk, "失效号", "invalid", [{"name": "b", "value": "y"}])

    seen: list[int] = []

    def fake_check(account_id, cookies):
        seen.append(account_id)
        return {"status": "captcha", "user_info": {"nickname": "小明", "user_id": "u1"}}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    checked = await checker.check_once()

    assert checked == 1
    assert seen == [valid_id]  # 只检了 valid 号,invalid 号没碰

    async with smk() as s:
        v = await s.get(XhsAccount, valid_id)
        assert v.cookie_status == "captcha"  # 三态写回
        assert v.last_check_at is not None
        assert v.nickname == "小明"  # 回填资料
        assert v.user_id == "u1"
        iv = await s.get(XhsAccount, invalid_id)
        assert iv.cookie_status == "invalid"  # 未被巡检,保持原状


async def test_check_once_skips_valid_without_cookies(smk, monkeypatch):
    """cookie_status=valid 但无 login_cookies 的号:跳过检测,不误改状态。"""
    empty_id = await _add_account(smk, "空号", "valid", cookies=None)

    called = {"n": 0}

    def fake_check(account_id, cookies):
        called["n"] += 1
        return {"status": "invalid", "user_info": None}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    checked = await checker.check_once()

    assert checked == 0  # 无 cookie 的号不计入
    assert called["n"] == 0  # check_login_once 未被调用

    async with smk() as s:
        acc = await s.get(XhsAccount, empty_id)
        assert acc.cookie_status == "valid"  # 保持不变


async def test_start_stop_runs_at_least_one_cycle(smk, monkeypatch):
    """start → 后台至少跑一轮(检到 valid 号)→ stop 干净退出。"""
    await _add_account(smk, "号A", "valid", [{"name": "a", "value": "x"}])

    calls: list[int] = []

    def fake_check(account_id, cookies):
        calls.append(account_id)
        return {"status": "valid", "user_info": None}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    # interval 很小让后台循环快速进入下一轮;account_gap=0 免号间隔延时
    checker = CookieChecker(smk, interval=0.01, account_gap=0)
    checker.start()
    # 轮询等待第一轮完成(最多 ~2s),避免 sleep 竞态
    for _ in range(200):
        if calls:
            break
        import asyncio

        await asyncio.sleep(0.01)
    await checker.stop()

    assert calls, "后台循环应至少检测一次 valid 号"
    assert checker._loop_task is None  # stop 后无遗留 task


# ---------------- lifespan 开关 ----------------


class _FakeChecker:
    """记录构造/启停的假 checker,替换 server.CookieChecker 以观测 lifespan 开关。"""

    instances: list["_FakeChecker"] = []

    def __init__(self, session_factory, interval, account_gap=5.0):
        self.interval = interval
        self.started = False
        self.stopped = False
        _FakeChecker.instances.append(self)

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


async def _drive_lifespan_with_interval(tmp_path, monkeypatch, interval):
    """用隔离库驱动一次 create_app 的真实 lifespan,返回捕获的假 checker 实例列表。"""
    tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/l.db", future=True)
    tmp_smk = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_smk)
    monkeypatch.setattr(server_mod.settings, "COOKIE_CHECK_INTERVAL", interval)

    _FakeChecker.instances = []
    monkeypatch.setattr(server_mod, "CookieChecker", _FakeChecker)

    app = server_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as c:
                r = await c.get("/healthz")
                assert r.status_code == 200
    finally:
        await tmp_engine.dispose()
    return _FakeChecker.instances


async def test_lifespan_no_checker_when_interval_zero(tmp_path, monkeypatch):
    """COOKIE_CHECK_INTERVAL=0(默认):lifespan 完全不构造 cookie checker。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 0)
    assert instances == []


async def test_lifespan_starts_and_stops_checker_when_positive(tmp_path, monkeypatch):
    """COOKIE_CHECK_INTERVAL>0:lifespan 构造并 start,shutdown 时 stop。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 42)
    assert len(instances) == 1
    checker = instances[0]
    assert checker.interval == 42
    assert checker.started is True
    assert checker.stopped is True
