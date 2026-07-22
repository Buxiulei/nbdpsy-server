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
# 注：hasty / skilled 两档为历史死配置，全仓实例化均走 casual（默认亦为 casual），
# 跨号随机分档属推测性防御本轮不做，故删除以免误导。
PROFILES = {
    "casual": {
        # 用户要求:单次鼠标操作(移动+悬停+按压)控制在 2s 内。camoufox humanize 已关,
        # 每步 mouse.move 仅 ~17ms,故点击总耗时=下列 sleep 预算之和。收紧后单次点击
        # 约:移动≤0.8 + 悬停≤0.25 + 按压前≤0.18 + 按下0.05~0.15 ≈ 最坏 1.4s(元素目标另加
        # scroll≤0.28),稳在 2s 内。
        "click_pre_delay": (0.06, 0.18),
        "hover_delay": (0.1, 0.25),
        "think_delay": (0.4, 1.0),
        "type_char_delay": (55, 110),
        "type_pause_prob": 0.08,
        "type_pause_range": (0.2, 0.5),
        "mouse_steps_range": (5, 9),
        "mouse_duration_range": (0.2, 0.45),
        "scroll_step_delay": (0.04, 0.08),
        "mistake_prob": 0.05,
        "typo_prob": 0.04,
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
                element.scroll_into_view_if_needed(timeout=2000)
                time.sleep(random.uniform(0.12, 0.28))
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
                    # H6 别让兜底悄悄打穿拟人承诺：先用 getBoundingClientRect 取坐标，
                    # 走下方常规坐标点击（mouse.down/up）；仅当坐标彻底拿不到时 fail-loud
                    # 抛异常，绝不降级合成点击（详见下方 else 分支）。
                    rect = None
                    try:
                        rect = self.page.evaluate(
                            "(el) => { const r = el.getBoundingClientRect();"
                            " return {x: r.x, y: r.y, width: r.width, height: r.height}; }",
                            element,
                        )
                    except Exception:
                        rect = None
                    if (
                        rect and rect['width'] > 0 and rect['height'] > 0
                        and rect['x'] >= 0 and rect['y'] >= 0
                    ):
                        box = rect  # 交回常规拟人点击路径
                    else:
                        # 【绝不降级合成点击】坐标彻底拿不到(detached/尺寸0/闭合shadow)时,
                        # 过去这里静默走 element.click()(无贝塞尔/无时序的合成点击)——正是
                        # AI托管检测盯的信号,且是全部 ElementHandle 调用点(含 self_heal 发布按钮)
                        # 的唯一残留非拟人路径。改为 fail-loud 抛异常:交由调用方 try/except 重试
                        # 或判失败,宁可失败也绝不发合成点击。
                        raise RuntimeError(
                            f"[SyncHuman] 元素坐标彻底不可得(detached/尺寸0/闭合shadow),"
                            f"拒绝降级合成点击 | {reason}"
                        )

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
            # H5 仅对 ASCII 字母模拟打错：中文字符 ord±1 会插入无关生僻字，比不打错更假，故跳过
            if (
                char.isascii() and char.isalpha()
                and random.random() < self.params["typo_prob"]
                and len(text) > 3
            ):
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
        """贝塞尔曲线鼠标移动（Fitts 距离缩放 + 缓入缓出 + 手部微颤）"""
        if self.last_mouse_pos:
            cx, cy = self.last_mouse_pos
        else:
            # H4 首次移动不从视口正中心冷启动（恒定中心是 tell），
            # 取一个靠近下部/合理落点的随机起点，指针整体连续性由 last_mouse_pos 跨操作携带。
            vp = self.page.viewport_size or {"width": 1366, "height": 768}
            w, h = vp["width"], vp["height"]
            cx = random.uniform(w * 0.2, w * 0.8)
            cy = random.uniform(h * 0.45, h * 0.9)

        dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)

        # H1 Fitts：时长与步数随移动距离缩放，10px 微调与 400px 长移不再同速。
        base_steps = random.randint(*self.params["mouse_steps_range"])
        base_duration = random.uniform(*self.params["mouse_duration_range"])
        steps = base_steps + int(dist / 140)                       # 距离越远步数越多(每步仅~17ms往返)
        duration = base_duration * (1 + math.log2(dist / 120 + 1))  # 距离越远时长越长（对数缩放）
        duration = min(duration, 0.8)  # 上限 clamp 0.8s:满足"单次鼠标操作≤2s",长移也不拖拉

        path = self._bezier((cx, cy), (x, y), steps)

        # H2 缓入缓出：每步 sleep 按 ease-in-out 权重分配总时长——两端权重大（减速、每步耗时长）、
        # 中段权重小（加速、每步耗时短），而非等时步进。权重取 1-sin(pi*t)：端点 sin≈0 权重最大、
        # 中段 sin≈1 权重最小，正好实现「两端减速中段加速」。
        n_moves = max(1, len(path) - 1)
        weights = [1.0 - 0.7 * math.sin(math.pi * ((i + 0.5) / n_moves)) for i in range(n_moves)]
        w_sum = sum(weights) or 1.0

        for i, (px, py) in enumerate(path):
            # H3 手部微颤：中间落点叠加 ±1-2px 抖动，消除「完美解析贝塞尔零手颤」tell；
            # 末点不加抖动，保证指针精确落在目标坐标（不改点击落点语义）。
            if i < len(path) - 1:
                px += random.uniform(-2, 2)
                py += random.uniform(-2, 2)
            self.page.mouse.move(px, py)
            if i < n_moves:
                d = duration * weights[i] / w_sum + random.uniform(-0.01, 0.01)
                time.sleep(max(0.001, d))

        self.last_mouse_pos = (x, y)

    def _bezier(self, start: Tuple[float, float], end: Tuple[float, float], steps: int) -> List[Tuple[float, float]]:
        """三次贝塞尔曲线（控制点沿运动法向量偏移，任意方向都有自然弧）"""
        x0, y0 = start
        x3, y3 = end
        dx, dy = x3 - x0, y3 - y0
        dist = math.hypot(dx, dy) or 1.0
        # H3 单位法向量（垂直于运动方向）：让水平移动（dy≈0）与垂直移动（dx≈0）都产生横向弧，
        # 而非旧实现只在 y 方向偏移导致垂直移动零弧。偏移量按 dist 缩放（远移弧大、近移弧小）。
        nx, ny = -dy / dist, dx / dist
        off1 = random.uniform(-0.15, 0.15) * dist
        off2 = random.uniform(-0.15, 0.15) * dist
        cp1_x = x0 + dx * random.uniform(0.2, 0.4) + nx * off1
        cp1_y = y0 + dy * random.uniform(0.2, 0.4) + ny * off1
        cp2_x = x0 + dx * random.uniform(0.6, 0.8) + nx * off2
        cp2_y = y0 + dy * random.uniform(0.6, 0.8) + ny * off2

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
