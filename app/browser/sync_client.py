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
- ``check_login_once(account_id, cookies, probe_user_id=None) -> dict``
- ``PublishResult``:``{success, note_id, note_url, error, need_manual_login}``;
  返回契约**允许 success=True 但 note_id=""**(只有 note_url)。
"""
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.browser.atomic_tasks import XHSPublishAtomicTasks
from app.browser.fingerprint import get_fingerprint
from app.browser.login_detector import (
    DETECT_LOGIN_JS,
    GET_USER_INFO_JS,
    PAGE_TEXT_JS,
    WALL_UNKNOWN,
    classify_wall_text,
    is_wall_url,
)
from app.browser.note_components import (
    ComponentResponses,
    apply_components,
    apply_original_declaration,
    apply_video_cover,
)
from app.browser.podcast import select_podcast_collection
from app.browser.profile_guard import (
    clean_locks,
    delete_cookies_db,
    kill_orphans,
    profile_dir,
    sanitize_launch_options,
)
from app.browser.sync_human_actions import SyncHumanActions

def _build_ark_blackhole_pac() -> str:
    """构造把 ``ark.xiaohongshu.com`` 指向死代理、其余全 DIRECT 的 PAC data URL。

    千帆商家后台 ark 的带货权限探测对"曾绑过千帆"的号返 401,而创作页把任意 401 当成整体
    登录失效 → 跳 login → 编辑器被摧毁(详见 start() 处注释)。把该单域打进死代理,请求变成
    **网络错误**而非 401,前端就不跳登录。PAC 作用于浏览器全局代理层,SW/iframe/主线程全覆盖
    —— 这正是它不可替代的原因:实测 Playwright 的 page/context.route 都拦不到这些 ark 请求。
    """
    import base64

    pac = (
        "function FindProxyForURL(url, host){"
        " if (host == 'ark.xiaohongshu.com') return 'PROXY 127.0.0.1:1';"
        " return 'DIRECT'; }"
    )
    return ("data:application/x-ns-proxy-autoconfig;base64,"
            + base64.b64encode(pac.encode()).decode())


_ARK_BLACKHOLE_PAC_URL = _build_ark_blackhole_pac()


def _resolve_headed_display() -> Optional[Tuple[str, str]]:
    """动态解析本用户(roots)图形会话的 (DISPLAY, XAUTHORITY)。

    headed 浏览器要接到真屏(RTX 4090)会话做硬件渲染,而 gdm autologin 的图形会话
    display 号(:0/:1)与 auth 路径可能随会话重启/回落 greeter 而变——硬编码 systemd env
    会失效。故运行时扫本 uid 的 gnome-session/gnome-shell 进程,读其 /proc/<pid>/environ
    里的 DISPLAY+XAUTHORITY(排除 Xvfb :99 与 Wayland)。找到即返回,让浏览器始终跟随当前
    真实图形会话;找不到返回 None(回落调用方环境里的 DISPLAY)。
    """
    import glob
    uid = os.getuid()
    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            if os.stat(pid_dir).st_uid != uid:
                continue
            with open(pid_dir + "/comm", encoding="utf-8", errors="ignore") as f:
                comm = f.read().strip()
            # comm 被内核截断到 15 字符:gnome-session-binary → "gnome-session-b"
            if comm not in ("gnome-session-b", "gnome-shell"):
                continue
            env: Dict[str, str] = {}
            with open(pid_dir + "/environ", "rb") as f:
                for kv in f.read().split(b"\x00"):
                    if b"=" in kv:
                        k, _, v = kv.partition(b"=")
                        env[k.decode("utf-8", "ignore")] = v.decode("utf-8", "ignore")
            if env.get("WAYLAND_DISPLAY"):
                continue  # Wayland 会话不适用(需 X11 让 camoufox headed)
            disp = (env.get("DISPLAY") or "").strip()
            if disp and disp != ":99":  # 排除 Xvfb 虚拟屏
                xauth = (env.get("XAUTHORITY") or "").strip() or f"/run/user/{uid}/gdm/Xauthority"
                return (disp, xauth)
        except Exception:
            continue
    return None


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
    # 服务端**实际应用**了什么(话题逐个成败 + 三组件逐项结果)。回显给调用方:
    # "参数被静默丢弃"这类问题没有回显就只能事后人工抽查(2026-08-03 运营为此白删一篇)。
    applied: Optional[Dict[str, Any]] = None


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

    # 同名双份坑(实测 RCA 2026-07-25,三路真验活铁证,推翻 07-24 的错误修复):
    # 插件快照里同一 name 常同时存在 `.xiaohongshu.com`(域 cookie)与 `xiaohongshu.com`
    # (host-only)两份且**值不同**——但哪份是活凭据不能靠 domain 形态武断判定:
    # 实测 acc2/acc5 的活 web_session 恰在 **host-only** 那份(且其 expires 更晚)。
    # 07-24 曾武断"保留 .域 / 排除 host-only",把每个号真活 session 踢掉 → 全号 invalid。
    # 正解:同名归一到 `.xiaohongshu.com` 后按 **expires 更晚者胜**(cookie 每次 Set
    # 刷新过期,更晚过期=更近写入=活凭据),平局时 host-only 源胜(实测活值所在),
    # 确定性地让活值对 www/creator 生效,不靠列表顺序。
    def _norm_domain(raw: str) -> str:
        if "www.xiaohongshu.com" in raw:
            return ".xiaohongshu.com"
        return raw if raw.startswith(".") else "." + raw.lstrip(".")

    def _exp(c: Dict[str, Any]) -> float:
        v = c.get("expires")
        return float(v) if v and v > 0 else 0.0

    # 按 (name, 归一 domain) 选代表:expires 更晚胜,平局 host-only 源胜。
    chosen: dict[tuple[str, str], Dict[str, Any]] = {}
    order: List[tuple[str, str]] = []
    for cookie in cookies:
        name = cookie.get("name")
        if not name or "value" not in cookie:
            continue
        key = (name, _norm_domain(str(cookie.get("domain", ".xiaohongshu.com"))))
        cur = chosen.get(key)
        if cur is None:
            chosen[key] = cookie
            order.append(key)
            continue
        c_exp, cur_exp = _exp(cookie), _exp(cur)
        cur_domain = str(cur.get("domain", ""))
        is_hostonly = not str(cookie.get("domain", "")).startswith(".")
        cur_hostonly = not cur_domain.startswith(".")
        if c_exp > cur_exp or (c_exp == cur_exp and is_hostonly and not cur_hostonly):
            chosen[key] = cookie

    result: List[Dict[str, Any]] = []
    for name, domain in order:
        cookie = chosen[(name, domain)]
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
        exp = cookie.get("expires")
        if exp and exp > 0:
            entry["expires"] = exp
        result.append(entry)

        # creator 子域 fallback:以 url 方式补注入,确保创作中心能读到 cookie
        # (Camoufox/Firefox 对 domain 前缀点的子域匹配不可靠)。
        if domain == ".xiaohongshu.com":
            creator = {
                "name": name,
                "value": cookie["value"],
                "url": "https://creator.xiaohongshu.com/",
                "httpOnly": cookie.get("httpOnly", False),
                "secure": cookie.get("secure", True),
                "sameSite": same_site,
            }
            if exp and exp > 0:
                creator["expires"] = exp
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

            # headed 模式:动态把 DISPLAY/XAUTHORITY 指向 roots 当前真实图形会话(真屏 4090),
            # 不依赖 systemd 硬编码——会话 display 号/auth 变动或回落 greeter 时自动跟随/告警。
            if not self.headless:
                resolved = _resolve_headed_display()
                if resolved:
                    os.environ["DISPLAY"], os.environ["XAUTHORITY"] = resolved
                    logger.info(
                        f"[SyncClient] headed 接真屏图形会话 DISPLAY={resolved[0]} "
                        f"XAUTHORITY={resolved[1]}"
                    )
                else:
                    logger.warning(
                        "[SyncClient] 未找到 roots 真实图形会话(可能掉回 gdm greeter);"
                        f"沿用环境 DISPLAY={os.environ.get('DISPLAY', '?')}——"
                        "若是 :99/greeter,headed 接 4090 会失败,请检查 autologin 会话"
                    )

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
                # 【关掉 camoufox 原生 humanize】它会把**每一次** page.mouse.move 平滑动画化
                # (最多 1.5s/次),与 SyncHumanActions 的多步贝塞尔叠乘 → 单次点击要 14×1.7s≈24s
                # 拖拉(实测 humanize=False 单步 move 1.7s→0.017s)。拟人化统一由 SyncHumanActions
                # 一层负责(贝塞尔轨迹+变速+微颤+犹豫),这里不再重复humanize。
                humanize=False,
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
                # 【恢复 ark PAC 死代理】07-21 曾因"会崩 driver"弃用,但那个崩溃(ark 网络错误
                # 冒泡成 location 为空的 pageerror → Playwright Firefox driver 读 location.url
                # 崩)后来已被下方 add_init_script 的全局错误吞噬根治,前提不再成立;而弃用时
                # 认定的替代方案("sing-box 直连后 ark 就返 200")经 2026-07-25 实测**不成立**:
                # camoufox 27/27 走 direct-out、出口北京联通,ark 仍稳定 401。
                # 机理:编辑器一打开,创作页就去问千帆商家后台 ark 的带货权限,曾绑过千帆的号
                # (NBDpsy聊心理 / NBDpsy 官号)收到 401,而创作页把**任意 401 当整体登录失效**,
                # 0.6s 内跳 login?redirectReason=401 → 编辑器连同已上传的图一起没了,只剩草稿。
                # 没绑过千帆的号(NBDpsy-聊创伤)不触发该探测,故一直正常。
                # PAC 把 ark 单域指向死代理,让它变成**网络错误而非 401**,前端遂不跳登录;其余
                # 全 DIRECT(camoufox 出网本就由 sing-box process_path 规则直连,不受影响)。
                # 实测(严格测法:上传后连续观察 18s 再复检):聊心理 由"+2.0s 被踢、编辑器丢失"
                # 转为"不被踢、20s 后编辑器在、12 张缩略图",与健康号表现一致。
                firefox_user_prefs={
                    "permissions.default.geo": 2,   # 2=拒绝:不弹"允许获取位置"授权框
                    "geo.enabled": False,
                    "network.proxy.type": 2,        # 2=用 PAC(autoconfig)
                    "network.proxy.autoconfig_url": _ARK_BLACKHOLE_PAC_URL,
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
            # 条件等待登录态渲染(命中即走,最多 3s),替代固定 sleep(3)。命中=真登录(安全);
            # invalid 号轮询满 3s 退化到原状,不比原来差。SPA 渲染好后 detect 才准。
            detect = {}
            _deadline = time.monotonic() + 3.0
            while time.monotonic() < _deadline:
                detect = self._detect_login()
                if detect.get("is_logged_in"):
                    break
                time.sleep(0.3)
            self._last_detect = detect
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

    def _current_wall(self, target_url: str) -> Dict[str, Any]:
        """按当前页构造一份风控墙取证 dict(纯只读,不做任何交互)。

        ``target_url`` 记的是**当时想访问什么**(被重定向前的目标),``landed_url`` 是实际
        落到的墙 URL —— 排查时这两个一起看才知道"哪类操作会撞墙"。
        """
        landed = ""
        text = ""
        try:
            landed = self.page.url or ""
        except Exception:
            pass
        try:
            text = self.page.evaluate(PAGE_TEXT_JS) or ""
        except Exception:
            pass  # 取证失败不影响判定,URL 才是硬判据
        return {
            "wall_type": classify_wall_text(text),
            "target_url": target_url,
            "landed_url": landed,
            "page_text": text,
        }

    def _probe_peer_profile(self, probe_user_id: str) -> Optional[Dict[str, Any]]:
        """访问一个**他人主页**探风控墙;撞墙返回取证 dict,正常/探测失败返回 None。

        为什么非探他人主页不可:验证墙只在访问他人主页时弹,首页与自己主页照常渲染
        (2026-07-31 NBDpsy-聊创伤 实测)——只看首页登录标志的旧判定必然漏,号被当成
        好号继续派互动/导出任务,任务全败,人也据此做了错误决策。

        只做**一次导航**,不滚动、不点击、不抓列表:反复起会话/多加请求本身就会把号
        打成限流(同号后来文案从「扫码验证身份」变成「请求太频繁」正是这么来的)。
        探测异常一律返回 None(不改判定):探不出来只退化回原判定,绝不能因为探测失败
        把好号误标风控。
        """
        target = f"https://www.xiaohongshu.com/user/profile/{probe_user_id}"
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            wall = self._current_wall(target)
            if not is_wall_url(wall["landed_url"]):
                return None
            logger.warning(
                f"[SyncClient] 账号 {self.account_id} 撞风控墙 type={wall['wall_type']} "
                f"landed={wall['landed_url']} text={wall['page_text'][:60]!r}"
            )
            return wall
        except Exception as e:
            logger.warning(f"[SyncClient] 他人主页可达性探测失败(忽略,不改判定): {e}")
            return None

    def _probe_wall(
        self, probe_user_id: Optional[str], user_info: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """无探测目标 / 目标就是本号 → 跳过探测返回 None,不报错。

        自己主页不弹墙,拿本号 user_id 去探等于白花一次导航,故显式短路。
        """
        pid = (probe_user_id or "").strip()
        if not pid:
            return None
        own = ((user_info or {}).get("user_id") or "").strip()
        if own and pid == own:
            return None
        return self._probe_peer_profile(pid)

    def _get_user_info(self, profile_url: Optional[str]) -> Optional[Dict[str, Any]]:
        """导航到个人主页(用登录检测提取的 profile_url)抓取昵称/小红书号等。

        不用 ``/user/profile/me`` —— 该路径触发小红书风控强制扫码。无 profile_url → None。
        """
        if not profile_url:
            return None
        try:
            self.page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            # 条件等待:主页渲染出昵称即走(最多 2s),替代固定 sleep(2)
            info = None
            _deadline = time.monotonic() + 2.0
            while time.monotonic() < _deadline:
                info = self.page.evaluate(GET_USER_INFO_JS)
                if info and info.get("nickname"):
                    break
                time.sleep(0.3)
            return info
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

    def check_login(self, probe_user_id: Optional[str] = None) -> Dict[str, Any]:
        """检查登录态,返回 ``{status, user_info, wall?}``。

        status: 'valid'(已登录,附 user_info)| 'invalid'(未登录)| 'captcha'(验证码拦截)
        | 'restricted'(cookie 有效但账号被挂风控验证墙,附 ``wall`` 取证)。

        判定优先级：官方 API 地面真值 > DOM 启发式。API 明确过期 → 直接 invalid；
        API 明确登录 → valid；API 不可达才回落到原 DOM 启发式(避免 API 抖动误杀好号)。

        ``probe_user_id`` 给定时,在判定为 valid 后**再加一次**他人主页导航探风控墙
        (见 ``_probe_peer_profile``);不给或探测失败则退化为原来的首页判定。
        """
        if self._is_captcha():
            return {
                "status": "captcha",
                "user_info": None,
                "wall": self._current_wall("https://www.xiaohongshu.com/explore"),
            }

        api = self._api_login_status()
        if api is False:
            return {"status": "invalid", "user_info": None}

        detect = self._last_detect or self._detect_login()
        # API 不可达时才用 DOM 结论把关；API 明确登录(True)则不被 DOM 假阴性否决。
        if api is None and not detect.get("is_logged_in"):
            return {"status": "invalid", "user_info": None}

        user_info = self._get_user_info(detect.get("profile_url"))
        # cookie 本身有效,但账号可能被挂验证墙:探他人主页可达性。撞墙 → restricted,
        # 与 invalid 严格区分(cookie 没坏,是账号被风控,运营动作是扫码而非重新登录)。
        wall = self._probe_wall(probe_user_id, user_info)
        if wall is not None:
            return {"status": "restricted", "user_info": user_info, "wall": wall}
        return {"status": "valid", "user_info": user_info}

    def publish_note(
        self,
        title: str,
        content: str,
        image_paths: List[str],
        topics: Optional[List[str]] = None,
        components: Optional[Dict[str, Any]] = None,
        job_tag: Optional[str] = None,
        video_path: Optional[str] = None,
        cover_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        podcast_collection: Optional[str] = None,
    ) -> Dict[str, Any]:
        """走 step1-6 录入内容 + 三组件(可选)+ step7 真发布。

        媒体三选一,**判型优先级 audio → video → 图文**(与 REST/scheduler/worker 全链统一):

        - ``audio_path``:**播客笔记**,媒体段走 step2a(切发播客 tab + 灌音频)+ step3a
          (等上传完成并点「去发布」);``cover_path`` 此时是音频封面(≤32MB),
          ``podcast_collection`` 是要加入的播客合集**名称**。
        - ``video_path``:**视频笔记**,媒体段走 step2v(灌视频)+ step3v(等上传+转码);
          ``cover_path`` 是自定义封面,不给就用平台自动截取的第一帧。
        - 都不给:一行不变走图文老路(step2/3/4)。

        三者互斥由入口 POST /api/publish-jobs 钉死,这里只按有无路由。封面与播客合集
        失败均**只告警不阻断发布**(语义对齐三组件)。

        ⚠️ 播客那条分支的取证覆盖度低于图文/视频:tab 已取证,音频上传弹窗内部与
        「去发布」之后的发布表单未取证 —— 相关步骤写成 fail-loud(见 atomic_tasks
        step2a/step3a 的注释),且**尚未跑过真号 e2e**。

        ``job_tag``:发布任务 id,只用来给失败现场截图打标,让运营能按 job 取回
        (见 ``XHSPublishAtomicTasks._take_screenshot``);不传则行为与上线前一致。

        step1 会打开新窗口并把内部 page 引用切到创作中心;这里发布结束后把
        ``self.page`` 同步到 atomic 的最终 page,供 stop() 正确收尾。

        ``components``:``{"collection_id","quoted_note_id","activity_id"}``,值全空即
        完全跳过组件那一步(行为与本功能上线前逐字节一致)。设置在 step6 之后、step7 之前
        (设计 3.1),失败**只告警不阻断发布** —— 图都传完了,为一个辅助组件把整篇笔记
        废掉不划算;逐项结果记在返回值的 ``components`` 里。

        (历史:曾有"只存草稿"模式规避点发布时刻的人机检测——已删除:网页版草稿只存在
        服务器浏览器本地,用户手机/其它设备看不到,毫无交付价值;且拟人化链路多轮真发
        验证后该保险已无必要。)
        """
        atomic = XHSPublishAtomicTasks(self.page, job_tag=job_tag)
        # 三组件要用到编辑器加载时页面自己发的活动列表响应,故监听必须在 step1 之后、
        # 编辑器加载之前就挂上(响应过期了就读不回来了)。不设组件时一个监听都不挂。
        responses = None
        try:
            if audio_path:
                media_desc = f"播客音频 {audio_path}"
            elif video_path:
                media_desc = f"视频 {video_path}"
            else:
                media_desc = f"图片 {len(image_paths or [])} 张"
            logger.info(f"[SyncClient] 开始发布: {title} | {media_desc} | 话题 {len(topics or [])}")

            # step1 打开发布页(可能切新窗口 + SSO)
            r = atomic.step1_open_publish_page()
            self.page = atomic.page  # 同步新窗口引用
            if not r.get("success"):
                return {
                    "success": False,
                    "error": r.get("error"),
                    "need_manual_login": r.get("need_manual_login", False),
                }
            if components and any(components.values()):
                responses = ComponentResponses()
                responses.attach(self.page)

            audio_cover_result = None
            if audio_path:
                # ── 播客分支:step2a 切 tab + 灌音频(+封面)→ step3a 等上传完成并点「去发布」──
                # 与视频分支的差别:发布页默认落地是「上传视频」tab,播客**必须先切 tab**;
                # 上传是弹窗里做的,且「去发布」是一次显式点击(视频传完直接就在编辑器里)。
                r = atomic.step2a_upload_audio(audio_path, cover_path)
                if not r.get("success"):
                    return {"success": False, "error": r.get("error")}
                audio_cover_result = r.get("audio_cover")
                r = atomic.step3a_wait_for_audio_upload(audio_path=audio_path)
                if not r.get("success"):
                    return {"success": False, "error": r.get("error")}
                logger.info(f"✓ 音频已上传并进入发布表单(等待 {r.get('wait_time')}s)")
                # 与视频同源的保险:发布表单刚渲染时上方区域高度还在变,标题框 rect 会漂,
                # 而 step5 是按坐标做拟人点击的 —— 坐标落在遮挡物上打出去的字进不了输入框
                # **且不会报错**。播客表单结构未取证,这道校验是唯一能挡住"打错地方"的闸。
                if not atomic.ensure_editor_interactable():
                    return {
                        "success": False,
                        "error": "音频已就绪但发布表单不可交互(标题框在视口外/被遮挡/"
                                 "播客表单结构与图文不同源),为免把正文打到别处,不继续发布",
                    }
            elif video_path:
                # ── 视频分支:step2v 灌视频 → step3v 等上传+转码 ──
                # 发布页默认落地就是「上传视频」tab,不需要图文那段切 tab;
                # 视频传完直接就在编辑器里,也没有「继续编辑」那一步,故无 step4v。
                r = atomic.step2v_upload_video(video_path)
                if not r.get("success"):
                    return {"success": False, "error": r.get("error")}
                r = atomic.step3v_wait_for_video_processing(video_path=video_path)
                if not r.get("success"):
                    return {"success": False, "error": r.get("error")}
                logger.info(f"✓ 视频已上传并转码完成(等待 {r.get('wait_time')}s)")
                # 编辑区可交互校验:封面「智能推荐生成中」是独立异步任务,期间上方区域高度
                # 还在变,标题框 rect 一路漂移。step5 是按坐标做拟人点击的,坐标落在遮挡物上
                # 打出去的字进不了输入框**且不会报错**,所以必须在这里先把它挡住。
                if not atomic.ensure_editor_interactable():
                    return {
                        "success": False,
                        "error": "视频已就绪但编辑区不可交互(标题框在视口外或被遮挡),"
                                 "为免把正文打到别处,不继续发布",
                    }
            else:
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

            # step6 话题(失败仅告警,不阻断发布;逐话题成败要带回给调用方回显——
            # 参数被静默丢弃时运营当场可见,不用等笔记发出去人工读正文才发现)
            r6 = {}
            if topics:
                r6 = atomic.step6_set_publish_options(tags=topics)
                if not r6.get("success"):
                    # 逐话题的失败原因(content_box_focus_failed / topic_dropdown_not_shown / …)
                    # 一并打出来:只丢一句 error 的话,运营看到的永远是"话题没上",
                    # 分不清是正文框没聚焦还是这个话题在平台上根本不存在。
                    logger.warning(
                        f"步骤6警告: {r6.get('error')} | 逐话题失败明细: "
                        f"{r6.get('topics_failed')}"
                    )

            # step6.4 视频封面(仅视频 + 传了 cover_path 才跑):必须排在 step3v 转码完成
            # 之后(封面 UI 在上传未完成时不可交互),又必须赶在发布门之前。
            # 失败**只告警不阻断发布** —— 平台自动截取的第一帧就是兜底,不值得为它废掉
            # 一条已经传完转好的视频。
            cover_result = None
            if video_path and cover_path:
                try:
                    cover_result = apply_video_cover(
                        atomic.page, SyncHumanActions(atomic.page), cover_path
                    )
                except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断发布
                    cover_result = {"status": "error", "reason": f"cover_exception: {exc}"}
                if cover_result.get("status") == "error":
                    logger.warning(
                        f"[SyncClient] 封面未设上(不阻断发布,退回平台自动封面): "
                        f"{cover_result.get('reason')} | 取证: {cover_result.get('observed')}"
                    )

            # step6.45 播客合集(仅播客 + 传了名称才跑):按名称在发布表单里选中。
            # ⚠️ **控件形态完全未取证**(E4:「去发布」之后的表单从未到达),故走
            # fail-loud 的 select_podcast_collection —— 找不到就带当场取证报 error,
            # 绝不静默假装选上了。失败**只告警不阻断发布**(笔记照发,只是不进合集)。
            podcast_collection_result = None
            if audio_path and podcast_collection:
                try:
                    podcast_collection_result = select_podcast_collection(
                        atomic.page, SyncHumanActions(atomic.page), podcast_collection
                    )
                except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断发布
                    podcast_collection_result = {
                        "status": "error", "reason": f"podcast_collection_exception: {exc}"}
                if podcast_collection_result.get("status") == "error":
                    logger.warning(
                        f"[SyncClient] 播客合集未选上(不阻断发布,笔记照发但不进合集): "
                        f"{podcast_collection_result.get('reason')} | "
                        f"取证: {podcast_collection_result.get('observed')}"
                    )

            # step6.5 三组件(设计 3.1:step6 之后、step7 之前);失败仅告警,不阻断发布
            # 播客任务的 components 恒为空:collection_id 那一列在播客上存的是**合集名称**,
            # 已由上面的 step6.45 消费,绝不能再喂给按 hex id 找笔记合集的 apply_components。
            component_result = (
                {} if audio_path
                else self._apply_components(atomic, responses, components)
            )

            # step6.6 原创声明:**每次发布无条件打开**(运营裁定 2026-08-05);
            # 失败仅告警不阻断——辅助声明不值得废掉一篇图都传完的笔记。
            component_result = dict(component_result or {})
            if cover_result is not None:
                component_result["cover"] = cover_result
            if audio_cover_result is not None:
                # 播客的封面在 step2a 弹窗里就设了(与视频封面在编辑器里设不同),
                # 但回显仍归一到 components.cover —— 调用方不该为媒体类型换键名。
                component_result["cover"] = audio_cover_result
            if podcast_collection_result is not None:
                component_result["podcast_collection"] = podcast_collection_result
            try:
                # **两条路径都走协议弹窗链**(勾同意 → 等「声明原创」解禁 → 点它 → 回读)。
                # 图文原本走的是"见弹窗就 X 关掉 + 回读 checked",探针(account10 发布页)
                # 与生产数据双实锤该序列**从未真正声明成功过**:08-05 上线至今全部 published
                # 任务的 original_declaration 都是 error「original_not_applied」。
                # 一个从未工作过的功能没有"保持不变"的保护价值,故一并切过来;
                # 页面若不弹协议弹窗,链路内部自动回退老判据,不误伤。
                component_result["original_declaration"] = apply_original_declaration(
                    atomic.page, SyncHumanActions(atomic.page),
                    handle_consent_modal=True,
                )
            except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断发布
                component_result["original_declaration"] = {
                    "status": "error", "reason": f"original_exception: {exc}"}
            if component_result["original_declaration"].get("status") == "error":
                logger.warning(
                    f"[SyncClient] 原创声明未开成(不阻断发布): "
                    f"{component_result['original_declaration'].get('reason')}"
                )

            # step6.7 视频专属:等发布按钮真的可点再进 step7。
            # <xhs-publish-btn> 刚进编辑器时 submit-disabled="true"(真号夹具实测),
            # 何时翻转没有实测结论。点一个禁用按钮永远不可能发布成功,只会换来一句
            # 「发布超时(30秒)」——图文那边 2026-08-02 就栽在这。故这里等结果、
            # 超时带当场属性快照收口,绝不硬着头皮往下点。
            # 播客同样走这道门:表单结构未取证,更不能盲点。门不通就带取证收口 ——
            # 若真号跑出来 observed 是 ``{"found": false}``,说明播客发布页用的不是
            # <xhs-publish-btn>,那时把判据换成实测到的形态即可,控制流不必动。
            if video_path or audio_path:
                gate = atomic.wait_for_submit_enabled()
                if not gate.get("ready"):
                    return {
                        "success": False,
                        "error": (
                            "发布按钮始终不可点(submit-disabled 未翻转),当场取证:"
                            f"{gate.get('observed')}"
                        ),
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
                "components": component_result,
                "topics_applied": r6.get("topics_applied", []),
                "topics_failed": r6.get("topics_failed", []),
            }

        except Exception as e:
            logger.error(f"[SyncClient] 发布异常: {e}")
            return {"success": False, "error": f"发布笔记失败: {e}"}
        finally:
            if responses is not None:
                responses.detach()

    def _apply_components(self, atomic, responses, components) -> Dict[str, Any]:
        """发布链路里设置三组件(step6 与 step7 之间);**失败只告警不阻断发布**。

        与编辑已发布笔记的区别:这里没有"提交后重进页面回读"那一步 —— 新笔记要等发布拿到
        note_id 才能回读,而那时窗口已经关了。故这里的 done 只是**编辑器内**回读确认
        (合集区显示了名字 / 引用区出现了标题 / 活动按钮翻转成「取消关联」)。它足以逮住
        实测的静默失效(活动首次点击不生效),但逮不住服务端静默丢弃(私密笔记的合集绑定)
        —— 后者要事后调 POST /api/accounts/{id}/note-components 或人工核对。
        """
        if responses is None or not components or not any(components.values()):
            return {}
        try:
            # 用 atomic.page 而非 self.page:窗口引用以 atomic 为准(step1 会换窗)
            page = atomic.page
            human = SyncHumanActions(page)
            outcomes = apply_components(
                page, human, responses,
                collection_id=components.get("collection_id"),
                quoted_note_id=components.get("quoted_note_id"),
                activity_id=components.get("activity_id"),
            )
        except Exception as exc:  # noqa: BLE001 — 组件失败绝不阻断发布
            logger.warning(f"[SyncClient] 三组件设置异常(不阻断发布): {exc}")
            return {"error": f"components_exception: {exc}"}
        failed = [k for k, v in outcomes.items() if v["status"] == "error"]
        if failed:
            logger.warning(
                f"[SyncClient] 三组件部分未设上(不阻断发布): "
                f"{[(k, outcomes[k].get('reason')) for k in failed]}"
            )
        return outcomes

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
    components: Optional[Dict[str, Any]] = None,
    job_tag: Optional[str] = None,
    video_path: Optional[str] = None,
    cover_path: Optional[str] = None,
    audio_path: Optional[str] = None,
    podcast_collection: Optional[str] = None,
) -> PublishResult:
    """一次性:建 client → start → 录入内容 → 三组件(可选)→ step7 真发布 → stop。

    供上层 ``asyncio.to_thread(publish_once, ...)`` 调用。任何阶段失败都落到 ``PublishResult``。
    ``components`` 为 None / 全空时完全跳过组件那一步(默认值,行为不变)。
    ``video_path`` 给了就发**视频笔记**、``audio_path`` 给了就发**播客笔记**(两种情况下
    ``image_paths`` 都应为空列表);``cover_path`` 是二者的自定义封面(可空);
    ``podcast_collection`` 仅播客用,是要加入的合集**名称**。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return PublishResult(success=False, error=start.get("error"))

        result = client.publish_note(
            title, content, image_paths, topics, components,
            job_tag=job_tag, video_path=video_path, cover_path=cover_path,
            audio_path=audio_path, podcast_collection=podcast_collection,
        )
        return PublishResult(
            success=bool(result.get("success")),
            note_id=result.get("note_id", "") or "",
            note_url=result.get("note_url", "") or "",
            error=result.get("error"),
            need_manual_login=bool(result.get("need_manual_login", False)),
            account_restricted=bool(result.get("account_restricted", False)),
            applied={
                "topics_requested": list(topics or []),
                "topics_applied": result.get("topics_applied", []),
                "topics_failed": result.get("topics_failed", []),
                "components": result.get("components") or {},
            } if result.get("success") else None,
        )
    except Exception as e:
        logger.error(f"[publish_once] 异常 account_id={account_id}: {e}")
        return PublishResult(success=False, error=f"发布异常: {e}")
    finally:
        client.stop()


def check_login_once(
    account_id: int,
    cookies: List[Dict[str, Any]],
    probe_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """一次性登录检测:建 client → start → 登录/验证码判定 + 取 user_info → stop。

    返回 ``{status, user_info, reason?, wall?}``,status 五态:
      - ``valid`` / ``invalid`` / ``captcha`` / ``restricted``:来自 ``check_login()``,即
        "页面正常加载后"的真实判定(``invalid`` = 页面加载正常但未登录,cookie 真失效;
        ``restricted`` = cookie 有效但账号被挂风控验证墙,附 ``wall`` 取证);
      - ``error``:浏览器基础设施失败(启动失败/页面超时/异常),带 ``reason`` 说明,**与 cookie
        失效严格区分**——调用方据此保留原状态,不把好号误标失效。

    ``probe_user_id``:他人主页探测目标(矩阵内另一个账号的 user_id),由调用方从库里挑;
    为 None 则跳过风控墙探测,退化为原来的首页判定。

    登录检测纯只读,故 ``block_images=True`` 瘦身(拦图省内存,不影响登录判定)。
    """
    client = SyncClient(account_id, cookies, block_images=True)
    try:
        start = client.start()
        if not start.get("success"):
            reason = f"浏览器启动失败:{start.get('error')}"
            logger.warning(f"[check_login_once] {reason} account_id={account_id}")
            return {"status": "error", "user_info": None, "reason": reason}
        return client.check_login(probe_user_id)
    except Exception as e:
        reason = f"浏览器异常:{e}"
        logger.error(f"[check_login_once] {reason} account_id={account_id}")
        return {"status": "error", "user_info": None, "reason": reason}
    finally:
        client.stop()
