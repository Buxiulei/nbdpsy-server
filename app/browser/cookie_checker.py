"""可选的后台 cookie 巡检循环:周期性对 valid / unknown 账号跑登录检测并写回状态。

lifespan 仅在 ``settings.COOKIE_CHECK_INTERVAL > 0`` 时启一个 ``CookieChecker`` 后台
协程;默认 0(测试环境亦默认 0)**不起**该循环,故单测/CI 完全不受影响。号间隔
``account_gap`` 秒(默认 5s)防频控。

设计对齐 ``PublishScheduler``:注入 ``session_factory``、``stop_event`` + 后台 task、
优雅 ``stop``。``check_login_once`` 的阻塞浏览器调用经 ``asyncio.to_thread`` 下沉到线程,
不卡事件循环。巡检无 operator 上下文(系统级任务),直接解密 cookie 不走 access 鉴权。
"""

import asyncio
import json
from datetime import datetime

from loguru import logger
from sqlalchemy import and_, or_, select

from app.browser import sync_client
from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import risk_events
from app.services.cookie_check import (
    _run_check_with_watchdog,
    kick_onboarding_chain,
    pick_probe_user_id,
)

# check_login_once 返回 user_info 时回填到账号的字段(与 cookies 工具一致的子集)
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")


def _decrypt_account_cookies(account: XhsAccount | None) -> list[dict]:
    """解密账号 login_cookies 回列表;后台巡检无 operator 上下文,不走 access 鉴权。空 → []。"""
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)


class CookieChecker:
    """周期 cookie 巡检:每 ``interval`` 秒对该巡的账号(见 ``_list_patrol_account_ids``)
    逐个检测并写回。

    ``account_gap`` 为号间隔(默认 5s)防频控;``stop()`` 优雅取消(可打断 interval/gap 休眠)。
    """

    def __init__(
        self,
        session_factory,
        interval: float,
        account_gap: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._account_gap = account_gap
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台巡检循环(每 poll 周期跑一轮 check_once)。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台循环:每 interval 秒跑一轮巡检,单轮异常不打断循环。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.check_once()
            except Exception:
                logger.exception("cookie 巡检轮次异常")
            await self._sleep(self._interval)

    async def check_once(self) -> int:
        """跑一轮:取该巡的账号逐个检测并写回;返回实际检测的账号数。

        号与号之间隔 ``account_gap`` 秒防频控(首个号不等);运行中收到停止信号即提前退出。
        """
        account_ids = await self._list_patrol_account_ids()
        checked = 0
        for index, account_id in enumerate(account_ids):
            if self._is_stopping():
                break
            if index > 0:
                await self._sleep(self._account_gap)  # 号间隔防频控
                if self._is_stopping():
                    break
            if await self._check_account(account_id):
                checked += 1
        return checked

    async def _list_patrol_account_ids(self) -> list[int]:
        """选出该巡的账号 id:``valid`` + ``unknown``(且有 cookie),按 id 升序稳定排序。

        **unknown 必须纳入**:插件推 cookie 落库时 ``cookie_status='unknown'``,只巡 valid
        的话新号永远没人替它检测转正——也就永远当不了矩阵互动方(账号 10/11 加入后
        last_check_at 一直是 NULL 正是这个原因)。要求 ``login_cookies`` 非空:没 cookie
        的号检测必败,起一次 camoufox 纯浪费。

        **restricted(被风控)的号刻意不纳入周期巡检**:墙一旦挂上,继续每隔 interval 起一次
        camoufox 正是把「扫码验证身份」催成「请求太频繁」的原因(2026-07-31 NBDpsy-聊创伤
        实测)。恢复走人工:运营用手机扫码后,在插件里对该号点一次检测(REST
        POST /api/accounts/{id}/cookie-checks)即写回 valid,重新进入巡检。
        代价:不会自动恢复——但状态在账号列表里明晃晃是"风控",本就需要人处理。
        ``invalid`` / ``captcha`` 同理不自动重试:同样是需要人处置的状态。
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(XhsAccount.id)
                .where(
                    or_(
                        XhsAccount.cookie_status == "valid",
                        and_(
                            XhsAccount.cookie_status == "unknown",
                            XhsAccount.login_cookies.isnot(None),
                            XhsAccount.login_cookies != "",
                        ),
                    )
                )
                .order_by(XhsAccount.id)
            )
            return list(result.scalars().all())

    async def _check_account(self, account_id: int) -> bool:
        """检测单个号:解密 cookie → 线程内跑登录检测 → valid/invalid/captcha/restricted 写回。

        返回是否真正执行了检测:无 cookie 可检时跳过(不误改状态)返回 False;基础设施
        失败(error 态)不写回、保留原状态,但仍算已检测(返回 True)。
        """
        async with self._session_factory() as session:
            account = await session.get(XhsAccount, account_id)
            cookies = _decrypt_account_cookies(account)
        if not cookies:
            return False  # 无 cookie 可检,跳过(不误改状态)

        # 他人主页探测目标(矩阵内另一个号);取不到就传 None,退化为原首页判定不报错。
        probe_user_id = await pick_probe_user_id(self._session_factory, account_id)

        # 阻塞的 sync 浏览器调用下沉到线程,避免卡事件循环。次序与另三入口一致:
        # account_lock(外)→ browser_slot(内)→ to_thread。
        # - 持账号锁:同号的 publish/手动检测/导出串行,防同一 profile 目录被多个 camoufox
        #   争用(各自 start 时 kill_orphans 会互杀);并让孤儿回收 reaper 视本巡检浏览器"有主"不误杀。
        # - 套全局浏览器闸:周期巡检的 camoufox 也计入总并发上限(否则绕过闸,"全局"上限被击穿)。
        async with account_locks.get(account_id):
            async with browser_slot():
                # 与手动检测同一把看门狗(180s 强杀+限时 rejoin):巡检跑在 supervisor
                # 常驻进程里,裸 to_thread 僵死会无限占着进程内账号锁,同号发布/检测全排队。
                result = await _run_check_with_watchdog(
                    account_id, cookies, probe_user_id
                )
        status = result.get("status", "invalid")
        user_info = result.get("user_info")
        # 撞墙留痕(captcha / restricted 都带 wall):落库失败不影响巡检结论。
        await risk_events.record_wall(
            self._session_factory, account_id, result.get("wall"), "cookie_patrol"
        )

        # 基础设施失败(error)不写回 —— 保留原 cookie_status,与 check_cookies 工具一致,
        # 避免后台巡检把浏览器起不来误当成 cookie 失效、把好号刷成非 valid 后续不再巡检。
        if status == "error":
            logger.warning(
                f"cookie 巡检基础设施失败,保留原状态 account_id={account_id}: "
                f"{result.get('reason')}"
            )
            return True

        previous_status: str | None = None
        async with self._session_factory() as session:
            account = await session.get(XhsAccount, account_id)
            if account is not None:
                previous_status = account.cookie_status  # 转正判定要旧值,须在覆写前取
                account.cookie_status = status
                account.last_check_at = datetime.utcnow()
                if user_info:
                    for field in _USER_INFO_FIELDS:
                        value = user_info.get(field)
                        if value:
                            setattr(account, field, value)
                    # 内部展示名跟随小红书昵称实时更新:后台巡检拿到最新昵称即同步 name
                    # (前端/插件展示 name),运营在小红书改名后无需手工改。
                    nickname = user_info.get("nickname")
                    if nickname:
                        account.name = nickname
                await session.commit()
        # 新账号首次基底:cookie 确认 valid 且从未有快照 → 立即 enqueue 一次数据采集。
        # 绝不打断巡检主流程(基底失败只记日志,调度器每小时兜底)。
        if status == "valid":
            try:
                from app.services.note_metrics_scheduler import ensure_baseline

                await ensure_baseline(self._session_factory, account_id)
            except Exception:
                logger.exception(f"基底采集 enqueue 失败(不影响巡检)account_id={account_id}")
            # 转正引导链(台账同步 + newcomer 补量):只在从非 valid 转过来时走一次。
            # 巡检是 unknown 新号转正的主路径,这个 seam 必须与手动检测的 _write_back 一致。
            if previous_status != "valid":
                await kick_onboarding_chain(self._session_factory, account_id)
        return True

    def _is_stopping(self) -> bool:
        """是否已收到停止信号(未 start 时视为不停止,便于直接调 check_once 测试)。"""
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, timeout: float) -> None:
        """可被 stop() 立即打断的休眠;未 start 时退化为普通 sleep。"""
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        """优雅停:置停止信号 → 等后台循环退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
