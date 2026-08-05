"""笔记媒体清单抓取服务(kind=``note_media_sync``):详情页只读 → 归一化 → 落台账。

与 ``note_purpose`` 回填同构(号锁 → 浏览器闸 → 线程内同步浏览器,异常收敛成
``{"error": ...}`` 绝不上抛),两条纪律照抄:

1. **节流**:每轮最多 ``limit`` 篇(默认 12),同一会话内连续开详情页,篇间有人味停顿;
   ``media_fetched_at`` 非空的篇**不再开页**(重跑不白烧会话);
2. **只抓公开笔记**(``permission_code == 0``):私密篇读者看不到,不值一次会话;NULL
   是未知,同样不抓。

**只存链接不落文件**(2026-08-05 实测定案,见 ``app.browser.note_media`` docstring):
平台给的带签名 URL 18 天就过期,归一化成 ``sns-img-qc/{段}/{file_id}`` 则永久有效且是
原图 —— 于是台账存清单、要图时按需下载,省几十 GB 且画质更高。

纯只读 + 按行 upsert,故**幂等**(进 ``_IDEMPOTENT_KINDS``,僵死可自动重跑)。
"""

import asyncio
import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_media import fetch_note_media
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

JOB_KIND = "note_media_sync"

# 单轮(单次浏览器会话)最多开几篇详情页。与正文回填同量级:会话数才是风控成本,
# 同一会话内多开几篇几乎免费,但一轮太长会占着号锁挡住发布。
DEFAULT_LIMIT = 12


def start_sync(account_id: int, note_id: str | None = None, limit: int | None = None) -> str:
    """登记一次媒体清单抓取,返回轮询 id(REST/编排共用)。"""
    payload: dict = {}
    if note_id:
        payload["note_id"] = note_id
    if limit:
        payload["limit"] = int(limit)
    job_id = browser_jobs_repo.enqueue_from_request(JOB_KIND, payload, account_id=account_id)
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


async def pick_targets(session, account_id: int, note_id: str | None, limit: int) -> list[dict]:
    """挑这一轮要抓的篇:有 note_id + 公开 + 还没抓过,按发布时间倒序。

    不要求 xsec_token:媒体走**编辑页**(深链只要 note_id),token 是详情页那条废弃路
    才需要的东西 —— 要求它会把台账里没同步到 token 的篇白白漏掉。

    ``note_id`` 显式指定时只放宽"还没抓过"(点名重抓某篇理应能重来),公开性仍照旧
    —— 那是可行性约束不是偏好。
    """
    stmt = select(PublishedNote).where(
        PublishedNote.account_id == account_id,
        PublishedNote.note_id.is_not(None),
        PublishedNote.note_id != "",
        PublishedNote.permission_code == 0,
    )
    if note_id:
        stmt = stmt.where(PublishedNote.note_id == note_id)
    else:
        stmt = stmt.where(PublishedNote.media_fetched_at.is_(None))
    rows = (
        (
            await session.execute(
                stmt.order_by(
                    PublishedNote.platform_published_at.desc(),
                    PublishedNote.published_at.desc(),
                    PublishedNote.id.desc(),
                ).limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "note_id": row.note_id,
            "xsec_token": row.xsec_token,
            "xsec_source": row.xsec_source,
        }
        for row in rows
    ]


async def execute(account_id: int, payload: dict) -> dict:
    """抓一批笔记的媒体清单并落台账(契约函数,不碰 browser_jobs 台账)。

    返回 ``{"picked","fetched","media_total","failed"}``;无 cookie / 没得可抓 / 任何
    异常 → ``{"error": reason}``,**不抛出**。
    """
    try:
        limit = int(payload.get("limit") or DEFAULT_LIMIT)
        note_id = payload.get("note_id")
        async with get_session() as session:
            targets = await pick_targets(session, account_id, note_id, limit)
        if not targets:
            return {"picked": 0, "fetched": 0, "media_total": 0, "failed": []}

        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": f"账号 {account_id} 无可用 cookie"}

        async with account_locks.get(account_id):
            async with browser_slot():
                results = await asyncio.to_thread(
                    _fetch_sync, account_id, cookies, targets
                )
        if "error" in results:
            return results

        fetched = 0
        media_total = 0
        failed: list[dict] = []
        async with get_session() as session:
            for target in targets:
                item = results.get(target["note_id"]) or {}
                if "error" in item:
                    failed.append({"note_id": target["note_id"], "reason": item["error"]})
                    continue
                row = await session.get(PublishedNote, target["id"])
                if row is None:
                    continue
                media = item.get("media") or []
                row.media_json = json.dumps(media, ensure_ascii=False)
                # 抓到空清单也记时刻:"看过了,这篇页面上没图"也是事实,别重开会话
                row.media_fetched_at = datetime.utcnow()
                fetched += 1
                media_total += len(media)
            await session.commit()
        logger.info(
            f"[note_media] 账号{account_id} 抓 {len(targets)} 篇,成功 {fetched},"
            f"媒体 {media_total} 项,失败 {len(failed)}"
        )
        return {
            "picked": len(targets),
            "fetched": fetched,
            "media_total": media_total,
            "failed": failed,
        }
    except Exception as exc:  # noqa: BLE001 — 收敛成结果,绝不上抛
        logger.exception(f"媒体清单抓取异常 account_id={account_id}")
        return {"error": f"媒体清单抓取异常:{exc}"}


def _fetch_sync(account_id: int, cookies: list[dict], targets: list[dict]) -> dict:
    """线程内:起浏览器 → 逐篇只读抓 → 关。基础设施失败收敛成 {"error"}。"""
    # 不拦图:2026-08-05 探针拿到 img URL 是在**不拦图**下(currentSrc 有值)。拦图省流量,
    # 但可能让懒加载图的 currentSrc 落空 —— 未验证的优化不做(E-gate 纪律)。
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"error": f"浏览器启动失败:{start.get('error')}"}
        return fetch_note_media(client.page, account_id, targets)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"浏览器异常:{exc}"}
    finally:
        client.stop()
