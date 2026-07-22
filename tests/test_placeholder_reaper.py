"""placeholder_reaper 占位废账号 TTL 兜底回收单测。

覆盖:
- reap_placeholders_once:超 TTL 的占位行(user_id 空 + xhs_account_ 前缀)及其授权行被删;
  未超 TTL 的不删;带 user_id 的行(即便超龄)不删。
- lifespan 开关(类比 browser_reaper):PLACEHOLDER_REAP_INTERVAL=0 不起 reaper;>0 起 + 干净 stop。

隔离:reap 走 db_factory(每测试独立临时 sqlite);lifespan 用隔离库驱动真实 create_app。
"""

from datetime import datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
import app.server as server_mod
from app.core.security import hash_apikey
from app.models import Operator, OperatorAccountAccess, XhsAccount
from app.services import cookie_service
from app.services import placeholder_reaper as reaper_mod
from app.services.placeholder_reaper import reap_placeholders_once


async def _make_operator(session_factory, name: str = "op") -> int:
    """造一个已提交的运营者,返回其 id。"""
    async with session_factory() as s:
        op = Operator(
            name=name, apikey_hash=hash_apikey(name), role="operator", enabled=True
        )
        s.add(op)
        await s.commit()
        return op.id


async def _push(session_factory, op_id, name, user_info):
    """用 import_cookies 落一个账号(占位或真号),返回 account_id。"""
    async with session_factory() as s:
        op = await s.get(Operator, op_id)
        acc, _created, _cleaned = await cookie_service.import_cookies(
            s, op, name, [{"name": "web_session", "value": "x"}], user_info
        )
        return acc.id


async def _age_created_at(session_factory, account_id, delta: timedelta):
    """把某账号 created_at 拨到 utcnow - delta,模拟"很久以前创建"。"""
    async with session_factory() as s:
        row = await s.get(XhsAccount, account_id)
        row.created_at = datetime.utcnow() - delta
        await s.commit()


# ---------------- reap_placeholders_once 行为 ----------------


async def test_reap_deletes_stale_placeholder(db_factory, monkeypatch):
    """超 TTL 的占位行 + 其授权行被删;未超 TTL 的占位与带 user_id 的行都保留。"""
    monkeypatch.setattr(reaper_mod.settings, "PLACEHOLDER_TTL_HOURS", 24)
    op_id = await _make_operator(db_factory)

    # 真号先落库(此刻无占位行,不触发方向 A 的近窗自愈),再拨老到超龄——带 user_id,reaper 不碰。
    real_id = await _push(db_factory, op_id, "真号", {"user_id": "u1"})
    await _age_created_at(db_factory, real_id, timedelta(hours=25))

    # 占位行:一条超龄(应被 reaper 删)、一条刚建(未超 TTL,应保留)。
    stale_id = await _push(db_factory, op_id, "xhs_account_1", None)
    await _age_created_at(db_factory, stale_id, timedelta(hours=25))
    fresh_id = await _push(db_factory, op_id, "xhs_account_2", None)

    deleted = await reap_placeholders_once(db_factory)
    assert deleted == 1

    async with db_factory() as s:
        assert (await s.get(XhsAccount, stale_id)) is None  # 超龄占位被删
        assert (await s.get(XhsAccount, fresh_id)) is not None  # 未超龄占位保留
        assert (await s.get(XhsAccount, real_id)) is not None  # 带 user_id 不删
        # 被删占位的授权行同步清除
        cnt = (
            await s.execute(
                select(func.count())
                .select_from(OperatorAccountAccess)
                .where(OperatorAccountAccess.xhs_account_id == stale_id)
            )
        ).scalar()
        assert cnt == 0


async def test_reap_noop_when_nothing_stale(db_factory, monkeypatch):
    """无超龄占位 → 删除 0,不误伤。"""
    monkeypatch.setattr(reaper_mod.settings, "PLACEHOLDER_TTL_HOURS", 24)
    op_id = await _make_operator(db_factory)
    await _push(db_factory, op_id, "xhs_account_x", None)  # 刚建
    assert await reap_placeholders_once(db_factory) == 0


async def test_reap_literal_prefix_not_wildcard(db_factory, monkeypatch):
    """修1:reaper 按字面前缀——形如 xhsXaccountY 的 user_id 空真号即便超龄也不被删。"""
    monkeypatch.setattr(reaper_mod.settings, "PLACEHOLDER_TTL_HOURS", 24)
    op_id = await _make_operator(db_factory)
    # 命中旧通配符但不匹配字面前缀 'xhs_account_' 的真号名
    decoy_id = await _push(db_factory, op_id, "xhsXaccountY123", None)
    await _age_created_at(db_factory, decoy_id, timedelta(hours=25))

    assert await reap_placeholders_once(db_factory) == 0
    async with db_factory() as s:
        assert (await s.get(XhsAccount, decoy_id)) is not None


# ---------------- lifespan 开关(类比 browser_reaper) ----------------


class _FakeReaper:
    """记录构造/启停的假 reaper,替换 server.PlaceholderReaper 以观测 lifespan 开关。"""

    instances: list["_FakeReaper"] = []

    def __init__(self, session_factory, interval):
        self.session_factory = session_factory
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
    monkeypatch.setattr(server_mod.settings, "PLACEHOLDER_REAP_INTERVAL", interval)

    _FakeReaper.instances = []
    monkeypatch.setattr(server_mod, "PlaceholderReaper", _FakeReaper)

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
    """PLACEHOLDER_REAP_INTERVAL=0:lifespan 完全不构造 reaper。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 0)
    assert instances == []


async def test_lifespan_starts_and_stops_reaper_when_positive(tmp_path, monkeypatch):
    """PLACEHOLDER_REAP_INTERVAL>0:lifespan 构造并 start,shutdown 时 stop。"""
    instances = await _drive_lifespan_with_interval(tmp_path, monkeypatch, 42)
    assert len(instances) == 1
    reaper = instances[0]
    assert reaper.interval == 42
    assert reaper.started is True
    assert reaper.stopped is True
