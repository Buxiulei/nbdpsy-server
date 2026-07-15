"""可选的后台 cookie 巡检循环:周期性对 valid 账号跑登录检测并写回状态。

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
from sqlalchemy import select

from app.browser import sync_client
from app.browser.browser_gate import browser_slot
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount

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
    """周期 cookie 巡检:每 ``interval`` 秒对 ``cookie_status='valid'`` 的账号逐个检测并写回。

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
        """跑一轮:取所有 valid 账号逐个检测并写回;返回实际检测的账号数。

        号与号之间隔 ``account_gap`` 秒防频控(首个号不等);运行中收到停止信号即提前退出。
        """
        account_ids = await self._list_valid_account_ids()
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

    async def _list_valid_account_ids(self) -> list[int]:
        """选出 cookie_status='valid' 的账号 id(按 id 升序,稳定顺序)。"""
        async with self._session_factory() as session:
            result = await session.execute(
                select(XhsAccount.id)
                .where(XhsAccount.cookie_status == "valid")
                .order_by(XhsAccount.id)
            )
            return list(result.scalars().all())

    async def _check_account(self, account_id: int) -> bool:
        """检测单个号:解密 cookie → 线程内跑登录检测 → valid/invalid/captcha 写回状态。

        返回是否真正执行了检测:无 cookie 可检时跳过(不误改状态)返回 False;基础设施
        失败(error 态)不写回、保留原状态,但仍算已检测(返回 True)。
        """
        async with self._session_factory() as session:
            account = await session.get(XhsAccount, account_id)
            cookies = _decrypt_account_cookies(account)
        if not cookies:
            return False  # 无 cookie 可检,跳过(不误改状态)

        # 阻塞的 sync 浏览器调用下沉到线程,避免卡事件循环;套全局浏览器闸,使周期巡检的
        # camoufox 也计入总并发上限(否则它绕过闸,让"全局"上限被击穿)。
        async with browser_slot():
            result = await asyncio.to_thread(
                sync_client.check_login_once, account_id, cookies
            )
        status = result.get("status", "invalid")
        user_info = result.get("user_info")

        # 基础设施失败(error)不写回 —— 保留原 cookie_status,与 check_cookies 工具一致,
        # 避免后台巡检把浏览器起不来误当成 cookie 失效、把好号刷成非 valid 后续不再巡检。
        if status == "error":
            logger.warning(
                f"cookie 巡检基础设施失败,保留原状态 account_id={account_id}: "
                f"{result.get('reason')}"
            )
            return True

        async with self._session_factory() as session:
            account = await session.get(XhsAccount, account_id)
            if account is not None:
                account.cookie_status = status
                account.last_check_at = datetime.utcnow()
                if user_info:
                    for field in _USER_INFO_FIELDS:
                        value = user_info.get(field)
                        if value:
                            setattr(account, field, value)
                await session.commit()
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
