"""账号子进程:一个账号一批任务串行执行完即退出(API/Worker 拆分设计的派发单元)。

CLI 契约(supervisor 派生调用)::

    python -m app.account_worker --account-id N [--publish-job-ids 1,2] \
        [--browser-job-ids h1,h2] [--db data/nbdpsy.db]

同进程串行:先发布任务(按 id 升序)后 browser jobs。每账号独立 OS 进程 + 独立
camoufox 真屏会话——本模块只是换了宿主进程调用既有拟人层(``sync_client.publish_once``
一行不改),不引入任何新浏览器行为。

台账纪律(全部 sqlite3 短事务,WAL 并发安全):
- publish 认领 = 乐观 ``UPDATE status pending->publishing``(rowcount=1 才算领到);
- browser job 认领 = ``browser_jobs_repo.claim_job_sync``(同款乐观 UPDATE);
- 发布终态语义 = ``app.publish.policy.decide_finish``(与 scheduler.finish 逐字节同源);
- 发布前冷却/日上限门:不满足 → 顺延 ``next_retry_at = now + uniform(MIN, MAX)`` 秒,
  不递增 retries、不执行;
- browser job 在跑期间心跳线程 300s touch ``heartbeat_at``(僵死判定 900s 归 supervisor);
- 进程退出码恒 0(个别 job 失败记录在台账行);任何异常兜底写终态,绝不留
  running/publishing 悬挂(publish 悬挂由入口 finally 归位 pending + next_retry)。
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from app.browser import sync_client
from app.browser.images import materialize_images
from app.core.config import settings
from app.core.security import decrypt_cookies
from app.publish.policy import cooldown_remaining_s, daily_cap_reached, decide_finish

# browser job 在跑心跳周期(秒);supervisor 侧僵死判定阈值 900s,300s 留足 3 次机会
HEARTBEAT_INTERVAL_S = 300


def _browser_jobs_repo():
    """延迟导入 browser_jobs_repo(与 Agent A 并行开发,按契约签名调用)。

    函数内 import:测试可向 ``sys.modules`` 注入桩模块,不依赖真实现、不起浏览器。
    """
    from app.services import browser_jobs_repo

    return browser_jobs_repo


# ─────────────────────────── sqlite3 基础 ───────────────────────────


def _connect(db_path: str) -> sqlite3.Connection:
    """开一条短事务连接(busy_timeout 防并发写锁竞争立即报错)。"""
    conn = sqlite3.connect(db_path, timeout=settings.SQLITE_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_dt(dt: datetime) -> str:
    """naive UTC datetime → SQLAlchemy sqlite 同款存储格式("YYYY-MM-DD HH:MM:SS.ffffff")。"""
    return dt.isoformat(sep=" ")


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    """存储格式 → naive UTC datetime;空值 → None。"""
    if not raw:
        return None
    return datetime.fromisoformat(raw)


# ─────────────────────────── publish 侧 ───────────────────────────


def _load_publish_job(db_path: str, job_id: int) -> Optional[dict]:
    """回读一条 publish_jobs 行(dict);不存在 → None。"""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM publish_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def _load_account_cookies(db_path: str, account_id: int) -> list[dict]:
    """读账号加密 cookie 并解密回列表(语义 = scheduler._decrypt_account_cookies)。

    后台发布无 operator 上下文,不走 cookie_service 鉴权,直接解密(账号能被派发布
    本身即代表已授权)。空/无 cookie → []。
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT login_cookies FROM xhs_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if row is None or not row["login_cookies"]:
        return []
    plaintext = decrypt_cookies(row["login_cookies"])
    if not plaintext:
        return []
    return json.loads(plaintext)


def _publish_gate(db_path: str, account_id: int, job_id: int, prefix: str) -> bool:
    """发布前冷却/日上限门(契约:不满足 → 顺延、不递增 retries、不执行)。

    - 冷却:该号最近一条 published/publishing 的 started_at(自排除本 job),间隔现抽
      ``uniform(PUBLISH_MIN_INTERVAL_MIN, PUBLISH_MIN_INTERVAL_MAX)``(抖动化防节律指纹);
    - 日上限:当日(UTC 自然日)已 published 数达 ``PUBLISH_DAILY_CAP``;
    - 任一不满足:``next_retry_at = now + uniform(MIN, MAX)`` 顺延(保持 pending,
      下批派发再捞),返回 False 不执行。
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT started_at FROM publish_jobs"
            " WHERE account_id = ? AND status IN ('published', 'publishing')"
            " AND started_at IS NOT NULL AND id != ?"
            " ORDER BY started_at DESC LIMIT 1",
            (account_id, job_id),
        ).fetchone()
        last_started = _parse_dt(row["started_at"]) if row is not None else None
        published_today = conn.execute(
            "SELECT COUNT(*) FROM publish_jobs"
            " WHERE account_id = ? AND status = 'published' AND started_at >= ?",
            (account_id, _fmt_dt(today_start)),
        ).fetchone()[0]

    interval = random.uniform(
        settings.PUBLISH_MIN_INTERVAL_MIN, settings.PUBLISH_MIN_INTERVAL_MAX
    )
    remaining = cooldown_remaining_s(last_started, now, interval)
    cap_hit = daily_cap_reached(published_today, settings.PUBLISH_DAILY_CAP)
    if remaining <= 0 and not cap_hit:
        return True

    # 顺延:保持 pending、只推 next_retry_at,retries 不动
    delay = random.uniform(
        settings.PUBLISH_MIN_INTERVAL_MIN, settings.PUBLISH_MIN_INTERVAL_MAX
    )
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE publish_jobs SET next_retry_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (_fmt_dt(now + timedelta(seconds=delay)), job_id),
        )
        conn.commit()
    logger.info(
        "{} publish job {} 冷却/日上限未满足(冷却剩余 {:.0f}s, 当日已发 {}),顺延 {:.0f}s",
        prefix, job_id, max(remaining, 0.0), published_today, delay,
    )
    return False


def _claim_publish(db_path: str, job_id: int, now: datetime) -> bool:
    """乐观认领:``UPDATE ... SET publishing WHERE status='pending'``,rowcount=1 才算领到。"""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE publish_jobs SET status = 'publishing', started_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (_fmt_dt(now), job_id),
        )
        conn.commit()
        return cur.rowcount == 1


def _apply_publish_decision(
    db_path: str,
    job_id: int,
    decision: dict,
    note_id: str = "",
    note_url: str = "",
) -> None:
    """把 decide_finish 裁决落库(C1 守卫:仅 status='publishing' 可落,防越权覆盖)。"""
    now = datetime.utcnow()
    with closing(_connect(db_path)) as conn:
        if decision["status"] == "published":
            # 成功:回填 note、清 error;started_at 保留(冷却门的历史依据)
            conn.execute(
                "UPDATE publish_jobs SET status = 'published', note_id = ?,"
                " note_url = ?, error = NULL"
                " WHERE id = ? AND status = 'publishing'",
                (note_id, note_url, job_id),
            )
        elif decision["status"] == "pending":
            # 排重试:retries 递增 + next_retry_at 排期 + 回 pending
            next_retry = now + timedelta(seconds=decision["next_retry_delta_s"])
            conn.execute(
                "UPDATE publish_jobs SET status = 'pending', started_at = NULL,"
                " error = ?, retries = retries + 1, next_retry_at = ?"
                " WHERE id = ? AND status = 'publishing'",
                (decision["error"], _fmt_dt(next_retry), job_id),
            )
        else:
            # 终态 failed(need_manual_login / account_restricted / 重试耗尽)
            conn.execute(
                "UPDATE publish_jobs SET status = 'failed', started_at = NULL,"
                " error = ? WHERE id = ? AND status = 'publishing'",
                (decision["error"], job_id),
            )
        conn.commit()


def _requeue_if_hanging(db_path: str, job_id: int, prefix: str) -> None:
    """入口 finally 兜底:job 仍卡 publishing(落库路径双重失败)→ 归位 pending + next_retry。

    正常路径 ``_apply_publish_decision`` 已把状态改走,这里的 UPDATE 命不中(rowcount=0)
    即 no-op;命中说明前面全挂了,按首档重试延迟(带 0.8~1.5 抖动)排期,绝不留悬挂。
    """
    delays = settings.retry_delays
    delay = (delays[0] if delays else 60) * random.uniform(0.8, 1.5)
    next_retry = datetime.utcnow() + timedelta(seconds=delay)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE publish_jobs SET status = 'pending', started_at = NULL,"
            " next_retry_at = ? WHERE id = ? AND status = 'publishing'",
            (_fmt_dt(next_retry), job_id),
        )
        conn.commit()
    if cur.rowcount == 1:
        logger.warning(
            "{} publish job {} 落库路径全挂,finally 兜底归位 pending(next_retry {:.0f}s 后)",
            prefix, job_id, delay,
        )


def _execute_publish(db_path: str, account_id: int, job: dict):
    """物料化图片 + 调既有拟人层真发布(``sync_client.publish_once``,一行不改)。

    临时物料目录发布结束(无论成败)清理。返回 ``PublishResult``。
    """
    cookies = _load_account_cookies(db_path, account_id)
    raw_images = json.loads(job["images_json"] or "[]")
    topics = json.loads(job["topics_json"] or "[]")
    workdir = Path(settings.UPLOAD_DIR) / f"job_{job['id']}"
    try:
        image_paths = [str(p) for p in materialize_images(raw_images, workdir)]
        return sync_client.publish_once(
            account_id, cookies, job["title"], job["content"], image_paths, topics
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_publish_job(db_path: str, account_id: int, job_id: int) -> None:
    """处理一条发布任务:门 → 认领 → 发布 → 终态落库;异常兜底,绝不留 publishing。"""
    prefix = f"[account_worker a{account_id}]"
    job = _load_publish_job(db_path, job_id)
    if job is None or job["status"] != "pending":
        logger.info(
            "{} publish job {} 非 pending(当前 {}),跳过",
            prefix, job_id, job["status"] if job else "不存在",
        )
        return
    if not _publish_gate(db_path, account_id, job_id, prefix):
        return
    if not _claim_publish(db_path, job_id, datetime.utcnow()):
        logger.info("{} publish job {} 认领失败(已被别处处理),跳过", prefix, job_id)
        return
    try:
        result = _execute_publish(db_path, account_id, job)
        decision = decide_finish(
            result.success,
            bool(getattr(result, "need_manual_login", False)),
            bool(getattr(result, "account_restricted", False)),
            result.error,
            job["retries"],
            settings.retry_delays,
        )
        _apply_publish_decision(
            db_path,
            job_id,
            decision,
            note_id=getattr(result, "note_id", "") or "",
            note_url=getattr(result, "note_url", "") or "",
        )
        logger.info("{} publish job {} 完成,终态 {}", prefix, job_id, decision["status"])
    except Exception as exc:
        # publish_once 内部已把可预期失败转成 PublishResult;能逃到这里的是载数据/物料化/
        # 落库等意外异常。兜底按普通失败裁决,交回重试/退避机制。
        logger.exception("{} publish job {} 执行异常,兜底转失败", prefix, job_id)
        decision = decide_finish(
            False, False, False, str(exc), job["retries"], settings.retry_delays
        )
        try:
            _apply_publish_decision(db_path, job_id, decision)
        except Exception:
            logger.exception("{} publish job {} 兜底落库也失败", prefix, job_id)
    finally:
        # 悬挂兜底:仍卡 publishing 才命中(正常路径 no-op)
        _requeue_if_hanging(db_path, job_id, prefix)


# ─────────────────────────── browser job 侧 ───────────────────────────


def _resolve_execute(kind: str) -> Callable[[Optional[int], dict], Any]:
    """按 kind 解析各 service 的 ``execute`` 协程(统一成 (account_id, payload) 签名)。

    lambda 内取模块属性 = 调用时晚绑定,测试 monkeypatch ``execute`` 即生效。
    """
    if kind == "cookie_check":
        from app.services import cookie_check

        return lambda account_id, payload: cookie_check.execute(account_id, payload)
    if kind == "note_export":
        from app.services import note_export

        return lambda account_id, payload: note_export.execute(account_id, payload)
    if kind == "note_delete":
        from app.services import note_delete

        return lambda account_id, payload: note_delete.execute(account_id, payload)
    if kind == "op_images":
        from app.services import op_images

        # op_images 无账号绑定,execute 只收 payload
        return lambda account_id, payload: op_images.execute(payload)
    raise ValueError(f"未知 browser job kind: {kind}")


def _heartbeat_loop(
    db_path: str,
    job_id: str,
    stop_event: threading.Event,
    interval_s: float = HEARTBEAT_INTERVAL_S,
) -> None:
    """心跳守护线程体:每 interval_s touch 一次 heartbeat_at,stop_event 置位即退。"""
    while not stop_event.wait(interval_s):
        try:
            _browser_jobs_repo().heartbeat_sync(db_path, job_id)
        except Exception:
            logger.exception("browser job {} 心跳写入失败(不中断执行)", job_id)


def run_browser_job(
    db_path: str, account_id: int, job_id: str, worker_tag: str
) -> None:
    """处理一条 browser job:认领 → 心跳线程 → execute → 终态写回;异常兜底置 error。"""
    prefix = f"[account_worker a{account_id}]"
    repo = _browser_jobs_repo()
    row = repo.claim_job_sync(db_path, job_id, worker_tag)
    if row is None:
        logger.info("{} browser job {} 认领失败(非 queued),跳过", prefix, job_id)
        return
    kind = row.get("kind", "")
    payload = row.get("payload") or {}
    if isinstance(payload, str):  # 防御:repo 未反序列化时兜底解一次
        payload = json.loads(payload) if payload else {}

    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(db_path, job_id, stop_event),
        daemon=True,
        name=f"heartbeat-{job_id}",
    )
    hb_thread.start()
    try:
        execute = _resolve_execute(kind)
        result = asyncio.run(execute(row.get("account_id") or account_id, payload))
        if not isinstance(result, dict):
            result = {"error": f"execute 返回非 dict: {type(result).__name__}"}
        status = "error" if result.get("error") else "done"
        repo.finish_job_sync(db_path, job_id, status, result, worker_tag)
        logger.info("{} browser job {}({}) 完成,终态 {}", prefix, job_id, kind, status)
    except Exception as exc:
        # 任何异常(未知 kind / execute 崩 / asyncio 崩)兜底写 error 终态,绝不留 running
        logger.exception("{} browser job {}({}) 执行异常,兜底置 error", prefix, job_id, kind)
        try:
            repo.finish_job_sync(db_path, job_id, "error", {"error": str(exc)}, worker_tag)
        except Exception:
            logger.exception("{} browser job {} 兜底写终态也失败", prefix, job_id)
    finally:
        stop_event.set()
        hb_thread.join(timeout=5)


# ─────────────────────────── CLI 入口 ───────────────────────────


def _parse_int_ids(raw: str) -> list[int]:
    """逗号分隔 → int 列表,升序(publish 按 id 升序串行的契约)。"""
    return sorted(int(x) for x in raw.split(",") if x.strip())


def _parse_str_ids(raw: str) -> list[str]:
    """逗号分隔 → str 列表,保持传入顺序(browser job id 是 uuid/复合串)。"""
    return [x.strip() for x in raw.split(",") if x.strip()]


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 主入口:同进程串行,先 publish(id 升序)后 browser jobs;退出码恒 0。"""
    parser = argparse.ArgumentParser(
        prog="python -m app.account_worker",
        description="账号子进程:串行执行该账号本批 publish / browser 任务后退出",
    )
    parser.add_argument("--account-id", type=int, required=True, help="账号 id")
    parser.add_argument(
        "--publish-job-ids", default="", help="逗号分隔的 publish_jobs id(可空)"
    )
    parser.add_argument(
        "--browser-job-ids", default="", help="逗号分隔的 browser_jobs id(可空)"
    )
    parser.add_argument("--db", default="data/nbdpsy.db", help="SQLite 数据库路径")
    args = parser.parse_args(argv)

    prefix = f"[account_worker a{args.account_id}]"
    publish_ids = _parse_int_ids(args.publish_job_ids)
    browser_ids = _parse_str_ids(args.browser_job_ids)
    worker_tag = f"account_worker:a{args.account_id}:pid{os.getpid()}"
    logger.info(
        "{} 启动:publish jobs {} / browser jobs {} / db {}",
        prefix, publish_ids, browser_ids, args.db,
    )

    # 单 job 失败已在各 run_* 内兜底写终态;这里再兜一层保证"后续 job 照跑 + 退出码 0"
    for job_id in publish_ids:
        try:
            run_publish_job(args.db, args.account_id, job_id)
        except Exception:
            logger.exception("{} publish job {} 处理崩溃(已跳过,继续后续)", prefix, job_id)
    for job_id in browser_ids:
        try:
            run_browser_job(args.db, args.account_id, job_id, worker_tag)
        except Exception:
            logger.exception("{} browser job {} 处理崩溃(已跳过,继续后续)", prefix, job_id)

    logger.info("{} 本批任务全部处理完毕,退出", prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
