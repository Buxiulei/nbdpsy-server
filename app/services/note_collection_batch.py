"""合集批量清理:一次会话里连续**扫描**(P1 名单)或**移出**(P2)多篇笔记。

2026-08-07 运营需求(``/home/roots/NBDpsy/docs/2026-08-07-移出合集能力-需求.md``):
skill 侧把约 36 篇科普笔记误归拢进「咨询师简介」类合集(6 账号约 100 篇合集成员),
逐篇跑 ``POST /note-components`` 要开一百次浏览器会话 —— 而**会话频次才是被弹墙的直接
原因**(``interaction_backfill`` 模块 docstring 的实测结论:同号一小时 5 次会话就能把号
打上验证墙)。所以批量的正确形态是 **一轮一会话里连续处理 N 篇**,不是一篇一 job。

一个 kind 两条路,靠 ``dry_run`` 分:

- ``dry_run=true``(**P1 名单**):逐篇进更新页只**读**合集区,报这篇在不在目标合集。
  **零点击零提交**,风险约等于浏览自己的笔记,故单轮上限放到 60 篇。
- ``dry_run=false``(**P2 批量移出**):逐篇走 ``set_note_components(remove_collection_id=…)``
  —— 与单篇端点**同一条代码路径**(同一次悬停 → 点 × → 提交 → 重进页面回读),
  批量在这里只负责"把它跑 N 遍并管住节奏"。单轮上限 5 篇。

P1 为什么是"扫描"而不是"读合集页的成员接口":调研已确证 ``note/collection/pc/list_v2``
的响应体只有 ``id/name/desc/note_num`` 四键,**没有成员列表**;而合集详情页的入口 / URL /
是否存在分页成员接口,取证轮尚未跑到(设计 docs/design/2026-08-07-collection-remove-design.md §6-5)。在拿到实证之前编一个页面路径
出来点,正是本仓最忌讳的"猜着点"。扫描路径只用两个**已三次独立验证过**的既有能力
(``open_update_page`` 深链 + ``read_collection_label`` 只读),一行新选择器都不引入。
等成员接口取证到手,换掉这条路的数据源即可,对外契约不变。

四条纪律照抄 ``interaction_backfill``(每一条都是实测踩出来的):

1. **一轮一会话**:整轮持有 ``browser_slot``,N 篇共用一个 camoufox;
2. **单轮预算**:``ROUND_BUDGET_SECONDS`` 内做几篇算几篇,剩下的 ``remaining`` 报给调用方,
   下一轮接着来(账号子进程有硬超时,撞上就是被强杀、连账都记不上);
3. **撞墙即停**:任一篇处理中 ``page.url`` 出现 ``captcha`` / ``website-login`` 立刻 break,
   剩余一篇不碰,该号置 ``cookie_status='restricted'`` 并落 ``risk_events``;
   **已完成的部分照常记账不回滚**,撞墙那一篇不记账(它压根没被真正处理);
4. **调用方的 ``limit`` 只能往小压** —— 单轮上限是风控闸,不是默认值。

``note_collection_batch`` **非幂等**,不进 ``browser_jobs_repo._IDEMPOTENT_KINDS``:
移出路每篇都是一次全量覆盖提交,僵死重跑等于再覆盖一遍、再开一次会话。
(移出**动作本身**是幂等的 —— 本就不在该合集 → skipped 且零点击 —— 但那救的是正确性,
救不了白开浏览器与风控暴露这两份代价,与 ``interaction_backfill`` 同款取舍。)
"""

import asyncio
import random
import time
from typing import Any, Optional

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.login_detector import PAGE_TEXT_JS, classify_wall_text, is_wall_url
from app.browser.note_components import (
    NoteComponentsError,
    _norm,  # 名字比对的空白归一必须与浏览器层同一条,否则两边名单对不上
    open_update_page,
    read_collection_label,
    set_note_components,
)
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo, risk_events
from app.services.cookie_check import load_account_cookies

# browser_jobs 的 kind(登记 / 派发 / 轮询三处同名)
JOB_KIND = "note_collection_batch"

# 单轮浏览器段的时间预算(秒):与 interaction_backfill 同值同理由 —— 账号子进程硬超时是
# ``ACCOUNT_PROC_TIMEOUT``(默认 1800s),撞上就是被强杀,已完成的部分连账都记不上。
ROUND_BUDGET_SECONDS = 1200

# 篇间间隔(秒)。移出那条路每篇是一次真提交,节奏必须比只读扫描慢得多。
# 刻意**不用** ``human.wait``:它带疲劳系数(最高 ×2)会把上界放大到顶穿单轮预算
# (与 interaction_backfill 同款理由)。
_REMOVE_GAP = (45.0, 120.0)
_SCAN_GAP = (6.0, 18.0)


def round_limit_of(requested: Optional[int], *, dry_run: bool) -> int:
    """本轮篇数上限:调用方给了就用它,但**永远不超过配置的单轮上限**。

    调用方传进来的 limit 只能往小了压,不能放大 —— 单轮上限是风控闸,不是默认值。
    扫描与移出各有各的帽子(只读 vs 一次真提交,代价差一个数量级)。
    """
    cap = max(1, int(
        settings.NOTE_COLLECTION_SCAN_ROUND_LIMIT if dry_run
        else settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT
    ))
    try:
        wanted = int(requested) if requested is not None else cap
    except (TypeError, ValueError):
        wanted = cap
    return max(1, min(cap, wanted))


def start_batch(
    account_id: int,
    collection_id: str,
    collection_name: str,
    note_ids: list[str],
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """REST 触发一轮合集批量清理;登记 browser_jobs 台账,返回 ``{"job_id", "planned"}``。

    ``planned`` 是本轮**打算**处理的篇数(已按单轮上限压过),让调用方一眼知道剩下的
    要再点一次 —— 而不是以为一次就全做完了。
    """
    take = round_limit_of(limit, dry_run=dry_run)
    payload = {
        "collection_id": collection_id,
        "collection_name": collection_name,
        "note_ids": list(note_ids),
        "dry_run": bool(dry_run),
        "limit": limit,
    }
    job_id = browser_jobs_repo.enqueue_from_request(
        JOB_KIND, payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return {"job_id": job_id, "planned": min(take, len(note_ids))}


async def execute(account_id: int, payload: dict) -> dict:
    """执行一轮合集批量清理(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"dry_run","collection_id","collection_name","picked","handled",
    "in_collection","removed","skipped","failed","remaining","notes":[…]}``;
    入参不合法 / 无 cookie / 浏览器起不来 / 撞墙 → 结果里带 ``"error"`` 键(台账落 error),
    **绝不抛出**。撞墙时已完成的部分照常留在 ``notes`` 里,不回滚。
    """
    payload = payload or {}
    collection_id = str(payload.get("collection_id") or "").strip()
    collection_name = str(payload.get("collection_name") or "").strip()
    note_ids = [str(n).strip() for n in (payload.get("note_ids") or []) if str(n).strip()]
    dry_run = bool(payload.get("dry_run"))
    if not collection_id or not note_ids:
        return {"error": "payload 缺 collection_id 或 note_ids,无事可做"}
    if not collection_name:
        # 移出路名字比对不上会被浏览器层逐篇拒绝(绝不盲点),那样整轮白开一次会话;
        # 扫描路更是完全靠名字判"在不在这个合集里"。在入口拦掉比排队后逐篇失败便宜得多。
        return {"error": "payload 缺 collection_name:扫描靠它判定成员,移出靠它确认"
                         "「当前所在合集就是目标」(比对不上绝不动手),两条路都必给"}

    take = round_limit_of(payload.get("limit"), dry_run=dry_run)
    targets = note_ids[:take]
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过合集批量清理"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 整轮(含篇间间隔)都在闸内 —— 会话频次的风险高于闸的周转率。
            async with browser_slot():
                outcome = await asyncio.to_thread(
                    _run_sync, account_id, cookies, targets,
                    collection_id, collection_name, dry_run,
                )
        result = _summarize(
            note_ids, targets, outcome,
            collection_id=collection_id, collection_name=collection_name, dry_run=dry_run,
        )
        wall = outcome.get("wall")
        if wall:
            await _handle_wall(account_id, wall)
            result["error"] = (
                f"撞风控墙({wall.get('wall_type')})已中止本轮:已完成 "
                f"{result['handled']}/{result['picked']} 篇,账号已置 restricted 并落 "
                f"risk_events;**不要立刻重试**,先按墙的类型处置(scan_qr=人工扫码,"
                f"rate_limit=晾置别再碰)。已完成的那几篇照常记在 notes 里,不会重做"
            )
        elif outcome.get("error"):
            result["error"] = outcome["error"]
        return result
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"合集批量清理任务异常 account_id={account_id}")
        return {"error": f"合集批量清理任务异常:{exc}"}


def _summarize(
    note_ids: list[str], targets: list[str], outcome: dict, *,
    collection_id: str, collection_name: str, dry_run: bool,
) -> dict:
    """逐篇结果 → 对外计数。``remaining`` 把"这轮没轮到的"如实报出来,别让人以为全做完了。"""
    notes = outcome.get("notes") or []
    handled_ids = {n["note_id"] for n in notes}
    return {
        "dry_run": dry_run,
        "collection_id": collection_id,
        "collection_name": collection_name,
        "picked": len(targets),
        "handled": len(notes),
        "in_collection": sum(1 for n in notes if n.get("in_collection") is True),
        "removed": sum(1 for n in notes if n.get("status") == "removed"),
        "skipped": sum(1 for n in notes if n.get("status") == "skipped"),
        "failed": sum(1 for n in notes if n.get("status") == "error"),
        "remaining": [n for n in note_ids if n not in handled_ids],
        "notes": notes,
    }


def _run_sync(
    account_id: int, cookies: list[dict], targets: list[str],
    collection_id: str, collection_name: str, dry_run: bool,
) -> dict:
    """同一线程内:建 SyncClient → start → **一次会话里逐篇处理** → stop 收尾。

    返回 ``{"notes": [...], "wall": dict|None, "error": str|None}``。

    headed 真屏沿用 SyncClient 默认(``headless=False``);**不 block_images** ——
    移出路要走完整提交流程,而发布按钮靠截图找「小红书红」质心,拦图会改变配色分布。
    """
    notes: list[dict] = []
    wall: Optional[dict] = None
    gap_range = _SCAN_GAP if dry_run else _REMOVE_GAP
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"notes": notes, "wall": None,
                    "error": f"browser_start_failed: {start.get('error')}"}
        page = client.page
        deadline = time.monotonic() + ROUND_BUDGET_SECONDS
        for index, note_id in enumerate(targets):
            if index:
                gap = random.uniform(*gap_range)
                if time.monotonic() + gap > deadline:
                    logger.info(
                        f"[note_collection_batch] 账号{account_id} 本轮预算用尽,"
                        f"剩余 {len(targets) - index} 篇留给下一轮"
                    )
                    break
                time.sleep(gap)
            entry = (
                _scan_one(page, account_id, note_id, collection_name) if dry_run
                else _remove_one(page, account_id, note_id, collection_id, collection_name)
            )
            wall = _wall_if_any(page)
            if wall is not None:
                # 撞墙那一篇**不记账**:它压根没被真正处理,记成 error 只会误导下一轮
                logger.warning(
                    f"[note_collection_batch] 账号{account_id} 撞墙,立刻中止本轮"
                    f"(已完成 {len(notes)} 篇)"
                )
                break
            notes.append(entry)
        return {"notes": notes, "wall": wall, "error": None}
    finally:
        client.stop()


def _scan_one(page, account_id: int, note_id: str, collection_name: str) -> dict:
    """扫描一篇(P1):进更新页**只读**合集区,判它在不在目标合集。零点击零提交。"""
    entry: dict[str, Any] = {"note_id": note_id, "status": "scanned",
                             "in_collection": None, "label": None, "reason": None}
    try:
        open_update_page(page, account_id, note_id)
        label = read_collection_label(page)
        entry["label"] = label
        # 判据与 ``_remove_collection`` 的名字比对**同一条**:合集区文案**全等**目标名才算
        # 成员。用包含判据会把「科普合集」的笔记算进「科普」的名单,而这份名单正是 P2 批量
        # 移出的输入 —— 假阳性会一路喂到破坏性操作。
        # 读不到(None=空态)就是不在任何合集里,不是"未知"。
        entry["in_collection"] = bool(label) and _norm(label) == _norm(collection_name)
    except NoteComponentsError as exc:
        entry["status"] = "error"
        entry["reason"] = exc.reason
    except Exception as exc:  # noqa: BLE001 — 单篇异常不阻断整轮(墙由调用方统一判)
        logger.warning(f"[note_collection_batch] 账号{account_id} 扫描 {note_id} 异常: {exc}")
        entry["status"] = "error"
        entry["reason"] = f"scan_exception: {exc}"
    return entry


def _remove_one(
    page, account_id: int, note_id: str, collection_id: str, collection_name: str
) -> dict:
    """移出一篇(P2):走**单篇端点同一条**代码路径,批量只负责跑 N 遍 + 管节奏。

    三态与单篇一致:``removed``(真移出并回读确认)/ ``skipped``(本就不在,零点击零提交)/
    ``error``(含"确认弹窗未取证"这类硬失败——那一篇整单中止不提交,笔记原样未动)。
    """
    entry: dict[str, Any] = {"note_id": note_id, "status": "error",
                             "in_collection": None, "reason": None, "detail": None}
    try:
        result = set_note_components(
            page, account_id, note_id,
            remove_collection_id=collection_id, remove_collection_name=collection_name,
        )
    except NoteComponentsError as exc:
        entry["reason"] = exc.reason
        return entry
    except Exception as exc:  # noqa: BLE001 — 单篇异常不阻断整轮
        logger.warning(f"[note_collection_batch] 账号{account_id} 移出 {note_id} 异常: {exc}")
        entry["reason"] = f"remove_exception: {exc}"
        return entry

    step = (result.get("components") or {}).get("collection_remove") or {}
    applied = (result.get("applied") or {}).get("collection_remove")
    entry["detail"] = {
        "applied": applied, "submitted": result.get("submitted"),
        "permission_preserved": result.get("permission_preserved"),
        "step_status": step.get("status"),
    }
    if applied is not True:
        # **只有 True 才算数**:这条产品线的失败是静默的(设计 2.6),
        # False=回读确认没生效,None=没能回读(状态未知),两者都不是成功。
        entry["reason"] = step.get("reason") or result.get("error") or (
            "collection_remove_not_verified: 提交后回读没确认移出生效"
        )
        return entry
    if step.get("status") == "skipped":
        entry["status"] = "skipped"
        entry["in_collection"] = False
        entry["reason"] = step.get("reason")
    else:
        entry["status"] = "removed"
        entry["in_collection"] = False
    return entry


def _wall_if_any(page) -> Optional[dict]:
    """当前页是不是风控验证墙;是则返回取证 dict(形状与 cookie 检测那套一致)。

    **URL 是硬判据**(``is_wall_url``),正文只用来分型(扫码 / 限流)——两者的运营动作
    不同。取证失败不影响判定:宁可少一段正文,也不能因为读不出文案漏掉真墙。
    """
    try:
        landed = page.url
    except Exception:  # noqa: BLE001 — 连 URL 都读不到(页没了),当没撞墙,交由上层收敛
        return None
    if not is_wall_url(landed):
        return None
    text = ""
    try:
        text = page.evaluate(PAGE_TEXT_JS) or ""
    except Exception:  # noqa: BLE001 — 取证失败不影响判定,URL 才是硬判据
        pass
    wall = {
        "wall_type": classify_wall_text(text),
        "target_url": landed,
        "landed_url": landed,
        "page_text": text,
    }
    logger.warning(
        f"[note_collection_batch] 撞风控墙 type={wall['wall_type']} landed={landed}"
    )
    return wall


async def _handle_wall(account_id: int, wall: dict) -> None:
    """撞墙处置:该号置 ``cookie_status='restricted'`` + 事件落 ``risk_events``。

    ``restricted`` ≠ ``invalid``:cookie 没坏,是账号被挂了验证墙,运营动作是拿手机小红书
    App 扫码(``scan_qr``)或干脆晾着别再碰(``rate_limit``),不是重新登录。
    **绝不上抛**:处置失败不该把"已经完成的那几篇"的结果也拖没。

    没复用 ``interaction_backfill._handle_wall`` 的唯一理由是 ``source`` —— 它写死了那个
    模块的 JOB_KIND,复用会把本 kind 的墙记到互动补量名下,事后归因直接查错方向。
    """
    try:
        async with get_session() as session:
            account = await session.get(XhsAccount, account_id)
            if account is not None:
                account.cookie_status = "restricted"
                await session.commit()
    except Exception:  # noqa: BLE001 — 处置失败不吞结果,只告警
        logger.exception(f"[note_collection_batch] 置 restricted 失败(忽略)account={account_id}")
    await risk_events.record_wall(get_session, account_id, wall, JOB_KIND)


__all__ = ["JOB_KIND", "execute", "round_limit_of", "start_batch"]
