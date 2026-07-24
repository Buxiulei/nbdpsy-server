"""NBDPSY_ROLE 角色接缝测试(app/server.py,设计 §二 进程模型)。

- role=api:lifespan 只做 init_db + bootstrap_admin,不起任何后台组件
  (无 Supervisor / PublishScheduler / CookieChecker / BrowserReaper / PlaceholderReaper),
  API 面(manifest / publish enqueue)照常可用。
- role=all(含缺省):lifespan 经 asyncio.create_task 起 Supervisor.run()(顶替
  PublishScheduler),shutdown 时 request_stop 并等待任务退出;活跃调度器常态化 None。

Supervisor 经 app.worker 模块属性引用,这里 monkeypatch app.worker.Supervisor 注入假件
断言 create_task 的启停行为——不直接 mock 全局 asyncio.create_task:combine_lifespans /
MCP 子应用的 lifespan 内部同样用 create_task,全局替换会误伤无关组件。
"""

import asyncio

import pytest

import app.core.db as db_module
import app.worker as worker_module
from app.browser.browser_reaper import BrowserReaper
from app.browser.cookie_checker import CookieChecker
from app.publish import runtime as runtime_mod
from app.publish.scheduler import PublishScheduler
from app.services.placeholder_reaper import PlaceholderReaper
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeSupervisor:
    """记录构造 / run 调度 / request_stop 的假 Supervisor(顶掉 app.worker.Supervisor)。"""

    instances: list["_FakeSupervisor"] = []

    def __init__(self, session_factory, **kwargs):
        self.session_factory = session_factory
        self.kwargs = kwargs
        self.run_started = asyncio.Event()
        self._stop = asyncio.Event()
        self.stop_requested = False
        type(self).instances.append(self)

    async def run(self):
        self.run_started.set()
        await self._stop.wait()

    def request_stop(self):
        self.stop_requested = True
        self._stop.set()


def _spy_component_starts(monkeypatch) -> list[str]:
    """给四类后台组件的 start 打点:role=api 断言零启动。"""
    started: list[str] = []
    monkeypatch.setattr(
        PublishScheduler, "start", lambda self: started.append("publish_scheduler")
    )
    monkeypatch.setattr(
        CookieChecker, "start", lambda self: started.append("cookie_checker")
    )
    monkeypatch.setattr(
        BrowserReaper, "start", lambda self: started.append("browser_reaper")
    )
    monkeypatch.setattr(
        PlaceholderReaper, "start", lambda self: started.append("placeholder_reaper")
    )
    return started


def _install_fake_supervisor(monkeypatch) -> type["_FakeSupervisor"]:
    """替换 app.worker.Supervisor 为假件,并重置实例登记(monkeypatch 自动还原)。"""
    monkeypatch.setattr(_FakeSupervisor, "instances", [])
    monkeypatch.setattr(worker_module, "Supervisor", _FakeSupervisor)
    return _FakeSupervisor


async def test_role_api_starts_no_background_components(tmp_path, monkeypatch):
    """role=api:不起 Supervisor 与任何调度/巡检/reaper;API 面照常可用。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    fake_cls = _install_fake_supervisor(monkeypatch)
    started = _spy_component_starts(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        # init_db + bootstrap_admin 已跑:manifest(需 root admin 鉴权)照常响应
        r = await c.get("/api/manifest", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert fake_cls.instances == [], "role=api 不得实例化 Supervisor"
        assert runtime_mod.get_active_scheduler() is None
    assert started == [], "role=api 不得启动任何后台组件"


async def test_role_api_publish_enqueue_without_scheduler(tmp_path, monkeypatch):
    """role=api 下建立即发布任务:无活跃调度器,nudge 静默跳过 → 202 且落库 pending。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    _install_fake_supervisor(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号R", "uR", _COOKIES)
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc,
                "title": "T",
                "content": "C",
                "images": ["https://cdn/r.png"],
                "topics": [],
            },
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "pending"


@pytest.mark.parametrize("set_env", [True, False], ids=["显式all", "缺省即all"])
async def test_role_all_starts_supervisor(tmp_path, monkeypatch, set_env):
    """role=all(含缺省):create_task 起 Supervisor.run(),shutdown 时 request_stop 收尾;
    不再装 PublishScheduler 为活跃调度器(常态化 None,nudge 由扫描兜底)。"""
    if set_env:
        monkeypatch.setenv("NBDPSY_ROLE", "all")
    else:
        monkeypatch.delenv("NBDPSY_ROLE", raising=False)
    fake_cls = _install_fake_supervisor(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert len(fake_cls.instances) == 1, "role=all 必须起且仅起一个 Supervisor"
        sup = fake_cls.instances[0]
        # run() 确实被 create_task 调度执行(而非仅构造)
        await asyncio.wait_for(sup.run_started.wait(), timeout=2)
        # 注入的是 lifespan 时点的会话工厂(测试 monkeypatch 的隔离库工厂)
        assert sup.session_factory is db_module.async_session
        # 顶替 PublishScheduler:活跃调度器常态化 None
        assert runtime_mod.get_active_scheduler() is None
    assert sup.stop_requested is True, "shutdown 必须 request_stop 并等待任务退出"
