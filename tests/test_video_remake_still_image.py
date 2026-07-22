"""still_image 渲染器：模板填充（纯函数）+ 截图与出段冒烟 + 浏览器护栏收尾语义（全 mock）。"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.video.pipeline.remake import style
from app.video.pipeline.remake.renderers import still_image


def _scene(**kw):
    base = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "title_card",
            "renderer": "still_image", "content": {"title": "引言"},
            "transition": "fade"}
    base.update(kw)
    return base


class TestFillTemplate:
    pytestmark = pytest.mark.unit

    def test_title_card_contains_title_and_tokens(self):
        html = still_image._fill_template(_scene())
        assert "引言" in html
        assert style.CARD_BG in html            # 品牌底色进了模板
        # logo 由 muxer 水印层统一叠加，模板不得再烘 logo（防卡片段双 logo 重影）
        assert "data:image/png;base64," not in html
        assert 'class="logo"' not in html

    def test_text_card_contains_body(self):
        sc = _scene(type="text_card",
                    content={"title": "使用须知", "body": "正文内容"})
        html = still_image._fill_template(sc)
        assert "使用须知" in html and "正文内容" in html

    def test_html_escaped(self):
        sc = _scene(content={"title": "<b>x&y</b>"})
        html = still_image._fill_template(sc)
        assert "<b>x&y</b>" not in html and "&lt;b&gt;" in html


@pytest.mark.integration
@pytest.mark.slow
class TestRenderSmoke:
    # 真 Playwright 卡片截图 + 真 ffmpeg 出段（宿主 CI 跑 not slow，慢测本地跑）。
    @pytest.mark.asyncio
    async def test_render_card_segment(self, tmp_path):
        out = await still_image.render(_scene(), tmp_path / "card.mp4")
        import asyncio, json
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries",
            "format=duration:stream=width,height", "-of", "json", str(out),
            stdout=asyncio.subprocess.PIPE)
        raw, _ = await proc.communicate()
        info = json.loads(raw)
        assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.2)
        assert info["streams"][0]["width"] == 1920


def test_registry_matches_storyboard_constant():
    from app.video.pipeline.remake import storyboard
    from app.video.pipeline.remake.renderers import RENDERERS
    assert set(RENDERERS) == storyboard.IMPLEMENTED_RENDERERS


# ---------------------------------------------------------------------------
# 浏览器护栏收尾语义（平移自源 test_reraster_timeout 的 D1/D2/D3/D6/D7/D7b/D8）：
# 全 mock，不起真浏览器。锁死审查发现的三道收尾兜底——强杀 driver / 取消免疫 / start 孤儿回收，
# 缺任一道都会在常驻进程里泄漏 chromium+driver。patch 目标是**源模块命名空间**
# playwright.async_api.async_playwright（_screenshot 内部函数体 import），收尾预算常量则直接
# monkeypatch still_image 模块（护栏就地实现为本模块私有函数）。每个挂死用例外层套 _fused
# 保险丝：挂死就是测试失败，而不是卡死整轮 pytest。
# ---------------------------------------------------------------------------
_FUSE_S = 10                                    # 保险丝：超过它没返回即判「挂死」失败
_FAKE_PNG = b"fake-png-bytes"                   # 无人解码，占位即可
_HTML = "<html><body>x</body></html>"


async def _fused(coro):
    """跑一个可能挂死的协程，超时直接判测试失败。

    刻意不用 asyncio.wait_for：收尾被设计成对取消免疫，一旦回归成「收尾里某步没有超时」，
    wait_for 会卡在 _cancel_and_wait 上等一个永远取消不完的任务 —— 整轮 pytest 卡死而非用例变红。
    """
    task = asyncio.ensure_future(coro)
    _done, pending = await asyncio.wait({task}, timeout=_FUSE_S)
    if pending:
        task.cancel()
        raise AssertionError(f"挂死：{_FUSE_S}s 内未返回")
    return task.result()


async def _hang():
    """模拟永久挂死的 await（playwright new_page / start 的真实失效形态）。"""
    await asyncio.sleep(3600)


def _dl(browser_timeout=1.0):
    """浏览器阶段预算走 deadline 传入（不动 settings，避开 pydantic v2 extra=ignore 的赋值限制）。"""
    return time.monotonic() + browser_timeout


class _FakeProc:
    """假的 playwright driver 子进程句柄，记录 kill 是否被调用。"""

    def __init__(self):
        self.pid = 424242
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


class _FakeTransport:
    def __init__(self, proc):
        self._proc = proc


class _FakeConnection:
    def __init__(self, proc):
        self._transport = _FakeTransport(proc)


class Connection:  # noqa: N801 — 必须叫这个名字：匹配靠 coro.__qualname__ == "Connection.run"
    """模拟 playwright 的 Connection（只为验证 _cancel_connection_task 的精确匹配）。"""

    async def run(self):
        await _hang()


class _FakePage:
    """记录 set_content / screenshot 的完整参数，供快乐路径逐字断言。"""

    def __init__(self, calls):
        self.calls = calls

    async def set_content(self, html, **kwargs):
        self.calls.append(("set_content", dict(kwargs)))

    async def screenshot(self, **kwargs):
        self.calls.append(("screenshot", dict(kwargs)))
        path = kwargs.get("path")
        if path:
            Path(path).write_bytes(_FAKE_PNG)
        return _FAKE_PNG


class _FakeBrowser:
    def __init__(self, *, hang_new_page=False, hang_close=False):
        self.calls = []
        self._hang_new_page = hang_new_page
        self._hang_close = hang_close
        self.close_called = False
        self.close_entered = asyncio.Event()    # 精确挑「正在收尾」的时刻投递 cancel

    async def new_page(self, **kwargs):
        self.calls.append(("new_page", dict(kwargs)))
        if self._hang_new_page:
            await _hang()
        return _FakePage(self.calls)

    async def close(self):
        self.close_called = True
        self.close_entered.set()
        if self._hang_close:
            await _hang()


class _FakeChromium:
    def __init__(self, browser, launch_delay=0.0):
        self._browser = browser
        self._launch_delay = launch_delay

    async def launch(self, **kwargs):
        if self._launch_delay:
            await asyncio.sleep(self._launch_delay)
        return self._browser


class _FakePlaywright:
    def __init__(self, browser, launch_delay=0.0):
        self.chromium = _FakeChromium(browser, launch_delay)


class _FakeCM:
    """模拟 PlaywrightContextManager：start() / __aexit__() / _connection._transport._proc。"""

    def __init__(self, *, browser=None, hang_start=False, hang_exit=False,
                 proc_delay=None, launch_delay=0.0):
        self._browser = browser
        self._hang_start = hang_start
        self._hang_exit = hang_exit
        self._launch_delay = launch_delay
        self.proc = _FakeProc()
        # 真实 CM 里 _connection 在第一个 await 之前同步赋值（start 挂死时它已存在），
        # 但 _transport._proc 是后台 Connection.run() 里 create_subprocess_exec 才建的，
        # proc_delay 模拟「driver 进程晚于我们放弃的时刻才出现」。
        self._connection = _FakeConnection(None if proc_delay else self.proc)
        self._proc_delay = proc_delay
        self.exit_called = False

    async def _late_proc(self):
        await asyncio.sleep(self._proc_delay)
        self._connection._transport._proc = self.proc

    async def start(self):
        if self._proc_delay:
            asyncio.get_running_loop().create_task(self._late_proc())
        if self._hang_start:
            await _hang()
        return _FakePlaywright(self._browser, self._launch_delay)

    async def __aexit__(self, *args):
        self.exit_called = True
        if self._hang_exit:
            await _hang()


def _install(monkeypatch, cm, *, shutdown=1.5, step=0.3):
    """装假 CM 进 playwright 源模块命名空间 + 把收尾各段预算压到测试量级。

    step=None 保留模块真实单步上限（10/5/5），用来验证收尾总预算能把它们钳住。
    浏览器阶段预算不动 settings，改由各用例通过 _screenshot(deadline=...) 传入。
    """
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: cm)
    monkeypatch.setattr(still_image, "_SHUTDOWN_BUDGET_S", shutdown)
    if step is not None:
        monkeypatch.setattr(still_image, "_CLOSE_TIMEOUT_S", step)
        monkeypatch.setattr(still_image, "_STOP_TIMEOUT_S", step)
        monkeypatch.setattr(still_image, "_KILL_WAIT_S", step)


def _find(calls, name):
    return [c[1] for c in calls if c[0] == name]


@pytest.mark.unit
@pytest.mark.asyncio
class TestScreenshotGuard:
    """_screenshot 有界护栏：主保护封顶 + 收尾三道兜底（强杀 / 取消免疫 / start 孤儿）。"""

    async def test_new_page_hang_raises_timeout_and_closes(self, monkeypatch, tmp_path):
        """G1：new_page 永久挂死 → 预算内抛 TimeoutError，且收尾照样跑过 browser.close()。"""
        browser = _FakeBrowser(hang_new_page=True)
        cm = _FakeCM(browser=browser)
        _install(monkeypatch, cm)

        t0 = time.monotonic()
        with pytest.raises(TimeoutError) as ei:
            await _fused(still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl()))
        elapsed = time.monotonic() - t0

        assert "new_page" in str(ei.value)
        assert elapsed < 4.0, f"墙钟 {elapsed:.2f}s 超出 timeout+收尾预算"
        assert browser.close_called is True      # 收尾照样跑，进程树不泄漏

    async def test_hang_everywhere_falls_back_to_kill(self, monkeypatch, tmp_path):
        """G2：new_page / close / __aexit__ 全挂死 → 仍在预算内失败，且强杀 driver 生效。"""
        browser = _FakeBrowser(hang_new_page=True, hang_close=True)
        cm = _FakeCM(browser=browser, hang_exit=True)
        _install(monkeypatch, cm)

        with pytest.raises(TimeoutError):
            await _fused(still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl()))

        assert browser.close_called is True
        assert cm.exit_called is True
        assert cm.proc.killed is True

    async def test_start_hang_still_kills_driver(self, monkeypatch, tmp_path):
        """G3：cm.start() 挂死（pw/browser 都拿不到）→ 只靠 cm 完成收尾并强杀 driver。"""
        cm = _FakeCM(browser=None, hang_start=True, hang_exit=True)
        _install(monkeypatch, cm)

        with pytest.raises(TimeoutError) as ei:
            await _fused(still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl()))

        assert "playwright.start" in str(ei.value)
        assert cm.proc.killed is True

    async def test_happy_path_semantics_unchanged(self, monkeypatch, tmp_path):
        """G4：快乐路径关键调用参数逐字不变（VIDEO_W×VIDEO_H viewport / set_content / 输出路径）。"""
        browser = _FakeBrowser()
        cm = _FakeCM(browser=browser)
        _install(monkeypatch, cm)
        out = tmp_path / "card.png"

        await _fused(still_image._screenshot(_HTML, out, deadline=_dl(5.0)))

        assert _find(browser.calls, "new_page") == [
            {"viewport": {"width": style.VIDEO_W, "height": style.VIDEO_H}}
        ]
        assert _find(browser.calls, "set_content") == [{"wait_until": "networkidle"}]
        assert _find(browser.calls, "screenshot") == [
            {"path": str(out), "full_page": False}
        ]
        assert out.is_file()
        assert cm.exit_called is True            # 正常收尾走 stop，不触发强杀
        assert cm.proc.killed is False

    async def test_cancel_during_shutdown_still_kills_driver(self, monkeypatch, tmp_path):
        """G5：收尾窗口内被外层 cancel → 收尾不许被打断，仍走完强杀（CancelledError 是 BaseException）。"""
        browser = _FakeBrowser(hang_new_page=True, hang_close=True)
        cm = _FakeCM(browser=browser, hang_exit=True)
        _install(monkeypatch, cm)

        task = asyncio.ensure_future(
            still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl()))
        # 等它真的进到收尾（browser.close 已被调用）再投递 cancel，命中最危险的那个窗口
        await asyncio.wait_for(browser.close_entered.wait(), _FUSE_S)
        task.cancel()

        _done, pending = await asyncio.wait({task}, timeout=_FUSE_S)
        assert not pending, "收尾被 cancel 后挂死了"
        assert task.cancelled()                  # 取消语义保留：任务最终以 CancelledError 结束
        assert cm.exit_called is True            # 但收尾照样跑完：stop 试过、driver 被强杀
        assert cm.proc.killed is True

    async def test_start_hang_waits_for_late_driver_proc(self, monkeypatch, tmp_path):
        """G6：start() 超时时 driver 进程还没建好 → 在收尾预算内等到它出现并强杀（不留孤儿）。"""
        cm = _FakeCM(browser=None, hang_start=True, hang_exit=True, proc_delay=0.4)
        _install(monkeypatch, cm)

        with pytest.raises(TimeoutError) as ei:
            await _fused(still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl()))

        assert "playwright.start" in str(ei.value)
        assert cm.proc.killed is True, "晚出现的 driver 进程没被回收 → 孤儿进程"

    async def test_cancel_connection_task_only_touches_own(self):
        """G7：拿不到 driver 进程时取消后台任务，必须精确到本 cm，不误伤并发的兄弟任务。"""
        mine, other = Connection(), Connection()
        t_mine = asyncio.ensure_future(mine.run())
        t_other = asyncio.ensure_future(other.run())
        await asyncio.sleep(0)                    # 让两个任务真正进入 run()

        try:
            still_image._cancel_connection_task(SimpleNamespace(_connection=mine))
            await asyncio.sleep(0.05)
            assert t_mine.cancelled() is True
            assert t_other.done() is False, "误伤了别的截图的 Connection.run"
        finally:
            t_other.cancel()
            await asyncio.gather(t_mine, t_other, return_exceptions=True)

    async def test_shutdown_budget_caps_wall_clock(self, monkeypatch, tmp_path):
        """G8：收尾三步共享总预算 —— 单步上限保留真实值（10/5/5）也不能突破墙钟上限。"""
        browser = _FakeBrowser(hang_new_page=True, hang_close=True)
        cm = _FakeCM(browser=browser, hang_exit=True)
        _install(monkeypatch, cm, shutdown=1.0, step=None)

        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            await _fused(still_image._screenshot(_HTML, tmp_path / "c.png", deadline=_dl(1.0)))
        elapsed = time.monotonic() - t0

        # 上限 = 浏览器阶段 1s + 收尾 1s；留 1s 余量吸收调度抖动
        assert elapsed < 3.0, f"墙钟 {elapsed:.2f}s 超出 timeout+收尾预算"
