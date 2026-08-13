"""受众事件采集的定时调度:让受众库自己长起来,而不是等人每小时戳一次。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 3.1 节。
结构**照抄 ``InteractionBackfillScheduler``**,三条纪律逐条对齐(每条都是实测踩出来的):

1. **直插 ``queued`` 台账行,不 ``spawn_inline``**。inline 会在**当前进程**里把任务跑掉,
   而本组件活在 supervisor 进程里 —— 那等于让 supervisor 自己起浏览器,违背 API/Worker
   拆分里"supervisor 只派发、浏览器只在 account_worker 子进程"的规矩。
2. **在飞就不叠**。一轮采集要开一次真号会话滚好几分钟,扫描间隔比它短是常态;不挡一下
   队列里就会堆一串同号采集单,而"队列里堆着一串同类任务"正是被平台看出自动化特征的样子。
3. **挑不出就不登记**。全都还没到期时直接跳过 —— 开一个注定空转的浏览器任务毫无意义,
   还白烧一次该号一小时只有 4 次的会话额度。

**采集范围只有代管号**(``managed=1``,内容号)。水军号的通知流是它自己的社交噪音,
不是我们的受众;顺带也省掉一半会话开销。

到期判定用该号**最旧的那条 channel 游标**:两条 channel 只采成一条时(比如 connections
那次 tab 没找到),按最旧的判才会让漏掉的那条重新排上队;按最新的判会让它永远排不上。
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.models.audience_sync_state import AudienceSyncState
from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services.audience_sync import JOB_KIND

# 非请求上下文的进程内直调,台账 operator_id 约定用 0(与其余调度器一致)
_SYSTEM_OPERATOR_ID = 0
# 认作"在飞"的台账状态:这两种都意味着这一轮还没走完,不该再叠一条
_IN_FLIGHT_STATUSES = ("queued", "running")


class AudienceSyncScheduler:
    """每 interval 秒扫一次:有到期未采的代管号且当前没有在飞采集单,就 enqueue 一条。"""

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        # 上一次"没得可采"的原因:只在原因变化时打日志。稳态下每轮都挑不出号,
        # 不去重的话日志会被同一句话刷屏,真出问题反而看不见。
        self._last_idle_reason: str | None = None

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.scan_once()
            except Exception:
                # 调度轮次异常绝不能让循环退出:退出了就再没人推采集,而且是静默的
                logger.exception("[audience_sync_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮,返回本轮 enqueue 了几条(0 或 1)。"""
        async with self._session_factory() as session:
            in_flight = (await session.execute(
                select(BrowserJob.id)
                .where(
                    BrowserJob.kind == JOB_KIND,
                    BrowserJob.status.in_(_IN_FLIGHT_STATUSES),
                )
                .limit(1)
            )).scalar()
            if in_flight is not None:
                return 0

            picked = await self._pick_account(session)
            if picked is None:
                if self._last_idle_reason != "no_due_account":
                    logger.info("[audience_sync_scheduler] 暂无到期未采的代管号")
                    self._last_idle_reason = "no_due_account"
                return 0
            self._last_idle_reason = None
            account_id, full = picked

            session.add(BrowserJob(
                id=uuid.uuid4().hex,
                kind=JOB_KIND,
                account_id=account_id,
                operator_id=_SYSTEM_OPERATOR_ID,
                payload=json.dumps(
                    {"account_id": account_id, "full": full}, ensure_ascii=False
                ),
                status="queued",
            ))
            await session.commit()
        logger.info(
            f"[audience_sync_scheduler] 已登记受众采集:account={account_id} "
            f"{'全量回采' if full else '增量'}"
        )
        return 1

    async def _pick_account(self, session) -> tuple[int, bool] | None:
        """挑一个该采的代管号,返回 ``(account_id, 是否全量)``;没有就 None。

        优先级:**从没采过的号 > 最久没采的号**。从没采过的号一条数据都没有,缺口最大;
        它同时也是唯一需要走全量(翻到平台保留窗口的底)的场景 —— 之后一律增量。

        ``cookie_status != 'valid'`` 的号直接跳过:浏览器起来也只会撞登录页,
        白烧一次该号一小时只有 4 次的会话额度。
        """
        # 每号最旧的那条 channel 游标(理由见模块 docstring)
        oldest = dict((await session.execute(
            select(AudienceSyncState.account_id, func.min(AudienceSyncState.updated_at))
            .group_by(AudienceSyncState.account_id)
        )).all())

        accounts = (await session.execute(
            select(XhsAccount.id)
            .where(XhsAccount.managed.is_(True))
            .where(XhsAccount.cookie_status == "valid")
            .order_by(XhsAccount.id)
        )).scalars().all()

        never = [a for a in accounts if a not in oldest]
        if never:
            return never[0], True

        cutoff = datetime.utcnow() - timedelta(seconds=self._interval)
        due = sorted(
            (a for a in accounts if oldest[a] < cutoff), key=lambda a: oldest[a]
        )
        return (due[0], False) if due else None

    async def _sleep(self, timeout: float) -> None:
        """可被 stop 立刻打断的等待(照抄同族调度器,避免停机要等满一个周期)。"""
        if self._stop_event is None:
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
