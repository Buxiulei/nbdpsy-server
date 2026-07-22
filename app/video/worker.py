"""视频调度 worker 进程入口（方案 C：独立 asyncio 进程，``python -m app.video.worker``）。

与 API 进程（8848）隔离——API 重启不杀长任务。流程：init_db 建表 → 起 VideoScheduler 主循环
→ 收 SIGTERM/SIGINT 优雅收尾（停调度器 → 退出）。systemd 单元见
``deploy/systemd/nbdpsy-video-worker.service``。

并发上限读 Settings.VIDEO_WORKER_CONCURRENCY（Track M1 产出的字段），M2 独立开发期该字段尚不
存在 → getattr 兜底 1（设计 §1：单机 CPU 编码 1 足够，可调）。
"""

import asyncio
import signal

from loguru import logger

from app.core.config import settings
from app.core.db import async_session, init_db
from app.video.scheduler import VideoScheduler


async def main() -> None:
    """建表 → 起调度器 → 等停止信号 → 优雅收尾。"""
    await init_db()

    concurrency = int(getattr(settings, "VIDEO_WORKER_CONCURRENCY", 1) or 1)
    scheduler = VideoScheduler(async_session, concurrency=concurrency)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM（systemd stop）/ SIGINT（Ctrl-C）→ 置停止信号，触发优雅收尾。
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - 个别平台不支持 add_signal_handler
            pass

    scheduler.start()
    logger.info("VideoScheduler 已启动（concurrency={}），等待任务与信号...", concurrency)
    try:
        await stop.wait()
    finally:
        logger.info("收到停止信号，优雅收尾中...")
        await scheduler.stop()
        logger.info("VideoScheduler 已停止，进程退出。")


if __name__ == "__main__":
    asyncio.run(main())
