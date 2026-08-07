"""CookieChecker 行为 + lifespan 开关测试(不起真浏览器)。

隔离手法与其它工具测试一致:tmp sqlite 引擎 + async_sessionmaker;
sync_client.check_login_once 被 monkeypatch 成假实现,断言状态写回。

覆盖:
- check_once:检 cookie_status='valid' 与 'unknown'(有 cookie)的号,写回三态 +
  last_check_at + 回填资料;无 cookie 的 valid 号跳过(不误改状态);invalid /
  captcha / restricted 一律不巡(需人工处置的状态不自动重试)。
- 转正引导链:非 valid → valid 时登记台账同步 + newcomer 补量;valid → valid 不登记。
- account_gap=0 时不引入号间隔延时(测试可秒级跑完)。
- start/stop 生命周期:起循环 → 至少跑一轮 → 干净 stop(无遗留 task)。
- lifespan 开关:COOKIE_CHECK_INTERVAL=0 不起 checker;>0 起 + shutdown 干净 stop。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
import app.server as server_mod
from app.browser import cookie_checker as checker_mod
from app.browser.account_locks import account_locks
from app.browser.cookie_checker import CookieChecker
from app.core.security import encrypt_cookies
from app.models.browser_job import BrowserJob
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


async def _jobs(factory, kind: str) -> list[BrowserJob]:
    """取某 kind 的全部台账行(按创建顺序),供引导链断言用。"""
    async with factory() as s:
        rows = await s.execute(
            select(BrowserJob).where(BrowserJob.kind == kind).order_by(BrowserJob.id)
        )
        return list(rows.scalars().all())


async def test_check_once_only_valid_and_writes_back(smk, monkeypatch):
    """只检 valid 号:写回 check_login_once 的三态 + 回填 user_info;invalid 号不检。"""
    valid_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])
    invalid_id = await _add_account(smk, "失效号", "invalid", [{"name": "b", "value": "y"}])

    seen: list[int] = []

    def fake_check(account_id, cookies, probe_user_id=None):
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
        assert v.name == "小明"  # 内部展示名跟随昵称实时更新(原名"有效号"被最新昵称覆盖)
        assert v.user_id == "u1"
        iv = await s.get(XhsAccount, invalid_id)
        assert iv.cookie_status == "invalid"  # 未被巡检,保持原状


async def test_check_account_holds_lock_during_browser(smk, monkeypatch):
    """周期巡检持账号锁跑浏览器段:check_login_once 被调时同号锁必须已持有,
    使孤儿回收 reaper 视其"有主"不误杀,且与同号 publish/手动检测/导出串行防 profile 争用。"""
    acc_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])
    locked_at_call: dict = {}

    def fake_check(account_id, cookies, probe_user_id=None):
        # 浏览器段在线程内跑,此刻事件循环侧应已持有该号账号锁(locked() 读 bool,跨线程读安全)。
        locked_at_call["v"] = account_locks.get(account_id).locked()
        return {"status": "valid"}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    await checker.check_once()

    assert locked_at_call["v"] is True  # 浏览器段内账号锁已持有(reaper 不会误杀)
    assert account_locks.get(acc_id).locked() is False  # 检测结束锁归还,不泄漏


async def test_check_once_error_preserves_status(smk, monkeypatch):
    """基础设施失败(check_login_once 返回 error):不写回,保留原 cookie_status='valid'。"""
    valid_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])

    def fake_check(account_id, cookies, probe_user_id=None):
        return {"status": "error", "user_info": None, "reason": "浏览器启动失败:boom"}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    checked = await checker.check_once()

    assert checked == 1  # 仍算已检测(浏览器确实尝试过)
    async with smk() as s:
        v = await s.get(XhsAccount, valid_id)
        assert v.cookie_status == "valid"  # 好号未被误标失效
        assert v.last_check_at is None  # error 态不写任何字段


async def test_check_once_skips_valid_without_cookies(smk, monkeypatch):
    """cookie_status=valid 但无 login_cookies 的号:跳过检测,不误改状态。"""
    empty_id = await _add_account(smk, "空号", "valid", cookies=None)

    called = {"n": 0}

    def fake_check(account_id, cookies, probe_user_id=None):
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


# ---------------- 新号纳入巡检(unknown 有 cookie) ----------------


async def test_check_once_covers_unknown_with_cookies(smk, monkeypatch):
    """巡检选号 = valid + unknown(有 cookie)。

    新号插件推 cookie 落库时 cookie_status='unknown',若巡检只认 valid,这个号永远
    没人替它检测转正,也就永远当不了矩阵互动方(账号 10/11 实例)。
    invalid / captcha / restricted 维持不巡:那三态需人工处置,自动重试只会把限流催得更狠。
    """
    valid_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])
    unknown_id = await _add_account(smk, "新号", "unknown", [{"name": "b", "value": "y"}])
    await _add_account(smk, "新号无cookie", "unknown", cookies=None)
    await _add_account(smk, "失效号", "invalid", [{"name": "c", "value": "z"}])
    await _add_account(smk, "验证码号", "captcha", [{"name": "d", "value": "z"}])
    await _add_account(smk, "风控号", "restricted", [{"name": "e", "value": "z"}])

    seen: list[int] = []

    def fake_check(account_id, cookies, probe_user_id=None):
        seen.append(account_id)
        return {"status": "captcha", "user_info": None}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    await checker.check_once()

    # 无 cookie 的 unknown 号连选都不该被选(检测必败,纯浪费一次浏览器)
    assert seen == [valid_id, unknown_id]


# ---------------- 转正引导链 ----------------


async def test_patrol_unknown_to_valid_kicks_onboarding_chain(smk, monkeypatch):
    """unknown → valid:巡检写回后登记台账同步 + newcomer 补量各一条。

    这两条是新号融进矩阵的两个方向:台账同步把他的历史笔记入库(同步完成会经
    schedule_after_sync 让其余号来互动他),newcomer 补量让他去互动别人的历史笔记。
    """
    acc_id = await _add_account(smk, "新号", "unknown", [{"name": "a", "value": "x"}])

    def fake_check(account_id, cookies, probe_user_id=None):
        return {"status": "valid", "user_info": None}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    await checker.check_once()

    sync_jobs = await _jobs(smk, "note_ledger_sync")
    assert len(sync_jobs) == 1
    assert sync_jobs[0].account_id == acc_id
    assert sync_jobs[0].operator_id == 0  # 非请求上下文的进程内直调
    assert sync_jobs[0].status == "queued"

    backfill_jobs = await _jobs(smk, "interaction_backfill")
    assert len(backfill_jobs) == 1
    assert backfill_jobs[0].account_id == acc_id
    payload = json.loads(backfill_jobs[0].payload)
    assert payload == {
        "scope": "newcomer",
        "target_account_id": None,
        "actor_account_id": acc_id,
        "limit": None,
    }


async def test_patrol_valid_to_valid_skips_onboarding_chain(smk, monkeypatch):
    """valid → valid:老号每轮巡检都过这条路,绝不能每轮都叠一对引导任务。"""
    await _add_account(smk, "老号", "valid", [{"name": "a", "value": "x"}])

    def fake_check(account_id, cookies, probe_user_id=None):
        return {"status": "valid", "user_info": None}

    monkeypatch.setattr(checker_mod.sync_client, "check_login_once", fake_check)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    await checker.check_once()

    assert await _jobs(smk, "note_ledger_sync") == []
    assert await _jobs(smk, "interaction_backfill") == []


async def test_start_stop_runs_at_least_one_cycle(smk, monkeypatch):
    """start → 后台至少跑一轮(检到 valid 号)→ stop 干净退出。"""
    await _add_account(smk, "号A", "valid", [{"name": "a", "value": "x"}])

    calls: list[int] = []

    def fake_check(account_id, cookies, probe_user_id=None):
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
    """驱动 Supervisor 组件装配(组件接线已从 server lifespan 迁入 app.worker,断言随迁)。

    语义与旧 lifespan 驱动等价:interval>0 构造并 start,停机时 stop;=0 完全不构造。
    """
    import app.worker as worker_mod

    tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/l.db", future=True)
    tmp_smk = async_sessionmaker(tmp_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker_mod.settings, "COOKIE_CHECK_INTERVAL", interval)

    _Fake = _FakeChecker
    _Fake.instances = []
    monkeypatch.setattr(worker_mod, "CookieChecker", _Fake)

    sup = worker_mod.Supervisor(tmp_smk, include_video=False)
    try:
        await sup._start_components()
        await sup._stop_components()
    finally:
        await tmp_engine.dispose()
    return _Fake.instances


async def test_lifespan_no_checker_when_interval_zero(tmp_path, monkeypatch):
    """COOKIE_CHECK_INTERVAL=0(默认):supervisor 完全不构造 cookie checker。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 0)
    assert instances == []


async def test_lifespan_starts_and_stops_checker_when_positive(tmp_path, monkeypatch):
    """COOKIE_CHECK_INTERVAL>0:supervisor 构造并 start,停机时 stop。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 42)
    assert len(instances) == 1
    checker = instances[0]
    assert checker.interval == 42
    assert checker.started is True
    assert checker.stopped is True


async def test_check_account_routes_through_watchdog(smk, monkeypatch):
    """周期巡检的浏览器段必须走 _run_check_with_watchdog(与手动检测同一把看门狗)。

    背景:ac87011 只给手动检测(cookie_check.execute)加了 180s 看门狗;巡检路径
    (supervisor 进程内直调)同类僵死会无限占着进程内账号锁,同号发布/检测全部排队。
    本测锁定接线:巡检不再裸 to_thread(check_login_once),而是经同一个看门狗助手。
    """
    acc_id = await _add_account(smk, "有效号", "valid", [{"name": "a", "value": "x"}])
    called = {"v": False}

    async def fake_watchdog(account_id, cookies, probe_user_id):
        called["v"] = True
        return {"status": "error", "user_info": None, "reason": "看门狗桩"}

    monkeypatch.setattr(checker_mod, "_run_check_with_watchdog", fake_watchdog)

    checker = CookieChecker(smk, interval=999, account_gap=0)
    executed = await checker._check_account(acc_id)

    assert executed is True   # error 态也算已检测(不写回,保留原状态)
    assert called["v"] is True
