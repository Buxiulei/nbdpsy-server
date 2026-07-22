"""卡片段渲染：品牌 HTML 模板 → Playwright 截图 → ffmpeg 图转视频段（spec §6）。

模板占位符用 __NAME__ + str.replace（CSS 花括号与 str.format 冲突）；
全片右下角 logo 统一由 muxer 水印层叠加，卡片模板不再烘 logo（防双 logo 重影）。
"""
import asyncio
import html as html_escape
import logging
import time
from pathlib import Path

from app.video.pipeline.muxer import _run_ffmpeg
from app.video.pipeline.remake import style

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_FADE_S = 0.5

# 收尾阶段预算（收尾核心语义移植自源 app/utils/playwright_guard —— 生产 #379/#385 硬化产物）：
# browser.close() / playwright.stop() / 强杀 driver 三步共享一个总预算，墙钟上限是加一次
# _SHUTDOWN_BUDGET_S 而不是串行叠加 10+5+5。测试按需 monkeypatch 这几个常量把预算压到秒级。
_SHUTDOWN_BUDGET_S = 10.0    # 整个收尾阶段总预算（close / stop / 强杀共享）
_CLOSE_TIMEOUT_S = 10.0      # browser.close() 单步上限
_STOP_TIMEOUT_S = 5.0        # playwright.stop()（即 cm.__aexit__）单步上限
_KILL_WAIT_S = 5.0           # 强杀 driver 后回收进程的单步上限
_PROC_POLL_INTERVAL_S = 0.1  # 等 driver 子进程出现的轮询间隔


def _fill_template(scene: dict) -> str:
    content = scene.get("content") or {}
    name = "title_card.html" if scene.get("type") == "title_card" else "text_card.html"
    tpl = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return (tpl
            .replace("__CARD_BG__", style.CARD_BG)
            .replace("__GOLD__", style.GOLD)
            .replace("__BURGUNDY__", style.BURGUNDY)
            .replace("__CARD_TEXT__", style.CARD_TEXT)
            .replace("__FONT__", style.FONT_FAMILY)
            .replace("__TITLE__", html_escape.escape(content.get("title", "")))
            .replace("__BODY__", html_escape.escape(content.get("body", ""))))


def _driver_proc(cm):
    """取 playwright driver 子进程句柄；拿不到返回 None（全程防御取值）。"""
    try:
        conn = getattr(cm, "_connection", None)
        transport = getattr(conn, "_transport", None)
        return getattr(transport, "_proc", None)
    except Exception:  # noqa: BLE001
        return None


def _cancel_connection_task(cm) -> None:
    """取消本 cm 自己的 Connection.run() 后台任务（拿不到 driver 进程时的最后一道兜底）。

    PlaywrightContextManager.__aenter__ 里 `loop.create_task(self._connection.run())` 没留
    任何引用，而 driver 子进程是这个后台任务里 PipeTransport.connect() 的 create_subprocess_exec
    才创建的。start() 早期超时时，这个任务会在我们放弃之后才把 driver 拉起来 —— 在 scheduler
    常驻进程里就是永久孤儿进程 + 永久 pending 任务。这里按「协程帧里的 self 正是本 cm 的
    connection」精确匹配，绝不误伤同一个 loop 里其它并发截图的任务。
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
                logger.warning("still_image 取消残留的 Connection.run 后台任务")
        except Exception:  # noqa: BLE001
            continue


async def _bounded_shutdown(cm, browser) -> None:
    """有界收尾：任何一步都不抛、不无限等、被取消也照样跑完（移植自源 bounded_shutdown）。

    1) browser.close()；2) cm.__aexit__() 即 playwright.stop()，driver 正常退出会连带 chromium
    退出；3) 只有第 2 步失败/超时才强杀 driver 进程。三步共享 _SHUTDOWN_BUDGET_S 一个总预算，
    保证收尾本身也有墙钟上限。

    三道兜底对应审查发现的三处丢失，缺一道都会在 scheduler 常驻进程里泄漏浏览器：
    - 强杀 driver：stop 超时/失败时抓 cm._connection._transport._proc 并 proc.kill()，否则
      chromium+driver 进程树永久泄漏（源 docstring 实测 6 进程泄漏场景）。
    - 取消免疫：CancelledError 是 BaseException，`except Exception` 接不住。收尾窗口正是连接
      卡死时最宽的时候，一旦被外层 cancel 打断（worker SIGTERM / deadline / 僵死恢复），后面
      stop / 强杀全跳过。故这里捕获 BaseException，记下取消、跑完收尾，最后再重抛，既不泄漏
      进程也不吞掉调用方的取消语义。
    - start() 超时场景：browser 为 None，且 driver 子进程往往还没被后台任务拉起来，强杀分支
      要在剩余预算内轮询等 _proc 出现，实在等不到才去取消后台 Connection.run 任务。
    """
    deadline = time.monotonic() + _SHUTDOWN_BUDGET_S
    pending_exc: BaseException | None = None

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
            logger.warning("still_image %s 收尾失败: %r", what, e)
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
            logger.warning("still_image 等不到 playwright driver 进程句柄，改为取消后台任务")
            _cancel_connection_task(cm)
        else:
            try:
                proc.kill()
                logger.warning(
                    "still_image 强杀 playwright driver pid=%s", getattr(proc, "pid", "?")
                )
            except BaseException as e:  # noqa: BLE001
                logger.warning("still_image 强杀 playwright driver 失败: %r", e)
            await _bounded(proc.wait(), _KILL_WAIT_S, "driver.wait")

    if pending_exc is not None:
        raise pending_exc


async def _screenshot(html: str, out_png: Path, deadline: float | None = None) -> Path:
    """headless chromium 截 1920x1080（复用宿主装好的 playwright chromium）。

    平移适配：源用 app/utils/playwright_guard 的 guarded_chromium/guarded_step 护栏（生产
    #379/#385 硬化产物）；宿主 nbdpsy-server 无该公共件，本处又是唯一消费方，故把护栏的
    「主保护 + 收尾三道兜底」就地实现为本模块私有函数，语义与源一字不差。

    主保护：playwright 的 start()/launch()/new_page() 一个 timeout 参数都没有，是「起浏览器→
    截图→关掉」链路上仅有的能永久挂死的 await（挂死不抛异常、except 接不住，会把整个 job
    拖死）。用「总 deadline + 每步取剩余」把这些步骤逐个有界，无论卡在哪一步墙钟都硬封顶在
    timeout_s。整个浏览器阶段共享 BROWSER_SHOT_TIMEOUT 预算，调用方给了 deadline 就取较小值。

    收尾三道兜底（见 _bounded_shutdown）：stop 失败/超时强杀 driver 进程树、收尾对
    CancelledError 免疫、start() 超时的孤儿 connection task 回收 —— 少一道都会在常驻进程里
    泄漏浏览器。故这里持有显式 cm 并把 start() 纳入 try/finally，走同一条有界收尾路径。

    playwright 异步 API 本身非阻塞（全 awaitable），无需 to_thread（scheduler 非阻塞红线）。
    """
    from playwright.async_api import async_playwright

    from app.core.config import settings

    timeout_s = float(getattr(settings, "BROWSER_SHOT_TIMEOUT", 20))
    if deadline is not None:
        timeout_s = min(timeout_s, deadline - time.monotonic())
    dl = time.monotonic() + max(0.0, timeout_s)

    async def _step(coro, name: str):
        """按剩余总预算 await 一步，超时统一转成带步骤名的 TimeoutError（源 guarded_step 同语义）。"""
        remaining = max(0.0, dl - time.monotonic())
        try:
            return await asyncio.wait_for(coro, timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(f"still_image timeout at {name}") from None

    # 显式持有 context manager：driver 进程句柄挂在 cm._connection 上，收尾强杀/孤儿回收都要它；
    # 同时把 start() 纳入 try/finally —— start() 超时时后台惰性拉起的 driver 也能被收尾兜住
    # （源用显式 cm + __aexit__ 走同一条收尾路径，这里照搬）。
    cm = async_playwright()
    browser = None
    try:
        pw = await _step(cm.start(), "playwright.start")
        browser = await _step(pw.chromium.launch(headless=True), "chromium.launch")
        page = await _step(
            browser.new_page(viewport={"width": style.VIDEO_W, "height": style.VIDEO_H}),
            "new_page")
        await _step(page.set_content(html, wait_until="networkidle"), "set_content")
        await _step(page.screenshot(path=str(out_png), full_page=False), "screenshot")
    finally:
        await _bounded_shutdown(cm, browser)
    return out_png


async def render(scene: dict, out_path: Path, *,
                 deadline: float | None = None) -> Path:
    """卡片场景 → 无声视频段：截图静态图 + 首尾 fade（统一输出规格）。"""
    duration = float(scene["t1"]) - float(scene["t0"])
    png = Path(out_path).with_suffix(".card.png")
    try:
        await _screenshot(_fill_template(scene), png, deadline)
        fade_out_start = max(0.0, duration - _FADE_S)
        timeout = 600.0
        if deadline is not None:
            timeout = max(60.0, min(timeout, deadline - time.monotonic()))
        await _run_ffmpeg([
            "-loop", "1", "-t", f"{duration}", "-i", str(png),
            "-vf", (f"scale={style.VIDEO_W}:{style.VIDEO_H},"
                    f"fade=t=in:st=0:d={_FADE_S},"
                    f"fade=t=out:st={fade_out_start}:d={_FADE_S},fps={style.FPS}"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", str(out_path),
        ], timeout=timeout)
    finally:
        png.unlink(missing_ok=True)            # 渲染完/失败都清理临时截图
    return Path(out_path)
