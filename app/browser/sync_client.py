"""小红书 sync Camoufox 发布客户端(精简移植)。

移植自旧仓 ``backend/app/services/xhs_playwright_client.py`` + ``xhs_playwright_manager.py``,
精简为发布/登录检测两条落地路径。相对旧仓的收敛:

- **cookie 由参数注入**:删掉 ``SessionLocal`` 读 DB + ``decrypt_data``,cookie 以
  ``list[dict]`` 参数传入(上游 ``cookie_service`` 已 normalize sameSite)。
- profile 走 ``profile_guard``:统一目录 / 杀孤儿 / 清锁 / 删 cookies.sqlite;
  指纹走 ``fingerprint``;登录判定走 ``login_detector``。
- 删互动方法(comment/like/collect/文字封面)与 SmartLocator 兜底。
- 线程封装(旧仓 ``xhs_playwright_manager`` 的 ThreadPoolExecutor)简化内联:
  ``publish_once`` / ``check_login_once`` 是**纯 sync 函数**,内部建 client→start→操作→stop
  全部同一线程;由上层用 ``asyncio.to_thread`` 调用(P3.5 队列做 per-account 互斥)。

对外接口(P3.5 依赖,不可改名):
- ``publish_once(account_id, cookies, title, content, image_paths, topics) -> PublishResult``
- ``check_login_once(account_id, cookies) -> dict``
- ``PublishResult``:``{success, note_id, note_url, error, need_manual_login}``;
  返回契约**允许 success=True 但 note_id=""**(只有 note_url)。
"""
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.atomic_tasks import XHSPublishAtomicTasks
from app.browser.fingerprint import get_fingerprint
from app.browser.login_detector import DETECT_LOGIN_JS, GET_USER_INFO_JS
from app.browser.profile_guard import (
    clean_locks,
    delete_cookies_db,
    kill_orphans,
    profile_dir,
    sanitize_launch_options,
)

# ark.xiaohongshu.com 是小红书商家/专业号后端(trade_note/permission、experiment_info
# 等商品/直播权限门)。发布普通图文根本不需要它,但在 tun 环境下该域被路由到北京、
# 与会话直连 IP 不匹配 → 稳定返回 401 → XHS 前端全局 401 拦截器无脑跳登录页
# (login?redirectReason=401),表现为"上传/编辑后掉登录"。抓包实证:图片上传
# (ros-upload/creator permit)全 200,唯一 401 就是 ark 这些商家权限门;且这些请求
# 从 Service Worker/隔离上下文发出,Playwright route / window.fetch / XHR patch 均拦不到。
# 解法:用 Firefox PAC 把 ark.xiaohongshu.com 指向死代理(127.0.0.1:1)→ 连接失败
# (网络错误,而非 401)→ 前端 401 拦截器不触发,不跳登录;其余域 DIRECT(照旧走 tun)。
# PAC 是浏览器全局代理层,SW/iframe/主线程一律生效,弥补 route/JS 拦不到的盲区。
_ARK_BLACKHOLE_PAC = (
    "function FindProxyForURL(url, host){"
    'if(host=="ark.xiaohongshu.com"){return "PROXY 127.0.0.1:1";}'
    'return "DIRECT";}'
)


def _ark_blackhole_pac_url() -> str:
    import base64
    b64 = base64.b64encode(_ARK_BLACKHOLE_PAC.encode("utf-8")).decode("ascii")
    return "data:application/x-ns-proxy-autoconfig;base64," + b64


@dataclass
class PublishResult:
    """发布结果契约。

    ``success=True`` 时允许 ``note_id`` 为空(小红书成功页可能只有 note_url、
    创作中心抓不到 24 位 hex id)。``need_manual_login`` 是独立信号:创作中心 SSO
    自动认证失败、需人工扫码登录一次,与普通 ``error`` 字符串区分开。
    ``account_restricted`` 亦为独立信号:账号被小红书判违规/处罚禁发(step7 命中禁发 toast),
    重试也发不出,状态机据此直接置 failed 且不递增 retries、不排重试(重发=更强高频信号)。
    """

    success: bool
    note_id: str = ""
    note_url: str = ""
    error: Optional[str] = None
    need_manual_login: bool = False
    account_restricted: bool = False
    # 只存草稿模式:内容已录入并存为草稿,未真发布,待用户手动发布。
    draft_saved: bool = False
    message: str = ""


# sameSite 兜底映射(上游 cookie_service 已 normalize,这里防御性再收口一次)
def _coerce_same_site(value: Any) -> str:
    """把 sameSite 归一到 Camoufox/Firefox 接受的 Strict/Lax/None(默认 Lax)。"""
    if isinstance(value, str):
        low = value.lower()
        if low == "strict":
            return "Strict"
        if low == "none":
            return "None"
    return "Lax"


def normalize_cookies_for_injection(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把入库 cookie 规整为 Camoufox ``add_cookies`` 可用格式,并**双域注入**。

    §6.4 坑#6:主站 ``.xiaohongshu.com`` cookie 之外,creator 子域需以 ``url`` 方式
    补注入 —— Camoufox(Firefox)对 domain 前缀点的子域匹配不可靠,不补注入创作中心
    读不到 cookie。sameSite 上游已 normalize,这里仅规整 domain(补前导点 / www→.)
    并生成 creator 子域 fallback 项。

    纯函数(不依赖浏览器),可脱离真页面单测。
    """
    if not cookies:
        return []

    result: List[Dict[str, Any]] = []
    for cookie in cookies:
        name = cookie.get("name")
        if not name or "value" not in cookie:
            continue

        domain = cookie.get("domain", ".xiaohongshu.com")
        # 确保域名以 . 开头(应用到所有子域名);www.xiaohongshu.com 归一为 .xiaohongshu.com
        if not domain.startswith("."):
            domain = "." + domain.lstrip("www.")
        if "www.xiaohongshu.com" in domain:
            domain = ".xiaohongshu.com"

        same_site = _coerce_same_site(cookie.get("sameSite"))
        entry: Dict[str, Any] = {
            "name": name,
            "value": cookie["value"],
            "domain": domain,
            "path": cookie.get("path", "/"),
            "httpOnly": cookie.get("httpOnly", False),
            "secure": cookie.get("secure", True),
            "sameSite": same_site,
        }
        if cookie.get("expires") and cookie["expires"] > 0:
            entry["expires"] = cookie["expires"]
        result.append(entry)

        # creator 子域 fallback:以 url 方式补注入,确保创作中心能读到 cookie
        if domain == ".xiaohongshu.com":
            creator = {
                "name": name,
                "value": cookie["value"],
                "url": "https://creator.xiaohongshu.com/",
                "httpOnly": cookie.get("httpOnly", False),
                "secure": cookie.get("secure", True),
                "sameSite": same_site,
            }
            if cookie.get("expires") and cookie["expires"] > 0:
                creator["expires"] = cookie["expires"]
            result.append(creator)

    return result


class SyncClient:
    """小红书自动化 sync 客户端(Camoufox 引擎)。

    生命周期严格单线程:``start`` → ``publish_note`` / ``check_login`` → ``stop`` 必须在
    同一线程且 profile 独占(见 §6.4 坑#7)。cookie 由构造参数注入,不读 DB。
    """

    def __init__(
        self,
        account_id: int,
        cookies: List[Dict[str, Any]],
        headless: bool = False,
        block_images: bool = False,
    ):
        self.account_id = account_id
        self.cookies = cookies or []
        self.headless = headless
        # 瘦身开关:只读路径(cookie-check/note-export)传 True 拦图省内存;
        # 发布路径保持 False,保留发布页完整渲染(避免图元素缺失影响上传/发布按钮定位)。
        self.block_images = block_images

        self.playwright = None
        self.context = None
        self.page = None
        # start() 期间缓存登录检测结果(供 check_login 复用 profile_url)
        self._last_detect: Dict[str, Any] = {}

    def start(self) -> Dict[str, Any]:
        """启动浏览器:profile 守护 → 指纹 → 起 Camoufox → 注入 cookie → 开 explore → 登录判定。"""
        try:
            from camoufox import NewBrowser, launch_options
            from playwright.sync_api import sync_playwright

            fp = get_fingerprint(self.account_id)
            pdir = profile_dir(self.account_id)

            # profile 守护:杀孤儿 + 建目录 + 清锁 + 删旧 cookie
            # (旧 cookie 若不删,持久上下文可能覆盖新注入 → 登成别人号)
            kill_orphans(pdir)
            pdir.mkdir(parents=True, exist_ok=True)
            clean_locks(pdir)
            delete_cookies_db(pdir)

            # 从 UA 推断操作系统
            ua = fp.user_agent or ""
            if "Windows" in ua:
                target_os = "windows"
            elif "Macintosh" in ua or "Mac OS" in ua:
                target_os = "macos"
            else:
                target_os = "linux"

            camoufox_opts = launch_options(
                headless=self.headless,
                humanize=True,
                block_webrtc=True,
                block_webgl=False,  # headed 跑在真屏 :0 + RTX 4090:放开 WebGL 走真 GPU
                                    # 硬件渲染(真 NVIDIA 指纹),而非 Xvfb 软件渲染/headless 特征
                block_images=self.block_images,  # 只读路径拦图省内存;发布路径为 False 保真
                locale=fp.locale or "zh-CN",
                os=target_os,
                i_know_what_im_doing=True,
                config={
                    "navigator.userAgent": fp.user_agent,
                    "screen.width": fp.screen_resolution.get("width", fp.viewport["width"]),
                    "screen.height": fp.screen_resolution.get("height", fp.viewport["height"]),
                    "navigator.hardwareConcurrency": fp.hardware_concurrency or 8,
                    "navigator.platform": fp.platform or "Win32",
                },
                window=(fp.viewport["width"], fp.viewport["height"]),
                # PAC 把 ark.xiaohongshu.com 指向死代理 → 商家权限门 401 变网络错误,
                # 前端不再跳登录(详见模块顶部 _ARK_BLACKHOLE_PAC 注释)。其余 DIRECT。
                firefox_user_prefs={
                    "network.proxy.type": 2,  # 2 = 用 PAC
                    "network.proxy.autoconfig_url": _ark_blackhole_pac_url(),
                },
            )
            # 持久化参数注入(NewBrowser 通过 from_options 展开传给 launch_persistent_context)
            camoufox_opts["user_data_dir"] = str(pdir)
            camoufox_opts["viewport"] = fp.viewport
            camoufox_opts["timezone_id"] = fp.timezone or "Asia/Shanghai"
            # proxy=None 会被 Firefox 误解为空代理配置 → 拒连,必须剔除
            camoufox_opts = sanitize_launch_options(camoufox_opts)

            self.playwright = sync_playwright().start()
            self.context = NewBrowser(
                self.playwright,
                persistent_context=True,
                from_options=camoufox_opts,
            )
            logger.info(f"[SyncClient] Camoufox 已启动(账号 {self.account_id})")

            # 吞未捕获错误/未处理拒绝:ark 被 PAC 打到死代理后请求网络错误,XHS 若未 catch
            # 会冒泡成 pageerror,而 Playwright Firefox driver 处理 location 为空的 pageerror
            # 时会崩(coreBundle.js 读 pageError.location.url 抛 TypeError → 整个 driver 挂)。
            # 在文档最早期(capture 阶段)兜住**所有**未捕获错误/拒绝:init-script 先于
            # Juggler 内容脚本注册,capture+stopImmediatePropagation 抢在 Juggler 的
            # uncaughtError 监听器之前吞掉,使 driver 不再收到畸形(location 为空)pageerror。
            # 【必须吞全部,不能收窄】——实测收窄成"只吞 ark/畸形"后,真点发布时 ark 死代理
            # 网络失败产生的 pageerror 漏给 Juggler → driver 崩(coreBundle 读 location.url)。
            # 权衡:自动化场景防 driver 崩 > 保留页面真实报错,故无差别吞掉。
            try:
                self.context.add_init_script(
                    "window.addEventListener('unhandledrejection',function(e){try{"
                    "e.preventDefault();e.stopImmediatePropagation();}catch(_){}},true);"
                    "window.addEventListener('error',function(e){try{"
                    "e.preventDefault();e.stopImmediatePropagation();}catch(_){}},true);"
                )
            except Exception as e:
                logger.warning(f"[SyncClient] 错误吞噬 init-script 装配失败(忽略): {e}")

            if self.context.pages:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()

            # 先注入 cookie 再访问页面(避免 reload 超时);双域注入见 normalize
            cookies = normalize_cookies_for_injection(self.cookies)
            if cookies:
                self.context.add_cookies(cookies)
                logger.info(f"[SyncClient] 注入 {len(cookies)} 个 cookie(含 creator 子域)")

            # 带登录态访问探索页
            self.page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(3)

            self._last_detect = self._detect_login()
            logged_in = bool(self._last_detect.get("is_logged_in"))
            logger.info(f"[SyncClient] 登录检测: {logged_in} reason={self._last_detect.get('reason')}")
            return {"success": True, "logged_in": logged_in}

        except Exception as e:
            logger.error(f"[SyncClient] 启动浏览器失败: {e}")
            return {"success": False, "error": f"启动浏览器失败: {e}"}

    def _detect_login(self) -> Dict[str, Any]:
        """在当前页执行统一登录检测 JS,返回结论 dict(异常 → 未登录)。"""
        try:
            return self.page.evaluate(DETECT_LOGIN_JS) or {}
        except Exception as e:
            logger.warning(f"[SyncClient] 登录检测出错: {e}")
            return {"is_logged_in": False, "reason": str(e)}

    def _is_captcha(self) -> bool:
        """检测当前页是否为验证码/滑块拦截(URL 或 DOM 标志)。"""
        try:
            url = (self.page.url or "").lower()
            if "captcha" in url or "sec_tbc" in url:
                return True
            el = self.page.query_selector(
                'div.nc_wrapper, .nc-container, .slide-verify, iframe[src*="captcha"]'
            )
            return el is not None
        except Exception:
            return False

    def _get_user_info(self, profile_url: Optional[str]) -> Optional[Dict[str, Any]]:
        """导航到个人主页(用登录检测提取的 profile_url)抓取昵称/小红书号等。

        不用 ``/user/profile/me`` —— 该路径触发小红书风控强制扫码。无 profile_url → None。
        """
        if not profile_url:
            return None
        try:
            self.page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            return self.page.evaluate(GET_USER_INFO_JS)
        except Exception as e:
            logger.warning(f"[SyncClient] 获取用户信息失败: {e}")
            return None

    def _api_login_status(self) -> Optional[bool]:
        """用小红书官方 API 权威判定登录态（在浏览器页内 fetch，用真实会话 cookie）。

        返回 ``True``=已登录 / ``False``=登录已过期 / ``None``=不可达(降级 DOM 启发式)。

        为什么需要它：``DETECT_LOGIN_JS`` 是 DOM 启发式，explore 页登出态下仍会渲染
        笔记内容，容易把"未登录"误判成"已登录"(假阳性)—— 实测过期 cookie 也被判 valid，
        导致 cookie_status 说谎、发布静默失败。这里直接问 ``/user/me`` 拿地面真值。
        check_login 在 start() 后调用，此时 self.page 已在 www.xiaohongshu.com，
        fetch 同站子域 edith.xiaohongshu.com 合法(小红书自家前端亦如此调用)。
        """
        js = """async () => {
          try {
            const r = await fetch('https://edith.xiaohongshu.com/api/sns/web/v2/user/me',
                                  {credentials: 'include'});
            const j = await r.json();
            if (j && j.success && j.data && j.data.guest === false) return 'valid';
            if (j && (j.code === -100 || String(j.msg || '').indexOf('登录已过期') >= 0)) return 'expired';
            return 'unknown';
          } catch (e) { return 'error'; }
        }"""
        try:
            res = self.page.evaluate(js)
        except Exception as e:
            logger.warning(f"[api_login] 验活 API 调用异常，降级 DOM 启发式: {e}")
            return None
        if res == "valid":
            return True
        if res == "expired":
            return False
        logger.warning(f"[api_login] 验活 API 未决(res={res})，降级 DOM 启发式")
        return None

    def check_login(self) -> Dict[str, Any]:
        """检查登录态,返回 ``{status, user_info}``。

        status: 'valid'(已登录,附 user_info)| 'invalid'(未登录)| 'captcha'(验证码拦截)。

        判定优先级：官方 API 地面真值 > DOM 启发式。API 明确过期 → 直接 invalid；
        API 明确登录 → valid；API 不可达才回落到原 DOM 启发式(避免 API 抖动误杀好号)。
        """
        if self._is_captcha():
            return {"status": "captcha", "user_info": None}

        api = self._api_login_status()
        if api is False:
            return {"status": "invalid", "user_info": None}

        detect = self._last_detect or self._detect_login()
        # API 不可达时才用 DOM 结论把关；API 明确登录(True)则不被 DOM 假阴性否决。
        if api is None and not detect.get("is_logged_in"):
            return {"status": "invalid", "user_info": None}

        user_info = self._get_user_info(detect.get("profile_url"))
        return {"status": "valid", "user_info": user_info}

    @staticmethod
    def _resolve_draft_only(draft_only: Optional[bool]) -> bool:
        """draft_only 为 None 时回落到 settings.PUBLISH_DRAFT_ONLY(默认 True=只存草稿)。"""
        if draft_only is not None:
            return bool(draft_only)
        try:
            from app.core.config import settings
            return bool(getattr(settings, "PUBLISH_DRAFT_ONLY", True))
        except Exception:
            return True

    def publish_note(
        self,
        title: str,
        content: str,
        image_paths: List[str],
        topics: Optional[List[str]] = None,
        draft_only: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """走 step1-6 录入内容;``draft_only`` 时**不点发布**只存草稿,否则 step7 真发布。

        step1 会打开新窗口并把内部 page 引用切到创作中心;这里发布结束后把
        ``self.page`` 同步到 atomic 的最终 page,供 stop() 正确收尾。

        draft_only(默认取 settings.PUBLISH_DRAFT_ONLY):内容录入后由小红书编辑器自动存
        草稿、不点发布,规避"点发布提交"这一刻的「人机发布」风控;用户到草稿箱手动真人发布。
        """
        atomic = XHSPublishAtomicTasks(self.page)
        try:
            logger.info(f"[SyncClient] 开始发布: {title} | 图片 {len(image_paths or [])} 张 | 话题 {len(topics or [])}")

            # step1 打开发布页(可能切新窗口 + SSO)
            r = atomic.step1_open_publish_page()
            self.page = atomic.page  # 同步新窗口引用
            if not r.get("success"):
                return {
                    "success": False,
                    "error": r.get("error"),
                    "need_manual_login": r.get("need_manual_login", False),
                }

            # step2 上传图片
            if image_paths:
                r = atomic.step2_upload_images(image_paths)
                if not r.get("success"):
                    # 与 step1 同源:透出 step2 SSO 失败的 need_manual_login,交状态机直接置
                    # failed 而非徒劳重试(否则该独立信号在此层被丢弃,I1 修复形同虚设)。
                    return {
                        "success": False,
                        "error": r.get("error"),
                        "need_manual_login": r.get("need_manual_login", False),
                    }
                logger.info(f"✓ 已上传 {r.get('uploaded_count')} 张图片")
            else:
                logger.info("跳过图片上传(无图片)")

            # step3 等待上传处理
            r = atomic.step3_wait_for_upload_processing(max_wait=30)
            if not r.get("success"):
                return {"success": False, "error": r.get("error")}
            edit_page_loaded = r.get("edit_page_loaded", False)

            # step4 进入编辑界面(若未自动进入)
            if not edit_page_loaded:
                r = atomic.step4_enter_edit_page()
                if not r.get("success"):
                    return {"success": False, "error": r.get("error")}

            # step5 填写标题正文
            r = atomic.step5_fill_content(title, content)
            if not r.get("success"):
                return {"success": False, "error": r.get("error")}

            # step6 话题(失败仅告警,不阻断发布)
            if topics:
                r6 = atomic.step6_set_publish_options(tags=topics)
                if not r6.get("success"):
                    logger.warning(f"步骤6警告: {r6.get('error')}")

            # —— 只存草稿模式:内容已录入(标题/图/正文/话题),**不点发布**——
            # 小红书编辑器会自动把当前内容存为草稿;稍等让自动存草稿落定,然后由 stop() 关页
            # 收尾。不触发"点发布提交",从根上规避「人机发布」检测;用户到草稿箱手动真人发布。
            if self._resolve_draft_only(draft_only):
                self.page = atomic.page
                atomic.human.wait(3.0, 5.0, context="等待编辑器自动存草稿")
                logger.info("📝 只存草稿:内容已录入,编辑器自动存草稿;跳过发布,待用户手动发布")
                return {
                    "success": True,
                    "draft_saved": True,
                    "note_url": "",
                    "note_id": "",
                    "message": "已录入全部内容并存为草稿,请到小红书 App/网页版草稿箱手动发布。",
                }

            # step7 点击发布并等待
            r = atomic.step7_click_publish_and_wait(max_wait=30)
            self.page = atomic.page
            if not r.get("success"):
                # 透出 step7 的账号禁发独立信号(命中禁发 toast),交状态机直接置 failed
                # 而非徒劳重试——重发反而是更强的高频封号信号。
                return {
                    "success": False,
                    "error": r.get("error"),
                    "account_restricted": r.get("account_restricted", False),
                }

            logger.info("🎉 发布成功")
            return {
                "success": True,
                "note_url": r.get("note_url", "") or "",
                "note_id": r.get("note_id", "") or "",
            }

        except Exception as e:
            logger.error(f"[SyncClient] 发布异常: {e}")
            return {"success": False, "error": f"发布笔记失败: {e}"}

    def stop(self) -> None:
        """关闭浏览器(page → context → playwright,逐层容错)。"""
        try:
            if self.page:
                try:
                    self.page.close()
                except Exception:
                    pass
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.playwright:
                self.playwright.stop()
                self.playwright = None
            logger.info("[SyncClient] 浏览器已关闭")
        except Exception as e:
            logger.warning(f"[SyncClient] 关闭浏览器出错: {e}")


# =============================================================================
# 对外入口(P3.5 依赖,同一线程内建 client → start → 操作 → stop)
# =============================================================================

def publish_once(
    account_id: int,
    cookies: List[Dict[str, Any]],
    title: str,
    content: str,
    image_paths: List[str],
    topics: Optional[List[str]] = None,
    draft_only: Optional[bool] = None,
) -> PublishResult:
    """一次性:建 client → start → 录入内容 →(draft_only 存草稿 / 否则 step7 真发)→ stop。

    供上层 ``asyncio.to_thread(publish_once, ...)`` 调用。``draft_only`` 为 None 时回落
    ``settings.PUBLISH_DRAFT_ONLY``(默认 True=只存草稿)。任何阶段失败都落到 ``PublishResult``。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return PublishResult(success=False, error=start.get("error"))

        result = client.publish_note(title, content, image_paths, topics, draft_only=draft_only)
        return PublishResult(
            success=bool(result.get("success")),
            note_id=result.get("note_id", "") or "",
            note_url=result.get("note_url", "") or "",
            error=result.get("error"),
            need_manual_login=bool(result.get("need_manual_login", False)),
            account_restricted=bool(result.get("account_restricted", False)),
            draft_saved=bool(result.get("draft_saved", False)),
            message=result.get("message", "") or "",
        )
    except Exception as e:
        logger.error(f"[publish_once] 异常 account_id={account_id}: {e}")
        return PublishResult(success=False, error=f"发布异常: {e}")
    finally:
        client.stop()


def check_login_once(account_id: int, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一次性登录检测:建 client → start → 登录/验证码判定 + 取 user_info → stop。

    返回 ``{status, user_info, reason?}``,status 四态:
      - ``valid`` / ``invalid`` / ``captcha``:来自 ``check_login()``,即"页面正常加载后"的
        真实登录判定(``invalid`` = 页面加载正常但未登录,cookie 真失效);
      - ``error``:浏览器基础设施失败(启动失败/页面超时/异常),带 ``reason`` 说明,**与 cookie
        失效严格区分**——调用方据此保留原状态,不把好号误标失效。

    登录检测纯只读,故 ``block_images=True`` 瘦身(拦图省内存,不影响登录判定)。
    """
    client = SyncClient(account_id, cookies, block_images=True)
    try:
        start = client.start()
        if not start.get("success"):
            reason = f"浏览器启动失败:{start.get('error')}"
            logger.warning(f"[check_login_once] {reason} account_id={account_id}")
            return {"status": "error", "user_info": None, "reason": reason}
        return client.check_login()
    except Exception as e:
        reason = f"浏览器异常:{e}"
        logger.error(f"[check_login_once] {reason} account_id={account_id}")
        return {"status": "error", "user_info": None, "reason": reason}
    finally:
        client.stop()
