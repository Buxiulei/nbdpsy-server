"""受众事件采集服务:契约 ``execute()``(kind=``audience_sync``)+ 落库与游标推进。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 3 节。
分层与 ``note_extract_comments`` 一致:浏览器动作在 ``app.browser.audience_collect``,
本模块只管取 cookie、并发闸、解析落库、推游标。调度在 ``audience_sync_scheduler``。

**幂等**(纯只读 + ``ON CONFLICT DO NOTHING`` 入库 + 游标只进不退),进
``browser_jobs_repo._IDEMPOTENT_KINDS``:僵死可自动重跑,重跑最多把同一段重采一遍。

## 游标推进的两种死法(都很安静,所以单测锁死了)

- **不往前推** → 每轮从头翻 47 页,增量退化成全量,把该省的真号会话全烧回去;
- **推过头**(比如按"本轮最老一条"写)→ 中间那段永远补不回来,而库里看着挺满,
  没有任何报错告诉你缺了两个月。

正确口径只有一个:``last_event_time = max(本轮见过的 event_time, 原值)``。取 max 而不是
直接赋值,是因为增量轮完全可能只捞到几条比游标还老的事件(平台偶尔乱序下发),
那时候赋值就是把游标拉回去。

## 为什么没新事件也要刷 updated_at

``updated_at`` 是调度器的**到期判据**。不刷的话"这个号最近没人互动"会被读成"这个号还
没采过",于是每轮都挑中它 —— 会话额度全烧在最冷清的号上,而热闹的号反而排不上。
"""

import asyncio
import time
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser import audience_collect
from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.sync_client import SyncClient
from app.browser.sync_human_actions import SyncHumanActions
from app.core.db import get_session
from app.models.audience_sync_state import AudienceSyncState
from app.services import risk_events
from app.services.audience_events import (
    normalize_connection_event, normalize_like_event, upsert_events,
)
from app.services.cookie_check import load_account_cookies

JOB_KIND = "audience_sync"

# 单次采集的浏览器会话预算(秒)。到点无论采到哪一步都收尾 —— 真号会话开越久风控面越大,
# 而"采了一半"下一轮增量接着来就补上了,代价远小于"会话被硬超时强杀"。
# 账号子进程硬超时是 1800s(ACCOUNT_PROC_TIMEOUT),这里留足余量。
SESSION_BUDGET_S = 600

# 两条 channel 各自的解析器(likes 与 connections 字段形状完全不同,见 audience_events)
_NORMALIZERS = {
    audience_collect.CHANNEL_LIKES: normalize_like_event,
    audience_collect.CHANNEL_CONNECTIONS: normalize_connection_event,
}


async def execute(account_id: int, payload: dict) -> dict:
    """采一轮该号的受众事件(契约函数,不碰 browser_jobs 台账)。

    payload:``{"account_id": int, "full": bool}``(``full`` 省略即增量)。

    Returns:
        ``{"account_id","full","inserted","dropped","channels":{...}}``;
        无 cookie / 未登录 / 撞墙 / 任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    full = bool(payload.get("full"))
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": f"账号 {account_id} 无可用 cookie,不开会话"}

        async with get_session() as session:
            targets = await load_targets(session, account_id)

        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _collect_sync, account_id, cookies, targets, full
                )

        if result.get("wall"):
            # 撞墙留痕:与 cookie_check / interaction_backfill 同一张 risk_events 台账,
            # 运营看板才能把"这个号今天撞了几次墙"看全(record_wall 自己不上抛)
            await risk_events.record_wall(
                get_session, account_id, result["wall"], JOB_KIND
            )
        if "error" in result:
            return result

        async with get_session() as session:
            summary = await persist(session, account_id, result["channels"], full)
        return {"account_id": account_id, "full": full, **summary}
    except Exception as exc:  # noqa: BLE001 — 收敛成结果,绝不上抛
        logger.exception(f"受众采集异常 account_id={account_id}")
        return {"error": f"受众采集异常:{exc}"}


def _collect_sync(account_id: int, cookies: list[dict], targets: dict, full: bool) -> dict:
    """线程内:起浏览器 → 进通知页 → 只读采两条流 → 关。基础设施失败收敛成 error。"""
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"error": f"浏览器启动失败:{start.get('error')}"}
        page = client.page
        return audience_collect.collect_audience(
            page, SyncHumanActions(page),
            targets=targets, full=full,
            deadline=time.monotonic() + SESSION_BUDGET_S,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"浏览器异常:{exc}"}
    finally:
        client.stop()


async def load_targets(session: AsyncSession, account_id: int) -> dict[str, int | None]:
    """读该号两条 channel 的增量游标;没有行的给 None(首采走全量)。"""
    rows = (await session.execute(
        select(AudienceSyncState).where(AudienceSyncState.account_id == account_id)
    )).scalars().all()
    stored = {r.channel: r.last_event_time for r in rows}
    return {ch: stored.get(ch) for ch in audience_collect.CHANNELS}


async def persist(
    session: AsyncSession, account_id: int, channels: dict[str, dict], full: bool
) -> dict:
    """把采回来的 message 解析入库并推进游标。返回本轮汇总。

    ``dropped`` 是解析器认不出/字段残缺被丢掉的条数:平台加一种新通知不该打死整轮采集,
    但**也不能悄悄丢** —— 它长期非 0 就说明解析器该补分叉了,所以它进回执。
    """
    inserted = dropped = 0
    per_channel: dict[str, dict] = {}

    for channel, block in channels.items():
        normalize = _NORMALIZERS.get(channel)
        if normalize is None:  # pragma: no cover - CHANNELS 之外的 key 不该出现
            logger.warning(f"[audience_sync] 未知 channel={channel},跳过")
            continue
        messages = block.get("messages") or []
        rows = []
        for msg in messages:
            row = normalize(msg, account_id)
            if row is None:
                dropped += 1
                continue
            rows.append(row)

        added = await upsert_events(session, rows)
        inserted += added
        # **max(本轮最大, 原值)**,不是直接赋值:增量轮可能只捞到几条比游标还老的事件,
        # 赋值就等于把游标拉回去,下一轮又从那儿重采一遍
        newest = max((r["event_time"] for r in rows), default=None)
        await _bump_state(session, account_id, channel, newest, full)
        per_channel[channel] = {
            "messages": len(messages),
            "inserted": added,
            "stopped_by": block.get("stopped_by"),
            "rounds": block.get("rounds"),
            "pages": block.get("pages"),
        }

    await session.commit()
    return {"inserted": inserted, "dropped": dropped, "channels": per_channel}


async def _bump_state(
    session: AsyncSession, account_id: int, channel: str,
    newest: int | None, full: bool,
) -> None:
    """推进(或新建)一条 channel 的游标行。**没新事件也刷 updated_at**(见模块 docstring)。"""
    now = datetime.utcnow()
    state = await session.get(AudienceSyncState, (account_id, channel))
    if state is None:
        state = AudienceSyncState(account_id=account_id, channel=channel)
        session.add(state)
    if newest is not None:
        state.last_event_time = max(newest, state.last_event_time or 0)
    if full:
        state.last_full_sync_at = now
    state.updated_at = now
