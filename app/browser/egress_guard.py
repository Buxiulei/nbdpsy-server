"""出口链路自检:防「代理重装/更新后 camoufox 直连规则丢失」静默复发。

**为什么需要**(2026-07-25/26 两次 401 踢登录的教训):发布链路"上传后编辑器消失"
有两个**症状相同、病因不同**的来源——

1. **ark 商家权限 401**:创作页拿任意 401 当登录失效。修法是 Firefox PAC 把
   ``ark.xiaohongshu.com`` 打进死代理(见 ``sync_client._build_ark_blackhole_pac``)。
   这层**结构性自愈**:PAC 由代码每次启动现生成注入,不落盘、无状态可漂移。
2. **出口 IP 异常 401**:camoufox 流量若走了 tun 代理(出口非国内),小红书风控判风险
   直接踢登录。修法是 sing-box 里那条 ``process_path=camoufox-bin → direct-out`` 规则。
   这层**不自愈**——它是磁盘上的配置文件,代理软件更新/卸载重装会把它覆盖掉。

规则一丢,症状与 ark-401 一模一样,极易误判成"PAC 失效了"回头空查一遍(实测浪费过一晚)。
故本模块做两级自检,让规则丢失当天就暴露,而不是等发布失败才回头刨:

- **配置层(便宜)**:读 sing-box 配置,确认 camoufox 直连规则仍在。纯文件读,零成本。
- **链路层(真实)**:起一个 camoufox 实测出口地区。配置里有规则 ≠ 规则真生效
  (进程路径变了、服务没重载都会让它形同虚设),只有真跑一次才作数。

链路层用**空 cookie** 的一次性会话(不带任何账号身份、不登录、不碰发布链路),
对账号零风险;每 ``EGRESS_CHECK_INTERVAL`` 秒一次,默认 6h,开销可忽略。
"""

import asyncio
import json
from pathlib import Path

from loguru import logger

from app.core.config import settings

# camoufox 直连规则的判定关键词:配置里出现其一即认为规则在位
_RULE_MARKERS = ("camoufox-bin", "camoufox")
# 出口地区探测页(返回中文地区串,如「当前 IP:x  来自于:中国 北京 联通」)
_EGRESS_PROBE_URL = "https://myip.ipip.net"
# 期望出口地区关键词:命中即视为直连正常
_EXPECTED_REGION = "中国"
# 首检延迟(秒):**不在 start() 瞬间探测**。理由有二——①每次 worker 重启都立刻拉起一个
# camoufox 纯属浪费(开发期频繁重启会白起十几次);②启动瞬间探测会在测试里真的拉起
# playwright 进程,污染 Supervisor 相关用例(实测打红 test_request_stop_halts_dispatch)。
# 60s 足够避开这两者,又能在代理重装+重启后一分钟内就给出信号。
_FIRST_DELAY_S = 60.0


def check_singbox_rule(config_path: str | None = None) -> tuple[bool, str]:
    """检查 sing-box 配置里 camoufox 直连规则是否在位。返回 ``(ok, 说明)``。

    纯文件读 + 结构解析,不依赖网络与浏览器,可直接单测。配置读不到(路径不存在/无权限)
    时返回 ok=True + 说明——**未知不等于故障**,不能因为读不到配置就天天误报。
    """
    path = Path(config_path or settings.SINGBOX_CONFIG_PATH)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return True, f"跳过(配置不可读: {exc})"
    try:
        rules = (json.loads(raw).get("route") or {}).get("rules") or []
    except Exception:  # noqa: BLE001 — 配置非法 JSON 时退回子串判定,不误报
        return (any(m in raw for m in _RULE_MARKERS),
                "配置非 JSON,按子串判定")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("outbound") != "direct-out":
            continue
        targets = []
        for key in ("process_path", "process_name"):
            val = rule.get(key)
            if isinstance(val, str):
                targets.append(val)
            elif isinstance(val, list):
                targets.extend(str(v) for v in val)
        if any(m in t for t in targets for m in _RULE_MARKERS):
            return True, "规则在位"
    return False, "sing-box 里已无 camoufox → direct-out 规则(代理重装覆盖?)"


def _probe_egress_sync() -> str:
    """起一次性 camoufox(空 cookie、不登录)读出口地区文本;失败返回空串。

    必须用 camoufox 而非 httpx——sing-box 按**进程路径**分流,只有 camoufox 自己
    发出的连接才走那条直连规则,宿主进程测出来的是 tun 出口(无参考价值)。
    """
    from app.browser.sync_client import SyncClient

    client = SyncClient(0, [], block_images=True)   # account_id=0:专用探测 profile,不占真账号
    try:
        client.playwright = None
        start = client.start()
        if not start.get("success"):
            return ""
        client.page.goto(_EGRESS_PROBE_URL, wait_until="domcontentloaded", timeout=30000)
        return (client.page.inner_text("body") or "").strip()[:200]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[egress_guard] 出口探测异常(不告警,下轮再试): {exc}")
        return ""
    finally:
        try:
            client.stop()
        except Exception:  # noqa: BLE001
            pass


class EgressGuard:
    """周期自检 camoufox 出口链路;发现规则丢失/出口异常即 ERROR 告警。

    结构套本仓既有组件模板(CookieChecker / NoteMetricsScheduler)。
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        # 先睡 _FIRST_DELAY_S 再首检(不在 start() 瞬间拉浏览器,理由见常量注释);
        # 期间收到停止信号则一次都不探测,直接退出。
        await self._sleep(min(_FIRST_DELAY_S, self._interval))
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.check_once()
            except Exception:
                logger.exception("[egress_guard] 自检轮次异常")
            await self._sleep(self._interval)

    async def check_once(self) -> dict:
        """跑一轮两级自检;返回 ``{rule_ok, egress_ok, detail}``。异常不外抛。"""
        rule_ok, rule_msg = check_singbox_rule()
        if not rule_ok:
            logger.error(
                f"[egress_guard] ⚠️ camoufox 直连规则丢失:{rule_msg}。"
                f"camoufox 将走 tun 出国 → 小红书风控 401 踢登录(症状与 ark-401 相同,"
                f"勿误判为 PAC 失效)。修法:把 process_path 直连规则加回 "
                f"{settings.SINGBOX_CONFIG_PATH} 后 systemctl restart bui-tun.service")

        text = await asyncio.to_thread(_probe_egress_sync)
        if not text:
            logger.info(f"[egress_guard] 出口探测未取到结果(网络/浏览器异常),本轮跳过判定")
            return {"rule_ok": rule_ok, "egress_ok": None, "detail": rule_msg}

        egress_ok = _EXPECTED_REGION in text
        if egress_ok:
            logger.info(f"[egress_guard] 出口正常:{text[:60]}")
        else:
            logger.error(
                f"[egress_guard] ⚠️ camoufox 出口非国内:{text[:80]}。"
                f"发布会被小红书风控 401 踢登录。先查 {settings.SINGBOX_CONFIG_PATH} 的 "
                f"camoufox process_path 直连规则,再 systemctl restart bui-tun.service")
        return {"rule_ok": rule_ok, "egress_ok": egress_ok, "detail": text[:80]}

    def _is_stopping(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, timeout: float) -> None:
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
