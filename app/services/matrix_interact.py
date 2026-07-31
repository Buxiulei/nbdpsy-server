"""矩阵互动服务:发布成功钩子登记延时任务 + 契约 execute()。

设计见 docs/design/2026-07-31-matrix-interact-design.md。分层与 note_export /
note_delete 一致(浏览器动作在 app.browser.matrix_interact,本模块只管台账与调度):

- ``schedule_matrix_interact``(sync,发布 published 钩子调,与 ``archive_published_job``
  同址同模式):矩阵 = 全部 ``cookie_status='valid'`` 的账号排除发布者本人,给每个号
  登记一条 ``browser_jobs`` (kind=matrix_interact),各自分到 10 分钟窗口内的随机时刻。
  按 ``source_publish_job_id`` 幂等(同一发布重复调不重复登记);**绝不抛错**阻断发布终态。
- **延时靠落库排期,不靠进程内 sleep**:执行时刻写进 payload 的 ``not_before``,
  派发侧(``browser_jobs_repo.list_dispatchable``)按它过滤未到点的行。任务领取后干等
  会占死全局浏览器闸 ``browser_slot``,5 个号最多干等 10 分钟将阻塞 cookie_check /
  note_export / 发布等所有浏览器任务。
- ``execute()`` 为契约执行函数(account_worker 子进程消费):持号锁串行 → 浏览器闸 →
  线程内跑同步互动,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。
- ``matrix_interact`` **非幂等**(重复执行会取消已点的赞),故不在
  ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里:僵死置 error 不自动重跑。
- 评论文案是 payload 入参(``comment``),本模块不做 LLM 生成;为空即只点赞收藏。

已知边界:``NBDPSY_ROLE=all``(单进程回滚位/测试位)无 Supervisor,登记的延时任务无人
派发,会一直 queued —— 生产走 api + worker 拆分部署(worker 的 Supervisor 扫
``list_dispatchable``),不受影响。
"""

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.matrix_interact import MatrixInteractError, interact_with_note
from app.browser.sync_client import SyncClient
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# 互动窗口(秒):发布成功后各矩阵账号在该窗口内的随机时刻执行(设计第二节:10 分钟)。
WINDOW_SECONDS = 600


# ---------------- 发布成功钩子(登记延时任务)----------------


def schedule_matrix_interact(db_path: str, publish_job_id: int) -> list[str]:
    """为矩阵内其余账号登记延时互动任务;返回登记的 job id 列表(未登记则空表)。

    幂等:该 publish job 已登记过则跳过。**绝不抛错**——发布终态已先行落库,登记是
    事后副作用,任何异常只告警(与 ``archive_published_job`` 同款纪律)。
    """
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT id, account_id, title FROM publish_jobs WHERE id=?",
                (publish_job_id,),
            ).fetchone()
            if job is None or not (job["title"] or "").strip():
                logger.warning(
                    f"[matrix_interact] 发布 job={publish_job_id} 不存在或无标题,不登记互动"
                )
                return []
            publisher_id = job["account_id"]
            publisher = conn.execute(
                "SELECT user_id FROM xhs_accounts WHERE id=?", (publisher_id,)
            ).fetchone()
            # 主页路径定位依赖发布者 user_id;没有就无从进主页,直接放弃(不猜)
            if publisher is None or not (publisher["user_id"] or "").strip():
                logger.warning(
                    f"[matrix_interact] 发布者账号{publisher_id} 无 user_id,"
                    f"无法走主页路径,跳过 job={publish_job_id} 的矩阵互动"
                )
                return []
            dup = conn.execute(
                "SELECT id FROM browser_jobs WHERE kind='matrix_interact'"
                " AND json_extract(payload, '$.source_publish_job_id')=? LIMIT 1",
                (publish_job_id,),
            ).fetchone()
            if dup is not None:
                logger.info(
                    f"[matrix_interact] 发布 job={publish_job_id} 已登记过互动任务,跳过"
                )
                return []
            # 矩阵 = 全部 cookie_status='valid' 的账号,排除发布者本人(设计 4.1:
            # operator 是权限维度不是矩阵维度,按 operator 划分会永久排除只归单人的号)
            matrix_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM xhs_accounts WHERE cookie_status='valid'"
                    " AND id != ? ORDER BY id",
                    (publisher_id,),
                ).fetchall()
            ]

        now = datetime.utcnow()
        job_ids: list[str] = []
        for account_id in matrix_ids:
            payload = {
                "source_publish_job_id": publish_job_id,
                "publisher_account_id": publisher_id,
                "publisher_user_id": publisher["user_id"],
                "title": job["title"],
                # 评论文案入参(后续承载营销钩子话术);为空则只点赞收藏,不做 LLM 生成
                "comment": "",
                # 窗口内的随机执行时刻:派发侧按它过滤,执行方不 sleep 等待
                "not_before": (
                    now + timedelta(seconds=random.uniform(0, WINDOW_SECONDS))
                ).isoformat(sep=" "),
            }
            # operator_id=0(非请求上下文的进程内直调):不记到发布者名下,否则这批
            # queued 行会占掉运营的 OPERATOR_PENDING_QUOTA 未终态配额长达一个窗口。
            job_ids.append(
                browser_jobs_repo.enqueue_sync(
                    db_path, "matrix_interact", payload, 0, account_id=account_id
                )
            )
        logger.info(
            f"[matrix_interact] 发布 job={publish_job_id} 已登记 {len(job_ids)} 条矩阵"
            f"互动任务(账号 {matrix_ids},窗口 {WINDOW_SECONDS}s)"
        )
        return job_ids
    except Exception as exc:  # noqa: BLE001 — 登记绝不阻断发布终态
        logger.warning(
            f"[matrix_interact] 登记互动任务失败 job={publish_job_id}(忽略,不阻断发布): {exc}"
        )
        return []


# ---------------- 契约执行(account_worker 子进程消费)----------------


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次矩阵互动(契约函数,不碰 browser_jobs 台账)。

    payload: ``{"publisher_user_id","title","comment",...}``。成功返回
    ``{"note_url","actions"}``;定位失败 / 任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    publisher_user_id = (payload.get("publisher_user_id") or "").strip()
    title = (payload.get("title") or "").strip()
    comment = payload.get("comment") or ""
    if not publisher_user_id or not title:
        return {"error": "payload 缺 publisher_user_id / title,无法定位目标笔记"}
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过互动"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。
            async with browser_slot():
                return await asyncio.to_thread(
                    _interact_sync, account_id, cookies, publisher_user_id, title, comment
                )
    except MatrixInteractError as exc:
        # 定位类语义失败(笔记没找到 / 详情没打开):记 error,不重跑
        logger.warning(
            f"矩阵互动失败 account_id={account_id} title={title!r} reason={exc.reason}"
        )
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"矩阵互动任务异常 account_id={account_id}")
        return {"error": f"矩阵互动任务异常:{exc}"}


def _interact_sync(
    account_id: int,
    cookies: list[dict],
    publisher_user_id: str,
    title: str,
    comment: str,
) -> dict:
    """同一线程内:建 SyncClient → start → 互动 → stop 收尾(finally 防泄漏 camoufox)。

    headed 真屏沿用 SyncClient 默认(headless=False,自动接当前图形会话);互动要点真实
    卡片与按钮,不 block_images(缺封面会影响卡片布局与坐标)。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise MatrixInteractError(f"browser_start_failed: {start.get('error')}")
        return interact_with_note(
            client.page, account_id, publisher_user_id, title, comment
        )
    finally:
        client.stop()
