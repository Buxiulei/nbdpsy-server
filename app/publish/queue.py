"""发布内存队列 + 每账号互斥锁(替代 celery 的立即发路径)。

- ``AccountLocks``:按 account_id 惰性分配同一把 ``asyncio.Lock``,保证同号发布串行
  (禁并发发布)。
- ``PublishQueue``:内存 ``asyncio.Queue`` + concurrency 个 worker 协程;``submit`` 立即
  入队,worker 取 job_id 调注入的 runner。单个 job 异常被捕获记录,不拖垮 worker。
"""

import asyncio
from typing import Awaitable, Callable

from loguru import logger


class AccountLocks:
    """按 account_id 惰性分配 ``asyncio.Lock``;同一 account_id 恒返回同一把锁。

    同号并发发布会互相踩浏览器 profile / cookie,必须串行;不同号各自一把锁互不阻塞。
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, account_id: int) -> asyncio.Lock:
        """取该账号的锁;首次访问时惰性创建并缓存。"""
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock


class PublishQueue:
    """内存发布队列:concurrency 个 worker 协程消费 ``submit`` 进来的 job_id。"""

    def __init__(self, concurrency: int) -> None:
        # 至少 1 个 worker,防止 concurrency 配 0 时队列永不被消费
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._runner: Callable[[int], Awaitable] | None = None

    def submit(self, job_id: int) -> None:
        """把 job_id 放入内存队列(非阻塞)。"""
        self._queue.put_nowait(job_id)

    def start(self, runner: Callable[[int], Awaitable]) -> None:
        """起 concurrency 个 worker 协程,循环取 job_id 调 runner;已启动则忽略重复调用。"""
        if self._workers:
            return
        self._runner = runner
        for _ in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        """worker 主循环:阻塞取 job_id → 调 runner;runner 异常只记录不退出。"""
        while True:
            job_id = await self._queue.get()
            try:
                await self._runner(job_id)
            except Exception:
                logger.exception("发布 worker 处理 job {} 异常", job_id)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """优雅停:取消所有 worker 协程并等待其退出。"""
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
