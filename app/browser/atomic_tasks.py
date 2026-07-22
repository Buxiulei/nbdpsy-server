"""小红书发布笔记原子任务模块(step1-7)。

移植自旧仓 ``backend/app/services/xhs_publish_atomic_tasks.py``,忠实保留全部
发布历史坑(§6.4)。相对旧仓的收敛:

- logger:旧仓 ``app.core.logger`` → 本仓统一 ``loguru``。
- 拟人化层:``app.services.smart_browser.sync_human_actions`` → ``app.browser.sync_human_actions``。
- ``_find_element_with_retry`` 删除 SmartLocator lazy-import 兜底分支 —— 新仓无
  SmartLocator,所有 CSS 选择器失败即直接返回 None(降级为直接失败)。
- 删 step5b(@ 提及)与 ``_insert_one_mention``:本任务不做 @ 提及。
- 删 orchestrator 专用的模块级 async 函数(add_mention_in_note / add_topic_tag /
  check_risk_control)与 RISK_* 常量。
- step5 标题按 ``text_formatter.get_display_length`` **硬截断** ≤20(旧仓靠 LLM 缩减,
  新仓无 AI);正文剥 `#` 串 + 安全截断 900;话题去重截断 ≤10 抽成纯函数便于单测。

发布坑详见各 step docstring 与 task-3.3-report.md 逐条对照。
"""
import json
import re
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

from playwright.sync_api import Page, ElementHandle
from loguru import logger

from app.core.config import settings
from app.browser.text_formatter import get_display_length, truncate_by_display
from app.browser.selector_registry import get_default_registry
from app.browser.self_heal import SelfHealLocator

# ── 小红书发布硬约束常量 ──
XHS_MAX_TITLE_DISPLAY = 20   # 标题显示长度上限(get_display_length 度量)
XHS_MAX_BODY_LENGTH = 900    # 正文安全上限(正文+标签共 1000,标签另占约 60)
XHS_MAX_TOPICS = 10          # 单篇话题数上限(超过弹「最多添加10个话题」拦发布)

# 结尾 "#话题" 串正则(含其间空白/换行/全角空格),供 strip_trailing_hashtags 单一来源剥离
_TRAILING_HASHTAGS_RE = re.compile(r'(?:[\s　]*#[^#\s　]+)+[\s　]*$')


# =============================================================================
# 纯函数区(不依赖浏览器,可脱离真页面单测)
# =============================================================================

def strip_trailing_hashtags(content: str) -> str:
    """剥掉正文末尾的 ``#话题`` 串(话题单一来源原则)。

    正文里的 ``#话题`` 若由 step5 打字会被小红书自动转 topic chip,step6 又用下拉
    再加一遍同样的 tags → 双份 topic + 前缀误匹配 → 超过 10 个被「最多添加10个话题」
    拦截发布(实测 RCA 2026-05-18)。解法:step5 只填纯正文,把结尾话题串全部剥掉,
    话题统一交给 step6 受控插入。
    """
    if not content:
        return content
    return _TRAILING_HASHTAGS_RE.sub('', content).rstrip()


def truncate_title(title: str) -> str:
    """标题按小红书显示长度**硬截断**到 ≤20(用 ``text_formatter``,不切半个 emoji)。

    旧仓靠 LLM 缩减标题,新仓无 AI,超长直接硬截断(见 task-3.3 §6.4 坑#5)。
    """
    if not title:
        return title
    if get_display_length(title) <= XHS_MAX_TITLE_DISPLAY:
        return title
    return truncate_by_display(title, XHS_MAX_TITLE_DISPLAY)


def truncate_body(content: str) -> str:
    """正文安全截断到 900 字(标签另占约 60 字)。"""
    if content and len(content) > XHS_MAX_BODY_LENGTH:
        return content[:XHS_MAX_BODY_LENGTH]
    return content


def dedupe_topics(tags: Optional[List[str]]) -> List[str]:
    """话题去重 + 截断到 ≤10(小红书单篇最多 10 个话题,实测 RCA 2026-05-18)。

    去重键为 ``lstrip('#').strip()``,保留首次出现的原始写法;超过 10 个截断。
    """
    seen = set()
    dedup: List[str] = []
    for t in (tags or []):
        key = (t or "").lstrip("#").strip()
        if key and key not in seen:
            seen.add(key)
            dedup.append(t)
    if len(dedup) > XHS_MAX_TOPICS:
        logger.warning(f"话题 {len(dedup)} 个超过小红书上限 {XHS_MAX_TOPICS},截断")
    return dedup[:XHS_MAX_TOPICS]


class XHSPublishAtomicTasks:
    """小红书发布笔记的原子任务集合(sync Playwright)。

    所有与页面的交互通过 ``SyncHumanActions`` 收口,注入贝塞尔鼠标轨迹、逐字打字、
    随机延迟等拟人化行为,避免被小红书反自动化检测。
    """

    def __init__(self, page: Page, enable_debug: Optional[bool] = None, screenshot_dir: Optional[str] = None):
        """初始化原子任务执行器。

        Args:
            page: Playwright Page 对象
            enable_debug: 是否截图。None 跟随全局总开关 DEBUG_SCREENSHOTS_ENABLED;
                即使显式 True 也被全局开关 AND 压制(防绕过撑满磁盘)。
            screenshot_dir: 截图目录;None → DATA_DIR/debug_screenshots
        """
        from app.core.config import settings
        self.page = page
        global_on = settings.DEBUG_SCREENSHOTS_ENABLED
        self.enable_debug = global_on if enable_debug is None else (enable_debug and global_on)
        self.screenshot_dir = screenshot_dir or str(Path(settings.DATA_DIR) / "debug_screenshots")
        self.current_step = 0

        # 拟人化操作层(所有页面交互必须经过此层)
        from app.browser.sync_human_actions import SyncHumanActions
        self.human = SyncHumanActions(page, profile="casual")

        # 选择器自愈:learned 前置缓存 + LLM 兜底定位(默认关,SELFHEAL_ENABLED 才生效)。
        # 用进程级单例复用同一 registry + 同一把锁,消除并发发布跨实例写同一 JSON 的竞争。
        self._registry = get_default_registry()
        self._locator = SelfHealLocator()

    def _take_screenshot(self, name: str) -> str:
        """保存截图(仅在调试模式下),返回路径或空串。"""
        if not self.enable_debug:
            return ""
        os.makedirs(self.screenshot_dir, exist_ok=True)
        timestamp = int(time.time())
        screenshot_path = f"{self.screenshot_dir}/publish_{self.current_step:02d}_{name}_{timestamp}.png"
        try:
            self.page.screenshot(path=screenshot_path)
            logger.info(f"📸 截图已保存: {screenshot_path}")
            return screenshot_path
        except Exception as e:
            logger.error(f"截图失败: {e}")
            return ""

    def _wait_for_stable_url(self, timeout: int = 5) -> str:
        """等待 URL 稳定(连续 3 次不变)。"""
        last_url = self.page.url
        stable_count = 0
        max_checks = timeout * 2  # 每 0.5 秒检查一次

        for _ in range(max_checks):
            self.human.wait(0.3, 0.6, context="查找元素间隔")
            current_url = self.page.url
            if current_url == last_url:
                stable_count += 1
                if stable_count >= 3:
                    return current_url
            else:
                stable_count = 0
                last_url = current_url
        return last_url

    def _find_element_with_retry(
        self,
        selectors: List[str],
        timeout: int = 10,
        must_be_visible: bool = True,
        intent_key: Optional[str] = None,
        intent_desc: Optional[str] = None,
    ) -> Optional[ElementHandle]:
        """用多个选择器查找元素,支持重试。

        新仓删除了旧仓的 SmartLocator lazy-import 兜底 —— 所有 CSS 选择器在
        ``timeout`` 内均未命中即降级失败。若传 ``intent_key``,叠加选择器自愈:
        - learned 前置:``SELFHEAL_ENABLED`` 开时把 registry 已学到的选择器插到候选
          最前(去重);默认关时整段不触发,与硬编码分支逐字节一致(与下面 LLM 兜底
          同一开关口径,避免"学过再关开关仍前置"的非等价)。
        - LLM 兜底:硬编码选择器全失效 + ``SELFHEAL_ENABLED`` + ``LLM_API_KEY`` 时,
          调 SelfHealLocator 快照定位并 learn。默认关时整条不触发。
        """
        # learned 前置:已学到的选择器插到候选最前,去重保序。仅在自愈开关开时生效,
        # 与下面 LLM 兜底同口径 —— 关闭后即使 registry 有 learned 也不前置,严格字节等价。
        if intent_key and settings.SELFHEAL_ENABLED:
            try:
                learned = self._registry.get(intent_key)
            except Exception:
                learned = []
            if learned:
                selectors = learned + [s for s in selectors if s not in learned]

        start_time = time.time()

        while time.time() - start_time < timeout:
            for selector in selectors:
                try:
                    element = self.page.wait_for_selector(
                        selector,
                        timeout=1000,
                        state="visible" if must_be_visible else "attached"
                    )
                    if element:
                        logger.info(f"✓ 找到元素: {selector}")
                        return element
                except Exception:
                    # 退回 query_selector_all 再试一次
                    try:
                        elements = self.page.query_selector_all(selector)
                        for elem in elements:
                            if not must_be_visible or elem.is_visible():
                                logger.info(f"✓ 找到元素: {selector}")
                                return elem
                    except Exception:
                        continue
            self.human.wait(0.3, 0.6, context="查找元素间隔")

        logger.warning(f"未找到元素,尝试了 {len(selectors)} 个选择器")

        # 自愈兜底:硬编码选择器全失效 + 开关开 + 配了 key → LLM 快照定位并 learn。
        # locate 全程 try/except 不抛;learn 失败也不打断,始终返回 handle 或 None。
        if intent_key and settings.SELFHEAL_ENABLED and settings.LLM_API_KEY:
            try:
                found = self._locator.locate(self.page, intent_key, intent_desc or intent_key)
            except Exception as exc:
                logger.warning(f"[self_heal] 定位兜底异常:{exc}")
                found = None
            if found:
                handle, sel = found
                if sel:
                    try:
                        self._registry.learn(
                            intent_key, sel, intent_desc or intent_key,
                            datetime.now(timezone.utc).isoformat(),
                        )
                    except Exception as exc:
                        logger.warning(f"[self_heal] 学习选择器失败:{exc}")
                logger.info(f"✓ 自愈定位成功: intent={intent_key} selector={sel}")
                return handle

        return None

    # ==================== 步骤1: 打开发布页面 ====================

    def step1_open_publish_page(self) -> Dict[str, Any]:
        """步骤1: 打开发布页面。

        策略:先访问主站探索页,点击右上角「发布笔记」link 进入创作中心(会打开
        新窗口并触发 SSO 自动认证);SSO 失败时回主站再走一次 SSO 入口,仍失败则
        返回 ``need_manual_login=True``(独立信号,见 §6.4 坑#6)。
        """
        self.current_step = 1
        logger.info("=" * 60)
        logger.info("步骤1: 打开发布页面")
        logger.info("=" * 60)

        try:
            # 1.1 访问主站探索页,验证登录状态
            logger.info("1.1 访问小红书探索页,验证登录状态...")
            self.human.navigate("https://www.xiaohongshu.com/explore")
            self.page.wait_for_selector("body", timeout=5000, state="visible")
            self._take_screenshot("01_explore_page")

            main_url = self.page.url
            if "login" in main_url.lower():
                return {
                    "success": False,
                    "error": "主站未登录,Cookie可能已失效",
                    "screenshot": self._take_screenshot("01_main_not_logged_in"),
                }
            logger.info("✓ 主站已登录")

            # 1.2 关闭可能的弹窗
            logger.info("1.2 检查并关闭可能的弹窗...")
            try:
                close_button_selectors = [
                    '.reds-mask',
                    '[aria-label="关闭"]',
                    '[aria-label="Close"]',
                    '.close-button',
                    '.modal-close',
                    'button:has-text("关闭")',
                    'button:has-text("取消")',
                    'svg[class*="close"]',
                ]
                for selector in close_button_selectors:
                    try:
                        close_btn = self.page.wait_for_selector(selector, timeout=2000, state="visible")
                        if close_btn:
                            logger.info(f"找到弹窗,点击关闭: {selector}")
                            self.human.click(close_btn, reason=f"关闭弹窗 {selector}")
                            self.human.wait(0.5, 1.0, context="弹窗关闭后")
                            break
                    except Exception:
                        continue
            except Exception:
                logger.info("没有发现弹窗,继续...")

            # 1.3 查找并点击「发布」按钮
            # explore 页右上角本身就有「发布笔记」link(指向 creator.xiaohongshu.com/publish/publish),
            # 直接点它保留 SSO 跳转新窗口创作中心。禁跳 /user/profile/me(触发风控强制扫码)。
            logger.info("1.3 查找并点击'发布'按钮...")
            publish_button_selectors = [
                'a:has-text("发布笔记")',
                'a:has-text("发布")',
                'a[href*="creator.xiaohongshu.com"]',
                'a[href*="/publish"]',
                'button:has-text("发布笔记")',
                'button:has-text("发布")',
                '.publish-button',
                '[data-v-*]:has-text("发布")',
                'svg[class*="publish"]',
                '[aria-label*="发布"]',
                '[class*="publish-btn"]',
                '[class*="create-btn"]',
            ]

            publish_button = None
            for selector in publish_button_selectors:
                try:
                    publish_button = self.page.wait_for_selector(selector, timeout=3000, state="visible")
                    if publish_button:
                        logger.info(f"✓ 找到发布按钮: {selector}")
                        break
                except Exception:
                    continue

            if not publish_button:
                logger.warning("未找到发布按钮,尝试直接访问创作中心...")
                self.page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded", timeout=60000)
            else:
                logger.info("点击发布按钮(会打开新窗口)...")
                context = self.page.context
                with context.expect_page() as new_page_info:
                    try:
                        self.human.click(publish_button, reason="主站发布按钮")
                    except Exception as e:
                        logger.warning(f"拟人化点击失败: {e}")
                        logger.info("尝试再次点击...")
                        self.human.click(publish_button, reason="降级-主站发布")

                new_page = new_page_info.value
                logger.info("✓ 检测到新窗口打开")
                new_page.wait_for_load_state("domcontentloaded", timeout=60000)

                # 切换到新页面(同步更新拟人化操作层的 page 引用)
                self.page = new_page
                self.human.page = new_page
                logger.info("✓ 已切换到新窗口")

                # 等待页面稳定并监控 URL 变化
                # 流程:初始URL(/publish/publish) -> 登录页(/login) -> 自动认证(10-15秒) -> 发布页
                logger.info("等待页面加载和自动认证完成...")
                try:
                    logger.info("等待URL稳定在发布页...")
                    self.page.wait_for_url("**/publish/publish**", timeout=30000)
                    logger.info("✓ URL已匹配发布页模式")
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)

                    final_url = self.page.url
                    logger.info(f"最终URL: {final_url}")

                    if "login" in final_url.lower():
                        logger.warning("⚠️ 页面重定向到登录页,等待自动认证...")
                        for i in range(15):
                            self.human.wait(0.8, 1.5, context="SSO等待")
                            current_url = self.page.url
                            if "login" not in current_url.lower():
                                logger.info(f"✓ 自动认证完成,当前URL: {current_url}")
                                final_url = current_url
                                break
                            if (i + 1) % 5 == 0:
                                logger.info(f"等待自动认证... ({i+1}/15秒)")
                        else:
                            logger.error("❌ 等待15秒后仍未完成自动认证")
                except Exception as e:
                    logger.warning(f"等待URL时出错: {e}")
                    final_url = self.page.url
                    logger.info(f"当前URL: {final_url}")

            self._take_screenshot("02_after_click_publish")

            # 1.3 验证是否进入创作中心发布页面
            logger.info("1.3 验证是否进入创作中心发布页面...")
            final_url = self.page.url

            # 1.3.1 检查页面内容是否为登录表单(creator 域 SSO 失败时 URL 不变但显示登录页)
            self.human.wait(2.0, 4.0, context="页面渲染")
            is_login_page = False
            try:
                login_form = self.page.query_selector('input[placeholder*="手机号"], input[type="tel"], button:has-text("登录"), .login-container, [class*="login-form"]')
                if login_form:
                    is_login_page = True
                    logger.warning("⚠️ 检测到页面为登录表单(URL 未变但实际是登录页)")
            except Exception:
                pass

            # 1.4 检查是否成功进入创作中心
            if "login" in final_url.lower() or is_login_page:
                logger.warning("⚠️ 创作中心未登录,尝试通过主站 SSO 自动认证...")
                try:
                    self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
                    self.human.wait(1.5, 3.0, context="SSO导航")
                    self.page.goto("https://creator.xiaohongshu.com/creator/home", wait_until="domcontentloaded", timeout=60000)
                    self.human.wait(4.0, 6.0, context="SSO认证")

                    sso_url = self.page.url
                    logger.info(f"SSO 重试后 URL: {sso_url}")
                    self._take_screenshot("02_sso_retry")

                    sso_login_form = self.page.query_selector('input[placeholder*="手机号"], input[type="tel"], button:has-text("登录")')
                    if sso_login_form or "login" in sso_url.lower():
                        logger.error("❌ SSO 自动认证失败,创作中心需要手动登录")
                        return {
                            "success": False,
                            "error": "创作中心未登录,请使用远程浏览器手动登录一次",
                            "screenshot": self._take_screenshot("02_creator_not_logged_in"),
                            "need_manual_login": True,
                        }

                    logger.info("✓ SSO 认证成功,跳转到发布页...")
                    self.page.goto("https://creator.xiaohongshu.com/publish/publish?source=official", wait_until="domcontentloaded", timeout=30000)
                    self.human.wait(2.0, 4.0, context="发布页加载")
                    final_url = self.page.url
                except Exception as sso_err:
                    logger.error(f"SSO 重试失败: {sso_err}")
                    return {
                        "success": False,
                        "error": "创作中心未登录,请使用远程浏览器手动登录一次",
                        "screenshot": self._take_screenshot("02_creator_not_logged_in"),
                        "need_manual_login": True,
                    }

            # 1.5 验证是否成功打开发布页面
            if "creator.xiaohongshu.com" in final_url and ("publish" in final_url or self._check_upload_area_exists()):
                logger.info("✓ 成功进入创作中心发布页面")
                return {"success": True, "url": final_url}
            else:
                logger.warning(f"未能确认是否进入发布页面,当前URL: {final_url}")
                logger.info("尝试直接访问发布页面...")
                self.page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded", timeout=30000)
                self.human.wait(2.0, 4.0, context="页面加载")

                final_url = self._wait_for_stable_url(timeout=5)
                self._take_screenshot("03_direct_access")

                if "login" in final_url.lower():
                    return {
                        "success": False,
                        "error": "无法访问创作中心发布页面",
                        "screenshot": self._take_screenshot("03_access_failed"),
                        "need_manual_login": True,
                    }

                if "publish" in final_url or self._check_upload_area_exists():
                    logger.info("✓ 直接访问成功")
                    return {"success": True, "url": final_url}
                else:
                    return {
                        "success": False,
                        "error": "未能打开发布页面",
                        "url": final_url,
                        "screenshot": self._take_screenshot("03_failed_to_open"),
                    }

        except Exception as e:
            logger.error(f"打开发布页面失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("02_exception"),
            }

    def _check_upload_area_exists(self) -> bool:
        """检查上传区域是否存在。"""
        upload_selectors = [
            "input[type='file']",
            ".upload-wrapper",
            ".upload-area",
            "[class*='upload']",
        ]
        for selector in upload_selectors:
            try:
                if self.page.query_selector_all(selector):
                    return True
            except Exception:
                continue
        return False

    # ==================== 步骤2: 上传图片 ====================

    def step2_upload_images(self, image_paths: List[str]) -> Dict[str, Any]:
        """步骤2: 上传图片。

        §6.4 坑#3:创作中心默认「上传视频」tab,图文必须先切「上传图文」tab
        (JS 文本定位坐标点击 + URL ``?type=normal`` 兜底),否则 file input 是视频的。
        """
        self.current_step = 2
        logger.info("=" * 60)
        logger.info(f"步骤2: 上传 {len(image_paths)} 张图片")
        logger.info("=" * 60)

        try:
            # 2.1 检查 URL 是否稳定在发布页;含 login 则等自动认证
            current_url = self.page.url
            logger.info(f"步骤2开始时URL: {current_url}")

            if "login" in current_url.lower():
                logger.warning("⚠️ 检测到登录页,等待自动认证...")
                self._take_screenshot("02_01_login_page_detected")
                try:
                    page_text = self.page.inner_text("body")
                    if "扫码登录" in page_text or "二维码" in page_text:
                        logger.error("❌ 检测到需要扫码登录,无法自动完成")
                        raise Exception("创作中心需要扫码登录,请使用远程浏览器手动登录一次")
                    if "登录中" in page_text or "加载中" in page_text:
                        logger.info("✓ 检测到自动登录提示,继续等待...")
                except Exception as e:
                    logger.warning(f"检查页面内容时出错: {e}")

                for i in range(30):
                    self.human.wait(0.8, 1.5, context="弹窗等待")
                    current_url = self.page.url
                    if "login" not in current_url.lower() and "publish" in current_url.lower():
                        logger.info(f"✓ 自动认证完成,当前URL: {current_url}")
                        self._take_screenshot("02_02_auto_login_success")
                        break
                    if (i + 1) % 5 == 0:
                        logger.info(f"等待自动认证... ({i+1}/30秒)")
                        self._take_screenshot(f"02_01_waiting_login_{i+1}s")
                        if current_url != self.page.url:
                            logger.info(f"URL变化: {self.page.url}")
                            current_url = self.page.url
                else:
                    logger.error("❌ 等待30秒后仍未完成自动认证")
                    # 与 step1 SSO 失败同源:透出独立 need_manual_login 信号(cookie/SSO 坏,
                    # 重试无用),交状态机直接置 failed 而非当普通失败徒劳重试。
                    return {
                        "success": False,
                        "error": "创作中心未登录,自动认证失败。请使用远程浏览器手动登录一次。",
                        "screenshot": self._take_screenshot("02_01_auto_login_timeout"),
                        "need_manual_login": True,
                    }

            url_before_upload = current_url
            logger.info(f"上传前URL: {url_before_upload}")
            self._take_screenshot("03_before_upload")

            # 2.2 点击顶部 tab「上传图文」切换到图文模式(默认是「上传视频」)
            logger.info("2.1 等待页面渲染完成,查找并点击'上传图文' tab...")
            tab_clicked = False
            for attempt in range(15):
                image_upload_tab = self.page.evaluate("""
                    () => {
                        const candidates = Array.from(document.querySelectorAll('span, div, a, li'))
                            .filter(el => {
                                const text = el.textContent.trim();
                                if (text !== '上传图文') return false;
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0 && rect.top < 200 && rect.top > 0;
                            });
                        if (candidates.length > 0) {
                            const el = candidates[0];
                            const rect = el.getBoundingClientRect();
                            return { found: true, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                        }
                        return { found: false };
                    }
                """)

                if image_upload_tab and image_upload_tab.get("found"):
                    tab_x = image_upload_tab["x"]
                    tab_y = image_upload_tab["y"]
                    logger.info(f"✓ 找到'上传图文' tab 坐标: ({tab_x:.0f}, {tab_y:.0f})(第 {attempt+1} 次尝试)")
                    self.human.click((tab_x, tab_y), reason="上传图文 tab")
                    self.human.wait(1.0, 2.0, context="等待 tab 切换")
                    try:
                        self.page.wait_for_selector("input[type='file']", timeout=5000, state="attached")
                    except Exception:
                        pass
                    self._take_screenshot("03_01_after_click_image_upload")
                    logger.info("✓ 已切换到图文上传模式")
                    tab_clicked = True
                    break

                self.human.wait(0.8, 1.2, context="等待 tab 渲染")
                if (attempt + 1) % 5 == 0:
                    logger.info(f"   等待 tab 渲染... ({attempt+1}/15)")
                    self._take_screenshot(f"03_00_waiting_tab_{attempt+1}")

            if not tab_clicked:
                # 兜底:URL 参数直接切图文模式
                logger.warning("⚠️ 15秒内未找到'上传图文' tab,尝试 URL 兜底...")
                try:
                    current_url = self.page.url
                    if "publish" in current_url:
                        self.page.goto(current_url.split("?")[0] + "?source=official&type=normal", wait_until="domcontentloaded", timeout=10000)
                        self.human.wait(2.0, 3.0, context="URL 兜底等待")
                        self._take_screenshot("03_00_url_fallback")
                        logger.info("✓ 已通过 URL 参数切换到图文模式")
                        tab_clicked = True
                except Exception as e:
                    logger.warning(f"URL 兜底失败: {e}")

            if not tab_clicked:
                return {
                    "success": False,
                    "error": "无法切换到'上传图文'模式,页面 tab 未渲染",
                    "screenshot": self._take_screenshot("03_00_tab_not_found"),
                }

            # 2.2b 校验真进图文模式(坐标点击可能没生效、停留在视频 tab)。
            # 不校验会把图片塞进视频 file input 还误报成功 → 下游 step3/5 才暴露。
            if not self._ensure_image_mode():
                return {
                    "success": False,
                    "error": "点击'上传图文'后未进入图文模式(疑似停留在视频tab)",
                    "screenshot": self._take_screenshot("03_02_not_image_mode"),
                }
            logger.info("✓ 已确认处于图文上传模式")

            # 2.3 查找文件上传 input 元素(优先图片入口，绝不回退到视频 file input)
            logger.info("2.3 查找文件上传input元素...")
            upload_input_selectors = [
                "input[type='file'][accept*='image']",
                "input[type='file'][accept*='png']",
                "input[type='file'][accept*='jpg']",
                "input[type='file']",
                ".upload-input",
                "input.upload-input",
            ]
            # 2.4 上传文件:优先点「上传图片」按钮走 file_chooser。
            # 坑:新版编辑器直接 set_input_files 到隐藏 input 只存草稿 + 页面重置回视频
            # tab、编辑器不驻留;而点按钮触发的 file_chooser 会正常进入并停留在图文编辑器
            # (标题框 placeholder「填写标题会有更多赞哦」出现)。失败再回退 set_input_files。
            logger.info(f"2.4 点「上传图片」按钮上传 {len(image_paths)} 张(file_chooser)...")
            uploaded_ok = False
            try:
                with self.page.expect_file_chooser(timeout=8000) as fc_info:
                    clicked = False
                    # 合规:拟人化点「上传图片」按钮(定位坐标 → human.click 真实按压),
                    # 真实用户手势才能触发原生 file_chooser,禁裸 page.click。
                    for sel in ["button:has-text('上传图片')", "text=上传图片"]:
                        try:
                            bb = self.page.locator(sel).first.bounding_box(timeout=4000)
                            if not bb:
                                continue
                            self.human.click(
                                (bb["x"] + bb["width"] * 0.5,
                                 bb["y"] + bb["height"] * 0.5),
                                reason="上传图片按钮",
                            )
                            clicked = True
                            break
                        except Exception:
                            continue
                    if not clicked:
                        raise RuntimeError("未找到「上传图片」按钮")
                fc_info.value.set_files(image_paths)
                logger.info("✓ file_chooser 已设置全部图片")
                uploaded_ok = True
            except Exception as e:
                logger.warning(f"file_chooser 上传失败({e}),回退 set_input_files 到隐藏 input")

            if not uploaded_ok:
                upload_input = self._find_element_with_retry(
                    upload_input_selectors, timeout=10, must_be_visible=False,
                    intent_key="upload_image_input", intent_desc="上传图片的 file input",
                )
                if not upload_input:
                    return {
                        "success": False,
                        "error": "未找到文件上传input元素",
                        "screenshot": self._take_screenshot("03_02_no_upload_input"),
                    }
                upload_input.set_input_files(image_paths)
                logger.info("✓ (回退)set_input_files 完成")

            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self.human.wait(1.5, 2.5, context="上传完成")
            self._take_screenshot("04_after_upload")

            # 2.5 验证 URL 未变化(防止自动返回)
            url_after_upload = self.page.url
            logger.info(f"上传后URL: {url_after_upload}")
            if url_after_upload != url_before_upload and "publish" not in url_after_upload:
                return {
                    "success": False,
                    "error": f"上传后页面跳转了: {url_before_upload} -> {url_after_upload}",
                    "screenshot": self._take_screenshot("04_url_changed"),
                }

            logger.info("✓ URL未变化,上传成功")
            return {"success": True, "uploaded_count": len(image_paths)}

        except Exception as e:
            logger.error(f"上传图片失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("04_exception"),
            }

    # ==================== 步骤3: 等待上传处理 ====================

    def step3_wait_for_upload_processing(self, max_wait: int = 30) -> Dict[str, Any]:
        """步骤3: 等待上传处理完成(编辑界面出现 / 继续编辑按钮出现)。"""
        self.current_step = 3
        logger.info("=" * 60)
        logger.info("步骤3: 等待上传处理完成")
        logger.info("=" * 60)

        try:
            # 新版 file_chooser 上传后编辑器即时加载 —— 先快速检测，命中直接成功。
            # 关键:小红书会在编辑器打开几十秒后自动存草稿 + 重置回视频 tab，慢速
            # _find_element_with_retry(自愈 LLM)期间会错过窗口(实测 step2 结束时
            # 编辑器在、step3 慢检时已重置)。故这里抢先快速判定。
            if self._check_edit_page_loaded():
                logger.info("✓ 编辑器已即时加载(file_chooser 上传后),直接进入编辑")
                self._take_screenshot("05_editor_ready_fast")
                return {"success": True, "edit_page_loaded": True}

            logger.info("3.1 等待编辑界面加载...")
            edit_indicators = [
                "input[placeholder*='标题']",
                "input[placeholder*='填写标题']",
                "//button[contains(text(), '继续编辑')]",
                "div[contenteditable='true']",
            ]
            self._find_element_with_retry(
                edit_indicators, timeout=10,
                intent_key="editor_ready", intent_desc="编辑器就绪的指示元素",
            )
            self._take_screenshot("05_after_initial_wait")

            logger.info("3.2 检查页面状态...")
            url_current = self.page.url
            logger.info(f"当前URL: {url_current}")

            if "publish" not in url_current:
                return {
                    "success": False,
                    "error": f"页面已自动返回: {url_current}",
                    "screenshot": self._take_screenshot("05_auto_returned"),
                }

            if self._check_edit_page_loaded():
                logger.info("✓ 已自动进入编辑界面")
                self._take_screenshot("06_edit_page_loaded")
                return {"success": True, "edit_page_loaded": True}

            logger.info("3.3 查找'继续编辑'按钮...")
            continue_button = self._find_continue_edit_button()
            if continue_button:
                logger.info("✓ 找到'继续编辑'按钮")
                return {"success": True, "edit_page_loaded": False, "continue_button_found": True}

            logger.info("3.4 等待编辑界面或继续编辑按钮出现...")
            waited = 5
            while waited < max_wait:
                self.human.wait(1.5, 2.5, context="上传处理")
                waited += 2

                current_url = self.page.url
                if "publish" not in current_url:
                    return {
                        "success": False,
                        "error": f"等待过程中页面跳转: {current_url}",
                        "screenshot": self._take_screenshot("06_url_changed_during_wait"),
                    }

                if self._check_edit_page_loaded():
                    logger.info(f"✓ 编辑界面已加载(等待了{waited}秒)")
                    self._take_screenshot("06_edit_page_loaded")
                    return {"success": True, "edit_page_loaded": True, "wait_time": waited}

                continue_button = self._find_continue_edit_button()
                if continue_button:
                    logger.info(f"✓ 找到'继续编辑'按钮(等待了{waited}秒)")
                    return {"success": True, "edit_page_loaded": False, "continue_button_found": True, "wait_time": waited}

                if waited % 10 == 0:
                    logger.info(f"仍在等待... ({waited}/{max_wait}秒)")
                    self._take_screenshot(f"06_waiting_{waited}s")

            return {
                "success": False,
                "error": f"等待超时({max_wait}秒),未找到编辑界面或继续编辑按钮",
                "screenshot": self._take_screenshot("06_timeout"),
            }

        except Exception as e:
            logger.error(f"等待上传处理失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("06_exception"),
            }

    def _check_edit_page_loaded(self) -> bool:
        """检查图文笔记编辑界面是否已加载(快速 DOM 检测)。

        以**标题输入框**为图文编辑器唯一标志(新版占位「填写标题会有更多赞哦」，
        旧版「添加标题」等)。不接受宽松裸 ``contenteditable``(视频 tab 也有 → 假阳性)。
        用 page.evaluate 一次性 DOM 存在性判定(不逐个 wait/self-heal，抢在小红书自动
        存草稿+重置回视频 tab 之前命中——实测编辑器驻留窗口只有几十秒)。
        """
        try:
            return bool(self.page.evaluate("""() => {
                const q = s => document.querySelector(s);
                const title = q("input[placeholder*='标题']") || q("textarea[placeholder*='标题']");
                const body = q("[contenteditable='true'][data-placeholder*='正文']")
                    || q("[contenteditable='true'][placeholder*='正文']")
                    || q("textarea[placeholder*='正文']")
                    || q("div[contenteditable='true']");
                // 标题框是图文编辑器的确定标志；正文框作为辅助
                return !!title || (!!body && document.body.innerText.includes('填写标题'));
            }"""))
        except Exception:
            return False

    def _image_mode_ready(self) -> bool:
        """是否已进入「上传图文」模式(而非默认「上传视频」)。

        用**页面文本标志**判定(小红书图文上传区是按钮触发、file input 隐藏且不带
        accept=image，靠 input 选择器判不出，实测会假阴性把已切好的图文模式误判成
        未切、白重试到驱动崩溃)。图文模式独有文案:"上传图片/文字配图/写文字生成图片
        /图片格式/图片分辨率"；已进编辑器(标题框)也算。反向:仍以"拖拽视频到此"为主 = 视频模式。
        """
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""
        image_signals = (
            "写文字生成图片" in body
            or "文字配图" in body
            or ("图片格式" in body and "图片分辨率" in body)
            or ("上传图片" in body and "拖拽视频到此" not in body)
        )
        if image_signals:
            return True
        return self._check_edit_page_loaded()

    def _click_image_text_tab(self) -> bool:
        """定位并坐标点击顶部「上传图文」tab，返回是否点击成功。"""
        try:
            tab = self.page.evaluate("""
                () => {
                    const cands = Array.from(document.querySelectorAll('span, div, a, li'))
                        .filter(el => {
                            if (el.textContent.trim() !== '上传图文') return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && r.top < 200 && r.top > 0;
                        });
                    if (!cands.length) return { found: false };
                    const r = cands[0].getBoundingClientRect();
                    return { found: true, x: r.x + r.width / 2, y: r.y + r.height / 2 };
                }
            """)
        except Exception:
            return False
        if tab and tab.get("found"):
            self.human.click((tab["x"], tab["y"]), reason="上传图文 tab(校验重试)")
            return True
        return False

    def _ensure_image_mode(self, tries: int = 4) -> bool:
        """确保处于图文模式；未进入则重试点 tab + URL 兜底，直到出现图文上传入口。

        坑：坐标点击「上传图文」偶发不生效(实测三次跑里一次卡在视频 tab、一次编辑器
        没渲染)，只查 input[type=file] 存在会误判(视频 tab 也有)。这里以图片上传入口
        /标题框为准反复校验，配 URL ``?type=normal`` 兜底。
        """
        for attempt in range(1, tries + 1):
            if self._image_mode_ready():
                if attempt > 1:
                    logger.info(f"✓ 已确认进入图文模式(第 {attempt} 次校验)")
                return True
            logger.warning(f"⚠️ 尚未进入图文模式(第 {attempt}/{tries} 次)，重试切换...")
            self._click_image_text_tab()
            if attempt >= 2:
                # URL 兜底直切图文
                try:
                    cur = self.page.url
                    if "publish" in cur:
                        self.page.goto(
                            cur.split("?")[0] + "?source=official&type=normal",
                            wait_until="domcontentloaded", timeout=10000,
                        )
                except Exception as e:
                    logger.warning(f"URL 兜底失败: {e}")
            self.human.wait(1.2, 2.0, context="等图文模式渲染")
        return self._image_mode_ready()

    def _find_continue_edit_button(self) -> Optional[ElementHandle]:
        """查找'继续编辑'按钮。"""
        continue_selectors = [
            "//button[contains(text(), '继续编辑')]",
            "//span[contains(text(), '继续编辑')]",
            "//div[contains(text(), '继续编辑')]",
            "//a[contains(text(), '继续编辑')]",
            "button:has-text('继续编辑')",
            "span:has-text('继续编辑')",
            ".btn:has-text('继续编辑')",
        ]
        for selector in continue_selectors:
            try:
                if selector.startswith("//"):
                    elements = self.page.query_selector_all(f"xpath={selector}")
                else:
                    elements = self.page.query_selector_all(selector)
                for elem in elements:
                    try:
                        if elem.is_visible() and "继续编辑" in elem.inner_text():
                            return elem
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    # ==================== 步骤4: 进入编辑界面 ====================

    def step4_enter_edit_page(self, continue_button: Optional[ElementHandle] = None) -> Dict[str, Any]:
        """步骤4: 点击'继续编辑'进入编辑界面。"""
        self.current_step = 4
        logger.info("=" * 60)
        logger.info("步骤4: 进入编辑界面")
        logger.info("=" * 60)

        try:
            if self._check_edit_page_loaded():
                logger.info("✓ 已在编辑界面,无需操作")
                return {"success": True, "already_in_edit_page": True}

            if not continue_button:
                logger.info("4.1 查找'继续编辑'按钮...")
                continue_button = self._find_continue_edit_button()

            if not continue_button:
                return {
                    "success": False,
                    "error": "未找到'继续编辑'按钮",
                    "screenshot": self._take_screenshot("07_no_continue_button"),
                }

            logger.info("4.2 点击'继续编辑'按钮...")
            url_before_click = self.page.url
            self._take_screenshot("07_before_click_continue")
            self.human.click(continue_button, reason="继续编辑按钮")
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self._take_screenshot("08_after_click_continue")

            logger.info("4.3 等待编辑界面加载...")
            max_wait = 20
            waited = 0
            while waited < max_wait:
                current_url = self.page.url
                if current_url != url_before_click and "publish" not in current_url:
                    return {
                        "success": False,
                        "error": f"点击后页面跳转: {current_url}",
                        "screenshot": self._take_screenshot("08_url_changed"),
                    }

                if self._check_edit_page_loaded():
                    logger.info(f"✓ 编辑界面已加载(等待了{waited}秒)")
                    self._take_screenshot("08_edit_page_loaded")
                    return {"success": True, "wait_time": waited}

                self.human.wait(1.5, 2.5, context="编辑页加载")
                waited += 2
                if waited % 6 == 0:
                    logger.info(f"仍在等待... ({waited}/{max_wait}秒)")
                    self._take_screenshot(f"08_waiting_{waited}s")

            return {
                "success": False,
                "error": f"等待编辑界面超时({max_wait}秒)",
                "screenshot": self._take_screenshot("08_timeout"),
            }

        except Exception as e:
            logger.error(f"进入编辑界面失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("08_exception"),
            }

    # ==================== 步骤5: 填写标题和内容 ====================

    def _type_into_robust(
        self,
        selectors: List[str],
        value: str,
        *,
        intent_key: Optional[str] = None,
        intent_desc: Optional[str] = None,
        tries: int = 3,
    ) -> tuple:
        """定位并填入文本，抗 DOM 脱离。返回 ``(ok: bool, err: Optional[str])``。

        坑：小红书创作页编辑器一聚焦即 React 重渲染，把先前 ``_find_element_with_retry``
        拿到的 ElementHandle 指向的节点从 DOM 脱离——此后旧句柄无论 ``type_text`` 还是
        降级 ``fill`` 都抛 ``Element is not attached to the DOM``（历史 job2、account1
        实测复现均死在此）。故**每次尝试都重新定位取新句柄**；命中脱离异常则短暂等待
        （等编辑器渲染稳定）后重定位重试，而非死抱一个已脱离的旧句柄。
        """
        css_selectors = [s for s in selectors if not s.startswith("//")]
        # 拟人化输入(合规硬要求:发布链路所有交互必须走 SyncHumanActions,禁止 JS 注入
        # 赋值/dispatchEvent —— JS 直填是"AI 托管"检测的典型信号,曾致账号被判违规禁发)。
        # 做法:只**读取**输入框坐标(不持 ElementHandle,规避 React 聚焦重渲染导致的旧句柄
        # "not attached" 脱离),用 human.click(坐标) 拟人聚焦(贝塞尔移动+悬停+真实按压),
        # 再 human.type_text 逐字键盘输入(随机延迟/偶尔打错退格/标点稍慢)。真人点击即触发的
        # 重渲染由 React 自身保留焦点到新节点,键盘输入照常落入 —— 与真人打字不可区分。
        box_js = r"""(sels) => {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    return {x: r.x, y: r.y, w: r.width, h: r.height, sel: sel};
                }
            }
            return null;
        }"""
        last_err: Optional[str] = None
        for attempt in range(1, tries + 1):
            try:
                box = self.page.evaluate(box_js, css_selectors)
                if not box:
                    last_err = f"未找到{intent_desc or '输入框'}"
                    self.human.wait(0.4, 0.8, context="定位输入框重试")
                    continue
                cx = box["x"] + box["w"] * 0.5
                cy = box["y"] + box["h"] * 0.5
                # 拟人化聚焦:坐标点击(不碰句柄,规避脱离),而非 element.click()/focus()
                self.human.click((cx, cy), reason=f"聚焦{intent_desc or intent_key}")
                self.human.wait(0.2, 0.5, context="聚焦后停顿")
                # 拟人化逐字键盘输入(已聚焦,不再重复 click)
                self.human.type_text(None, value, click_first=False)
                logger.info(f"[{intent_key}] 拟人输入成功 selector={box['sel']}({len(value)}字)")
                return True, None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            self.human.wait(0.5, 1.0, context="填入重试")
        return False, last_err

    def step5_fill_content(self, title: str, content: str) -> Dict[str, Any]:
        """步骤5: 填写标题和内容。

        §6.4 坑#4/#5:
        - 正文剥结尾 ``#话题`` 串(单一来源,交 step6 受控插入)
        - 标题按 get_display_length 硬截断 ≤20(新仓无 AI 缩减)
        - 正文安全截断 900
        """
        self.current_step = 5
        logger.info("=" * 60)
        logger.info("步骤5: 填写标题和内容")
        logger.info("=" * 60)

        # 正文:剥结尾话题串(单一来源)
        _before = content
        content = strip_trailing_hashtags(content)
        if content != _before:
            _stripped = _before[len(content):].strip()
            logger.info(f"5.0 已剥离正文末尾话题串(交由 step6 统一插入): {_stripped[:120]}")

        # 正文安全截断
        if len(content) > XHS_MAX_BODY_LENGTH:
            logger.warning(f"正文 {len(content)} 字超过安全上限 {XHS_MAX_BODY_LENGTH} 字,截断(标签另占约 60 字)")
            content = truncate_body(content)

        # 标题硬截断 ≤20
        _title_before = title
        title = truncate_title(title)
        if title != _title_before:
            logger.warning(f"标题显示长度超 {XHS_MAX_TITLE_DISPLAY},硬截断: '{_title_before}' -> '{title}'")

        try:
            title_selectors = [
                "input[placeholder*='标题']",
                "input[placeholder*='填写标题']",
                "input[placeholder*='添加标题']",
                "input.title-input",
                "input[type='text']",
            ]
            logger.info(f"5.1 填写标题: {title} ({len(title)}字符)")
            ok, err = self._type_into_robust(
                title_selectors, title,
                intent_key="title_input", intent_desc="笔记标题输入框",
            )
            if not ok:
                return {
                    "success": False,
                    "error": f"填写标题失败: {err}",
                    "screenshot": self._take_screenshot("09_title_fill_failed"),
                }
            self._take_screenshot("09_title_filled")
            logger.info(f"✓ 标题已填写 ({len(title)}字符)")

            content_selectors = [
                "div[contenteditable='true'][placeholder*='正文']",
                "div[contenteditable='true'][placeholder*='添加']",
                "div[contenteditable='true'][placeholder*='内容']",
                "textarea[placeholder*='正文']",
                "textarea[placeholder*='内容']",
                "div.c-input[contenteditable='true']",
                "div[contenteditable='true']",
            ]
            logger.info(f"5.2 填写内容({len(content)}字符)...")
            ok, err = self._type_into_robust(
                content_selectors, content,
                intent_key="content_input", intent_desc="笔记正文输入框",
            )
            if not ok:
                return {
                    "success": False,
                    "error": f"填写内容失败: {err}",
                    "screenshot": self._take_screenshot("10_content_fill_failed"),
                }
            self._take_screenshot("10_content_filled")
            logger.info("✓ 内容已填写")

            return {
                "success": True,
                "title_length": len(title),
                "content_length": len(content),
            }

        except Exception as e:
            logger.error(f"填写内容失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("10_exception"),
            }

    # ==================== 步骤6: 设置发布选项(话题) ====================

    def step6_set_publish_options(
        self,
        tags: Optional[List[str]] = None,
        location: Optional[str] = None,
    ) -> Dict[str, Any]:
        """步骤6: 添加话题标签(可选)。

        §6.4 坑#4:去重截断 ≤10 + 下拉精确/完整前缀匹配 + 失败回删。
        """
        self.current_step = 6
        logger.info("=" * 60)
        logger.info("步骤6: 设置发布选项")
        logger.info("=" * 60)

        try:
            options_set = []

            if tags and len(tags) > 0:
                # 去重 + 截断 ≤10(纯函数)
                tags = dedupe_topics(tags)
                # 用 JS 把话题作为纯 #标签快速追加到正文末尾(不 click / 不等下拉 /
                # 不走慢速自愈)。坑:旧版逐个 click+type+等下拉+回删要几十秒,而小红书
                # 编辑器只驻留几十秒就自动存草稿+重置回视频 tab —— 慢 step6 会让后续 step7
                # 点发布落到已重置的页面(实测发布超时)。故这里瞬时追加,保住发布窗口。
                # 纯文本 #tag 不是下拉精选话题,但足以发布;精选话题为次要,让位于"能发出去"。
                tag_str = " " + " ".join(
                    (t if str(t).startswith("#") else f"#{str(t).lstrip('#')}")
                    for t in tags
                )
                logger.info(f"6.1 拟人化追加话题标签: {tag_str.strip()}")
                # 合规:走 SyncHumanActions,禁 JS 注入。定位正文框坐标(只读)→ 拟人点击聚焦
                # → Ctrl+End 移到正文末尾 → 逐字键盘输入 #标签(纯文本 tag,足以发布)。
                try:
                    box = self.page.evaluate(r"""(sels) => {
                        for (const sel of sels) {
                            const el = document.querySelector(sel);
                            if (!el) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return {x: r.x, y: r.y, w: r.width, h: r.height};
                            }
                        }
                        return null;
                    }""", [
                        "div[contenteditable='true'][data-placeholder*='正文']",
                        "div[contenteditable='true'][placeholder*='正文']",
                        "textarea[placeholder*='正文']",
                        "div[contenteditable='true']",
                    ])
                    if box:
                        cx = box["x"] + box["w"] * 0.5
                        cy = box["y"] + box["h"] * 0.5
                        self.human.click((cx, cy), reason="聚焦正文框(追加话题)")
                        self.human.wait(0.2, 0.5, context="聚焦后停顿")
                        self.human.press_key("Control+End", reason="光标移到正文末尾")
                        self.human.type_text(None, tag_str, click_first=False)
                        logger.info("✓ 话题标签已拟人化追加(纯文本 #tag)")
                        options_set.append("tags")
                    else:
                        logger.warning("未找到正文框,跳过话题(不阻断发布)")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"拟人化追加话题失败({e}),跳过话题不阻断发布")

            if location:
                logger.info(f"6.2 设置地点: {location}")
                logger.info("地点设置功能待实现")
                options_set.append("location")

            self._take_screenshot("11_options_set")
            return {"success": True, "options_set": options_set}

        except Exception as e:
            logger.error(f"设置发布选项失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("11_exception"),
            }

    # ==================== 步骤7: 点击发布并等待 ====================

    def step7_click_publish_and_wait(self, max_wait: int = 30) -> Dict[str, Any]:
        """步骤7: 点击发布按钮并等待发布完成。

        §6.4 坑#1/#2:
        - 发布按钮在 ``<xhs-publish-btn>`` 自定义元素、可能是 **closed Shadow DOM**
          (playwright/querySelector 不穿透)。策略:JS 诊断 host + shadow 状态,
          open/light 直接坐标点;closed 时对 host 像素带按「小红书红」求 centroid
          (DPR 自适应)定位 + 级联多手段点击 + 每次点后 ``_published()`` 验证。
        - 级联点击已权威确认发布(``publish_confirmed``)→ **立即收口返回**,禁止再进
          30s 等待循环(否则与成功页 3 秒自动跳转赛跑 → 误判 failed → 重试 → 重复发布)。
        """
        self.current_step = 7
        logger.info("=" * 60)
        logger.info("步骤7: 点击发布并等待")
        logger.info("=" * 60)

        try:
            logger.info("7.1 综合探测发布按钮 DOM(light/open-shadow/closed-shadow + 全页候选)...")
            self._take_screenshot("12_before_publish")
            publish_clicked = False
            publish_confirmed = False  # 级联点击已确认页面跳转发布成功(权威信号)
            click_strategy = ""
            try:
                # 先滚到底部让发布栏进视口
                self.page.evaluate(
                    "() => { const e=document.querySelector('xhs-publish-btn'); "
                    "if(e) e.scrollIntoView({block:'center'}); else window.scrollTo(0, document.body.scrollHeight); }"
                )
                time.sleep(0.6)

                diag = self.page.evaluate(r"""() => {
                    const txtEq = (b) => (b.textContent || '').trim() === '发布';
                    const rectOf = (el) => { const r = el.getBoundingClientRect();
                        return {cls: el.className, x: Math.round(r.x), y: Math.round(r.y),
                                w: Math.round(r.width), h: Math.round(r.height),
                                vis: r.width > 0 && r.height > 0}; };
                    const host = document.querySelector('xhs-publish-btn');
                    const res = { hostFound: !!host };
                    res.globalPublishBtns = [...document.querySelectorAll('button')]
                        .filter(txtEq).map(rectOf);
                    res.globalRedBtns = [...document.querySelectorAll('button.ce-btn.bg-red, button.d-button.bg-red')]
                        .map(rectOf);
                    if (!host) return res;
                    const hr = host.getBoundingClientRect();
                    res.host = {x: Math.round(hr.x), y: Math.round(hr.y),
                                w: Math.round(hr.width), h: Math.round(hr.height)};
                    res.shadowOpen = !!host.shadowRoot;
                    res.lightChildCount = host.childElementCount;
                    res.hostInnerHTML = (host.innerHTML || '').slice(0, 500);
                    const lb = host.querySelector('button.ce-btn.bg-red') ||
                        [...host.querySelectorAll('button')].find(txtEq);
                    if (lb) res.lightBtn = rectOf(lb);
                    if (host.shadowRoot) {
                        const sr = host.shadowRoot;
                        const sb = sr.querySelector('button.ce-btn.bg-red') ||
                            [...sr.querySelectorAll('button')].find(txtEq);
                        if (sb) res.shadowBtn = rectOf(sb);
                        res.shadowBtnCount = sr.querySelectorAll('button').length;
                        res.shadowInnerHTML = (sr.innerHTML || '').slice(0, 500);
                    }
                    return res;
                }""")
                logger.info(f"[发布按钮综合诊断] {json.dumps(diag, ensure_ascii=False)[:1800]}")

                # 按优先级挑一个真实坐标做鼠标点击(对 light/open-shadow 都有效)
                target = None
                if diag.get('lightBtn') and diag['lightBtn'].get('vis'):
                    target = diag['lightBtn']; click_strategy = "light DOM 按钮"
                elif diag.get('shadowBtn') and diag['shadowBtn'].get('vis'):
                    target = diag['shadowBtn']; click_strategy = "open shadow 按钮"
                else:
                    for cand in (diag.get('globalRedBtns') or []) + (diag.get('globalPublishBtns') or []):
                        if cand.get('vis'):
                            target = cand; click_strategy = f"全页候选({cand.get('cls','')[:30]})"
                            break

                if target:
                    cx = target['x'] + target['w'] / 2
                    cy = target['y'] + target['h'] / 2
                    self.human.wait(0.3, 0.8, context="确认发布内容")
                    # 拟人化点击(贝塞尔移动+悬停+真实按压),禁裸 mouse.click
                    self.human.click((cx, cy), reason=f"发布按钮({click_strategy})")
                    logger.info(f"✓ [{click_strategy}] 拟人点击 ({cx:.0f},{cy:.0f})")
                    publish_clicked = True
                elif diag.get('hostFound') and diag.get('host', {}).get('w', 0) > 0:
                    # closed shadow:playwright/JS 都拿不到内部按钮坐标。
                    # 实时截图按「小红书红」颜色在 host 像素带内定位发布按钮中心(DPR 自适应)。
                    h = diag['host']
                    from io import BytesIO
                    try:
                        from PIL import Image as _PILImg
                    except Exception:
                        _PILImg = None

                    def _vp():
                        try:
                            return self.page.evaluate(
                                "() => ({iw: innerWidth, ih: innerHeight, "
                                "dpr: window.devicePixelRatio || 1})")
                        except Exception:
                            return {"iw": 1920, "ih": 987, "dpr": 1}

                    def _red_centroid_css():
                        if _PILImg is None:
                            return None
                        try:
                            im = _PILImg.open(BytesIO(self.page.screenshot())).convert("RGB")
                            sw, sh = im.size
                            vp = _vp()
                            scale = sw / max(1, vp["iw"])  # 物理px / CSSpx
                            px = im.load()
                            x0 = max(0, int(h['x'] * scale)); x1 = min(sw, int((h['x'] + h['w']) * scale))
                            y0 = max(0, int(h['y'] * scale)); y1 = min(sh, int((h['y'] + h['h']) * scale))
                            xs = []; ys = []
                            for yy in range(y0, y1):
                                for xx in range(x0, x1):
                                    r, g, b = px[xx, yy]
                                    if r > 180 and g < 120 and b < 140 and (r - g) > 90 and (r - b) > 60:
                                        xs.append(xx); ys.append(yy)
                            logger.info(f"[红按钮检测] vp={vp} screenshot=({sw}x{sh}) "
                                        f"scale={scale:.3f} 红像素n={len(xs)}")
                            if len(xs) < 50:
                                return None
                            ccx = (sum(xs) / len(xs)) / scale
                            ccy = (sum(ys) / len(ys)) / scale
                            logger.info(f"[红按钮检测] 物理centroid=({sum(xs)//len(xs)},"
                                        f"{sum(ys)//len(ys)}) → CSS=({ccx:.0f},{ccy:.0f})")
                            return (ccx, ccy)
                        except Exception as ce:
                            logger.info(f"[红按钮检测失败] {ce}")
                            return None

                    def _published():
                        try:
                            if "/publish/publish" not in self.page.url:
                                return True
                            if not self.page.query_selector("xhs-publish-btn"):
                                return True
                            bt = self.page.inner_text("body")[:400]
                            return any(k in bt for k in ("发布成功", "已发布", "发布完成"))
                        except Exception:
                            return False

                    rc = _red_centroid_css()
                    fx = h['x'] + h['w'] * 0.59
                    fy = h['y'] + h['h'] * 0.55
                    tx, ty = rc if rc else (fx, fy)
                    locate = "颜色定位" if rc else "0.59回退"
                    logger.info(f"[closed shadow] 发布按钮目标=({tx:.0f},{ty:.0f}) [{locate}]")

                    # 合规:闭合 shadow 发布按钮也全部走拟人化点击(贝塞尔移动+悬停+真实按压),
                    # 禁裸 mouse.click / JS dispatchEvent(合成事件是 AI 检测信号)。实测拟人点击
                    # 能被按钮识别(点后弹出 XHS 回执 toast),多次拟人点击不同落点作兜底。
                    attempts = [
                        ("拟人点击", lambda: self.human.click(
                            (tx, ty), reason="发布(closed shadow)")),
                        ("拟人点击-重试", lambda: self.human.click(
                            (tx, ty), reason="发布(closed shadow 重试)")),
                        ("拟人点击-0.59", lambda: self.human.click(
                            (fx, fy), reason="发布(closed shadow 0.59)")),
                    ]
                    for name, act in attempts:
                        try:
                            self.human.wait(0.3, 0.7, context="确认发布内容")
                            act()
                            logger.info(f"✓ [closed shadow] 尝试[{name}] @({tx:.0f},{ty:.0f})")
                            time.sleep(2.0)
                            if _published():
                                logger.info(f"✓ [{name}] 发布生效(页面已变化)")
                                publish_clicked = True
                                publish_confirmed = True
                                click_strategy = f"closed shadow:{name}"
                                break
                            logger.info(f"… [{name}] 后页面未变,尝试下一手段")
                        except Exception as ae:
                            logger.info(f"[{name}] 执行异常: {ae}")
                    if not publish_clicked:
                        # 全手段后未确认生效:仍进入等待逻辑兜底(可能延迟跳转)
                        click_strategy = "closed shadow:多手段(未确认)"
                        publish_clicked = True
            except Exception as e:
                logger.error(f"发布按钮探测/点击失败: {e}")

            # 自愈兜底:上面所有硬策略(light/open-shadow/closed-shadow 像素/全页候选)都未点成,
            # 返回失败前用 LLM 快照定位发布按钮点一次。命中经 SelfHealLocator 内部发布按钮安全校验
            # (须含「发布/publish」文案 + button/a/role)。closed-shadow 情形快照看不见按钮 →
            # locate 自然返回 None,维持上面像素兜底不动。默认关时整条不触发,行为逐字节等价。
            if not publish_clicked and settings.SELFHEAL_ENABLED and settings.LLM_API_KEY:
                try:
                    found = self._locator.locate(
                        self.page, "publish_button", "发布笔记的发布按钮"
                    )
                except Exception as exc:
                    logger.warning(f"[self_heal] 发布按钮定位兜底异常:{exc}")
                    found = None
                if found:
                    # 发布按钮定位走 shadow-DOM 诊断 JS,不经 _find_element_with_retry,
                    # registry.get("publish_button") 全仓无消费点 —— 故这里只用 handle 点击,
                    # 不 learn(学了没人读,且点击生效前 learn 会污染 registry)。
                    handle, _ = found
                    try:
                        self.human.click(handle, reason="自愈发布按钮")
                        time.sleep(2.0)
                        # 复用 closed-shadow 同款发布生效判定:离开发布页 / host 消失 / 成功文案
                        confirmed = False
                        try:
                            if ("/publish/publish" not in self.page.url
                                    or not self.page.query_selector("xhs-publish-btn")):
                                confirmed = True
                            else:
                                bt = self.page.inner_text("body")[:400]
                                confirmed = any(
                                    k in bt for k in ("发布成功", "已发布", "发布完成"))
                        except Exception:
                            confirmed = False
                        # 点击成功即进等待兜底(可能延迟跳转);确认生效才置 confirmed 走立即收口。
                        publish_clicked = True
                        if confirmed:
                            publish_confirmed = True
                            click_strategy = "自愈发布按钮"
                            logger.info("✓ [自愈] 发布按钮点击生效")
                        else:
                            click_strategy = "自愈发布按钮(未确认)"
                            logger.info("… [自愈] 发布按钮点击后页面未变,转入等待兜底")
                    except Exception as exc:
                        logger.warning(f"[self_heal] 发布按钮点击异常:{exc}")

            if not publish_clicked:
                return {
                    "success": False,
                    "error": "未找到发布按钮(shadow 探测失败)",
                    "screenshot": self._take_screenshot("12_no_publish_button"),
                }

            logger.info("✓ 发布按钮已点击")

            # 点击后取证(关键状态写进持久 log)
            try:
                time.sleep(2.0)
                forensic = self.page.evaluate(r"""() => {
                    const out = { url: location.href };
                    out.hostStillPresent = !!document.querySelector('xhs-publish-btn');
                    const dlg = document.querySelector(
                        '[role=dialog],.d-modal,.modal,.el-dialog,.el-message-box,'
                        + '[class*=dialog],[class*=Modal],[class*=mask]');
                    out.dialogText = dlg ? (dlg.innerText||'').trim().slice(0,300) : null;
                    out.toasts = [...document.querySelectorAll(
                        '[class*=toast],[class*=message],[class*=Toast],[class*=tip]')]
                        .map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,6);
                    out.bodyHead = (document.body.innerText||'').trim().slice(0,160);
                    return out;
                }""")
                logger.info(f"[点击后取证] {json.dumps(forensic, ensure_ascii=False)[:1200]}")
            except Exception as fe:
                logger.error(f"点击后取证失败: {fe}")
                forensic = {}

            # 账号级禁发检测:点发布后小红书用 toast/弹窗告知"因违反社区规范禁止发笔记"
            # 等账号处罚态。此时发布按钮点击其实生效了(toast 是 XHS 的拒绝回执),但笔记
            # 永远发不出去,继续等 30 秒只会误报"发布超时"。命中即以明确原因立即收口,避免
            # 误判 + 无谓重试(重试也发不出)。
            try:
                _probe = " ".join(
                    (forensic.get("toasts") or [])
                    + [forensic.get("dialogText") or "", forensic.get("bodyHead") or ""]
                )
                ban_markers = [
                    "因违反社区规范禁止发笔记", "违反社区规范", "禁止发笔记",
                    "账号存在异常", "账号异常无法发布", "涉嫌违规", "限制发布",
                ]
                hit = next((m for m in ban_markers if m in _probe), None)
                if hit:
                    logger.error(f"❌ 账号被限制发布:命中「{hit}」→ 该账号当前无法发笔记")
                    return {
                        "success": False,
                        "error": f"账号被小红书限制发布(命中「{hit}」):该账号处于违规/处罚态,"
                                 f"无法发布笔记。请更换未受限账号,或在小红书 App 内核实账号状态。",
                        "account_restricted": True,
                        "screenshot": self._take_screenshot("13_account_restricted"),
                    }
            except Exception as be:
                logger.warning(f"禁发检测异常(忽略): {be}")

            # §6.4 坑#1:级联点击已权威确认发布成功 → 立即收口,禁止再进 30 秒等待循环
            # (小红书成功页仅停留约 3 秒就自动跳回发布页,继续等会与跳转赛跑 → 误判 failed
            #  → 触发重试 → 重复发布。实测 RCA 2026-05-18,task 61469cfd)。
            if publish_confirmed:
                cur_url = self.page.url
                note_id = (self._extract_note_id_from_url(cur_url)
                           or self._fetch_latest_note_id_from_creator() or "")
                logger.info(f"✓ 发布成功(级联确认 [{click_strategy}])note_id={note_id}")
                self._take_screenshot("16_publish_success")
                return {
                    "success": True,
                    "note_url": cur_url,
                    "note_id": note_id,
                }

            # 点击后快速连续截图抓 toast(toast 只显示 2-3 秒)
            for t in range(4):
                time.sleep(0.8)
                self._take_screenshot(f"13_after_click_{t}s")

            # 检查是否有 toast/弹窗错误或成功提示
            try:
                page_text = self.page.inner_text("body")
                logger.info(f"[发布后页面文字片段] {page_text[:200]}")
                error_keywords = ["请上传图片", "请填写标题", "内容不能为空", "图片处理中", "请稍后", "发布失败", "网络错误", "请重试", "正文最多支持"]
                for kw in error_keywords:
                    if kw in page_text:
                        logger.error(f"⚠️ 检测到页面提示: {kw}")
                success_keywords = ["发布成功", "已发布", "审核中"]
                for kw in success_keywords:
                    if kw in page_text:
                        logger.info(f"✓ 检测到成功提示: {kw}")
            except Exception:
                pass

            # 7.3 等待发布完成
            logger.info("7.3 等待发布完成...")
            waited = 0
            while waited < max_wait:
                self.human.wait(1.5, 2.5, context="等待发布")
                waited += 2
                current_url = self.page.url

                # 页面文字命中成功(小红书可能不跳转而是显示 toast)
                try:
                    body_text = self.page.inner_text("body")
                    for kw in ["发布成功", "已发布", "审核中"]:
                        if kw in body_text:
                            logger.info(f"✓ 检测到页面文字: {kw}")
                            self._take_screenshot("16_publish_success_text")
                            note_id = self._fetch_latest_note_id_from_creator() or ""
                            return {
                                "success": True,
                                "note_url": current_url,
                                "note_id": note_id,
                                "screenshot": self._take_screenshot("16_publish_success"),
                            }
                except Exception:
                    pass

                # URL 跳转到成功页/内容管理
                success_indicators = [
                    "creator/home",
                    "creator/content",
                    "/explore/",
                    "/notePublish/success",
                ]
                for indicator in success_indicators:
                    if indicator in current_url:
                        logger.info(f"✓ 发布成功!URL变化: {current_url}")
                        self._take_screenshot("16_publish_success")
                        note_id = self._extract_note_id_from_url(current_url)
                        return {
                            "success": True,
                            "note_url": current_url,
                            "note_id": note_id,
                            "wait_time": waited,
                        }

                if self._check_success_message():
                    logger.info("✓ 发布成功!检测到成功提示")
                    self._take_screenshot("16_publish_success")
                    note_id = self._fetch_latest_note_id_from_creator() or ""
                    return {
                        "success": True,
                        "note_url": self.page.url,
                        "note_id": note_id,
                        "wait_time": waited,
                    }

                # 错误弹窗(精确选择器,不取 body 全文;「遇到问题」是固有反馈入口非错误)
                try:
                    error_selectors = [
                        ".error-message",
                        ".toast-error",
                        "[class*='error-tip']",
                        "[class*='fail-tip']",
                        ".el-message--error",
                        ".notification-error",
                    ]
                    for err_sel in error_selectors:
                        try:
                            err_elem = self.page.query_selector(err_sel)
                            if err_elem and err_elem.is_visible():
                                error_text = err_elem.inner_text()
                                logger.error(f"❌ 检测到错误弹窗: {error_text}")
                                self._take_screenshot("13_error_detected")
                                return {
                                    "success": False,
                                    "error": f"发布失败:{error_text[:500]}",
                                    "screenshot": self._take_screenshot("13_publish_error"),
                                }
                        except Exception:
                            continue

                    page_text = self.page.inner_text("body")
                    if "发布失败" in page_text or "内容违规" in page_text or "审核不通过" in page_text:
                        logger.error("❌ 检测到发布失败文本!")
                        self._take_screenshot("13_error_detected")
                        return {
                            "success": False,
                            "error": f"发布失败:{page_text[:500]}",
                            "screenshot": self._take_screenshot("13_publish_error"),
                        }
                except Exception as e:
                    logger.warning(f"检查错误提示失败: {e}")

                if waited % 6 == 0:
                    logger.info(f"仍在等待发布完成... ({waited}/{max_wait}秒)")
                    self._take_screenshot(f"16_waiting_{waited}s")

            return {
                "success": False,
                "error": f"发布超时({max_wait}秒),未检测到成功标志",
                "current_url": self.page.url,
                "screenshot": self._take_screenshot("16_timeout"),
            }

        except Exception as e:
            logger.error(f"发布失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("16_exception"),
            }

    def _check_success_message(self) -> bool:
        """检查页面上是否有成功提示。"""
        for text in ("发布成功", "笔记已发布", "发布完成"):
            try:
                if self.page.query_selector_all(f"text={text}"):
                    return True
            except Exception:
                continue
        return False

    def _extract_note_id_from_url(self, url: str) -> Optional[str]:
        """从 URL 中提取笔记 ID(explore / discovery/item;成功页则回创作中心取)。"""
        match = re.search(r'/explore/([a-f0-9]+)', url)
        if match:
            return match.group(1)
        match = re.search(r'/discovery/item/([a-f0-9]+)', url)
        if match:
            return match.group(1)
        if 'publish/success' in url or 'notePublish/success' in url:
            return self._fetch_latest_note_id_from_creator()
        return None

    def _fetch_latest_note_id_from_creator(self) -> Optional[str]:
        """从创作中心笔记管理页提取最新发布的 24 位 hex note_id(可能取不到 → None)。

        §6.4 坑说明:返回契约允许 success=True 但 note_id=""(只有 note_url)。
        """
        try:
            logger.info("[发布] 从创作中心获取最新笔记 ID...")
            note_mgmt_urls = [
                "https://creator.xiaohongshu.com/publish/publish?source=official",
                "https://creator.xiaohongshu.com/creator/home",
            ]
            for url in note_mgmt_urls:
                try:
                    self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(3)
                    html = self.page.content()
                    patterns = [
                        r'"noteId"\s*:\s*"([a-f0-9]{24})"',
                        r'"note_id"\s*:\s*"([a-f0-9]{24})"',
                        r'/explore/([a-f0-9]{24})',
                        r'/discovery/item/([a-f0-9]{24})',
                        r'"id"\s*:\s*"([a-f0-9]{24})"',
                    ]
                    for pattern in patterns:
                        ids = re.findall(pattern, html)
                        if ids:
                            note_id = ids[0]
                            logger.info(f"[发布] 从 {url} 提取到 note_id: {note_id}")
                            return note_id

                    links = self.page.query_selector_all("a[href*='/explore/'], a[href*='/discovery/item/']")
                    for link in links[:3]:
                        href = link.get_attribute("href") or ""
                        match = re.search(r'(?:/explore/|/discovery/item/)([a-f0-9]{24})', href)
                        if match:
                            note_id = match.group(1)
                            logger.info(f"[发布] 从链接提取到 note_id: {note_id}")
                            return note_id
                except Exception as e:
                    logger.debug(f"[发布] {url} 获取失败: {e}")
                    continue

            # 兜底:点击「笔记管理」侧边栏(拟人化点击,禁裸 element.click)
            try:
                note_mgmt_btn = self.page.query_selector("text=笔记管理")
                if note_mgmt_btn:
                    self.human.click(note_mgmt_btn, reason="笔记管理侧边栏")
                    time.sleep(3)
                    html = self.page.content()
                    ids = re.findall(r'"noteId"\s*:\s*"([a-f0-9]{24})"', html)
                    if ids:
                        note_id = ids[0]
                        logger.info(f"[发布] 从笔记管理页提取到 note_id: {note_id}")
                        return note_id
            except Exception:
                pass

            logger.warning("[发布] 无法从创作中心提取 note_id")
            self._take_screenshot("17_creator_no_note_id")
            return None

        except Exception as e:
            logger.warning(f"[发布] 从创作中心获取 note_id 失败: {e}")
            return None
