"""受众事件采集单测:增量停止判据 + 滚动纪律,用假 page/human 离线回放。

这一层没有真实浏览器也能锁死三件事,而这三件事全是真号上踩出来的:

1. **增量真的停得住**:通知流新事件在最前,翻到 ``event_time <= last_event_time`` 就该停。
   停不住的代价不是慢 —— 号1 实采到底要 47 页滚 40 轮,每小时全量重翻一次等于拿真号
   会话额度去换早就有的数据,而会话额度是本仓最稀缺的风控资源;
2. **滚之前必须先 hover 到通知行**:``mouse.wheel`` 把事件投在鼠标当前位置,鼠标没动过
   时停在 (0,0),那里是不滚动的顶栏 —— 取证首采就栽在这,赞和收藏一页都没翻动;
3. **停滞判据必须含 ``document.scrollTop``**:通知页滚的是 document。漏了它,"滚轮还在
   往下推、只是懒加载还没触发"的那几轮会被判成到底,而接口自报 has_more=true ——
   把"没翻到底"误报成到底,增量库从此永远缺一段。

外加一条合规断言:全程**只**点 tab,不点任何点赞/关注/进主页类元素。
"""

import pytest

from app.browser import audience_collect as ac
from app.browser.login_detector import PAGE_TEXT_JS


def _msg(event_id: str, time: int) -> dict:
    return {"id": event_id, "time": time, "type": "liked/item",
            "user_info": {"userid": "u1"}, "item_info": {"type": "note_info", "id": "n1"}}


# ---------------- 纯逻辑:增量截断 ----------------


def test_first_sync_takes_everything():
    """首采(游标为空)不截断,一路翻到底。"""
    msgs = [_msg("a", 300), _msg("b", 200), _msg("c", 100)]

    fresh, reached = ac.take_until_known(msgs, None)

    assert fresh == msgs and reached is False


def test_stops_at_first_known_event():
    """遇到 ``<= last_event_time`` 当场截断,后面的一条都不要。"""
    msgs = [_msg("新", 300), _msg("也新", 250), _msg("采过了", 200), _msg("更老", 100)]

    fresh, reached = ac.take_until_known(msgs, 200)

    assert [m["id"] for m in fresh] == ["新", "也新"]
    assert reached is True


def test_boundary_equal_time_counts_as_known():
    """时间戳**相等**算已知(游标是"采到这一刻为止",含这一刻),否则边界那条每轮重采。"""
    _fresh, reached = ac.take_until_known([_msg("边界", 200)], 200)
    assert reached is True


def test_all_new_does_not_claim_reached():
    fresh, reached = ac.take_until_known([_msg("a", 300)], 100)
    assert len(fresh) == 1 and reached is False


# ---------------- 假浏览器:滚动纪律与停止条件 ----------------


class _FakeElement:
    def __init__(self, box=None):
        self._box = box or {"x": 0, "y": 400, "width": 600, "height": 80}

    def bounding_box(self):
        return self._box


class _FakeHuman:
    """记录每一个拟人动作,顺序即断言依据。"""

    def __init__(self, page):
        self.page = page
        self.actions: list[tuple] = []

    def navigate(self, url, **_kw):
        self.actions.append(("navigate", url))
        self.page.fire_page_load()

    def wait(self, *_a, **_kw):
        self.actions.append(("wait",))

    def click(self, element, *, reason=""):
        self.actions.append(("click", reason))

    def hover(self, point, *, reason=""):
        self.actions.append(("hover", point))

    def scroll(self, direction="down", **_kw):
        self.actions.append(("scroll", direction))
        self.page.fire_scroll()


class _FakePage:
    """按脚本吐响应的假通知页。

    ``pages_by_channel`` 给每个 channel 一串响应体,每次滚动吐一页;``scroll_only_rounds``
    模拟"滚轮在动但懒加载还没触发"的那几轮(只涨 scrollTop,不出新响应)。
    """

    def __init__(self, pages_by_channel, *, scroll_only_rounds=0, logged_in=True):
        self._pages = {ch: list(v) for ch, v in pages_by_channel.items()}
        self._channel = ac.CHANNEL_LIKES
        self._handlers = []
        self._scroll_only = scroll_only_rounds
        self.url = "https://www.xiaohongshu.com/notification"
        self.viewport_size = {"width": 1280, "height": 800}
        self.scroll_top = 0
        self.logged_in = logged_in
        self.emitted = 0
        self.evaluate_calls: list[str] = []

    # -- playwright 侧接口 --
    def on(self, event, handler):
        assert event == "response"
        self._handlers.append(handler)

    def evaluate(self, js, arg=None):
        self.evaluate_calls.append(js)
        if js is ac.STATE_JS:
            return {
                "scroll_top": self.scroll_top,
                # 文档高度与文本量只在**懒加载真的吐了新一屏**时才涨(真页面就是这样),
                # 所以"只滚不出数据"的那几轮里,除了 scroll_top 之外三项全不动 ——
                # 这样这条用例才真的只在考 scroll_top 那一项。
                "scroll_height": 2000 + 500 * self.emitted,
                "body_text_len": 5000 + 100 * self.emitted,
                "has_login_modal": not self.logged_in,
                "url": self.url,
            }
        if js is PAGE_TEXT_JS:
            return "请完成验证"
        raise AssertionError(f"假页面没预期到的 evaluate: {js[:40]}")

    def evaluate_handle(self, _js, _arg=None):
        return _FakeHandle(_FakeElement())

    def query_selector(self, _sel):
        return _FakeElement()

    # -- 驱动 --
    def fire_page_load(self):
        self._emit_next()

    def fire_scroll(self):
        self.scroll_top += 300
        if self._scroll_only > 0:
            self._scroll_only -= 1
            return
        self._emit_next()

    def switch_channel(self, channel):
        """切到另一条 channel:真页面点 tab 会重新拉第一页,这里跟着吐一页。"""
        self._channel = channel
        self._emit_next()

    def _emit_next(self):
        queue = self._pages.get(self._channel) or []
        if not queue:
            return
        body = queue.pop(0)
        self.emitted += 1
        mark = (ac.LIKES_MARK if self._channel == ac.CHANNEL_LIKES
                else ac.CONNECTIONS_MARK)
        for handler in self._handlers:
            handler(_FakeResponse(f"https://edith.xiaohongshu.com{mark}?num=20", body))


class _FakeHandle:
    def __init__(self, element):
        self._element = element

    def as_element(self):
        return self._element


class _FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self._body = body

    def json(self):
        return self._body


def _body(messages, has_more) -> dict:
    return {"data": {"message_list": messages, "has_more": has_more, "cursor": "c"}}


def _run(page, targets, *, full=False):
    human = _FakeHuman(page)
    # 切 tab 时让假页面跟着换 channel(真页面靠点击自己换)
    original_click = human.click

    def click(element, *, reason=""):
        if ac.CHANNEL_CONNECTIONS in reason or "新增关注" in reason:
            page.switch_channel(ac.CHANNEL_CONNECTIONS)
        original_click(element, reason=reason)

    human.click = click
    return ac.collect_audience(page, human, targets=targets, full=full), human


def test_incremental_stops_when_reaching_known_events():
    """第二页出现已采过的事件 → 立刻停,不再往下翻。"""
    page = _FakePage({
        ac.CHANNEL_LIKES: [
            _body([_msg("新1", 500), _msg("新2", 450)], True),
            _body([_msg("采过", 300), _msg("更老", 200)], True),
            _body([_msg("不该翻到", 100)], True),
        ],
    })

    result, _human = _run(page, {ac.CHANNEL_LIKES: 400})

    channel = result["channels"][ac.CHANNEL_LIKES]
    assert channel["stopped_by"] == "reached_known"
    assert [m["id"] for m in channel["messages"]] == ["新1", "新2"]


def test_full_sync_walks_to_has_more_false():
    """全量:一路翻到 ``has_more=false``。"""
    page = _FakePage({
        ac.CHANNEL_LIKES: [
            _body([_msg("a", 500)], True),
            _body([_msg("b", 400)], True),
            _body([_msg("c", 300)], False),
        ],
    })

    result, _human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    channel = result["channels"][ac.CHANNEL_LIKES]
    assert channel["stopped_by"] == "exhausted"
    assert [m["id"] for m in channel["messages"]] == ["a", "b", "c"]


def test_hover_precedes_every_scroll():
    """滚之前必须先 hover 到通知行 —— 否则滚轮打在不滚动的顶栏上,一页都翻不动。"""
    page = _FakePage({
        ac.CHANNEL_LIKES: [_body([_msg("a", 500)], True), _body([_msg("b", 400)], False)],
    })

    _result, human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    kinds = [a[0] for a in human.actions]
    assert "hover" in kinds, "一次 hover 都没有,滚轮必然空转"
    assert kinds.index("hover") < kinds.index("scroll")


def test_scroll_top_movement_prevents_premature_stall_stop():
    """滚轮在动(scrollTop 涨)但懒加载还没吐数据的那几轮**不算停滞**。

    漏掉 scrollTop 这一项,这几轮会被判成到底 —— 而接口自报 has_more=true。
    取证首采就是这么把"没翻到底"误报成到底的(4 轮就收工,实际有 47 页)。
    """
    page = _FakePage(
        {ac.CHANNEL_LIKES: [
            _body([_msg("a", 500)], True),
            _body([_msg("b", 400)], False),
        ]},
        # 前两页之间插 4 轮"只滚不出数据",超过 STALL_ROUNDS
        scroll_only_rounds=ac.STALL_ROUNDS + 1,
    )

    result, _human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    channel = result["channels"][ac.CHANNEL_LIKES]
    assert channel["stopped_by"] == "exhausted", "被误判成停滞,少采了一页"
    assert [m["id"] for m in channel["messages"]] == ["a", "b"]


def test_real_stall_stops():
    """页面真的不动了(scrollTop 也不涨)→ 连续几轮后停,不空转到轮数上限。"""

    class _Frozen(_FakePage):
        def fire_scroll(self):
            pass  # 既不出数据也不动 scrollTop

    page = _Frozen({ac.CHANNEL_LIKES: [_body([_msg("a", 500)], True)]})

    result, _human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    channel = result["channels"][ac.CHANNEL_LIKES]
    assert channel["stopped_by"] == "stalled"
    assert channel["rounds"] <= ac.STALL_ROUNDS + 1


def test_incremental_round_cap_is_much_smaller_than_full():
    """增量轮的滚动封顶远小于全量:增量只需摸到已知区,不该有翻 40 轮的机会。"""
    assert ac.INCREMENTAL_SCROLL_ROUNDS < ac.FULL_SCROLL_ROUNDS


def test_both_channels_collected_and_tab_switched():
    """两条 channel 各采各的,切 tab 靠拟人点击。"""
    page = _FakePage({
        ac.CHANNEL_LIKES: [_body([_msg("赞", 500)], False)],
        ac.CHANNEL_CONNECTIONS: [_body([{"id": "f1", "time": 400,
                                         "type": "follow/you",
                                         "user": {"userid": "u9"}}], False)],
    })

    result, human = _run(
        page, {ac.CHANNEL_LIKES: None, ac.CHANNEL_CONNECTIONS: None}, full=True
    )

    assert [m["id"] for m in result["channels"][ac.CHANNEL_LIKES]["messages"]] == ["赞"]
    assert [m["id"] for m in
            result["channels"][ac.CHANNEL_CONNECTIONS]["messages"]] == ["f1"]
    assert sum(1 for a in human.actions if a[0] == "click") >= 1


def test_only_tab_clicks_no_interaction_clicks():
    """合规红线:采集全程只点 tab,**没有**任何点赞/关注/进主页类点击。"""
    page = _FakePage({
        ac.CHANNEL_LIKES: [_body([_msg("a", 500)], False)],
        ac.CHANNEL_CONNECTIONS: [_body([], False)],
    })

    _result, human = _run(
        page, {ac.CHANNEL_LIKES: None, ac.CHANNEL_CONNECTIONS: None}, full=True
    )

    for kind, reason in [a for a in human.actions if a[0] == "click"]:
        assert kind == "click"
        assert "tab" in reason, f"出现了非切 tab 的点击:{reason}"


def test_not_logged_in_yields_error_and_no_data():
    """未登录当场判错,一条数据都不入 —— 未登录页上读到的东西不是我们的受众。"""
    page = _FakePage({ac.CHANNEL_LIKES: [_body([_msg("a", 500)], False)]},
                     logged_in=False)

    result, _human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    assert result["error"]
    assert "channels" not in result


def test_wall_stops_immediately():
    """撞验证墙立刻停手并留取证 —— 继续滚只会把号推得更深。"""
    page = _FakePage({ac.CHANNEL_LIKES: [_body([_msg("a", 500)], True)]})
    page.url = "https://www.xiaohongshu.com/website-login/captcha?redirectPath=x"

    result, _human = _run(page, {ac.CHANNEL_LIKES: None}, full=True)

    assert result["wall"]
    assert result["error"]


@pytest.mark.parametrize("channel", list(ac.CHANNELS))
def test_every_channel_has_tab_and_api_mark(channel):
    """每条 channel 都要有 tab 文案与接口特征串,漏一个就是采不到还不报错。"""
    spec = ac.CHANNEL_SPEC[channel]
    assert spec["tab_texts"] and spec["api_mark"]
