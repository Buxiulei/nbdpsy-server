"""一次性 HTML 截图链路的 playwright 护栏：进出都有界 + 收尾对取消免疫。

为什么存在：playwright 1.56 的 `Browser.new_page()` / `new_context()` 签名里
**一个 timeout 参数都没有**，`async_playwright().start()` 同样没有；而
launch / goto / wait_for_load_state / screenshot 都带 30s 默认超时。
所以 `start` 和 `new_page` 是「起浏览器 → 截一张图 → 关掉」这类一次性链路上
**仅有的两个能永久挂死的 await** —— 挂死不抛异常，`except Exception` 接不住，
会把调用方整个 celery task 一起拖死。

本模块把 #379 在 `ai/synthid/screenshot_reraster.py` 里落地并被真浏览器 A/B
验证过的那套护栏抽成公共件，供四个一次性截图点复用：
reraster / html_card_renderer / cover_renderer / video_transport.still_image。

**不适用**长驻浏览器管理器（gemini_page_pool / smart_browser / xhs_automation /
cdp_browser_service / camoufox_helper 等）：它们的浏览器生命周期跨任务复用，
收尾语义与这里"用完立刻整棵杀干净"完全不同。

超时值本模块不读配置，一律由调用方传入（reraster 有自己的字段，另外三处共用
`BROWSER_SHOT_TIMEOUT`）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# 收尾阶段的预算：三步共享一个总预算，不再各自串行叠加，
# 所以墙钟上限是加一次 _SHUTDOWN_BUDGET_S 而不是加 10+5+5。
_SHUTDOWN_BUDGET_S = 10.0   # 整个收尾阶段的总预算（close / stop / 强杀共享）
_CLOSE_TIMEOUT_S = 10.0     # browser.close() 单步上限
_STOP_TIMEOUT_S = 5.0       # playwright.stop()（即 cm.__aexit__）单步上限
_KILL_WAIT_S = 5.0          # 强杀 driver 后回收进程的单步上限
_PROC_POLL_INTERVAL_S = 0.1  # 等 driver 子进程出现的轮询间隔


async def guarded_step(coro, deadline: float, step_name: str, *, label: str = "browser"):
    """按剩余总预算 await 一步；超时统一转成带步骤名的 TimeoutError。

    用「总 deadline + 每步取剩余」而不是「每步各给一份超时」，
    保证无论卡在哪一步，浏览器阶段的墙钟都硬封顶在 timeout_s。

    label 只影响错误文案与日志前缀（各调用点的失败语义已被各自的测试锁死，
    例如 reraster 的 "reraster timeout at {step}"），不改变任何行为。
    """
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return await asyncio.wait_for(coro, timeout=remaining)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{label} timeout at {step_name}") from None


def _driver_proc(cm):
    """取 playwright driver 子进程句柄；拿不到返回 None（全程防御取值）。"""
    try:
        conn = getattr(cm, "_connection", None)
        transport = getattr(conn, "_transport", None)
        return getattr(transport, "_proc", None)
    except Exception:  # noqa: BLE001
        return None


def _cancel_connection_task(cm, label: str = "browser") -> None:
    """取消本 cm 自己的 Connection.run() 后台任务（拿不到 driver 进程时的最后一道兜底）。

    PlaywrightContextManager.__aenter__ 里 `loop.create_task(self._connection.run())`
    没有留任何引用，而 driver 子进程是这个后台任务里 PipeTransport.connect() 的
    create_subprocess_exec 才创建的。start() 早期超时时，这个任务会在我们放弃之后
    才把 driver 拉起来 —— 在 celery 常驻进程里就是永久孤儿进程 + 永久 pending 任务。
    这里按「协程帧里的 self 正是本 cm 的 connection」精确匹配，
    绝不误伤同一个 loop 里其它并发截图的任务。
    """
    conn = getattr(cm, "_connection", None)
    if conn is None:
        return
    try:
        tasks = asyncio.all_tasks()
    except Exception:  # noqa: BLE001
        return
    for task in tasks:
        try:
            coro = task.get_coro()
            if getattr(coro, "__qualname__", "") != "Connection.run":
                continue
            frame = getattr(coro, "cr_frame", None)
            if frame is not None and frame.f_locals.get("self") is conn:
                task.cancel()
                logger.warning("%s 取消残留的 Connection.run 后台任务", label)
        except Exception:  # noqa: BLE001
            continue


async def bounded_shutdown(cm, browser, *, label: str = "browser") -> None:
    """有界收尾：任何一步都不抛、不无限等、被取消也照样跑完。

    1) browser.close()；2) cm.__aexit__() 即 playwright.stop()，driver 正常退出会连带
    chromium 退出；3) 只有第 2 步失败/超时才强杀 driver 进程。三步共享 _SHUTDOWN_BUDGET_S
    一个总预算，保证收尾本身也有墙钟上限。

    两个必须守住的边界：
    - 取消免疫：CancelledError 是 BaseException，`except Exception` 接不住。收尾窗口正是
      连接卡死时最宽的时候，一旦被外层 cancel 打断，后面的 stop / 强杀全跳过 →
      整棵浏览器进程树泄漏（实测 6 个进程）。所以这里捕获 BaseException，记下取消、
      跑完收尾，最后再重抛，既不泄漏进程也不吞掉调用方的取消语义。
    - start() 超时场景：browser 为 None，且 driver 子进程往往还没被后台任务拉起来，
      所以强杀分支要在剩余预算内轮询等 _proc 出现，实在等不到才去取消后台任务。
    """
    deadline = time.monotonic() + _SHUTDOWN_BUDGET_S
    pending_exc: Optional[BaseException] = None

    async def _bounded(coro, budget: float, what: str) -> bool:
        """收尾单步：预算取 min(单步上限, 收尾剩余)；失败/超时/被取消都只告警不抛。"""
        nonlocal pending_exc
        remaining = max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(coro, timeout=min(budget, remaining))
            return True
        except BaseException as e:  # noqa: BLE001 — 含 CancelledError，收尾不许被打断
            # CancelledError / KeyboardInterrupt / SystemExit 都不是 Exception：
            # 先记下、等收尾跑完再重抛，既不泄漏进程也不吞掉调用方的取消/中断语义
            if not isinstance(e, Exception):
                pending_exc = e
            logger.warning("%s %s 收尾失败: %r", label, what, e)
            return False

    if browser is not None:
        await _bounded(browser.close(), _CLOSE_TIMEOUT_S, "browser.close")

    if not await _bounded(cm.__aexit__(), _STOP_TIMEOUT_S, "playwright.stop"):
        # 强杀兜底：driver 进程句柄 = cm._connection._transport._proc，可能还没创建，
        # 在剩余预算内轮询等它出现，否则它会在我们放弃之后才被后台任务拉起来
        proc = _driver_proc(cm)
        while proc is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.sleep(min(_PROC_POLL_INTERVAL_S, remaining))
            except BaseException as e:  # noqa: BLE001
                if not isinstance(e, Exception):
                    pending_exc = e
                if isinstance(e, asyncio.CancelledError):
                    continue  # 被取消也继续轮询，整体仍由 deadline 封顶
                break
            proc = _driver_proc(cm)

        if proc is None:
            logger.warning("%s 等不到 playwright driver 进程句柄，改为取消后台任务", label)
            _cancel_connection_task(cm, label)
        else:
            try:
                proc.kill()
                logger.warning(
                    "%s 强杀 playwright driver pid=%s", label, getattr(proc, "pid", "?")
                )
            except BaseException as e:  # noqa: BLE001
                logger.warning("%s 强杀 playwright driver 失败: %r", label, e)
            await _bounded(proc.wait(), _KILL_WAIT_S, "driver.wait")

    if pending_exc is not None:
        raise pending_exc


@asynccontextmanager
async def guarded_chromium(timeout_s: float, *, label: str = "browser"):
    """产出 (browser, deadline)：进出都有界，收尾对取消免疫 + 强杀兜底。

    用法：

        async with guarded_chromium(timeout_s, label="xxx") as (browser, deadline):
            page = await guarded_step(browser.new_page(...), deadline, "new_page", label="xxx")
            ...

    `deadline` 是整个浏览器阶段共享的总期限，调用方每一步都要用 guarded_step 包住，
    否则 new_page 这种无超时的 await 依然能挂死。

    launch 不开放 kwargs：现有四个调用点全是 headless 默认参数（playwright 的
    `launch()` 默认就是 headless=True），不做没人要的可配置性。
    """
    # 函数体内 import：让测试可以 patch 源模块命名空间 playwright.async_api.async_playwright
    from playwright.async_api import async_playwright

    deadline = time.monotonic() + timeout_s
    # 拆开 `async with` 用显式生命周期：收尾也必须自己控超时，否则收尾本身能挂死
    cm = async_playwright()
    browser = None
    try:
        p = await guarded_step(cm.start(), deadline, "playwright.start", label=label)
        browser = await guarded_step(
            p.chromium.launch(headless=True), deadline, "chromium.launch", label=label
        )
        yield browser, deadline
    finally:
        await bounded_shutdown(cm, browser, label=label)
