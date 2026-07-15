"""进程级浏览器并发闸:封顶同时运行的 camoufox 数,超出排队不崩。

publish / cookie-check / note-export 三条浏览器入口,在起 camoufox 前统一套
``async with browser_slot():``——总浏览器数 ≤ ``settings.BROWSER_CONCURRENCY``,
超出的操作在信号量上 await 排队(不拒绝),前面的操作 stop 释放名额后依次放行。

单 uvicorn worker = 单事件循环:模块级 ``asyncio.Semaphore`` 单例懒建(首次在 async
上下文调用时创建/首次阻塞时绑定当前 loop,全程一致),之后复用。信号量非锁,每操作
acquire→release,无死锁风险;出作用域(含异常)由 ``async with`` 保证名额归还。
"""

import asyncio
from contextlib import asynccontextmanager

from app.core.config import settings

# 进程级信号量单例:首次 _get_semaphore() 时按 settings.BROWSER_CONCURRENCY 懒建。
_sem: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """取进程级浏览器名额信号量;首次调用按 settings.BROWSER_CONCURRENCY 懒建,之后复用。

    懒建而非模块加载即建:让信号量在运行中的事件循环里创建/首次阻塞即绑定该 loop
    (单 worker 单 loop 下全程一致),避免在无 loop 的 import 期构造并误绑。
    """
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(settings.BROWSER_CONCURRENCY)
    return _sem


@asynccontextmanager
async def browser_slot():
    """占用一个浏览器名额;满则 await 排队,出作用域(含异常)自动归还。

    用法::

        async with browser_slot():
            await asyncio.to_thread(<起 camoufox 的同步活>)
    """
    async with _get_semaphore():
        yield


def _reset_for_test() -> None:
    """把模块级信号量单例置空(仅测试用)。

    测试 monkeypatch settings.BROWSER_CONCURRENCY 后调此函数,使下次 _get_semaphore()
    按新并发值重建;也用于隔离各测试的信号量状态。
    """
    global _sem
    _sem = None
