"""笔记台账同步的保底调度:让**纯手工运营的号**也有人管,而不是只有发过布的号才新鲜。

这是个真缺口,不是锦上添花。台账同步(``note_ledger_sync``)原本只有两个触发点:
发布任务完成后的钩子 ``schedule_note_ledger_sync``、REST 手工触发 ``start_sync``——
**没有任何周期性触发**。后果是同步频率完全跟着"这个号发不发布"走:系统常发的号
一天被刷好几次,而禁发号(老板定的、纯手工运营)十几天只被人手工戳过一次,
它手工发的笔记因此全都不在 ``published_notes`` 里。而互动补量选篇要求 note_id 非空 ——
于是**自然流量最好的那个号反而永远补不到量**,最不该漏的号漏得最彻底。

结构照抄 ``AudienceSyncScheduler`` / ``InteractionBackfillScheduler``,三条纪律逐条对齐:

1. **直插 ``queued`` 台账行,不 ``spawn_inline``**。inline 会在**当前进程**里把任务跑掉,
   而本组件活在 supervisor 进程里 —— 那等于让 supervisor 自己起浏览器,违背 API/Worker
   拆分里"supervisor 只派发、浏览器只在 account_worker 子进程"的规矩。
2. **单 kind 在飞不叠**。发布钩子与手工触发插的单也算在飞 —— **保底调度给它们让路**:
   它们带着明确意图(刚发完 / 运营现在就要看),而保底只要求"最终会被同步到"。
3. **挑不出就不登记**。全都还新鲜时直接跳过 —— 开一个注定没新东西可同步的浏览器任务,
   白烧一次该号一小时只有 4 次的会话额度。

**覆盖范围是全部有效号,不分代管**(与受众采集相反)。水军号的台账同样要新鲜:
台账里的"孤儿行"判定(平台上已不存在的笔记)依赖同步结果,水军号台账一旦发霉,
孤儿判定就跟着失真。

"最后一次同步"直接查 ``browser_jobs``(该号该 kind 最新的 ``created_at``,**不分终态**),
不建新表也不加新列:失败单也算跑过一次,否则一个一直失败的号会被每轮反复推。
判据用 ``created_at`` 而非终态时间,是因为发布钩子插的单可能带 ``not_before`` 排期、
要等一阵才真跑 —— 用登记时刻判,排期中的单天然把该号挡在门外(在飞闸也会挡),
不会出现"排着队还被保底再推一条"。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount

# 与 note_ledger 的 kind 字符串一致(那边是散落的字面量,不去改它:改动只为本组件服务)
JOB_KIND = "note_ledger_sync"
# 非请求上下文的进程内直调,台账 operator_id 约定用 0(与其余调度器一致)
_SYSTEM_OPERATOR_ID = 0
# 认作"在飞"的台账状态:这两种都意味着这一轮还没走完,不该再叠一条
_IN_FLIGHT_STATUSES = ("queued", "running")


class NoteLedgerSyncScheduler:
    """每 scan_interval 秒扫一次:有超过 min_interval 没同步的有效号就 enqueue 一条。"""

    def __init__(self, session_factory, scan_interval: float, min_interval: float) -> None:
        self._session_factory = session_factory
        self._scan_interval = scan_interval
        # 每号的到期门槛:与扫描间隔分开,因为两者管的是不同的事 —— 扫描间隔决定
        # "多久看一眼队列有没有空位",门槛决定"一个号多久该被同步一次"。
        self._min_interval = min_interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        # 上一次"没号可派"的原因:只在原因变化时打日志。稳态下每轮都挑不出号,
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
                # 调度轮次异常绝不能让循环退出:退出了就再没人推同步,而且是静默的
                logger.exception("[note_ledger_sync_scheduler] 调度轮次异常")
            await self._sleep(self._scan_interval)

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

            account_id = await self._pick_account(session)
            if account_id is None:
                if self._last_idle_reason != "no_due_account":
                    logger.info("[note_ledger_sync_scheduler] 暂无到期未同步的账号")
                    self._last_idle_reason = "no_due_account"
                return 0
            self._last_idle_reason = None

            # payload 与手工触发(start_sync)同构:空 payload = "同步这个号的全部笔记"。
            # 不带 not_before,登记即可派 —— 保底本来就是"该同步却一直没同步"的补救。
            session.add(BrowserJob(
                id=uuid.uuid4().hex,
                kind=JOB_KIND,
                account_id=account_id,
                operator_id=_SYSTEM_OPERATOR_ID,
                payload="{}",
                status="queued",
            ))
            await session.commit()
        logger.info(f"[note_ledger_sync_scheduler] 已登记保底台账同步:account={account_id}")
        return 1

    async def _pick_account(self, session) -> int | None:
        """挑一个该同步的号(最久未同步者优先),没有就 None。

        候选是**全部** ``cookie_status='valid'`` 且有 ``login_cookies`` 的号:cookie 无效
        或从没登录过的号,浏览器起来也只会撞登录页,白烧一次该号一小时只有 4 次的会话额度。

        "最后一次同步"取该号该 kind 的最新 ``created_at``(不分终态,理由见模块 docstring);
        从没同步过的号视为无穷久,排在所有"很久没同步"的号前面 —— 它一条台账都没有,缺口最大。
        """
        latest = dict((await session.execute(
            select(BrowserJob.account_id, func.max(BrowserJob.created_at))
            .where(BrowserJob.kind == JOB_KIND, BrowserJob.account_id.isnot(None))
            .group_by(BrowserJob.account_id)
        )).all())

        accounts = (await session.execute(
            select(XhsAccount.id)
            .where(XhsAccount.cookie_status == "valid")
            .where(XhsAccount.login_cookies.isnot(None))
            .order_by(XhsAccount.id)
        )).scalars().all()

        never = [a for a in accounts if a not in latest]
        if never:
            return never[0]

        cutoff = datetime.utcnow() - timedelta(seconds=self._min_interval)
        due = sorted((a for a in accounts if latest[a] < cutoff), key=lambda a: latest[a])
        return due[0] if due else None

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
