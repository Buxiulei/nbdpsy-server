"""新号接入调度:让「插件灌完 cookie 的新号」自己走到 valid,而不是等人手动戳一次。

动机(这是个真缺口,不是锦上添花):2026-08-07 上线的转正引导链
(``cookie_check.kick_onboarding_chain``)本身是对的 —— cookie 从非 valid 转 valid 时
自动登记「历史笔记入台账 + newcomer 补量」两条任务,新号就此融进互动矩阵。但它**在生产
从未触发过**:引导链挂在两条写回路径上,一条是手动 REST 检测,一条是周期巡检
``CookieChecker`` —— 而周期巡检 ``COOKIE_CHECK_INTERVAL`` 默认 0、生产 ``.env`` 也没覆盖,
根本没起。于是新号除非有人手动 POST 一次 ``/api/accounts/{id}/cookie-checks``,否则永远
卡在 ``cookie_status='unknown'``(账号 12 加入 27 小时仍是 unknown,账号 10/11 是人工排
了一次检测才转正的)。

**为什么不是直接打开周期巡检**:巡检是**全矩阵逐号**的周期行为,每号每轮各烧一次浏览器
会话;而 2026-08-07 刚上线的同号会话总闸是系统任务 4 次/时/号(风控红线实测同号一小时
5 次就弹验证墙)。十个号的巡检会把这份额度吃掉一大块,挤掉发布、补量、采集这些真正在
产出的任务。**新号要的根本不是周期巡检,是一次性检测** —— 转正即出局,不再复检。

本组件只做一件事:每 interval 秒问一次「有没有灌了 cookie 却还没转正的号」,有就给他
登记**一条** ``cookie_check`` 台账行。检测怎么做、转正后干什么,一概不在这里判 ——
分别归 ``cookie_check.execute`` 与 ``kick_onboarding_chain``,那两段代码是好的,只是
没人触发它们。

三条边界,每条都对应一种"推过头"的真实后果:

1. **在途不叠**(任意年龄的 queued/running):一次检测是一次浏览器会话,间隔比它短是常态;
   不挡一下就会在队列里堆一串同号检测。注意在途判据**不带时间窗** —— 撞上会话总闸的
   任务会在 queued 里停留很久(总闸的语义就是"留在队列等下轮重估"),按时间窗判会把它
   当成"过期了"再叠一条。
2. **失败退避**(retry_hours 内有过任意状态的 ``cookie_check`` 行就跳过):没有它,一个
   坏号会以 interval 的频率无限重试,独自打满自己的会话额度。
3. **转正即出局**:候选条件是 ``cookie_status='unknown'``,转 valid 后天然不再入选,
   不需要额外的"已处理"标记。

**退避实际治理的是哪种失败**,值得写清楚免得后人调错值:``invalid`` / ``captcha`` /
``restricted`` 三态都会被 ``_write_back`` 写回账号,写回后账号不再是 ``unknown``,
自己就从候选里掉出去了 —— 也就是说**唯一会留在 unknown 里反复入选的是 ``error``**
(基础设施失败:camoufox 起不来、看门狗 180s 超时等,按设计不写回、保留原值)。所以
``ONBOARDING_CHECK_RETRY_HOURS`` 调的是"基础设施失败多久重试一次",不是"坏号多久复检
一次"。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount

# 检测任务的台账 kind(与 cookie_check.start_check 登记的同一种,执行路径完全同路)
_JOB_KIND = "cookie_check"
# 非请求上下文的进程内直调,台账 operator_id 约定用 0(与其余调度器一致)
_SYSTEM_OPERATOR_ID = 0
# 认作"在途"的台账状态:这两种都意味着这条还没走完,不该再叠一条
_IN_FLIGHT_STATUSES = ("queued", "running")
# 候选账号的 cookie 状态:只有"还没被检测过"这一态需要本组件推一把
_PENDING_STATUS = "unknown"


class OnboardingScheduler:
    """每 interval 秒扫一次:有 cookie 但还没转正的号,各登记一条 cookie_check。"""

    def __init__(self, session_factory, interval: float, retry_hours: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._retry_hours = retry_hours
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.scan_once()
            except Exception:
                # 轮次异常绝不能让循环退出:退出了就再没人推新号转正,而且是静默的
                logger.exception("[onboarding_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮,返回本轮登记了几条检测。

        两道判据分开查而不是合成一条:在途看状态**不看年龄**(见模块 docstring 第 1 条),
        退避看年龄**不看状态**(失败与成功都算数——成功的号本就该已经出局)。
        """
        cutoff = datetime.utcnow() - timedelta(hours=self._retry_hours)
        async with self._session_factory() as session:
            account_ids = list((await session.execute(
                select(XhsAccount.id)
                .where(
                    XhsAccount.cookie_status == _PENDING_STATUS,
                    XhsAccount.login_cookies.isnot(None),
                    XhsAccount.login_cookies != "",
                )
                .order_by(XhsAccount.id)
            )).scalars().all())
            if not account_ids:
                return 0

            in_flight = set((await session.execute(
                select(BrowserJob.account_id)
                .where(
                    BrowserJob.kind == _JOB_KIND,
                    BrowserJob.account_id.in_(account_ids),
                    BrowserJob.status.in_(_IN_FLIGHT_STATUSES),
                )
                .distinct()
            )).scalars().all())
            recent = set((await session.execute(
                select(BrowserJob.account_id)
                .where(
                    BrowserJob.kind == _JOB_KIND,
                    BrowserJob.account_id.in_(account_ids),
                    BrowserJob.created_at >= cutoff,
                )
                .distinct()
            )).scalars().all())

            enqueued = 0
            for account_id in account_ids:
                if self._is_stopping():
                    break
                if account_id in in_flight:
                    logger.info(
                        f"[onboarding_scheduler] 账号 {account_id} 已有在途检测,不重复登记"
                    )
                    continue
                if account_id in recent:
                    continue
                # 直插 queued 台账行,不走 cookie_check.start_check:后者会 spawn_inline
                # 在**当前进程**把检测跑掉,而本组件活在 supervisor 进程里——那等于让
                # supervisor 自己起浏览器,违背"supervisor 只派发、浏览器只在
                # account_worker 子进程"的规矩。插行交 supervisor 派子进程,与手工
                # POST /api/accounts/{id}/cookie-checks 完全同路(同样受会话总闸约束)。
                session.add(BrowserJob(
                    id=uuid.uuid4().hex,
                    kind=_JOB_KIND,
                    account_id=account_id,
                    operator_id=_SYSTEM_OPERATOR_ID,
                    payload="{}",
                    status="queued",
                ))
                logger.info(
                    f"[onboarding_scheduler] 账号 {account_id} 有 cookie 但未转正,"
                    f"已登记一次检测"
                )
                enqueued += 1
            if enqueued:
                await session.commit()
        return enqueued

    def _is_stopping(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _sleep(self, timeout: float) -> None:
        """可被 stop 立刻打断的等待(照抄同族调度器,避免停机要等满一个周期)。"""
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
