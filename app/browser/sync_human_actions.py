"""同步版拟人化操作层 — 给 sync Playwright 使用。

移植自旧仓 ``smart_browser/sync_human_actions.py``,核心算法原样保留:
- 贝塞尔曲线鼠标轨迹
- 逐字打字 + 偶尔退格修正
- 分段变速滚动 + 偶尔回看
- 行为档案参数
- 犹豫/疲劳模拟

唯一改动:logger 从旧仓 ``app.core.logger`` 换成本仓统一的 ``loguru``。

用法:
    from app.browser.sync_human_actions import SyncHumanActions
    human = SyncHumanActions(page, profile="casual")
    human.click(element, reason="点击发布按钮")
    human.type_text(element, "标题文字")
"""
import random
import math
import time
from typing import Tuple, Optional, List, Union

from playwright.sync_api import Page, ElementHandle

from loguru import logger


# 行为档案参数（与 async 版一致）
PROFILES = {
    "hasty": {
        "click_pre_delay": (0.05, 0.15),
        "hover_delay": (0.1, 0.25),
        "think_delay": (0.2, 0.6),
        "type_char_delay": (50, 100),
        "type_pause_prob": 0.05,
        "type_pause_range": (0.1, 0.3),
        "mouse_steps_range": (4, 7),
        "mouse_duration_range": (0.15, 0.4),
        "scroll_step_delay": (0.03, 0.06),
        "mistake_prob": 0.02,
        "typo_prob": 0.01,
    },
    "casual": {
        "click_pre_delay": (0.1, 0.3),
        "hover_delay": (0.2, 0.5),
        "think_delay": (0.5, 1.5),
        "type_char_delay": (80, 150),
        "type_pause_prob": 0.10,
        "type_pause_range": (0.3, 0.8),
        "mouse_steps_range": (5, 10),
        "mouse_duration_range": (0.3, 0.8),
        "scroll_step_delay": (0.05, 0.10),
        "mistake_prob": 0.08,
        "typo_prob": 0.04,
    },
    "skilled": {
        "click_pre_delay": (0.05, 0.12),
        "hover_delay": (0.08, 0.18),
        "think_delay": (0.3, 0.8),
        "type_char_delay": (40, 80),
        "type_pause_prob": 0.03,
        "type_pause_range": (0.1, 0.3),
        "mouse_steps_range": (3, 6),
        "mouse_duration_range": (0.1, 0.3),
        "scroll_step_delay": (0.02, 0.05),
        "mistake_prob": 0.01,
        "typo_prob": 0.005,
    },
}


class SyncHumanActions:
    """同步版拟人化操作层

    所有与小红书页面的交互必须经过此层，禁止直接调用 Playwright API。
    """

    def __init__(self, page: Page, profile: str = "casual"):
        self.page = page
        self.params = PROFILES.get(profile, PROFILES["casual"])
        self.last_mouse_pos: Optional[Tuple[float, float]] = None
        self._action_count = 0
        logger.info(f"[SyncHuman] 初始化 | 行为档案: {profile}")

    # ══════════════════════════════════════════════════
    # 核心 API
    # ══════════════════════════════════════════════════

    def click(
        self,
        target: Union[ElementHandle, Tuple[float, float]],
        *,
        random_offset: bool = True,
        reason: str = "",
    ):
        """拟人化点击（贝塞尔移动 → 悬停 → mouse.down/up）

        完全模拟真人：鼠标移动 → 悬停确认 → 按下 → 释放。
        不使用任何 element.click() / evaluate("click") 等非人类 API。
        """
        self._action_count += 1

        if isinstance(target, tuple):
            click_x, click_y = target
            element = None
        else:
            element = target
            # 先确保元素在视口内
            try:
                element.scroll_into_view_if_needed()
                time.sleep(random.uniform(0.2, 0.4))
            except Exception:
                pass

            box = element.bounding_box()
            if not box or box['x'] < 0 or box['y'] < 0:
                logger.warning(f"[SyncHuman] 元素不在视口内 (box={box})，降级 scrollIntoView+click | {reason}")
                try:
                    self.page.evaluate("(el) => el.scrollIntoView({block: 'center'})", element)
                    time.sleep(0.5)
                    box = element.bounding_box()
                except Exception:
                    pass
                if not box or box['x'] < 0 or box['y'] < 0:
                    logger.warning(f"[SyncHuman] 仍无法定位，最终降级 | {reason}")
                    element.click()
                    return

            if random_offset:
                click_x = box['x'] + box['width'] * random.uniform(0.3, 0.7)
                click_y = box['y'] + box['height'] * random.uniform(0.3, 0.7)
            else:
                click_x = box['x'] + box['width'] / 2
                click_y = box['y'] + box['height'] / 2

        # 概率犹豫
        if random.random() < self.params["mistake_prob"]:
            self._hesitate(click_x, click_y)

        # 贝塞尔移动到目标位置
        self._move_to(click_x, click_y)

        # 悬停
        time.sleep(random.uniform(*self.params["hover_delay"]))

        # 点击前停顿
        time.sleep(random.uniform(*self.params["click_pre_delay"]))

        # 执行点击：mouse.down + 短延迟 + mouse.up（完全模拟真人按压释放）
        self.page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))  # 真人按下到释放有 50-150ms
        self.page.mouse.up()

        logger.info(f"[SyncHuman] 点击 ({click_x:.0f}, {click_y:.0f}) | {reason}")

    def type_text(
        self,
        target: Optional[ElementHandle],
        text: str,
        *,
        clear_first: bool = False,
        click_first: bool = True,
    ):
        """拟人化文本输入（逐字 + 随机暂停 + 偶尔打错退格）"""
        self._action_count += 1

        if target and click_first:
            self.click(target, reason="聚焦输入框")

        if clear_first:
            self.page.keyboard.press("Control+a")
            time.sleep(random.uniform(0.1, 0.2))
            self.page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.1, 0.3))

        # 思考一下再开始打字
        time.sleep(random.uniform(*self.params["think_delay"]))

        char_min, char_max = self.params["type_char_delay"]

        for char in text:
            # 偶尔打错再退格
            if random.random() < self.params["typo_prob"] and len(text) > 3:
                wrong = chr(ord(char) + random.choice([-1, 1]))
                if wrong.isprintable():
                    self.page.keyboard.type(wrong, delay=random.randint(char_min, char_max))
                    time.sleep(random.uniform(0.1, 0.3))
                    self.page.keyboard.press("Backspace")
                    time.sleep(random.uniform(0.05, 0.15))

            # 正常输入
            self.page.keyboard.type(char, delay=random.randint(char_min, char_max))

            # 偶尔暂停
            if random.random() < self.params["type_pause_prob"]:
                time.sleep(random.uniform(*self.params["type_pause_range"]))

            # 标点稍慢
            if char in ",.!?;:：，。！？\n":
                time.sleep(random.uniform(0.02, 0.08))

        logger.info(f"[SyncHuman] 输入 {len(text)} 字符")

    def press_key(self, key: str, *, reason: str = ""):
        """拟人化按键"""
        time.sleep(random.uniform(0.05, 0.2))
        self.page.keyboard.press(key)
        time.sleep(random.uniform(0.03, 0.1))

    def scroll(self, direction: str = "down", distance: int = None):
        """拟人化滚动（分段变速 + 偶尔回看）"""
        self._action_count += 1
        if distance is None:
            distance = random.randint(300, 800)
        if direction == "up":
            distance = -distance

        steps = random.randint(5, 12)
        step_dist = distance / steps

        for i in range(steps):
            t = i / steps
            speed = 1.0 - 0.5 * t
            actual = step_dist * speed * random.uniform(0.8, 1.2)
            self.page.mouse.wheel(0, actual)
            time.sleep(random.uniform(*self.params["scroll_step_delay"]))

        # 5% 概率回看
        if random.random() < 0.05:
            back = random.randint(100, 300)
            for _ in range(3):
                self.page.mouse.wheel(0, -back / 3)
                time.sleep(random.uniform(0.03, 0.08))
            time.sleep(random.uniform(0.5, 1.0))
            for _ in range(3):
                self.page.mouse.wheel(0, back / 3)
                time.sleep(random.uniform(0.03, 0.08))

        time.sleep(random.uniform(0.2, 0.5))

    def scroll_to_element(self, element: ElementHandle):
        """滚动到元素可见"""
        try:
            element.scroll_into_view_if_needed()
            time.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    def navigate(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 60000):
        """拟人化导航"""
        self._action_count += 1
        self.page.goto(url, wait_until=wait_until, timeout=timeout)
        time.sleep(random.uniform(0.5, 2.0))
        logger.info(f"[SyncHuman] 导航到 {url[:60]}...")

    def wait(self, min_s: float = 0.5, max_s: float = 2.0, *, context: str = ""):
        """拟人化等待（疲劳递增 + 抖动 + 手部微颤）"""
        base = random.uniform(min_s, max_s)
        fatigue = min(1.0 + (self._action_count // 20) * 0.1, 2.0)
        jitter = base * random.uniform(-0.15, 0.15)
        actual = max(0.1, (base + jitter) * fatigue)

        # 偶尔手部微颤
        if random.random() < 0.03 and self.last_mouse_pos:
            cx, cy = self.last_mouse_pos
            self.page.mouse.move(cx + random.uniform(-5, 5), cy + random.uniform(-5, 5))

        time.sleep(actual)

    def think(self):
        """思考延迟"""
        self.wait(*self.params["think_delay"], context="思考")

    # ══════════════════════════════════════════════════
    # 内部工具
    # ══════════════════════════════════════════════════

    def _move_to(self, x: float, y: float):
        """贝塞尔曲线鼠标移动"""
        if self.last_mouse_pos:
            cx, cy = self.last_mouse_pos
        else:
            vp = self.page.viewport_size or {"width": 1366, "height": 768}
            cx, cy = vp["width"] / 2, vp["height"] / 2

        dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        steps = random.randint(*self.params["mouse_steps_range"])
        duration = random.uniform(*self.params["mouse_duration_range"])

        if dist > 500:
            steps = max(steps, random.randint(10, 15))
            duration = max(duration, random.uniform(0.5, 1.0))

        path = self._bezier((cx, cy), (x, y), steps)
        for i, (px, py) in enumerate(path):
            self.page.mouse.move(px, py)
            if i < len(path) - 1:
                d = duration / steps + random.uniform(-0.01, 0.01)
                time.sleep(max(0.001, d))

        self.last_mouse_pos = (x, y)

    def _bezier(self, start: Tuple[float, float], end: Tuple[float, float], steps: int) -> List[Tuple[float, float]]:
        """三次贝塞尔曲线"""
        x0, y0 = start
        x3, y3 = end
        cp1_x = x0 + (x3 - x0) * random.uniform(0.2, 0.4)
        cp1_y = y0 + (y3 - y0) * random.uniform(-0.2, 0.2)
        cp2_x = x0 + (x3 - x0) * random.uniform(0.6, 0.8)
        cp2_y = y0 + (y3 - y0) * random.uniform(-0.2, 0.2)

        points = []
        for i in range(steps + 1):
            t = i / steps
            px = (1-t)**3*x0 + 3*(1-t)**2*t*cp1_x + 3*(1-t)*t**2*cp2_x + t**3*x3
            py = (1-t)**3*y0 + 3*(1-t)**2*t*cp1_y + 3*(1-t)*t**2*cp2_y + t**3*y3
            points.append((px, py))
        return points

    def _hesitate(self, target_x: float, target_y: float):
        """模拟犹豫"""
        off_x = target_x + random.uniform(-80, 80)
        off_y = target_y + random.uniform(-80, 80)
        self._move_to(off_x, off_y)
        time.sleep(random.uniform(0.2, 0.6))
