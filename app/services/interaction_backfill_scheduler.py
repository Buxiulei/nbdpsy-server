"""历史笔记互动补量的定时续跑:让补量自己走完,而不是等人每天戳一次。

动机(这是个真缺口,不是锦上添花):补量能力本身早就齐了 —— REST 手工触发三种 scope、
台账同步发现手工新增笔记后自动派单。但**没有任何东西负责"把存量补完"**。存量是
「公开笔记 × 互动方账号」的组合,当前 900 多个待做,而日上限是每号每天 20 篇,
算下来要六天多。也就是说:没有本组件,这个功能只有在有人连续六天、每天手动 POST 一次
``/api/interaction-backfills`` 的前提下才跑得完 —— 换句话说它交付了却完不成自己的活。

本组件只做一件事:**每 interval 秒问一次"现在还有得补吗",有就往台账里放一条**。
选谁去补、补哪几篇、日上限与冷却全部**不在这里判**,原样交给 ``plan_round``——
那些闸是补量的核心风控,复制一份到调度器里就等于埋下两套口径迟早对不上的雷。

结构套 ``DraftCleanScheduler`` / ``NoteMetricsScheduler`` 模板,三处刻意保持一致:

1. **直插台账行,不走 ``start_backfill``**。后者会 ``spawn_inline`` 在**当前进程**里
   把任务跑掉,而本组件活在 supervisor 进程里 —— 那等于让 supervisor 自己起浏览器,
   违背 API/Worker 拆分里"supervisor 只派发、浏览器只在 account_worker 子进程"的规矩。
   插一条 ``queued`` 行,supervisor 照常派子进程,与手工触发完全同路。
2. **在途就不叠**。补量一轮要十几分钟(篇间刻意停 60-240 秒),扫描间隔比它短是常态;
   不挡一下就会在队列里堆一串同号任务,而"队列里堆着一串补量任务"正是被平台看出补量
   特征的样子。
3. **挑不出来不登记**。``plan_round`` 挑不出 actor(都到日上限 / 没得可补了)时返回空,
   本组件直接跳过 —— 开一个注定空转的浏览器任务毫无意义,还白占号锁与浏览器闸。

日上限归零靠的是 ``plan_round`` 内部的 UTC 日界,本组件不需要知道"今天"是哪天:
配额吃完后它每轮都挑不出 actor,自然空转到次日零点,不必额外写跨日唤醒逻辑。
"""

import asyncio
import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.models.browser_job import BrowserJob
from app.services.interaction_backfill import JOB_KIND, SCOPE_ALL, plan_round

# 非请求上下文的进程内直调,台账 operator_id 约定用 0(与其余调度器一致)
_SYSTEM_OPERATOR_ID = 0
# 认作"在途"的台账状态:这两种都意味着这一轮还没走完,不该再叠一条
_IN_FLIGHT_STATUSES = ("queued", "running")


class InteractionBackfillScheduler:
    """每 interval 秒扫一次:还有存量可补且当前没有在途补量任务,就 enqueue 一条。"""

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        # 上一次"没得可补"的原因:只在原因变化时打日志。补完之后每轮都挑不出 actor,
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
                # 调度轮次异常绝不能让循环退出:退出了就再没人推补量,而且是静默的
                logger.exception("[interaction_backfill_scheduler] 调度轮次异常")
            await self._sleep(self._interval)

    async def scan_once(self) -> int:
        """跑一轮,返回本轮 enqueue 了几条(0 或 1)。

        只登记 ``scope=all``:存量补量就是"所有号的公开笔记互相补齐"。``account`` 与
        ``newcomer`` 是**运营带着意图**手工发起的(某个号要冲、某个新号要融进矩阵),
        由 REST 触发,自动续跑不替运营做这种决定。
        """
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

            plan = await plan_round(session, SCOPE_ALL)
            actor = plan["actor_account_id"]
            if actor is None:
                reason = plan.get("reason") or "没有可补的笔记"
                if reason != self._last_idle_reason:
                    logger.info(f"[interaction_backfill_scheduler] 暂无可补:{reason}")
                    self._last_idle_reason = reason
                return 0
            self._last_idle_reason = None

            # 直插台账(理由见模块 docstring 第 1 条):payload 与 start_backfill 同构,
            # execute 拿到后会**再挑一次篇**——排队期间日配额可能已被别的轮次吃掉,
            # 拿登记那一刻的选篇快照去做等于绕过日上限。
            session.add(BrowserJob(
                id=uuid.uuid4().hex,
                kind=JOB_KIND,
                account_id=actor,
                operator_id=_SYSTEM_OPERATOR_ID,
                payload=json.dumps({
                    "scope": SCOPE_ALL,
                    "target_account_id": None,
                    "actor_account_id": actor,
                    "limit": None,
                }, ensure_ascii=False),
                status="queued",
            ))
            await session.commit()
        logger.info(
            f"[interaction_backfill_scheduler] 已登记续跑补量:actor={actor} "
            f"本轮候选 {len(plan['targets'])} 篇"
        )
        return 1

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
