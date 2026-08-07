"""视频笔记发布的浏览器层:判据纯函数 + step2v/step3v 分支 + 发布按钮就绪门。

判据函数刻意抽成纯函数(只吃文本 + 布尔),这样最要命的那条坑
——「标题输入框在上传进度还是 0% 时就已经挂进 DOM」——可以脱离真页面被钉死:
拿图文那套「标题框存在即编辑器就绪」的判定套到视频上会立刻假阳性,
后面 step5 打字打进一个还没转码完的编辑器,失败还查不出原因。
"""

import app.browser.atomic_tasks as atomic_mod
from app.browser.atomic_tasks import (
    XHSPublishAtomicTasks,
    classify_video_upload_state,
)


# ---------------- classify_video_upload_state ----------------


def test_progress_text_means_uploading():
    """cover 区还在报「上传中 / 当前速度」→ uploading。"""
    assert classify_video_upload_state(
        "视频文件.mp4 上传中 37% 当前速度 2.1MB/s", ""
    ) == "uploading"


def test_done_text_means_ready():
    """cover 区变成「真实文件名 + 检测为高清视频」→ ready(真号夹具原文)。"""
    cover = (
        "xhs_video_publish_test.mp4 检测为高清视频。"
        "清晰的画面能极大提升观看体验，有利于提升观看时长"
    )
    assert classify_video_upload_state(cover, "") == "ready"


def test_reupload_entry_means_ready():
    """「重新上传」入口出现 = 平台认为这条视频已就位 → ready。"""
    assert classify_video_upload_state("我的视频.mov", "重新上传 设置封面") == "ready"


def test_progress_wins_over_done_markers():
    """完成文案与进度文案同时在(页面正在切换)→ 以进度为准,继续等。

    进度文案是**最强否定信号**:它在,就说明这一刻绝对没传完。宁可多等一轮,
    也不能在半态上放行。
    """
    assert classify_video_upload_state(
        "视频.mp4 上传中 99% 检测为高清视频", "重新上传"
    ) == "uploading"


def test_original_switch_enabled_is_auxiliary_ready_signal():
    """文案都没匹配上,但 cover 区有内容 + 「原创声明」开关已可点 → 当作 ready。

    夹具实测:上传没完成时该开关是 pointer-events:none 的禁用态。平台改文案的概率
    远高于改这个交互约束,故留成辅助判据,避免文案一变整条链路就等到超时。
    """
    assert classify_video_upload_state(
        "我的视频.mp4", "", original_switch_enabled=True) == "ready"
    assert classify_video_upload_state(
        "我的视频.mp4", "", original_switch_enabled=False) == "unknown"


def test_switch_alone_without_cover_is_not_ready():
    """cover 区空(页面上还没挂视频)时,光凭开关可点**不**算就绪。

    否则一进发布页、视频字节都还没开始传就会被判成"传完了",随后往空编辑器打字点发布。
    """
    assert classify_video_upload_state("", "", original_switch_enabled=True) == "unknown"


def test_title_input_presence_is_not_a_ready_signal():
    """回归钉死:标题框/正文框在 DOM 里**不构成**就绪信号。

    这正是图文版 _check_edit_page_loaded 的判据。视频页上传 0% 时它就已经为真,
    照搬即假阳性。这里给一个"标题框在 + 进度还在跑"的页面,必须仍判 uploading。
    """
    page_text = "填写标题会有更多赞哦 添加正文 上传中 3%"
    assert classify_video_upload_state("视频.mp4 上传中 3%", page_text) == "uploading"


def test_empty_page_is_unknown_not_ready():
    """什么信号都读不到 → unknown(不是 ready):读不到 ≠ 好了。"""
    assert classify_video_upload_state("", "") == "unknown"


# ---------------- step2v / step3v ----------------


class _FakeHuman:
    def __init__(self):
        self.clicks = []

    def wait(self, *a, **k):
        return None

    def click(self, target, reason=""):
        self.clicks.append((target, reason))


class _FakeInput:
    def __init__(self):
        self.files = None

    def set_input_files(self, paths):
        self.files = paths


class _FakePage:
    """最小 page 替身:按脚本吐 evaluate 结果,记录选择器查询。"""

    url = "https://creator.xiaohongshu.com/publish/publish?source=official"

    def __init__(self, probes=None, file_input=None):
        self._probes = list(probes or [])
        self.file_input = file_input
        self.screenshots = 0

    def evaluate(self, script, *args):
        # 脚本吐完就一直重复最后一帧(真页面也是这样:状态不变时读多少次都一样),
        # 这样超时类用例不会因为轮询次数比脚本长而读到 None。
        if len(self._probes) > 1:
            return self._probes.pop(0)
        return self._probes[0] if self._probes else None

    def query_selector(self, selector):
        return None

    def query_selector_all(self, selector):
        return []

    def screenshot(self, **k):
        self.screenshots += 1
        return b""

    def wait_for_selector(self, *a, **k):
        raise Exception("no match")


def _make_tasks(page):
    """跳过 __init__(会起 SyncHumanActions/浏览器),只装判据用得到的最小状态。"""
    tasks = XHSPublishAtomicTasks.__new__(XHSPublishAtomicTasks)
    tasks.page = page
    tasks.human = _FakeHuman()
    tasks.enable_debug = False
    tasks.screenshot_dir = "/tmp"
    tasks.job_tag = ""
    tasks.current_step = 0
    return tasks


def test_step2v_sets_input_files_without_native_dialog(monkeypatch):
    """step2v 直接 set_input_files 灌视频,不点任何上传按钮(避原生文件框卡死)。"""
    file_input = _FakeInput()
    page = _FakePage()
    tasks = _make_tasks(page)
    monkeypatch.setattr(
        XHSPublishAtomicTasks, "_find_element_with_retry",
        lambda self, *a, **k: file_input,
    )
    r = tasks.step2v_upload_video("/data/note.mp4")
    assert r["success"] is True
    assert file_input.files == ["/data/note.mp4"]
    # 拟人层一次点击都不该发生 —— 点「上传视频」按钮会弹原生 GTK 框
    assert tasks.human.clicks == []


def test_step2v_fails_when_no_file_input(monkeypatch):
    """找不到 file input → 明确失败(不静默往下走)。"""
    tasks = _make_tasks(_FakePage())
    monkeypatch.setattr(
        XHSPublishAtomicTasks, "_find_element_with_retry", lambda self, *a, **k: None
    )
    r = tasks.step2v_upload_video("/data/note.mp4")
    assert r["success"] is False and "input" in r["error"]


def test_step3v_waits_until_ready(monkeypatch):
    """连续两轮 ready 才收口:先 uploading,再两轮 ready → success。"""
    probes = [
        {"cover_text": "note.mp4 上传中 10% 当前速度 1MB/s", "page_text": "",
         "original_switch_enabled": False},
        {"cover_text": "note.mp4 检测为高清视频", "page_text": "重新上传",
         "original_switch_enabled": True},
        {"cover_text": "note.mp4 检测为高清视频", "page_text": "重新上传",
         "original_switch_enabled": True},
    ]
    tasks = _make_tasks(_FakePage(probes=probes))
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.step3v_wait_for_video_processing(max_wait=60)
    assert r["success"] is True and r["state"] == "ready"


def test_step3v_single_ready_poll_is_not_enough(monkeypatch):
    """只闪一轮 ready 随即回到 uploading → 不放行(防半态假阳性)。"""
    probes = [
        {"cover_text": "note.mp4 检测为高清视频", "page_text": "",
         "original_switch_enabled": False},
        {"cover_text": "note.mp4 上传中 40%", "page_text": "",
         "original_switch_enabled": False},
    ]
    tasks = _make_tasks(_FakePage(probes=probes))
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.step3v_wait_for_video_processing(max_wait=1)
    assert r["success"] is False


def test_step3v_timeout_reports_last_observed_state(monkeypatch):
    """超时失败必须带当场取证(最后一次读到的判据),否则运营只看得到一句"超时"。"""
    probes = [
        {"cover_text": "note.mp4 上传中 5% 当前速度 0.3MB/s", "page_text": "",
         "original_switch_enabled": False}
    ]
    tasks = _make_tasks(_FakePage(probes=probes))
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.step3v_wait_for_video_processing(max_wait=1)
    assert r["success"] is False
    assert r["state"] == "uploading"
    assert "上传中 5%" in r["observed"]["cover_text"]


# ---------------- 发布按钮就绪门 ----------------


def test_wait_submit_enabled_true_when_attr_flips(monkeypatch):
    """submit-disabled 从 'true' 翻到 'false' → 放行。"""
    page = _FakePage(probes=[
        {"found": True, "submit_disabled": "true"},
        {"found": True, "submit_disabled": "false"},
    ])
    tasks = _make_tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.wait_for_submit_enabled(timeout=30)
    assert r["ready"] is True


def test_wait_submit_enabled_timeout_carries_attr_snapshot(monkeypatch):
    """一直禁用 → 超时返回,并带上当场的属性快照(取证,别只丢一句超时)。"""
    page = _FakePage(probes=[{"found": True, "submit_disabled": "true"}])
    tasks = _make_tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.wait_for_submit_enabled(timeout=1)
    assert r["ready"] is False
    assert r["observed"]["submit_disabled"] == "true"


def test_wait_submit_enabled_host_missing_is_not_ready(monkeypatch):
    """<xhs-publish-btn> 都不在页面上 → 不是"就绪",是异常,必须 ready=False。"""
    page = _FakePage(probes=[{"found": False}])
    tasks = _make_tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.wait_for_submit_enabled(timeout=1)
    assert r["ready"] is False
