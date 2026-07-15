"""进程级浏览器并发闸(app.browser.browser_gate)单测。

覆盖设计三点:
- 峰值封顶 + 排队不丢:BROWSER_CONCURRENCY=2 时,5 个并发任务同时进闸的峰值 ≤2,
  且 5 个最终全部完成(超出的排队等待,不被拒绝、不丢失);
- 异常归还名额:闸内抛异常,出作用域(async with)仍归还名额,后续 acquire 不阻塞;
- 单例复用:_get_semaphore() 多次返回同一对象(单 worker 单 loop 全程复用)。

隔离:每个测试前后 _reset_for_test() 置空模块级信号量单例,避免跨测试/跨并发值污染。
"""

import asyncio

import pytest

from app.browser import browser_gate


@pytest.fixture(autouse=True)
def _reset_gate():
    """每个测试前后重置信号量单例,隔离并发值与 loop 绑定状态。"""
    browser_gate._reset_for_test()
    yield
    browser_gate._reset_for_test()


async def test_gate_caps_concurrency(monkeypatch):
    """BROWSER_CONCURRENCY=2:5 个并发任务同时在闸内的峰值 ≤2,且 5 个全部完成(排队不丢)。"""
    monkeypatch.setattr(browser_gate.settings, "BROWSER_CONCURRENCY", 2)
    browser_gate._reset_for_test()  # 令下次懒建按 patch 后的并发值(=2)重建

    state = {"cur": 0, "peak": 0, "done": 0}

    async def worker():
        async with browser_gate.browser_slot():
            # 单线程事件循环下计数更新是原子的(await 前完成读改写)
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.05)  # 持槽期间给其它任务排队的机会
            state["cur"] -= 1
        state["done"] += 1

    await asyncio.gather(*(worker() for _ in range(5)))

    assert state["peak"] <= 2  # 同时在闸内的浏览器数不超过上限
    assert state["done"] == 5  # 超出上限的排队等待,最终全部完成(不丢不拒)
    assert state["cur"] == 0  # 名额全部归还


async def test_gate_releases_on_exception(monkeypatch):
    """闸内抛异常:名额仍在出作用域时归还,后续连续 acquire 不阻塞。"""
    monkeypatch.setattr(browser_gate.settings, "BROWSER_CONCURRENCY", 1)
    browser_gate._reset_for_test()

    with pytest.raises(ValueError):
        async with browser_gate.browser_slot():
            raise ValueError("闸内炸了")

    # 名额未泄漏:唯一名额已归还,连续 3 次 acquire 都能立刻拿到(否则 wait_for 超时)
    async def acquire_once():
        async with browser_gate.browser_slot():
            return True

    for _ in range(3):
        assert await asyncio.wait_for(acquire_once(), timeout=1.0) is True


async def test_singleton():
    """_get_semaphore() 多次返回同一信号量对象(进程级单例复用)。"""
    first = browser_gate._get_semaphore()
    second = browser_gate._get_semaphore()
    assert first is second
