"""browser_reaper 孤儿回收单测(不扫真 /proc、不杀真进程)。

隔离手法:monkeypatch 进程枚举 ``_scan_processes``(返 ``(pid, is_camoufox,
account_id, age_seconds)`` 元组列表)+ ``os.kill``(记录被杀 pid)+ ``account_locks``
(假锁控制 locked 状态),使 reap_once 的判定行为可确定性验证,绝不触碰真实进程。

覆盖:
- should_reap 纯判定三条件(is_camoufox + not locked + age>threshold)缺一不杀。
- reap_once:超龄无主杀 / 锁持有不杀(即便超龄)/ 未超龄不杀 / 非 camoufox 不碰。
- reap_once 异常安全:枚举抛异常返 0 不冒泡;单进程 kill 抛异常不中断其余。
- lifespan 开关:BROWSER_REAP_INTERVAL=0 不起 reaper;>0 起 + shutdown 干净 stop。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
import app.server as server_mod
from app.browser import browser_reaper as reaper_mod
from app.browser.browser_reaper import (
    _account_id_from_argv,
    _proc_age_seconds,
    reap_once,
    should_reap,
)
from app.browser.profile_guard import browser_profiles_root
from app.core.config import settings


# ---------------- 假件:控制锁状态,避免用真 asyncio.Lock 与跨测泄漏 ----------------


class _FakeLock:
    """只暴露 locked() 的假锁,预置返回值。"""

    def __init__(self, locked: bool) -> None:
        self._locked = locked

    def locked(self) -> bool:
        return self._locked


class _FakeLocks:
    """按 account_id 返回预置 locked 状态的假锁表(未列出的默认未持有)。"""

    def __init__(self, states: dict[int, bool]) -> None:
        self._states = states

    def get(self, account_id: int) -> _FakeLock:
        return _FakeLock(self._states.get(account_id, False))


def _patch(monkeypatch, procs, states=None, age_threshold=900):
    """统一装配:替换进程枚举 / 锁表 / 阈值,并返回被杀 pid 列表。"""
    killed: list[int] = []

    def fake_kill(pid, sig):
        killed.append(pid)

    monkeypatch.setattr(reaper_mod, "_scan_processes", lambda: list(procs))
    monkeypatch.setattr(reaper_mod.os, "kill", fake_kill)
    monkeypatch.setattr(reaper_mod, "account_locks", _FakeLocks(states or {}))
    monkeypatch.setattr(reaper_mod.settings, "BROWSER_REAP_AGE", age_threshold)
    return killed


# ---------------- 纯判定 should_reap ----------------


def test_should_reap_all_conditions():
    """三条件齐备才杀;缺任一(非camoufox / 锁持有 / 未超龄)都不杀。"""
    assert should_reap(True, False, 1000, 900) is True
    assert should_reap(False, False, 1000, 900) is False  # 非 camoufox
    assert should_reap(True, True, 1000, 900) is False  # 锁被持有
    assert should_reap(True, False, 100, 900) is False  # 未超龄
    assert should_reap(True, False, 900, 900) is False  # 恰等于阈值(须严格大于)


# ---------------- reap_once 行为 ----------------


def test_reap_kills_stale_ownerless(monkeypatch):
    """camoufox 进程 + 账号锁未持有 + 存活>REAP_AGE → 被杀,返回杀数 1。"""
    killed = _patch(monkeypatch, [(111, True, 5, 1000)], states={5: False})
    assert reap_once() == 1
    assert killed == [111]


def test_reap_skips_locked(monkeypatch):
    """账号锁被持有(有在跑操作)→ 不杀,即便已超龄。"""
    killed = _patch(monkeypatch, [(111, True, 5, 5000)], states={5: True})
    assert reap_once() == 0
    assert killed == []


def test_reap_skips_young(monkeypatch):
    """未超 REAP_AGE → 不杀(即便无主)。"""
    killed = _patch(monkeypatch, [(111, True, 5, 100)], states={5: False})
    assert reap_once() == 0
    assert killed == []


def test_reap_skips_non_camoufox(monkeypatch):
    """非 camoufox 进程 → 完全不碰(即便超龄无主)。"""
    killed = _patch(monkeypatch, [(111, False, None, 9999)], states={})
    assert reap_once() == 0
    assert killed == []


def test_reap_skips_unknown_account(monkeypatch):
    """camoufox 但无法从 argv 解析出 account_id(account_id=None)→ 保守不杀。"""
    killed = _patch(monkeypatch, [(111, True, None, 5000)], states={})
    assert reap_once() == 0
    assert killed == []


def test_reap_scan_exception_safe(monkeypatch):
    """进程枚举抛异常 → reap_once 不冒泡,返回 0。"""

    def boom():
        raise RuntimeError("扫描炸了")

    monkeypatch.setattr(reaper_mod, "_scan_processes", boom)
    assert reap_once() == 0  # 不抛


def test_reap_kill_exception_safe(monkeypatch):
    """单进程 kill 抛异常(如已退出/无权限)→ 不中断,其余进程照杀,返回成功数。"""
    killed: list[int] = []

    def flaky_kill(pid, sig):
        if pid == 111:
            raise ProcessLookupError("已退出")
        killed.append(pid)

    monkeypatch.setattr(
        reaper_mod, "_scan_processes", lambda: [(111, True, 5, 1000), (222, True, 6, 1000)]
    )
    monkeypatch.setattr(reaper_mod.os, "kill", flaky_kill)
    monkeypatch.setattr(reaper_mod, "account_locks", _FakeLocks({}))
    monkeypatch.setattr(reaper_mod.settings, "BROWSER_REAP_AGE", 900)

    assert reap_once() == 1  # 222 成功,111 失败被吞
    assert killed == [222]


# ---------------- 纯函数:_account_id_from_argv(误杀防线命根) ----------------


def _root(monkeypatch, tmp_path):
    """把 DATA_DIR 指到 tmp,返回 browser_profiles_root() 的真实绝对前缀。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    return browser_profiles_root()


def test_account_id_prefix_trap(monkeypatch, tmp_path):
    """profile 子文件路径 account_20/prefs.js → 解析 20,绝不截成 2。"""
    root = _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", "-profile", str(root / "account_20" / "prefs.js")]
    assert _account_id_from_argv(argv) == 20


def test_account_id_exact_dir(monkeypatch, tmp_path):
    """argv token 恰为 profile 目录本身 → 解析该 id。"""
    root = _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", "-profile", str(root / "account_3")]
    assert _account_id_from_argv(argv) == 3


def test_account_id_not_our_root(monkeypatch, tmp_path):
    """含 account_ 但不在 browser_profiles_root 下 → None(不误认他人路径)。"""
    _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", "-profile", "/home/x/account_5/foo"]
    assert _account_id_from_argv(argv) is None


def test_account_id_non_digit_suffix(monkeypatch, tmp_path):
    """account_ 后缀非数字 → None。"""
    root = _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", "-profile", str(root / "account_abc" / "x")]
    assert _account_id_from_argv(argv) is None


def test_account_id_concatenated_token_not_matched(monkeypatch, tmp_path):
    """``--profile=<root>/account_7`` 整体作一个 token(不以 root 前缀开头)→ None。

    与 ``_argv_targets_profile`` 逐 token 语义一致:真实 camoufox 把 profile 作独立
    token 传,故拼接形式为预期不命中。
    """
    root = _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", f"--profile={root}/account_7"]
    assert _account_id_from_argv(argv) is None


def test_account_id_no_profile_token(monkeypatch, tmp_path):
    """argv 里没有任何 profile token → None。"""
    _root(monkeypatch, tmp_path)
    argv = ["camoufox-bin", "--headless", "--foo=bar"]
    assert _account_id_from_argv(argv) is None


# ---------------- 纯函数:_proc_age_seconds(解析失败保守视为很新) ----------------


def test_proc_age_nonexistent_pid_returns_zero():
    """不存在的 pid → 读 /proc 失败返回 0.0(视作很新,不被超龄命中 → 保守不杀)。"""
    assert _proc_age_seconds(999999999, boot_time=0.0, now=1e9) == 0.0


# ---------------- lifespan 开关(类比 cookie_checker) ----------------


class _FakeReaper:
    """记录构造/启停的假 reaper,替换 server.BrowserReaper 以观测 lifespan 开关。"""

    instances: list["_FakeReaper"] = []

    def __init__(self, interval):
        self.interval = interval
        self.started = False
        self.stopped = False
        _FakeReaper.instances.append(self)

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


async def _drive_lifespan_with_interval(tmp_path, monkeypatch, interval):
    """用隔离库驱动一次 create_app 的真实 lifespan,返回捕获的假 reaper 实例列表。"""
    tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/l.db", future=True)
    tmp_smk = async_sessionmaker(tmp_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_smk)
    monkeypatch.setattr(server_mod.settings, "BROWSER_REAP_INTERVAL", interval)

    _FakeReaper.instances = []
    monkeypatch.setattr(server_mod, "BrowserReaper", _FakeReaper)

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
    return _FakeReaper.instances


async def test_lifespan_no_reaper_when_interval_zero(tmp_path, monkeypatch):
    """BROWSER_REAP_INTERVAL=0:lifespan 完全不构造 reaper。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 0)
    assert instances == []


async def test_lifespan_starts_and_stops_reaper_when_positive(tmp_path, monkeypatch):
    """BROWSER_REAP_INTERVAL>0:lifespan 构造并 start,shutdown 时 stop。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 42)
    assert len(instances) == 1
    reaper = instances[0]
    assert reaper.interval == 42
    assert reaper.started is True
    assert reaper.stopped is True
