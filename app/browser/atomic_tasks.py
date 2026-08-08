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
from app.browser.topic_dropdown import COLLECT_LAYERS_JS, select_topic_option
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


# 话题失败取证:回读正文框末尾多少字(够看清刚打进去的 #tag 就行)
TOPIC_EDITOR_TAIL_CHARS = 40


def read_editor_tail(content_el, limit: int = TOPIC_EDITOR_TAIL_CHARS) -> str:
    """回读正文框当前内容的**末尾**几个字;读不到返回空串(取证绝不制造新异常)。

    话题失败时它是关键分水岭:末尾是 ``#心理科普`` 说明字真打进去了(那就是词本身
    不存在或下拉没刷新),末尾还是正文说明**输入压根没落到编辑器里**。
    """
    try:
        text = " ".join((content_el.inner_text() or "").split())
    except Exception:  # noqa: BLE001
        return ""
    return text[-limit:]


def topic_failure_detail(tag_name: str, option_pos: Optional[Dict[str, Any]],
                         editor_tail: str) -> Dict[str, Any]:
    """把话题失败的**当场证据**打包成 topics_failed 的一条。

    2026-08-07 视频 e2e 6/6 全 ``no_exact_match``,而同期图文 171/181 成功 —— 回执里只有
    一个 reason 字符串,判不出到底是"浮层里是默认推荐话题(说明搜索没触发)"还是"这些词
    平台真的没有"。所以把浮层实际枚举到的候选文案、候选条数、浮层容器 class 和正文框
    回读一并交出去,下一次真跑一眼即可定性 —— 这四个字段当场就把真因指了出来
    (抓到的是右侧预览面板的 ``base-info``),故一个不删,再补两个:浮层总层数、被判据
    拒掉的层 class(定位失败时要看的正是"我们把什么当成了下拉、又拒了什么")。
    """
    detail: Dict[str, Any] = {
        "tag": tag_name,
        "reason": (option_pos or {}).get("reason", "error") if option_pos else "error",
        "editor_tail": editor_tail,
    }
    if option_pos:
        detail["candidates"] = list(option_pos.get("candidates") or [])[:10]
        detail["item_count"] = option_pos.get("item_count", 0)
        detail["layer_class"] = str(option_pos.get("layer_class") or "")[:80]
        detail["layers_seen"] = option_pos.get("layers_seen", 0)
        detail["rejected_classes"] = list(option_pos.get("rejected_classes") or [])[:5]
    return detail


# ── 视频笔记:上传/转码判据的文本标志 ──
# 进度类(在 = 这一刻绝对没传完)。**只放真号夹具里逐字见过的两个**:cover 区上传中显示
# 「视频文件.mp4 上传中 N% 当前速度 …」。故意不猜「转码中/处理中」之类没见过的文案 ——
# 猜错方向是致命的:页面上若有个无关的常驻「处理中」,每一条视频都会被判成永远没传完、
# 干等到超时。而漏掉一个真实进度文案代价很小:它落到 unknown,unknown 本来就不放行。
VIDEO_PROGRESS_MARKERS = ("上传中", "当前速度")
# 完成类:夹具实测 cover-container 从「视频文件.mp4 上传中 N% 当前速度」变成
# 「真实文件名 + 检测为高清视频…」,同时页面出现「重新上传」入口。
VIDEO_DONE_MARKERS = ("检测为高清视频", "重新上传")


def classify_video_upload_state(
    cover_text: str,
    page_text: str,
    original_switch_enabled: Optional[bool] = None,
) -> str:
    """判定视频上传/转码处于哪一态:``uploading`` / ``ready`` / ``unknown``(纯函数)。

    **绝不能用图文那套 ``_check_edit_page_loaded``(标题框存在即就绪)**:真号采集实证,
    视频页的标题 input 在上传进度还是 0% 时就已经挂进 DOM(存在但不可交互),照搬即
    100% 假阳性 —— step5 会往一个还没转码完的编辑器里打字,失败还查不出原因。

    判据优先级(顺序即语义):
    1. 进度文案在 → ``uploading``。这是**最强否定信号**,压过一切完成文案:页面正在
       切换时两种文案会短暂同时存在,宁可多等一轮也不能在半态上放行。
    2. 完成文案在 → ``ready``。
    3. 「原创声明」开关已从禁用变为可点 **且 cover 区已有内容** → ``ready``。辅助判据:
       夹具实测上传未完成时该开关是 ``pointer-events:none``,平台改文案的概率远高于改这个
       交互约束,留它兜底避免文案一变整条链路就干等到超时。**必须叠加 cover 区非空**:
       光凭开关可点没法区分"传完了"和"压根还没开始传"(空白发布页上开关若恰好可点,
       就会在视频还没进去时判就绪),cover 区有内容 = 页面上确实挂着一个视频。
    4. 其余 → ``unknown``。**读不到 ≠ 好了**,不放行。
    """
    blob = f"{cover_text or ''}\n{page_text or ''}"
    if any(m in blob for m in VIDEO_PROGRESS_MARKERS):
        return "uploading"
    if any(m in blob for m in VIDEO_DONE_MARKERS):
        return "ready"
    if original_switch_enabled and (cover_text or "").strip():
        return "ready"
    return "unknown"


def go_publish_enabled(button_state: dict) -> bool:
    """播客「去发布」按钮到底能不能点(纯函数,吃 ``_audio_probe`` 读出来的一颗按钮)。

    ⚠️ 该按钮的禁用态形态**未取证**(实拍只确认了"未传音频时是禁用的"这件事)。
    故三路判据取**与**:``disabled`` 属性、``aria-disabled``、class 里的 disabled 类,
    **全都表明不禁用**才算可点。

    为什么取"与"而不是"或":读不懂的形态一律当禁用。点一颗禁用按钮永远不会成功,
    只会换来一句"发布超时"(图文那边 2026-08-02 真号事故就是这么来的);而多等一轮
    的代价只是超时报错,那个报错还带着当场取证,反而能推进排查。
    """
    if not button_state:
        return False
    if button_state.get("disabled"):
        return False
    if str(button_state.get("aria") or "").lower() == "true":
        return False
    cls = str(button_state.get("cls") or "").lower()
    if "disabled" in cls:
        return False
    return True


# 内部别名:step3a 里按逐颗按钮判定用(纯函数已导出,单测直接打 go_publish_enabled)
_go_publish_enabled = go_publish_enabled


def _find_button_by_text(page, texts) -> Optional[ElementHandle]:
    """按文案精确匹配找一颗 ``<button>``;找不到返回 None。

    限定 ``<button>`` 标签是有原因的:播客合集创建页上,「创建」按钮的外层包裹 div
    的 innerText 也是「创建」,按纯文本找会抓到那个点不动的容器(真号取证实录)。
    """
    wanted = {(t or "").replace("　", " ").strip() for t in texts}
    try:
        for el in page.query_selector_all("button"):
            try:
                if (el.inner_text() or "").replace("　", " ").strip() in wanted:
                    return el
            except Exception:  # noqa: BLE001 — 单个元素读失败只跳过它
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _prune_old_screenshots(root: str, keep_days: int) -> int:
    """删掉超期的调试截图,返回删了几个;**绝不抛异常**。

    为什么需要:截图目录只增不减 —— 2026-08-02 实测已经 1633 个文件 / 469MB。
    这不是"以后再说"的问题:磁盘满了会把发布、补量、同步一起拖垮。

    **故意不新起一个调度器**:发布本来就低频,每次发布前顺手扫一次目录足够了,
    比多养一个后台任务简单得多(且截图只在发布路径产生,清理跟着它走天然对齐)。

    ``keep_days <= 0`` 视为不清理(留给需要长期取证的排查期)。
    清理失败只告警:它是卫生工作,绝不能让一次删不掉的文件把发布搞崩。
    """
    if keep_days <= 0:
        return 0
    try:
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for path in Path(root).glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue  # 单个文件删不掉就跳过,不影响其余
        if removed:
            logger.info(f"[atomic_tasks] 清理超期调试截图 {removed} 个(保留 {keep_days} 天)")
        return removed
    except Exception as exc:  # noqa: BLE001 — 卫生工作绝不阻断发布
        logger.warning(f"[atomic_tasks] 清理调试截图异常(忽略): {exc}")
        return 0


class XHSPublishAtomicTasks:
    """小红书发布笔记的原子任务集合(sync Playwright)。

    所有与页面的交互通过 ``SyncHumanActions`` 收口,注入贝塞尔鼠标轨迹、逐字打字、
    随机延迟等拟人化行为,避免被小红书反自动化检测。
    """

    def __init__(self, page: Page, enable_debug: Optional[bool] = None,
                 screenshot_dir: Optional[str] = None, job_tag: Optional[str] = None):
        """初始化原子任务执行器。

        Args:
            page: Playwright Page 对象
            enable_debug: 是否截图。None 跟随全局总开关 DEBUG_SCREENSHOTS_ENABLED;
                即使显式 True 也被全局开关 AND 压制(防绕过撑满磁盘)。
            screenshot_dir: 截图目录;None → DATA_DIR/debug_screenshots
            job_tag: 发布任务 id。给了就写进截图文件名,让失败现场能按 job 取回
                (见 ``_take_screenshot``);不给则文件名与本参数上线前逐字节一致。
        """
        from app.core.config import settings
        self.page = page
        global_on = settings.DEBUG_SCREENSHOTS_ENABLED
        self.enable_debug = global_on if enable_debug is None else (enable_debug and global_on)
        self.screenshot_dir = screenshot_dir or str(Path(settings.DATA_DIR) / "debug_screenshots")
        self.current_step = 0
        # 只收 [0-9A-Za-z_-]:文件名要用它,放行别的字符等于把路径拼接权交给调用方
        self.job_tag = re.sub(r"[^0-9A-Za-z_-]", "", str(job_tag)) if job_tag else ""
        if self.enable_debug:
            _prune_old_screenshots(
                self.screenshot_dir, settings.DEBUG_SCREENSHOT_RETENTION_DAYS
            )

        # 拟人化操作层(所有页面交互必须经过此层)
        from app.browser.sync_human_actions import SyncHumanActions
        self.human = SyncHumanActions(page, profile="casual")

        # 选择器自愈:learned 前置缓存 + LLM 兜底定位(默认关,SELFHEAL_ENABLED 才生效)。
        # 用进程级单例复用同一 registry + 同一把锁,消除并发发布跨实例写同一 JSON 的竞争。
        self._registry = get_default_registry()
        self._locator = SelfHealLocator()

    def _take_screenshot(self, name: str) -> str:
        """保存截图(仅在调试模式下),返回路径或空串。

        文件名带 ``job{id}_`` 前缀(有 job_tag 时):截图一直在存,但此前**没有任何东西
        把它和某次发布关联起来**,于是运营拿不到失败现场,只能对着一句「发布超时(30秒)」
        换变量试错。带上前缀后按前缀 glob 就能取回该次发布的全部现场,
        **不需要为此加数据库列**。
        """
        if not self.enable_debug:
            return ""
        os.makedirs(self.screenshot_dir, exist_ok=True)
        timestamp = int(time.time())
        prefix = f"job{self.job_tag}_" if self.job_tag else ""
        screenshot_path = (
            f"{self.screenshot_dir}/{prefix}publish_"
            f"{self.current_step:02d}_{name}_{timestamp}.png"
        )
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

    def _fast_open_publish_page(self) -> bool:
        """Fast-path:直连 creator 发布页,上传区就绪即成功;否则 False 交慢路径兜底。

        提速依据(2026-07-25):start() 已在 explore 验证登录,且 cookie 双域注入
        (normalize_cookies_for_injection 含 .xiaohongshu.com + creator 子域)已让
        creator 子域带登录态,故绝大多数情况可直接 goto 发布页秒进——**跳过慢路径的
        explore 重导航 + 8×2s 弹窗串行探测 + 开新窗口 + SSO 认证轮询(合计 ~25s)**。
        判据用 ``input[type='file']``(发布页必有、登录页必无,比宽泛 upload 类可靠):
        就绪即登录态生效;落到 /login 或 8s 内上传区未现 → 回退慢路径(cookie 未覆盖
        creator / 需 SSO / 风控),最坏退化到原状,不会更糟。
        """
        try:
            self.page.goto(
                "https://creator.xiaohongshu.com/publish/publish?source=official",
                wait_until="domcontentloaded", timeout=30000,
            )
        except Exception as e:
            logger.info(f"[fast-path] goto 发布页异常,回退慢路径: {e}")
            return False
        # 换页后复位虚拟光标(与慢路径切窗口后一致),避免沿用 explore 页坐标画错贝塞尔轨迹
        self.human.last_mouse_pos = None
        # 条件等待「发布页真可交互」再返回:判据 = file input 存在 **且「上传图文」tab 已渲染**。
        # 只看 input 存在会进得太快(页面 tab 栏未 hydrate)→ 把等待转嫁给 step2 的 tab 查找循环
        # 空转(实测 fast-path 后 step2 达 19.8s vs 慢路径 2s)。等 tab 就绪再走,step2 第一轮即命中。
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            url = (self.page.url or "").lower()
            if "login" in url:
                logger.info("[fast-path] 落到登录页,回退慢路径")
                return False
            try:
                ready = self.page.evaluate(r"""() => {
                    if (!document.querySelector("input[type='file']")) return false;
                    return Array.from(document.querySelectorAll('span,div,a,li')).some(el => {
                        if (el.textContent.trim() !== '上传图文') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                }""")
                if ready:
                    self.human.wait(0.3, 0.7, context="发布页就绪")  # 真人看一眼页面的自然短停顿
                    logger.info("✓ [fast-path] 直达发布页,上传区+图文tab 就绪(跳过慢链路)")
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        logger.info("[fast-path] 12s 内发布页未就绪,回退慢路径")
        return False

    def step1_open_publish_page(self) -> Dict[str, Any]:
        """步骤1: 打开发布页面。

        策略:**先走 fast-path 直连 creator 发布页**(双域 cookie 带登录态,~3-5s 秒进);
        失败才回退慢路径——访问主站探索页,点击右上角「发布笔记」link 进入创作中心(会打开
        新窗口并触发 SSO 自动认证);SSO 失败时回主站再走一次 SSO 入口,仍失败则
        返回 ``need_manual_login=True``(独立信号,见 §6.4 坑#6)。
        """
        self.current_step = 1
        logger.info("=" * 60)
        logger.info("步骤1: 打开发布页面")
        logger.info("=" * 60)

        try:
            # 1.0 Fast-path:直连发布页(双域 cookie 已带登录态),成功即跳过整条慢链路
            if self._fast_open_publish_page():
                self._take_screenshot("02_fast_publish_page")
                return {"success": True, "url": self.page.url}
            logger.info("fast-path 未命中,回退传统慢路径(explore→点发布→SSO)...")

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
                # 换窗后复位虚拟光标:置 None 让下一次移动从新页的合理起点起步,
                # 避免沿用旧窗坐标做贝塞尔起点画错轨迹
                self.human.last_mouse_pos = None
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
                    # 条件等待 file input 就绪(命中即走,最多 2.5s),缩短原「固定 wait(1,2)+5s 探测」;
                    # 图文模式真校验交下游 _ensure_image_mode 兜底,这里不必久等。
                    try:
                        self.page.wait_for_selector("input[type='file']", timeout=2500, state="attached")
                    except Exception:
                        pass
                    self.human.wait(0.4, 0.8, context="tab 切换后短停顿")
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
            # 2.4 上传文件:直接 set_input_files 到隐藏 <input type=file>,**不点上传按钮**。
            # 坑(headed 实测录屏确认):真桌面上点「上传图片」按钮会弹**原生 GTK 文件框**
            # (Playwright expect_file_chooser 拦不住)、模态卡死整个流程。故不点按钮,直接把文件
            # 灌进隐藏 input —— 无原生框、一次处理多图;set_input_files 触发 input change 事件,
            # XHS 上传处理照常接管。上传后**验证缩略图真渲染出来**,避免"假成功"。
            logger.info(f"2.4 set_input_files 直传 {len(image_paths)} 张(不点按钮,避开原生文件框)...")
            upload_input = self._find_element_with_retry(
                ["input[type='file'][accept*='image']", "input[type='file']"],
                timeout=10, must_be_visible=False,
                intent_key="upload_image_input", intent_desc="隐藏文件 input",
            )
            if not upload_input:
                return {
                    "success": False,
                    "error": "未找到文件上传 input 元素",
                    "screenshot": self._take_screenshot("03_02_no_file_input"),
                }
            upload_input.set_input_files(image_paths)
            logger.info(f"✓ set_input_files 已灌入 {len(image_paths)} 张,等上传渲染...")

            # 等图片真正上传渲染(blob/xhscdn/ros 缩略图出现),而非盲目往下走(修"假成功")。
            uploaded_ok = False
            for _ in range(15):
                self.human.wait(0.8, 1.4, context="等图片上传渲染")
                try:
                    n = self.page.evaluate(
                        "() => document.querySelectorAll("
                        "\"img[src^='blob:'],img[src*='xhscdn'],img[src*='ros']\").length"
                    )
                except Exception:
                    n = 0
                if n and n >= 1:
                    uploaded_ok = True
                    logger.info(f"✓ 图片已上传渲染(检测到 {n} 张缩略图)")
                    break
            if not uploaded_ok:
                return {
                    "success": False,
                    "error": "set_input_files 后图片未上传渲染(编辑器可能拒绝/重置回视频tab)",
                    "screenshot": self._take_screenshot("04_upload_not_rendered"),
                }
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

            # 编辑器没等到:走草稿箱恢复(图已上传成 XHS 草稿,把它继续编辑即可拿回编辑器)
            if self._recover_editor_from_draft():
                return {"success": True, "edit_page_loaded": True, "recovered_from_draft": True}

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

    def _recover_editor_from_draft(self) -> bool:
        """草稿箱恢复:进草稿箱点最新图文草稿的「编辑」,拿回带图的编辑器。成功返 True。

        RCA 2026-07-25(job14 连败,抓包实证):set_input_files 上传其实**成功**,编辑器也
        确实打开了(实测 +0.3s 标题框在位、缩略图已渲染);但编辑器一打开,创作页会去问
        千帆商家后台 ``ark.xiaohongshu.com/api/edith/.../trade_note/permission`` 与商品实验
        开关——我们是普通内容号无商家权限,ark 返 **401**,而创作页的全局拦截器把任意 401
        当成"整个登录态失效",同一毫秒跳 ``login?redirectReason=401``;SSO 秒回发布页,但
        编辑器状态已丢、页面重置回视频 tab,只在草稿箱留下一篇带图草稿。

        401 拦不住(实测 page/context.route 都拦不到这些请求;cookie 也确实带全了,是 ark
        真拒绝),故不与平台对抗:直接把那篇草稿捡回来继续编辑——图已在里面,后续
        step5/6/7 照常。只点**最上面一篇**(刚建的那篇);点不动就放弃走原失败路径。
        """
        try:
            entry = self.page.evaluate(r"""() => {
                for (const el of document.querySelectorAll('span,div')) {
                    const m = (el.textContent || '').trim().match(/^草稿箱\((\d+)\)$/);
                    if (m) { const r = el.getBoundingClientRect();
                        if (r.width > 0) return {n: +m[1], x: r.x + r.width/2, y: r.y + r.height/2}; }
                }
                return null;
            }""")
            if not entry or entry["n"] <= 0:
                logger.info("[草稿恢复] 草稿箱为空,放弃恢复")
                return False
            logger.info(f"[草稿恢复] 草稿箱 {entry['n']} 篇,进箱找最新图文草稿")
            self.human.click((entry["x"], entry["y"]), reason="草稿箱")
            self.human.wait(1.5, 2.2, context="草稿箱加载")

            tab = self.page.evaluate(r"""() => {
                for (const el of document.querySelectorAll('div,span,li')) {
                    const m = (el.textContent || '').trim().match(/^图文笔记\((\d+)\)$/);
                    if (m) { const r = el.getBoundingClientRect();
                        if (r.width > 0) return {n: +m[1], x: r.x + r.width/2, y: r.y + r.height/2}; }
                }
                return null;
            }""")
            if not tab or tab["n"] <= 0:
                logger.info("[草稿恢复] 无图文草稿,放弃恢复")
                return False
            self.human.click((tab["x"], tab["y"]), reason="图文笔记 tab")
            self.human.wait(1.2, 1.8, context="图文草稿列表")

            # 取最上面(最新)一篇的「编辑/继续编辑」按钮
            btns = self.page.evaluate(r"""() => {
                const out = [];
                for (const el of document.querySelectorAll('*')) {
                    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
                        .map(n => n.nodeValue.trim()).join('');
                    if (own === '继续编辑' || own === '编辑') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            out.push({x: r.x + r.width/2, y: r.y + r.height/2, y0: r.y});
                    }
                }
                out.sort((a, b) => a.y0 - b.y0);
                return out.slice(0, 1);
            }""")
            if not btns:
                logger.info("[草稿恢复] 未找到「编辑」按钮,放弃恢复")
                return False
            self.human.click((btns[0]["x"], btns[0]["y"]), reason="继续编辑最新草稿")

            # 条件等待编辑器回来(命中即走,最多 20s)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                if self._check_edit_page_loaded():
                    logger.info("✓ [草稿恢复] 编辑器已恢复(草稿内图片在位)")
                    self._take_screenshot("06_recovered_from_draft")
                    return True
            logger.warning("[草稿恢复] 点了继续编辑但编辑器未出现,放弃")
            return False
        except Exception as e:  # noqa: BLE001 — 恢复是兜底,失败不额外制造异常
            logger.warning(f"[草稿恢复] 异常(忽略,走原失败路径): {e}")
            return False

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

    # ============ 视频笔记分支(step2v/step3v,替代图文的 step2/3/4) ============
    #
    # 与图文分支的差别只有"媒体怎么进去"这一段:
    # - 免切 tab —— 创作中心发布页默认落地就是「上传视频」(真号采集实证),
    #   图文那个坐标点 tab 的整段在这里一行都不需要;
    # - 上传完成要等平台**转码**,判据完全不同(见 classify_video_upload_state);
    # - 没有「继续编辑」那一步(视频传完直接就在编辑器里),故无 step4v。
    # 往后的 step5/6/组件/原创声明/step7 与图文共用同一套。

    # 视频上传 input:平台 accept 给的是**扩展名列表**(.mp4,.mov,...),
    # 所以 `input[type='file'][accept*='video']` 一个都匹配不到 —— 只能按 class 收口,
    # 再退回裸 file input(默认落地页实测只有这一个)。
    _VIDEO_FILE_INPUT_SELECTORS = [
        "input[type='file'].upload-input",
        "input[type='file'][accept*='.mp4']",
        "input[type='file']",
    ]

    def step2v_upload_video(self, video_path: str) -> Dict[str, Any]:
        """步骤2v: 上传视频文件(``set_input_files`` 直传,**不点上传按钮**)。

        不点按钮与图文同源(见 step2_upload_images):真桌面上点「上传视频」会弹原生
        GTK 文件框,Playwright 拦不住、模态卡死整条流程。这里也不切 tab —— 发布页默认
        就是「上传视频」。

        只负责把文件灌进去;传没传完、转码好没好一律交 step3v 判(这里立刻回读会永远
        读到"上传中",判据放在一处才不会两处漂移)。
        """
        self.current_step = 2
        logger.info("=" * 60)
        logger.info(f"步骤2v: 上传视频 {video_path}")
        logger.info("=" * 60)

        try:
            self._take_screenshot("03_before_video_upload")
            upload_input = self._find_element_with_retry(
                self._VIDEO_FILE_INPUT_SELECTORS,
                timeout=15, must_be_visible=False,
                intent_key="upload_video_input", intent_desc="隐藏的视频文件 input",
            )
            if not upload_input:
                return {
                    "success": False,
                    "error": "未找到视频上传 input 元素",
                    "screenshot": self._take_screenshot("03_no_video_file_input"),
                }
            upload_input.set_input_files([video_path])
            logger.info("✓ set_input_files 已灌入视频,交 step3v 等上传+转码")
            return {"success": True, "video_path": video_path}
        except Exception as e:
            logger.error(f"上传视频失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("04_video_upload_exception"),
            }

    # 视频页三路判据的一次性读取:cover 区文本 / 页面文本 /「原创声明」开关是否已可点。
    # 开关那一路读 pointer-events 而不是 class:夹具实测禁用态是 pointer-events:none,
    # 而这套组件库的 class 并不随状态翻转(见 note_components.apply_original_declaration)。
    _VIDEO_PROBE_JS = r"""() => {
        const cover = document.querySelector('.cover-container');
        const sw = document.querySelector('.original-wrapper .d-switch');
        let enabled = null;
        if (sw) {
            const cs = window.getComputedStyle(sw);
            enabled = cs.pointerEvents !== 'none' && cs.display !== 'none';
        }
        return {
            cover_text: cover ? (cover.innerText || '').trim() : '',
            page_text: (document.body.innerText || '').slice(0, 4000),
            original_switch_enabled: enabled,
        };
    }"""

    def _video_upload_probe(self) -> Dict[str, Any]:
        """读一次视频上传判据的三路信号;读不到就给空值(空 ≠ 就绪,交判据函数裁决)。"""
        try:
            probe = self.page.evaluate(self._VIDEO_PROBE_JS)
        except Exception as exc:  # noqa: BLE001 — 读判据失败只当"这轮没读到"
            logger.info(f"[step3v] 读取判据异常(当作未就绪): {exc}")
            probe = None
        if not isinstance(probe, dict):
            return {"cover_text": "", "page_text": "", "original_switch_enabled": None}
        return probe

    # 连续多少轮判定 ready 才收口。视频页在"传完 → 转码 → 渲染完成态"之间会闪半态,
    # 单轮命中就放行会让 step5 打进一个还没稳的编辑器。
    _VIDEO_READY_CONFIRM_POLLS = 2

    def step3v_wait_for_video_processing(
        self, max_wait: Optional[int] = None, video_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """步骤3v: 等视频上传 + 平台转码完成(判据见 ``classify_video_upload_state``)。

        ``max_wait`` 缺省**按 video_path 的文件体积伸缩**(``policy.media_timeout_s``,
        与账号子进程硬超时共用同一公式):用户会传 15-30 分钟的 GB 级视频,固定超时在
        这个量级上必错 —— 给小了大文件永远发不出去。读不到大小就退回基数。

        失败一律带**当场取证**(最后一次读到的三路判据原文)。这条路径的失败若只丢一句
        「超时」,运营除了换变量试错什么也做不了 —— 图文那边为此吃过整整一轮排查。
        """
        self.current_step = 3
        if max_wait is not None:
            timeout = max_wait
        else:
            from app.publish.policy import media_file_size, media_timeout_s

            timeout = media_timeout_s(
                media_file_size(video_path),
                base_s=settings.VIDEO_UPLOAD_TIMEOUT_BASE_S,
                per_100mb_s=settings.VIDEO_UPLOAD_TIMEOUT_PER_100MB_S,
                cap_s=settings.VIDEO_UPLOAD_TIMEOUT_CAP_S,
            )
        logger.info("=" * 60)
        logger.info(f"步骤3v: 等待视频上传+转码(上限 {timeout}s)")
        logger.info("=" * 60)

        deadline = time.monotonic() + timeout
        ready_streak = 0
        probe: Dict[str, Any] = {
            "cover_text": "", "page_text": "", "original_switch_enabled": None
        }
        state = "unknown"
        last_log = 0.0
        started = time.monotonic()
        while time.monotonic() < deadline:
            probe = self._video_upload_probe()
            state = classify_video_upload_state(
                probe.get("cover_text", ""),
                probe.get("page_text", ""),
                probe.get("original_switch_enabled"),
            )
            if state == "ready":
                ready_streak += 1
                if ready_streak >= self._VIDEO_READY_CONFIRM_POLLS:
                    waited = time.monotonic() - started
                    logger.info(f"✓ 视频上传+转码完成(等待 {waited:.0f}s)")
                    self._take_screenshot("05_video_ready")
                    return {
                        "success": True,
                        "state": "ready",
                        "edit_page_loaded": True,
                        "wait_time": round(waited, 1),
                        "observed": probe,
                    }
            else:
                ready_streak = 0
            elapsed = time.monotonic() - started
            if elapsed - last_log >= 15:
                last_log = elapsed
                logger.info(
                    f"…视频处理中({elapsed:.0f}/{timeout}s) state={state} "
                    f"cover={probe.get('cover_text', '')[:80]!r}"
                )
                self._take_screenshot(f"05_video_waiting_{int(elapsed)}s")
            time.sleep(1.0)

        logger.error(f"❌ 视频上传+转码超时({timeout}s),最后判定 state={state}")
        return {
            "success": False,
            "error": (
                f"视频上传/转码超时({timeout}s),最后判定 {state};"
                f"cover 区文案={probe.get('cover_text', '')[:200]!r}"
            ),
            "state": state,
            "observed": probe,
            "screenshot": self._take_screenshot("06_video_timeout"),
        }

    # ============ 播客分支(step2a/step3a,替代图文的 step2/3/4) ============
    #
    # 取证状态**分两档,别混着读**(2026-08-07 真号取证,账号9):
    # - **已取证**:「发播客」tab 的切换与激活判据(见 app/browser/podcast.py);
    #   上传区文案「将音频文件拖拽到此,或点击上传音频」;红色「上传音频」按钮
    #   ``button.upload-button``;右侧「通过RSS导入音频」(不做)。
    # - **未取证**:点「上传音频」之后那个弹窗的**内部结构** —— 音频 file input、
    #   音频封面 file input、上传进度反馈、「去发布」按钮的禁用态判据,一个都没抓到。
    #   两轮真号会话都被「播客合集上线啦」引导浮层挡住(它正压在上传按钮上),
    #   而且发播客 tab 首屏的 ``input[type=file]`` 数量实测为 **0** —— 与视频 tab
    #   (首屏就有一个隐藏 input)完全不同,说明 input 是点开弹窗后才挂上去的。
    #
    # 所以下面这两步写成 **fail-loud**:定位不到就带当场取证报错,**绝不静默假装做过**。
    # 媒体步失败 = 整条发布任务失败(与视频的 step2v/step3v 同级),交状态机排重试。
    # 换真值时改的是这几个常量 + 补命中路径单测,控制流不必动。

    # 音频 file input:占位候选。第一条按视频那套 class 类推,第二条按 accept 里
    # 平台大概率会写的扩展名,最后退回"弹窗打开后新出现的裸 file input"。
    _AUDIO_FILE_INPUT_SELECTORS = [
        "input[type='file'].upload-input",
        "input[type='file'][accept*='.mp3']",
        "input[type='file'][accept*='audio']",
        "input[type='file']",
    ]
    # 音频封面 file input:与音频 input 的区分特征未取证 —— 只能靠 accept 里的图片
    # 扩展名把它与音频那个区分开(合集封面页实测 accept=".jpg,.jpeg,.png,.webp",
    # 弹窗里大概率同款)。**区分不出来就不传封面**,绝不把音频灌进封面位。
    _AUDIO_COVER_INPUT_SELECTORS = [
        "input[type='file'][accept*='.jpg']",
        "input[type='file'][accept*='.png']",
        "input[type='file'][accept*='image']",
    ]
    # 「去发布」按钮:实拍确认未传音频时禁用,故它的翻转 = 上传完成的主判据。
    # 具体禁用属性(disabled / class / 自定义属性)未取证,三种都读、任一表明可点即可点。
    _GO_PUBLISH_TEXTS = ("去发布",)

    def step2a_upload_audio(
        self, audio_path: str, cover_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """步骤2a: 打开「上传音频」弹窗并把音频(可选封面)灌进去。

        与图文/视频同源的硬纪律:**只 set_input_files,绝不点 file input 或上传按钮**
        去触发系统选择框 —— 真桌面上会弹原生 GTK 文件框,Playwright 拦不住、模态
        卡死整条流程。这里点的只有「上传音频」那颗红按钮(它开的是网页弹窗)。

        点按钮前先关引导浮层:实测「播客合集上线啦」的 popover 正压在这颗按钮上,
        不关就点在浮层上(两轮真号取证都卡在这)。

        封面是**可选辅助**:定位不到只告警不阻断(回退成不设封面),与视频封面同语义;
        音频本身定位不到则整步失败。
        """
        from app.browser import podcast as podcast_page

        self.current_step = 2
        logger.info("=" * 60)
        logger.info(f"步骤2a: 上传播客音频 {audio_path}")
        logger.info("=" * 60)

        try:
            if not podcast_page.ensure_podcast_tab(self.page, self.human):
                return {
                    "success": False,
                    "error": (
                        "切不到「发播客」tab,当前激活的是 "
                        f"{podcast_page.active_tab_text(self.page)!r}"
                    ),
                    "screenshot": self._take_screenshot("03_podcast_tab_failed"),
                }
            tooltip = podcast_page.dismiss_guide_tooltip(self.page, self.human)
            self._take_screenshot("03_before_audio_upload")

            button = self.page.query_selector(podcast_page.UPLOAD_AUDIO_BUTTON)
            if button is None:
                return {
                    "success": False,
                    "error": f"未找到「上传音频」按钮({podcast_page.UPLOAD_AUDIO_BUTTON})",
                    "observed": self._audio_probe(),
                    "screenshot": self._take_screenshot("03_no_upload_audio_button"),
                }
            self.human.click(button, reason="打开上传音频弹窗")
            self.human.wait(1.0, 1.8, context="等上传音频弹窗渲染")

            upload_input = self._find_element_with_retry(
                self._AUDIO_FILE_INPUT_SELECTORS,
                timeout=15, must_be_visible=False,
                intent_key="upload_audio_input", intent_desc="上传音频弹窗内的音频 file input",
            )
            if upload_input is None:
                return {
                    "success": False,
                    "error": (
                        "上传音频弹窗里没找到音频 file input(选择器待真号 fixtures 落定;"
                        f"引导浮层处理结果 {tooltip})"
                    ),
                    "observed": self._audio_probe(),
                    "screenshot": self._take_screenshot("03_no_audio_file_input"),
                }
            upload_input.set_input_files([audio_path])
            logger.info("✓ set_input_files 已灌入音频,交 step3a 等上传完成")

            cover_applied = self._set_audio_cover(cover_path, upload_input)
            return {"success": True, "audio_path": audio_path,
                    "audio_cover": cover_applied, "tooltip": tooltip}
        except Exception as e:
            logger.error(f"上传音频失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("04_audio_upload_exception"),
            }

    def _set_audio_cover(self, cover_path: Optional[str], audio_input) -> Dict[str, Any]:
        """给播客设音频封面(可选);**失败只告警不阻断**,回退成不设封面。

        ``audio_input`` 传进来只为一件事:**排除它** —— 弹窗里两个 file input 的区分
        特征未取证,若按图片 accept 找到的恰好就是刚才那个音频 input,说明区分不出来,
        此时**宁可不设封面也绝不把封面灌进音频位**(那会把已传好的音频顶掉)。
        """
        if not cover_path:
            return {"status": "skipped", "reason": "no_cover_requested"}
        cover_input = None
        for selector in self._AUDIO_COVER_INPUT_SELECTORS:
            try:
                candidate = self.page.query_selector(selector)
            except Exception:  # noqa: BLE001
                candidate = None
            if candidate is None:
                continue
            try:
                if audio_input is not None and candidate.evaluate(
                    "(el, other) => el === other", audio_input
                ):
                    continue
            except Exception:  # noqa: BLE001 — 比不出来就当它可能是同一个,跳过
                continue
            cover_input = candidate
            break
        if cover_input is None:
            logger.warning(
                "[step2a] 弹窗里认不出音频封面的 file input(与音频 input 区分特征未取证),"
                "本次不设封面 —— 笔记照发"
            )
            return {"status": "error", "reason": "audio_cover_input_not_found"}
        try:
            cover_input.set_input_files([cover_path])
        except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断发布
            logger.warning(f"[step2a] 音频封面灌入失败(不阻断): {exc}")
            return {"status": "error", "reason": f"audio_cover_set_input_failed: {exc}"}
        return {"status": "done", "cover_path": cover_path}

    def _audio_probe(self) -> Dict[str, Any]:
        """读一次播客上传现场的证据(失败时随 error 交出去,别只丢一句"没找到")。"""
        js = r"""() => {
            const inputs = Array.from(document.querySelectorAll("input[type='file']"))
                .map(el => ({cls: el.className || '', accept: el.getAttribute('accept') || ''}));
            const btn = Array.from(document.querySelectorAll('button'))
                .filter(b => (b.innerText || '').trim() === '去发布')
                .map(b => ({cls: b.className || '',
                            disabled: b.hasAttribute('disabled'),
                            aria: b.getAttribute('aria-disabled')}));
            return {
                file_inputs: inputs,
                go_publish: btn,
                page_text: (document.body.innerText || '').slice(0, 1500),
            };
        }"""
        try:
            got = self.page.evaluate(js)
        except Exception as exc:  # noqa: BLE001 — 取证本身绝不制造新异常
            return {"probe_error": str(exc)}
        return got if isinstance(got, dict) else {}

    def step3a_wait_for_audio_upload(
        self, max_wait: Optional[int] = None, audio_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """步骤3a: 等音频上传完成 —— 判据 = 「去发布」按钮从禁用翻转成可点,然后点它。

        ``max_wait`` 缺省**按 audio_path 的文件体积伸缩**(``policy.media_timeout_s``,
        与视频 step3v 及账号子进程硬超时共用同一公式,配置项也沿用 ``VIDEO_UPLOAD_TIMEOUT_*``
        —— 公式只看字节数,与"视频"这个字面无关,不为改个名去动 .env + manifest + worker 三处)。

        为什么以「去发布」的禁用态为主判据:实拍确认未传音频时它是禁用的,所以它翻转
        就是"平台认为音频到位了"的最直接信号。⚠️ 具体的禁用属性形态**未取证**,故
        ``disabled`` 属性 / ``aria-disabled`` / class 含 disabled 三路都读,**全都不表示
        禁用**才算可点(取"与"而不是"或":读不懂的形态一律当禁用,宁可等到超时也不点一颗
        可能禁用的按钮 —— 图文那边 2026-08-02 就栽在点禁用按钮换来一句"发布超时")。

        失败一律带**当场取证**(最后一次读到的 file input / 按钮属性 / 页面文本)。
        """
        self.current_step = 3
        if max_wait is not None:
            timeout = max_wait
        else:
            from app.publish.policy import media_file_size, media_timeout_s

            timeout = media_timeout_s(
                media_file_size(audio_path),
                base_s=settings.VIDEO_UPLOAD_TIMEOUT_BASE_S,
                per_100mb_s=settings.VIDEO_UPLOAD_TIMEOUT_PER_100MB_S,
                cap_s=settings.VIDEO_UPLOAD_TIMEOUT_CAP_S,
            )
        logger.info("=" * 60)
        logger.info(f"步骤3a: 等音频上传完成(上限 {timeout}s)")
        logger.info("=" * 60)

        deadline = time.monotonic() + timeout
        started = time.monotonic()
        last_log = 0.0
        observed: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            observed = self._audio_probe()
            buttons = observed.get("go_publish") or []
            ready = next((b for b in buttons if _go_publish_enabled(b)), None)
            if ready is not None:
                waited = time.monotonic() - started
                logger.info(f"✓ 音频上传完成,「去发布」已可点(等待 {waited:.0f}s)")
                self._take_screenshot("05_audio_ready")
                target = _find_button_by_text(self.page, self._GO_PUBLISH_TEXTS)
                if target is None:
                    return {"success": False,
                            "error": "「去发布」读到可点但取元素时没了(页面正在重渲染?)",
                            "observed": observed,
                            "screenshot": self._take_screenshot("06_go_publish_vanished")}
                self.human.click(target, reason="去发布")
                self.human.wait(1.5, 2.5, context="等发布表单渲染")
                return {"success": True, "wait_time": round(waited, 1),
                        "edit_page_loaded": True, "observed": observed}
            elapsed = time.monotonic() - started
            if elapsed - last_log >= 15:
                last_log = elapsed
                logger.info(
                    f"…音频上传中({elapsed:.0f}/{timeout}s) 「去发布」按钮 {buttons}"
                )
                self._take_screenshot(f"05_audio_waiting_{int(elapsed)}s")
            time.sleep(1.0)

        logger.error(f"❌ 音频上传超时({timeout}s)")
        return {
            "success": False,
            "error": (
                f"音频上传超时({timeout}s):「去发布」按钮始终未翻转成可点。"
                f"当场取证 file_inputs={observed.get('file_inputs')} "
                f"go_publish={observed.get('go_publish')}"
            ),
            "observed": observed,
            "screenshot": self._take_screenshot("06_audio_timeout"),
        }

    def ensure_editor_interactable(self, tries: int = 5) -> bool:
        """确认标题输入框**真的可交互**(在视口内、且中心点没被别的东西盖住)才放行。

        视频页的编辑区不像图文那样传完即定型:封面卡「智能推荐封面生成中」是独立异步
        任务,期间上方区域高度会变,标题框的 rect 一路漂移;而 step5 是按**坐标**做拟人
        点击的(不持 ElementHandle,规避 React 重渲染脱离),坐标一旦落在遮挡物上,
        打出去的字就进不了标题框,还不会报错。故这里用 ``elementFromPoint`` 反查中心点
        命中的到底是不是这个输入框 —— rect 本身在这个页面上不可信。
        """
        js = r"""() => {
            const el = document.querySelector("input[placeholder*='标题']")
                || document.querySelector("textarea[placeholder*='标题']");
            if (!el) return {found: false};
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return {found: true, sized: false};
            const inView = r.top >= 0 && r.bottom <= (window.innerHeight || 0);
            const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
            const hit = document.elementFromPoint(cx, cy);
            const covered = !hit || !(hit === el || el.contains(hit) || hit.contains(el));
            return {found: true, sized: true, inView, covered};
        }"""
        for attempt in range(1, tries + 1):
            try:
                st = self.page.evaluate(js)
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[编辑区可交互校验] 读取异常(重试): {exc}")
                st = None
            if isinstance(st, dict) and st.get("sized") and st.get("inView") \
                    and not st.get("covered"):
                if attempt > 1:
                    logger.info(f"✓ 编辑区可交互(第 {attempt} 次校验)")
                return True
            logger.info(f"⚠ 编辑区尚不可交互(第 {attempt}/{tries} 次): {st}")
            # 不可交互的两种成因都靠"把它滚到视口中段"解:在视口外,或被吸底发布栏压住。
            try:
                self.page.evaluate(
                    "() => { const el = document.querySelector(\"input[placeholder*='标题']\");"
                    " if (el) el.scrollIntoView({block: 'center'}); }"
                )
            except Exception:  # noqa: BLE001
                pass
            self.human.wait(0.8, 1.5, context="等编辑区稳定")
        return False

    def wait_for_submit_enabled(self, timeout: int = 120) -> Dict[str, Any]:
        """等 ``<xhs-publish-btn>`` 的 ``submit-disabled`` 属性翻成非 true 才放行点发布。

        视频页刚进编辑器时该属性是 ``"true"``(真号夹具实测);它**何时**翻转没有实测
        结论(可能等封面生成、可能等标题非空)。所以这里不猜条件、只等结果,超时就带
        当场的属性快照报错 —— 点一个禁用按钮永远不可能发布成功,只会换来一句"发布超时"
        (图文那边 2026-08-02 真号事故就是这么来的)。

        host 不存在同样判 **not ready**:那是页面状态异常,不是"可以点了"。
        """
        js = r"""() => {
            const h = document.querySelector('xhs-publish-btn');
            if (!h) return {found: false};
            return {
                found: true,
                submit_disabled: h.getAttribute('submit-disabled'),
                submit_loading: h.getAttribute('submit-loading'),
            };
        }"""
        deadline = time.monotonic() + timeout
        st: Dict[str, Any] = {"found": False}
        while time.monotonic() < deadline:
            try:
                got = self.page.evaluate(js)
            except Exception:  # noqa: BLE001
                got = None
            if isinstance(got, dict):
                st = got
                if st.get("found") and str(st.get("submit_disabled")).lower() != "true":
                    return {"ready": True, "observed": st}
            time.sleep(1.0)
        logger.warning(f"[step7 前置] 发布按钮 {timeout}s 内未就绪,当场取证: {st}")
        return {"ready": False, "observed": st}

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
                # clear_first:键盘输入是追加语义,重试时会叠加上一轮残留 → 先 Ctrl+a→Backspace
                # 清空恢复幂等;空框首次清空无副作用。
                # 首次(attempt==1)是空框,免 Ctrl+a→Backspace;仅重试时清残留保幂等。
                self.human.type_text(None, value, click_first=False, clear_first=(attempt > 1))
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
            # 逐话题记账:调用方要拿它回显给运营(参数被静默丢弃时当场可见,不用等
            # 笔记发出去人工读正文才发现 —— 2026-08-03 运营为此白删了一篇笔记)
            topics_applied: List[str] = []
            # 失败条目带当场证据(候选文案 / 条数 / 浮层 class / 正文框回读),故值不止 str
            topics_failed: List[Dict[str, Any]] = []

            if tags and len(tags) > 0:
                # 去重 + 截断 ≤10(纯函数)
                tags = dedupe_topics(tags)
                # 走主仓(薯营家)实证过的下拉精选流:逐个输入 #tag → 等下拉 → JS 只读定位
                # 精确匹配项坐标 → 拟人真实鼠标点击选中 → 生成**真话题实体**(蓝色可点击 chip),
                # 纯文本 #tag 只是文字不参与话题分发。无精确匹配则 Escape+回删,绝不留残缺文本
                # (残缺话题会撑爆 10 个上限拦发布,RCA 2026-05-18)。
                # 历史注:旧版曾因"逐个等下拉要几十秒、编辑器驻留窗口撑不住"降级成纯文本——
                # 那个慢主因是 camoufox humanize 与贝塞尔双重拟人化叠乘(单击 24s),已根治
                # (单击 1.3s),现在每个话题全流程 ~5-7s,窗口内绰绰有余,故恢复下拉精选流。
                logger.info(f"6.1 下拉精选流添加话题: {tags}")
                try:
                    # **先滚进视口再点,点完必须验焦点**(2026-08-03 文字版事故):
                    # 文字版是超长竖图,上传后图片预览把页面撑得极高,正文框被顶出视口。
                    # 旧实现拿 getBoundingClientRect 的中心直接点 —— 视口外元素的 rect
                    # 中心落在页面顶栏(实测 y=72,对照正常轮播 y=788),点击根本没进正文框,
                    # 光标不在,#话题 打进虚空,下拉永远不弹,6 个话题全报 no_floating_layer,
                    # 而日志看起来只是"没匹配上"。标签是核心分发渠道,静默全丢等于白发。
                    content_el = None
                    for sel in (
                        "div[contenteditable='true'][data-placeholder*='正文']",
                        "div[contenteditable='true'][placeholder*='正文']",
                        "textarea[placeholder*='正文']",
                        "div[contenteditable='true']",
                    ):
                        content_el = self.page.query_selector(sel)
                        if content_el is not None:
                            break
                    if content_el is None:
                        logger.warning("未找到正文框,跳过话题(不阻断发布)")
                        topics_failed = [
                            {"tag": str(t), "reason": "content_box_not_found"} for t in tags
                        ]
                    else:
                        focused = False
                        for focus_try in (1, 2):
                            self.human.scroll_to_element(content_el)
                            box = content_el.bounding_box() or {}
                            if not box:
                                break
                            cx = box["x"] + box["width"] * 0.5
                            cy = box["y"] + box["height"] * 0.5
                            self.human.click((cx, cy), reason="聚焦正文框(添加话题)")
                            # 只读验焦点:activeElement 必须落在 contenteditable 里。
                            # 不验就打字 = 把话题打进虚空还以为是"下拉没匹配"。
                            focused = bool(self.page.evaluate(
                                "() => { const ae = document.activeElement;"
                                " if (!ae) return false;"
                                " if (ae.getAttribute && ae.getAttribute('contenteditable') === 'true') return true;"
                                " return !!(ae.closest && ae.closest(\"[contenteditable='true']\")); }"
                            ))
                            if focused:
                                break
                            logger.warning(
                                f"聚焦正文框后焦点不在编辑区(第 {focus_try} 次),重滚动重点"
                            )
                        if not focused:
                            logger.warning("正文框聚焦失败,跳过话题(不阻断发布,但如实记账)")
                            topics_failed = [
                                {"tag": str(t), "reason": "content_box_focus_failed"}
                                for t in tags
                            ]
                            raise RuntimeError("content_box_focus_failed")
                        self.human.press_key("Control+End", reason="光标移到正文末尾")
                        self.human.press_key("Enter", reason="话题另起一行")
                        added = 0
                        for tag_idx, tag in enumerate(tags):
                            tag_text = tag if str(tag).startswith("#") else f"#{tag}"
                            tag_name = str(tag).lstrip("#").strip()
                            self.human.type_text(None, tag_text, click_first=False)
                            logger.info(f"   [{tag_idx+1}/{len(tags)}] 输入话题: {tag_text}")
                            self.human.wait(1.5, 2.5, context="等待话题下拉")

                            # JS 只**枚举**浮层(不判断、不 click),判据全在 Python 里 ——
                            # 见 topic_dropdown.py:旧版在 JS 里"取面积最小的浮层"没有正向
                            # 判据,视频页稳定抓成右侧预览面板的作者信息区(RCA 2026-08-07)。
                            option_pos = select_topic_option(
                                self.page.evaluate(COLLECT_LAYERS_JS, tag_name),
                                tag_name,
                                editor_rect=content_el.bounding_box(),
                            )

                            if option_pos and option_pos.get("success"):
                                ox, oy = option_pos["x"], option_pos["y"]
                                matched = option_pos.get("matched", "")
                                self.human.click((ox, oy), reason=f"点击话题选项: {matched[:20]}")
                                logger.info(f"   ✓ 话题下拉点击成功: '{matched}'")
                                added += 1
                                topics_applied.append(tag_name)
                            else:
                                # 没定位到选项:回删刚输入的 #tag,绝不留残缺文本。
                                # 回删**之前**先取证(回删后正文框就看不出打进去过什么了)。
                                detail = topic_failure_detail(
                                    tag_name, option_pos, read_editor_tail(content_el)
                                )
                                # 「没找到下拉」和「下拉里没这词」是两种处置,日志也别糊在一起:
                                # 前者是我们的定位判据失灵(要改代码),后者是词本身不存在(换词)
                                _fail_log = (
                                    logger.warning
                                    if detail["reason"] == "topic_dropdown_not_found"
                                    else logger.info
                                )
                                _fail_log(
                                    f"   话题未选中({detail['reason']}),回删该话题不插入"
                                    f" | 浮层候选 {detail.get('candidates')}"
                                    f" | 浮层 {detail.get('layer_class')}"
                                    f" 共 {detail.get('layers_seen')} 层"
                                    f",被拒 {detail.get('rejected_classes')}"
                                    f" | 正文框末尾「{detail['editor_tail']}」"
                                )
                                topics_failed.append(detail)
                                try:
                                    self.page.keyboard.press("Escape")
                                    for _ in range(len(tag_text)):
                                        self.page.keyboard.press("Backspace")
                                except Exception as _be:
                                    logger.info(f"   回删话题异常: {_be}")
                            self.human.wait(0.8, 1.5, context="话题处理")
                        logger.info(f"✓ 话题添加完成: {added}/{len(tags)} 个精选实体")
                        options_set.append("tags")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"下拉精选流添加话题失败({e}),跳过话题不阻断发布")
                    # 聚焦失败的话题在抛错前已记账;别的异常把还没处理的话题补记上
                    done = {t for t in topics_applied} | {f["tag"] for f in topics_failed}
                    for t in tags:
                        name = str(t).lstrip("#").strip()
                        if name not in done:
                            topics_failed.append({"tag": name, "reason": f"exception: {e}"[:80]})

            if location:
                logger.info(f"6.2 设置地点: {location}")
                logger.info("地点设置功能待实现")
                options_set.append("location")

            self._take_screenshot("11_options_set")
            return {
                "success": True,
                "options_set": options_set,
                "topics_applied": topics_applied,
                "topics_failed": topics_failed,
            }

        except Exception as e:
            logger.error(f"设置发布选项失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "screenshot": self._take_screenshot("11_exception"),
            }

    # ==================== 步骤7: 点击发布并等待 ====================

    def _scan_blocking_notice(self) -> Optional[str]:
        """扫可见 toast/dialog,返回阻断性提示文案(封禁/违规/失败/频繁等);无则 None。

        RCA 2026-07-25(账号1封禁):点发布后 0.2s 即弹 d-new-toast
        「因违反社区规范禁止发笔记」,~7.8s 消失。旧逻辑 12s 轮询跑完才在 14s 抓 forensic,
        toast 早没了 → 扑空 → 干等 30s 超时报「未检测到成功标志」。故抽成方法级,供 step7
        点发布后密集轮询(一现即捕捉)与超时兜底(收口前再扫一次)共用。"""
        try:
            return self.page.evaluate(r"""() => {
                const sel = '[class*=toast],[class*=Toast],[role=dialog],'
                  + '[class*=dialog],[class*=Dialog],[class*=modal],'
                  + '[class*=Modal],[class*=message],[class*=Message],'
                  + '[class*=notice],[class*=Notice],[class*=alert]';
                const KW = /违反社区规范|禁止发笔记|账号异常|违规|封禁|已被封|限制发布|无法发布|发布失败|操作(过于)?频繁|请稍后(重试)?|内容(审核|违规)|未通过|风险|需要验证|拦截/;
                for (const el of document.querySelectorAll(sel)) {
                    if (el.offsetParent === null) continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 120 && KW.test(t)) return t;
                }
                return null;
            }""")
        except Exception:
            return None

    @staticmethod
    def _notice_is_ban(notice: str) -> bool:
        """判定阻断文案是否为账号级封禁/限制(vs 临时频繁/网络类),据此置 account_restricted。"""
        return any(k in notice for k in (
            "违反社区规范", "禁止发笔记", "账号异常", "封禁", "已被封", "限制发布", "违规"))

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
            # 收尾清场:step6 打 #话题 会触发 XHS 话题自动补全下拉框,发布时若仍打开,首次点击
            # 会落到下拉框(实测截图 publish_07_12 确认下拉框覆盖)→ 只关下拉框、发布"页面未变"。
            # 先按 Esc 关掉补全下拉,再让页面稳一下,确保发布按钮可被干净点击。
            try:
                self.page.keyboard.press("Escape")
                time.sleep(0.4)
            except Exception:
                pass

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
                    // disabled 三处都读:原生 .disabled 属性、class 里的 disabled、
                    // 以及 aria-disabled —— 小红书这套组件库三种写法都出现过,
                    // 只看其中一种会漏(实测「确认引用」是 class 带 disabled)。
                    const isDisabled = (el) => !!(
                        el.disabled ||
                        /(^|\s)disabled(\s|$)/.test(el.getAttribute('class') || '') ||
                        el.getAttribute('aria-disabled') === 'true'
                    );
                    const rectOf = (el) => { const r = el.getBoundingClientRect();
                        return {cls: el.className, x: Math.round(r.x), y: Math.round(r.y),
                                w: Math.round(r.width), h: Math.round(r.height),
                                vis: r.width > 0 && r.height > 0,
                                disabled: isDisabled(el)}; };
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
                #
                # **禁用的按钮一律不点**(2026-08-02 真号事故):全页兜底原本只看"有没有
                # 尺寸"就点。当「选择笔记」弹窗因为引用失败而**留在页面上**时,弹窗里那个
                # disabled 的「确认引用」也是 .d-button.bg-red,尺寸正常,于是被当成发布
                # 按钮点掉——点禁用按钮什么都不会发生,而真正的发布按钮还被弹窗盖着,
                # 最后只报一句「发布超时(30秒)」。好好生活号连续三次全栽在这。
                # 点一个禁用按钮**永远不可能**发布成功,所以这不是"优先级低一点"而是排除。
                target = None
                if diag.get('lightBtn') and diag['lightBtn'].get('vis') \
                        and not diag['lightBtn'].get('disabled'):
                    target = diag['lightBtn']; click_strategy = "light DOM 按钮"
                elif diag.get('shadowBtn') and diag['shadowBtn'].get('vis') \
                        and not diag['shadowBtn'].get('disabled'):
                    target = diag['shadowBtn']; click_strategy = "open shadow 按钮"
                else:
                    skipped_disabled = []
                    for cand in (diag.get('globalRedBtns') or []) + (diag.get('globalPublishBtns') or []):
                        if cand.get('vis') and cand.get('disabled'):
                            skipped_disabled.append(cand.get('cls', '')[:40])
                            continue
                        if cand.get('vis'):
                            target = cand; click_strategy = f"全页候选({cand.get('cls','')[:30]})"
                            break
                    if skipped_disabled:
                        # 这条日志就是"页面上有别的浮层没收干净"的直接线索,别删
                        logger.warning(
                            f"⚠ 已跳过 {len(skipped_disabled)} 个禁用的红色按钮(多半是没关掉的"
                            f"弹窗留下的): {skipped_disabled}"
                        )

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

                    _blocking_notice = self._scan_blocking_notice

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
                    # 发布是**非幂等**操作:一旦点击被发布按钮接收(无论成功回执还是 Network
                    # Error 拒绝回执),都会生成一篇笔记。旧逻辑点后只 sleep 2s,未见跳转就换手段
                    # 再点 → 发布慢/并发/网络摩擦时首点其实已生效,补点又各生成一篇 → 重复发布
                    # (实测看世界并发真发布出 3 篇同文)。改为:点一次 → 长窗口(~12s)轮询成功页;
                    # 未见成功也先判「点击是否已被接收」(回执 toast / 按钮 loading / 已离开发布页),
                    # 已接收则**绝不补点**、转等待兜底,仅在明确未被接收(疑似点空)时才换位置补点。
                    def _click_registered():
                        try:
                            if _published():
                                return True
                            n = self.page.evaluate(
                                "() => document.querySelectorAll("
                                "'[class*=toast],[class*=Toast]').length")
                            if n and n > 0:
                                return True
                            return bool(self.page.evaluate(
                                "() => { const h=document.querySelector('xhs-publish-btn');"
                                " if(!h) return true;"
                                " const s=(h.innerHTML||'')+((h.shadowRoot&&h.shadowRoot.innerHTML)||'');"
                                " return /loading|disabled|submitting|发布中/i.test(s); }"))
                        except Exception:
                            return False

                    blocked_notice = None  # 点发布后捕捉到的阻断提示(封禁/违规/失败等)
                    for idx, (name, act) in enumerate(attempts):
                        # 补点前先确认上次点击**未被接收**;已接收则停手,绝不重复发布
                        if idx > 0 and _click_registered():
                            logger.info(
                                f"[{name}] 跳过补点:上次点击已被接收(防重复发布),转等待兜底")
                            publish_clicked = True
                            click_strategy = "closed shadow:已接收待确认"
                            break
                        try:
                            self.human.wait(0.3, 0.7, context="确认发布内容")
                            act()
                            logger.info(f"✓ [closed shadow] 尝试[{name}] @({tx:.0f},{ty:.0f})")
                            # 长窗口轮询成功页(发布慢/并发/网络摩擦时 sleep 2s 远不够);
                            # **每轮同步扫阻断 toast**——封禁/违规回执一出现即捕捉,不等超时。
                            published = False
                            for _ in range(12):
                                time.sleep(1.0)
                                blocked_notice = _blocking_notice()
                                if blocked_notice:
                                    break
                                if _published():
                                    published = True
                                    break
                            if blocked_notice:
                                break  # 命中阻断:跳出补点循环,下方统一以明确原因收口
                            if published:
                                logger.info(f"✓ [{name}] 发布生效(页面已变化)")
                                publish_clicked = True
                                publish_confirmed = True
                                click_strategy = f"closed shadow:{name}"
                                break
                            # 未跳成功页但点击已被接收 → 不补点,转等待兜底(防重复发布)
                            if _click_registered():
                                logger.info(
                                    f"… [{name}] 未跳成功页但点击已被接收 → 停止补点(防重复),转等待兜底")
                                publish_clicked = True
                                click_strategy = f"closed shadow:{name}(已接收待确认)"
                                break
                            logger.info(f"… [{name}] 点击未被接收(疑似点空),换手段补点")
                        except Exception as ae:
                            logger.info(f"[{name}] 执行异常: {ae}")
                    if blocked_notice:
                        # 点发布后小红书弹阻断回执(封禁/违规/失败/频繁等):以明确原因立即收口,
                        # 不干等 30 秒超时。封禁类置 account_restricted=True(状态机据此直接 failed
                        # 不重试——重发也发不出且是更强高频封号信号);其余(失败/频繁)带原文返回。
                        is_ban = self._notice_is_ban(blocked_notice)
                        logger.warning(f"❌ 发布被阻断: {blocked_notice!r} restricted={is_ban}")
                        return {
                            "success": False,
                            "error": f"发布被小红书阻断:{blocked_notice}",
                            "account_restricted": is_ban,
                            "screenshot": self._take_screenshot("13_blocked_notice"),
                        }
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
                        '[class*=toast],[class*=Toast]')]
                        .map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,6);
                    out.bodyHead = (document.body.innerText||'').trim().slice(0,160);
                    return out;
                }""")
                logger.info(f"[点击后取证] {json.dumps(forensic, ensure_ascii=False)[:1200]}")
            except Exception as fe:
                logger.error(f"点击后取证失败: {fe}")
                forensic = {}

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

            # 账号级禁发检测:点发布后小红书用 toast/弹窗告知"因违反社区规范禁止发笔记"
            # 等账号处罚态。此时发布按钮点击其实生效了(toast 是 XHS 的拒绝回执),但笔记
            # 永远发不出去,继续等 30 秒只会误报"发布超时"。命中即以明确原因立即收口,避免
            # 误判 + 无谓重试(重试也发不出)。
            # 注意:必须放在 publish_confirmed 收口之后 —— 已被级联点击权威确认成功的发布,
            # 绝不能再被禁发关键词误判翻盘成 failed(否则 scheduler 重试 → 重复发同一篇)。
            # 匹配源只取 dialogText + 真正 toast(不含整页 bodyHead,避免把用户自己正文/正常
            # 提示误判);关键词只保留整句处罚回执,删掉过泛子串。
            try:
                _probe = " ".join(
                    (forensic.get("toasts") or [])
                    + [forensic.get("dialogText") or ""]
                )
                ban_markers = [
                    "因违反社区规范禁止发笔记", "账号异常无法发布", "禁止发笔记",
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

                # 每轮先扫阻断回执(封禁/违规/失败/频繁):light/open-shadow 路径不走上面
                # closed-shadow 密集轮询,ban toast(~7.8s 灭)必须在此循环里一现即捕捉,
                # 否则又拖到 30s 超时误报。命中即以明确原因收口,封禁类置 account_restricted。
                _notice = self._scan_blocking_notice()
                if _notice:
                    is_ban = self._notice_is_ban(_notice)
                    logger.warning(f"❌ 发布被阻断: {_notice!r} restricted={is_ban}")
                    return {
                        "success": False,
                        "error": f"发布被小红书阻断:{_notice}",
                        "account_restricted": is_ban,
                        "screenshot": self._take_screenshot("13_blocked_notice"),
                    }

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

            # 超时兜底:收口前再扫一次阻断回执(轮询间隙外新弹的 toast/迟到的处罚回执),
            # 命中则带明确原因返回,不再报笼统"未检测到成功标志"。
            _final_notice = self._scan_blocking_notice()
            if _final_notice:
                is_ban = self._notice_is_ban(_final_notice)
                logger.warning(f"❌ 发布被阻断(超时收口扫得): {_final_notice!r} restricted={is_ban}")
                return {
                    "success": False,
                    "error": f"发布被小红书阻断:{_final_notice}",
                    "account_restricted": is_ban,
                    "current_url": self.page.url,
                    "screenshot": self._take_screenshot("13_blocked_notice"),
                }
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
