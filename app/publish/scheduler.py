"""发布调度器:DB 状态机 + 启动恢复 + 重试排期(替代 celery beat/worker)。

状态机:``pending → publishing(started_at) → published | failed``;失败按
``settings.retry_delays`` 排期重试(``retries`` + ``next_retry_at``),耗尽转 ``failed``。

- ``scan_once``:选到期 pending job(schedule_time / next_retry_at 空或已到)。
- ``recover_stale``:``publishing`` 且 ``started_at`` 超 ``PUBLISH_JOB_TIMEOUT`` 的僵死
  job 复位回 ``pending``(进程重启 / 崩溃留下的中间态)。
- ``mark_publishing``:``UPDATE ... WHERE status='pending'`` 原子占用,rowcount 判是否真占到,
  防"扫表 + 队列"双重处理同一 job。
- ``finish``:成功写 note_id/url 置 published;失败排期重试或置 failed。
- ``start``/``stop``:lifespan 循环——每个 poll 周期先 recover_stale 再 scan_once→submit;可停。

时间统一用 ``datetime.utcnow()``(naive UTC),与模型 ``created_at`` 一致。
"""

import asyncio
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import or_, select, update

from app.browser import sync_client
from app.browser.browser_gate import browser_slot
from app.browser.images import materialize_images
from app.core.config import settings
from app.core.security import decrypt_cookies
from app.models.publish_job import PublishJob
from app.models.xhs_account import XhsAccount
from app.publish.queue import AccountLocks, PublishQueue


def _decrypt_account_cookies(account: XhsAccount | None) -> list[dict]:
    """直接解密账号 login_cookies 回列表。

    后台发布流程无 operator 上下文,不走 cookie_service.get_cookies 的 access 鉴权,
    直接解密即可(账号能被派发布本身即代表已授权)。空/无 cookie → 返回 []。
    """
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)


def make_publish_runner(
    session_factory,
    scheduler: "PublishScheduler",
    account_locks: AccountLocks,
) -> Callable[[int], Awaitable]:
    """构造真实发布 runner:载 job+account+cookie → 原子占用 → per-account 锁 → 线程发布 → 落状态机。

    返回 ``async runner(job_id)``。``sync_client.publish_once`` 的真实浏览器调用经
    ``asyncio.to_thread`` 下沉到线程,不阻塞事件循环;测试可 monkeypatch
    ``sync_client.publish_once`` 验证状态机而不起浏览器。
    """

    async def publish_runner(job_id: int) -> None:
        # 1. 原子占用先行:占不到(别处已处理 / 非 pending)直接退,防双重发布
        if not await scheduler.mark_publishing(job_id):
            return

        # C1:占用成功后立刻登记在途 —— 真发布可能墙钟 > PUBLISH_JOB_TIMEOUT,
        #     recover_stale 据此排除本 job,避免"僵死误判 → 复位 → 重投 → 二次发布"。
        scheduler._in_flight.add(job_id)

        # 临时物料目录:URL/base64 图片落成本地文件的落盘处,发布结束(无论成败)清理。
        workdir = Path(settings.UPLOAD_DIR) / f"job_{job_id}"

        # 2. 占用成功后的整个执行(载数据 + 物料化 + 线程发布 + finish)统一兜底:任何一步抛异常
        #    都显式 finish(success=False),交回重试/退避机制,绝不让 job 卡死在 publishing。
        try:
            # 2a. 载 job + account + cookie(会话内取尽所需字段,出会话不再触发 lazy load)
            async with session_factory() as session:
                job = await session.get(PublishJob, job_id)
                if job is None:
                    return
                account = await session.get(XhsAccount, job.account_id)
                account_id = job.account_id
                title = job.title
                content = job.content
                raw_images = json.loads(job.images_json or "[]")
                topics = json.loads(job.topics_json or "[]")
                cookies = _decrypt_account_cookies(account)

            # 2b. 图片物料化:images_json 存的是 URL/base64(远程 agent 供图),而 publish_once
            #     的 set_input_files 只认本地文件路径 —— 先落成本地文件再传。下载/解码是阻塞
            #     I/O,下沉到线程避免卡事件循环;物料化失败照样落到下面兜底 finish(fail)。
            paths = await asyncio.to_thread(materialize_images, raw_images, workdir)
            image_paths = [str(p) for p in paths]

            # 2c. per-account 锁串行 + 全局浏览器并发闸 + 线程内跑 sync 发布(禁同号并发)
            #     browser_slot 封顶总 camoufox 数,超出排队;publish 不 block_images(保发布保真)。
            async with account_locks.get(account_id):
                async with browser_slot():
                    result = await asyncio.to_thread(
                        sync_client.publish_once,
                        account_id,
                        cookies,
                        title,
                        content,
                        image_paths,
                        topics,
                    )

            # 2d. 落状态机(成功→published;失败→重试排期或 failed)
            await scheduler.finish(job_id, result)
        except Exception as exc:
            # publish_once 内部已把可预期失败转成 PublishResult;能逃到这里的是构造/收尾/
            # 载数据/物料化等意外异常。兜底落一个失败结果,让状态机排重试而非永久 publishing。
            logger.exception("发布 runner 处理 job {} 异常,兜底转失败", job_id)
            await scheduler.finish(
                job_id, sync_client.PublishResult(success=False, error=str(exc))
            )
        finally:
            # C1:无论成败先撤销在途登记(finish 已落终态/重排),之后此 job 若再僵死可正常回收
            scheduler._in_flight.discard(job_id)
            # 清理临时物料目录(成功 / 失败 / 提前 return 都清;不存在则忽略)
            shutil.rmtree(workdir, ignore_errors=True)

    return publish_runner


class PublishScheduler:
    """发布调度器:持有会话工厂 + 注入的 runner,驱动状态机与 lifespan 循环。

    ``publish_runner`` 缺省时自建真实 runner(``make_publish_runner`` 绑定自身),测试可注入
    假 runner 只验状态机与队列。
    """

    def __init__(
        self,
        session_factory,
        publish_runner: Callable[[int], Awaitable] | None = None,
        account_locks: AccountLocks | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._account_locks = account_locks or AccountLocks()
        self._publish_runner = publish_runner or make_publish_runner(
            session_factory, self, self._account_locks
        )
        self._queue = PublishQueue(settings.PUBLISH_CONCURRENCY)
        self._poll_interval = poll_interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        # C1:本进程当前真发布中的 job id(runner 占用后登记、finally 撤销)。recover_stale
        # 据此排除在途 job,防"墙钟超时误判僵死 → 复位 → 重投 → 二次发布"。进程重启后天然为空
        # → 所有 publishing 均可回收,崩溃恢复语义不变。
        self._in_flight: set[int] = set()

    def submit(self, job_id: int) -> None:
        """把 job_id 立即投入内部队列(publish_note 无定时发布走此路径,免等下个 scan 周期)。

        与 scan 循环共用同一队列 + runner;即便扫表也捞到同一 job,``mark_publishing`` 的
        原子占用保证只处理一次,重复 submit 安全。
        """
        self._queue.submit(job_id)

    async def scan_once(self) -> list[int]:
        """选可发布的 pending job id:schedule_time 与 next_retry_at 均为空或已到期,按 id 升序。"""
        now = datetime.utcnow()
        async with self._session_factory() as session:
            stmt = (
                select(PublishJob.id)
                .where(PublishJob.status == "pending")
                .where(
                    or_(
                        PublishJob.schedule_time.is_(None),
                        PublishJob.schedule_time <= now,
                    )
                )
                .where(
                    or_(
                        PublishJob.next_retry_at.is_(None),
                        PublishJob.next_retry_at <= now,
                    )
                )
                .order_by(PublishJob.id)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def recover_stale(self) -> int:
        """把 ``publishing`` 且 ``started_at`` 超 ``PUBLISH_JOB_TIMEOUT`` 的僵死 job 复位回 pending。

        返回复位条数。未超时的 publishing job 不动(可能正常在途)。
        """
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=settings.PUBLISH_JOB_TIMEOUT)
        async with self._session_factory() as session:
            stmt = (
                update(PublishJob)
                .where(PublishJob.status == "publishing")
                .where(PublishJob.started_at.is_not(None))
                .where(PublishJob.started_at <= cutoff)
            )
            # C1:排除本进程在途 job —— runner 已占用且仍在真发布中,墙钟超时不等于僵死,
            # 复位会触发 scan 重投 + 二次发布。集合空(如进程重启后)则不加约束,崩溃恢复语义不变。
            if self._in_flight:
                stmt = stmt.where(PublishJob.id.not_in(self._in_flight))
            stmt = stmt.values(status="pending", started_at=None)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    async def mark_publishing(self, job_id: int) -> bool:
        """原子占用:``UPDATE ... SET status='publishing', started_at=now WHERE id=job_id AND status='pending'``。

        返回是否真占到(rowcount==1)。同一 pending job 两处并发调用只一处返回 True,
        防扫表与队列双重处理。
        """
        now = datetime.utcnow()
        async with self._session_factory() as session:
            stmt = (
                update(PublishJob)
                .where(PublishJob.id == job_id)
                .where(PublishJob.status == "pending")
                .values(status="publishing", started_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount == 1

    async def finish(self, job_id: int, result) -> None:
        """落发布结果:成功→published 写 note_id/url;失败→重试排期或耗尽转 failed。

        ``result`` 为 ``sync_client.PublishResult``(鸭子类型:success/note_id/note_url/
        error/need_manual_login)。失败时若 ``retries < len(retry_delays)`` 则按
        ``retry_delays[retries]`` 排下次重试并回 pending(retries+1、next_retry_at 排期);
        否则置 failed 写 error。

        - C1 守卫:仅 ``status=='publishing'`` 的 job 可被落终态/重排,非 publishing 一律
          no-op —— 防"僵死复位后被别处 runner 重占的复活 job / 已 cancel 的 job"被越权覆盖。
        - I1:``need_manual_login=True``(cookie/SSO 坏)重试无用 → 直接置 failed,**不排重试、
          retries 不递增**,免每次都拉 Camoufox 再 SSO 失败徒劳消耗。
        """
        now = datetime.utcnow()
        async with self._session_factory() as session:
            job = await session.get(PublishJob, job_id)
            if job is None:
                return
            # C1 守卫:非 publishing 态不落 —— 复活的重复 runner / 已 cancel 的 job 不被覆盖
            if job.status != "publishing":
                return
            if result.success:
                job.status = "published"
                job.note_id = result.note_id
                job.note_url = result.note_url
                job.error = None
            elif getattr(result, "need_manual_login", False):
                # I1:需人工登录 —— 重试也只会反复 SSO 失败,直接终态 failed,不排重试、不增 retries
                job.status = "failed"
                job.started_at = None
                job.error = result.error or "需要人工登录/重新扫码"
            else:
                delays = settings.retry_delays
                if job.retries < len(delays):
                    # 还有重试额度:按当前 retries 取延迟排下次,再回 pending
                    job.next_retry_at = now + timedelta(seconds=delays[job.retries])
                    job.retries += 1
                    job.status = "pending"
                    job.started_at = None
                    job.error = result.error
                else:
                    # 重试耗尽:终态 failed
                    job.status = "failed"
                    job.started_at = None
                    job.error = result.error
            await session.commit()

    def start(self) -> None:
        """启动 lifespan 循环:起队列 worker,后台协程每 poll 周期 recover_stale→scan→submit。"""
        self._stop_event = asyncio.Event()
        self._queue.start(self._publish_runner)
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台调度循环:每 poll_interval 先回收僵死 publishing(recover_stale),再扫到期 job 入队。

        recover_stale 放在循环内每轮先跑(而非仅启动一次):runner 兜底失效 / 进程被信号打断
        留下的 publishing 僵死 job 无需等下次重启,下一个 poll 周期即被复位重排。
        """
        while not self._stop_event.is_set():
            try:
                await self.recover_stale()
            except Exception:
                logger.exception("发布调度器僵死回收失败")
            try:
                for job_id in await self.scan_once():
                    self._queue.submit(job_id)
            except Exception:
                logger.exception("发布调度器扫表失败")
            # 可被 stop() 立即唤醒的休眠
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """优雅停:置停止信号 → 等调度循环退出 → 停队列 worker。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
        await self._queue.stop()
