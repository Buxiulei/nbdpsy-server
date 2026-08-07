"""矩阵互动服务(点赞 + 收藏):发布成功钩子登记延时任务 + 契约 execute()。

设计见 docs/design/2026-07-31-matrix-interact-design.md。分层与 note_export /
note_delete 一致(浏览器动作在 app.browser.matrix_interact,本模块只管台账与调度)。

**一轮一会话做多篇**(2026-08-07 改):原先是"一篇笔记一次浏览器会话",生产实测近一
小时发 4 篇 → 扇出 **26 次 matrix_interact 会话**(每篇让其余约 7 个号各起一次),而
派发层的同号会话总闸是 4 次/号/小时(风控红线,8 月初实测 5 次就把号弹上验证墙)——
光发布扇出就把全矩阵九个号的额度全部打满,队列里 11 条其它任务(台账同步 / 目的回填 /
互动补量 / 运营手工任务)全部饿死在后面(当时 running=0、queued=11)。

``interaction_backfill`` 早就是"一轮一会话做多篇"(它的模块 docstring 写死了理由:
**会话频次才是被弹墙的直接原因,所以宁可一次会话开久一点**),发布扇出这条老路没跟上。
现在跟上了,骨架照抄它:

- ``schedule_matrix_interact``(sync,发布 published 钩子调):矩阵 = 全部
  ``cookie_status='valid'`` 的账号排除发布者本人。给每个号登记互动任务时,**该号已有
  queued 的同 kind 任务就把这篇并进它的 ``notes``**,而不是新开一条 —— 一条任务 = 一次
  会话,合并才是省会话的唯一办法。并入是**单条 UPDATE 原子完成**的(见
  ``_try_merge``),不做读-改-写,并发登记不丢笔记。按 ``source_publish_job_id`` 幂等;
  **绝不抛错**阻断发布终态。
- **延时靠落库排期,不靠进程内 sleep**:执行时刻写进 payload 的 ``not_before``,
  派发侧(``browser_jobs_repo.list_dispatchable``)按它过滤未到点的行。任务领取后干等
  会占死全局浏览器闸 ``browser_slot``。
- ``execute()`` 为契约执行函数(account_worker 子进程消费):持号锁串行 → 浏览器闸 →
  **一次会话里逐篇互动**,篇间抖动 ``random.uniform(60, 240)`` 秒、整轮受
  ``ROUND_BUDGET_SECONDS`` 预算约束、撞墙即停(已完成的部分照常报,不回滚)。
  **不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成 ``{"error": ...}``,
  **绝不抛出**。
- **单轮上限** ``MATRIX_INTERACT_ROUND_LIMIT``(默认 5 篇):超出的与预算没轮到的一起
  排进下一轮(``_carry_over``,同样走合并),一篇都不丢。
- payload **兼容旧的单篇形态**(顶层 ``publisher_user_id`` / ``title``):部署那一刻在飞
  的任务不能因为换形态就崩,合并也能并进这种旧行(见 ``_normalize_notes``)。
- ``matrix_interact`` **非幂等**(重复执行会取消已点的赞),故不在
  ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里:僵死置 error 不自动重跑。
- **不含评论**(2026-07-31 起):评论是独立能力,走 ``app.services.note_comment`` 与
  REST ``note-comments`` 手工触发,payload 里也不再有 ``comment`` 字段。

已知边界:``NBDPSY_ROLE=all``(单进程回滚位/测试位)无 Supervisor,登记的延时任务无人
派发,会一直 queued —— 生产走 api + worker 拆分部署(worker 的 Supervisor 扫
``list_dispatchable``),不受影响。
"""

import asyncio
import json
import random
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.login_detector import PAGE_TEXT_JS, classify_wall_text, is_wall_url
from app.browser.matrix_interact import MatrixInteractError, interact_with_note
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo, risk_events
from app.services.cookie_check import load_account_cookies

# browser_jobs 的 kind(登记 / 派发 / 轮询 / 风控留痕四处同名)
JOB_KIND = "matrix_interact"

# 互动窗口(秒):发布成功后各矩阵账号在该窗口内的随机时刻执行(设计第二节:10 分钟)。
WINDOW_SECONDS = 600

# 篇间间隔(秒):与 ``interaction_backfill`` **同值同理由**。一次会话里连点几篇的机器
# 节奏才是被抓的特征,而这条路和补量做的是同一件事(进主页 → 找笔记 → 点赞收藏),
# 没有任何理由用更松的数。刻意**不用** ``human.wait``:它带疲劳系数(最高 ×2)与 ±15%
# 抖动,240s 会被放大到 ~550s,既超出设计区间,也能把单轮预算直接顶穿。
# 时效性不是理由:笔记本来就已经在 0~10 分钟的随机窗口里等着,点赞早几分钟没有任何收益。
MIN_GAP_SECONDS = 60
MAX_GAP_SECONDS = 240

# 单轮浏览器段的时间预算(秒):与 ``interaction_backfill`` 同值同理由 —— 账号子进程硬
# 超时是 ``ACCOUNT_PROC_TIMEOUT``(默认 1800s),撞上就是被强杀,已完成的部分连结果都
# 报不出来。5 篇 × (互动 ~40s + 间隔最长 240s) 最坏 ~19 分钟,正好在预算内。
ROUND_BUDGET_SECONDS = 1200

# 合并重试次数:每失败一次都意味着那条 queued 任务在 SELECT 与 UPDATE 之间被执行方领走
# 了(状态守卫拦下)。换下一条 queued 再试,三次还没成就新建 —— 同号同时被领走三条
# queued 的概率约等于零,再多试只是把登记路径拖长(它跑在发布终态之后,不该慢)。
_MERGE_ATTEMPTS = 3

# 记账/汇总用的两个动作与"算到位"的状态(与 interaction_backfill 同口径)
ACTIONS = ("like", "collect")
_COMPLETE_STATUSES = ("done", "skipped")


# ---------------- 发布成功钩子(登记 / 合并延时任务)----------------


def schedule_matrix_interact(db_path: str, publish_job_id: int) -> list[str]:
    """为矩阵内其余账号登记(或合并)延时互动任务;返回承接本次发布的 job id 列表。

    返回的 id 可能是**已存在的**任务 —— 这篇被并进了那条任务的 notes(合并才是省会话的
    唯一办法),调用方只用来记日志,不区分。

    幂等:该 publish job 已登记过(不论是老的单篇 payload 还是新的 notes 列表)则跳过。
    **绝不抛错**——发布终态已先行落库,登记是事后副作用,任何异常只告警(与
    ``archive_published_job`` 同款纪律)。
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
            if _already_scheduled(conn, publish_job_id):
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

        note = {
            "source_publish_job_id": publish_job_id,
            "publisher_account_id": publisher_id,
            "publisher_user_id": publisher["user_id"],
            "title": job["title"],
        }
        job_ids: list[str] = []
        merged_count = 0
        for account_id in matrix_ids:
            # operator_id=0(非请求上下文的进程内直调):不记到发布者名下,否则这批
            # queued 行会占掉运营的 OPERATOR_PENDING_QUOTA 未终态配额长达一个窗口。
            job_id, merged = _merge_or_enqueue(
                db_path, account_id, [note], _window_not_before()
            )
            job_ids.append(job_id)
            merged_count += int(merged)
        logger.info(
            f"[matrix_interact] 发布 job={publish_job_id} 已排入 {len(job_ids)} 条矩阵"
            f"互动任务(其中 {merged_count} 条并进在途任务,账号 {matrix_ids},"
            f"窗口 {WINDOW_SECONDS}s)"
        )
        return job_ids
    except Exception as exc:  # noqa: BLE001 — 登记绝不阻断发布终态
        logger.warning(
            f"[matrix_interact] 登记互动任务失败 job={publish_job_id}(忽略,不阻断发布): {exc}"
        )
        return []


def _already_scheduled(conn: sqlite3.Connection, publish_job_id: int) -> bool:
    """这次发布是否已经被登记过 —— 新的 notes 列表与旧的单篇 payload 都要认。

    只认新形态会让部署那一刻在飞的旧任务被重登记一遍(同一篇互动两次 = 取消已点的赞)。
    """
    row = conn.execute(
        "SELECT id FROM browser_jobs WHERE kind=? AND ("
        " json_extract(payload, '$.source_publish_job_id')=?"
        " OR EXISTS (SELECT 1 FROM json_each(browser_jobs.payload, '$.notes')"
        "            WHERE json_extract(value, '$.source_publish_job_id')=?)"
        ") LIMIT 1",
        (JOB_KIND, publish_job_id, publish_job_id),
    ).fetchone()
    return row is not None


def _window_not_before() -> str:
    """窗口内的随机执行时刻(落库排期用,执行方不 sleep 干等)。"""
    return (
        datetime.utcnow() + timedelta(seconds=random.uniform(0, WINDOW_SECONDS))
    ).isoformat(sep=" ")


def _merge_or_enqueue(
    db_path: str, account_id: int, notes: list[dict], not_before: str
) -> tuple[str, bool]:
    """把 ``notes`` 并进该号 queued 的同 kind 任务;并不进去就新建一条。

    返回 ``(job_id, 是否并入)``。并入的那条任务的 ``not_before`` **不动** —— 它已经排好
    队了,为后来的笔记把整条任务往后推,等于让先登记的那几篇陪着一起等。
    """
    merged_id = _try_merge(db_path, account_id, notes)
    if merged_id is not None:
        return merged_id, True
    return (
        browser_jobs_repo.enqueue_sync(
            db_path,
            JOB_KIND,
            {"notes": list(notes), "not_before": not_before},
            0,
            account_id=account_id,
        ),
        False,
    )


def _try_merge(db_path: str, account_id: int, notes: list[dict]) -> Optional[str]:
    """原子地把 ``notes`` 追加进该号最早那条 queued 任务;并不进去返回 None。

    **单条 UPDATE 完成读与写**(``json_insert(..., '$.notes[#]', …)`` 逐条追加),不做
    读-改-写:两个发布同时给同一个号登记时,读-改-写会让后写的整份 payload 覆盖先写的,
    直接丢掉一篇笔记 —— 而丢掉的那篇没有任何地方会补,永远不会被互动。

    ``AND status='queued'`` 是第二道守卫:任务刚被执行方领走(它已经把 payload 读进
    内存了)时 rowcount=0,这时并进去也没人看,换下一条 queued 再试。
    """
    dumped = [json.dumps(note, ensure_ascii=False) for note in notes]
    expr = "payload"
    for _ in dumped:
        expr = f"json_insert({expr}, '$.notes[#]', json(?))"
    now = datetime.utcnow().isoformat(sep=" ")
    with sqlite3.connect(db_path, timeout=30) as conn:
        for _attempt in range(_MERGE_ATTEMPTS):
            row = conn.execute(
                "SELECT id FROM browser_jobs WHERE kind=? AND account_id=? AND"
                " status='queued' ORDER BY created_at LIMIT 1",
                (JOB_KIND, account_id),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                f"UPDATE browser_jobs SET payload={expr}, updated_at=?"
                " WHERE id=? AND status='queued'",
                (*dumped, now, row[0]),
            )
            conn.commit()
            if cur.rowcount == 1:
                return row[0]
    return None


# ---------------- 契约执行(account_worker 子进程消费)----------------


def _round_limit() -> int:
    """本轮最多做几篇(风控闸,不是默认值)。"""
    return max(1, int(settings.MATRIX_INTERACT_ROUND_LIMIT))


def _normalize_notes(payload: dict) -> list[dict]:
    """payload → 待互动笔记列表,**兼容旧的单篇形态**。

    三种形态都要认:新的 ``{"notes": [...]}``、旧的顶层单篇、以及旧行被并入新笔记后的
    混合形态(旧的那篇排在前面,它先登记)。缺定位信息(``publisher_user_id`` /
    ``title`` 任一为空)的条目直接丢掉 —— 无从定位,开了页也是白开。
    """
    payload = payload or {}
    raw: list[dict] = []
    if payload.get("publisher_user_id") or payload.get("title"):
        raw.append(
            {
                key: payload.get(key)
                for key in (
                    "source_publish_job_id",
                    "publisher_account_id",
                    "publisher_user_id",
                    "title",
                )
            }
        )
    raw.extend(note for note in (payload.get("notes") or []) if isinstance(note, dict))
    return [
        note
        for note in raw
        if (note.get("publisher_user_id") or "").strip()
        and (note.get("title") or "").strip()
    ]


async def execute(account_id: int, payload: dict) -> dict:
    """执行一轮矩阵互动(契约函数,不碰 browser_jobs 台账)。

    payload: ``{"notes": [{"publisher_user_id","title",…}, …]}``(兼容旧的单篇形态)。
    返回 ``{"picked","handled","liked","collected","failed","remaining","notes",
    "carry_over_job_id"}``;撞墙 / 浏览器起不来 / 整轮全失败 → 附 ``"error"`` 键(台账落
    error),**绝不抛出**。

    撞墙时:已完成的部分照常报不回滚,账号置 ``cookie_status='restricted'`` 并落
    ``risk_events``,本轮剩余篇目一篇都不再碰,**也不排下一轮**(号都被挂墙了,再排一轮
    就是往墙上撞第二次)。
    """
    notes = _normalize_notes(payload)
    if not notes:
        return {"error": "payload 缺 publisher_user_id / title,无法定位目标笔记"}
    take = _round_limit()
    targets, over_cap = notes[:take], notes[take:]
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过互动"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。整轮(含篇间抖动)都在闸内
            # ——见模块 docstring:会话频次的风险高于闸的周转率。
            async with browser_slot():
                outcome = await asyncio.to_thread(
                    _interact_sync, account_id, cookies, targets
                )
        result = _summarize(targets, over_cap, outcome)
        wall = outcome.get("wall")
        if wall:
            await _handle_wall(account_id, wall)
            result["error"] = (
                f"撞风控墙({wall.get('wall_type')})已中止本轮:已完成 "
                f"{result['handled']}/{result['picked']} 篇,账号已置 restricted 并落 "
                f"risk_events;**不要立刻重试**,先按墙的类型处置(scan_qr=人工扫码,"
                f"rate_limit=晾置别再碰)"
            )
        elif outcome.get("error"):
            result["error"] = outcome["error"]
        elif result["handled"] and result["failed"] == result["handled"]:
            # 做了几篇、篇篇都失败 = 这一轮没起到任何作用,必须落 error 被看见
            # (老的单篇形态本来就是这个语义:一篇失败整条任务就是 error)
            first = next((n["error"] for n in result["notes"] if n.get("error")), "")
            result["error"] = f"本轮 {result['handled']} 篇全部失败:{first}"
        # 没轮到的(超单轮上限的 + 预算用尽剩下的)排进下一轮,一篇都不丢
        if result["remaining"] and not wall and not outcome.get("error"):
            result["carry_over_job_id"] = await _carry_over(
                account_id, result["remaining"]
            )
        return result
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"矩阵互动任务异常 account_id={account_id}")
        return {"error": f"矩阵互动任务异常:{exc}"}


def _summarize(targets: list[dict], over_cap: list[dict], outcome: dict) -> dict:
    """逐篇结果 → 对外计数(done 与 skipped 都算"到位",两者平台状态相同)。

    ``remaining`` 把"这轮没轮到的"如实报出来(预算用尽的 + 超单轮上限的),别让看结果的
    人以为全做完了。
    """
    results = outcome.get("results") or []
    return {
        "picked": len(targets),
        "handled": len(results),
        "liked": sum(
            1 for r in results
            if (r.get("actions") or {}).get("like", {}).get("status") in _COMPLETE_STATUSES
        ),
        "collected": sum(
            1 for r in results
            if (r.get("actions") or {}).get("collect", {}).get("status") in _COMPLETE_STATUSES
        ),
        "failed": sum(1 for r in results if r.get("error")),
        "remaining": targets[len(results):] + over_cap,
        "notes": results,
        "carry_over_job_id": None,
    }


async def _carry_over(account_id: int, remaining: list[dict]) -> Optional[str]:
    """把没轮到的笔记排进下一轮(优先并进该号在途的 queued 任务,没有就新建)。

    新建时的 ``not_before`` 至少隔一个完整窗口:这个号刚刚烧掉一次会话,紧接着再起一次
    正是被弹墙的那种节奏。并进在途任务时不改它的排期 —— 那次会话本来就要发生,把笔记
    塞进去反而少起一次。

    **绝不抛错**:排下一轮失败最多是这几篇这轮没做成(台账里 ``remaining`` 有记录),
    不能反过来把已经做完那几篇的结果也拖没。
    """
    try:
        not_before = (
            datetime.utcnow()
            + timedelta(seconds=random.uniform(WINDOW_SECONDS, 2 * WINDOW_SECONDS))
        ).isoformat(sep=" ")
        job_id, merged = _merge_or_enqueue(
            browser_jobs_repo.current_db_path(), account_id, remaining, not_before
        )
        logger.info(
            f"[matrix_interact] 账号{account_id} 剩余 {len(remaining)} 篇已排入下一轮 "
            f"job={job_id}({'并入在途任务' if merged else '新建任务'})"
        )
        return job_id
    except Exception as exc:  # noqa: BLE001 — 排下一轮失败不吞已完成的结果
        logger.warning(
            f"[matrix_interact] 账号{account_id} 剩余 {len(remaining)} 篇排下一轮失败"
            f"(忽略,本轮结果照常返回): {exc}"
        )
        return None


def _interact_sync(account_id: int, cookies: list[dict], targets: list[dict]) -> dict:
    """同一线程内:建 SyncClient → start → **一次会话里逐篇互动** → stop 收尾。

    返回 ``{"results": [...], "wall": dict|None, "error": str|None}``。三条硬约定与
    ``interaction_backfill._interact_sync`` 完全一致(一轮一会话 / 篇间抖动 / 撞墙即停),
    见本模块 docstring 与那边的逐条理由。

    headed 真屏沿用 SyncClient 默认(headless=False,自动接当前图形会话);互动要点真实
    卡片与按钮,不 block_images(缺封面会影响卡片布局与坐标)。
    """
    results: list[dict] = []
    wall: Optional[dict] = None
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {
                "results": results,
                "wall": None,
                "error": f"browser_start_failed: {start.get('error')}",
            }
        page = client.page
        deadline = time.monotonic() + ROUND_BUDGET_SECONDS
        for index, target in enumerate(targets):
            if index:
                gap = random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS)
                if time.monotonic() + gap > deadline:
                    logger.info(
                        f"[matrix_interact] 账号{account_id} 本轮预算用尽,"
                        f"剩余 {len(targets) - index} 篇留给下一轮"
                    )
                    break
                logger.info(f"[matrix_interact] 账号{account_id} 篇间间隔 {gap:.0f}s")
                time.sleep(gap)
            entry, wall = _interact_one(page, account_id, target)
            if wall is not None:
                # 撞墙那一篇**不算做过**:它压根没被真正处理,记进结果只会误导下一轮
                logger.warning(
                    f"[matrix_interact] 账号{account_id} 撞墙,立刻中止本轮"
                    f"(已完成 {len(results)} 篇)"
                )
                break
            results.append(entry)
        return {"results": results, "wall": wall, "error": None}
    finally:
        client.stop()


def _interact_one(page, account_id: int, target: dict) -> tuple[dict, Optional[dict]]:
    """处理一篇:互动 → 查墙。返回 ``(这篇的结果, 撞墙取证或 None)``。

    浏览器层的返回**原样透传**(``actions`` / ``forensics`` 一个字段都不重组):失败现场
    是排查时最先被翻的东西,在这里挑字段等于把证据悄悄丢掉。
    """
    entry: dict[str, Any] = {
        "title": target.get("title"),
        "source_publish_job_id": target.get("source_publish_job_id"),
        "note_url": None,
        "actions": {},
        "error": None,
        "forensics": None,
    }
    profile_url = (
        f"https://www.xiaohongshu.com/user/profile/{target['publisher_user_id']}"
    )
    try:
        outcome = interact_with_note(
            page, account_id, target["publisher_user_id"], target["title"]
        )
        entry["note_url"] = outcome.get("note_url")
        entry["actions"] = outcome.get("actions") or {}
        entry["error"] = outcome.get("error")
        entry["forensics"] = outcome.get("forensics")
    except MatrixInteractError as exc:
        # 定位类语义失败(笔记没找到 / 详情没打开):记 error,不重跑
        logger.warning(
            f"矩阵互动失败 account_id={account_id} title={target.get('title')!r} "
            f"reason={exc.reason}"
        )
        entry["error"] = exc.reason
    except Exception as exc:  # noqa: BLE001 — 单篇异常不阻断整轮(墙由下面统一判)
        logger.warning(f"[matrix_interact] 账号{account_id} 单篇互动异常: {exc}")
        entry["error"] = f"interact_exception: {exc}"
    return entry, _wall_if_any(page, profile_url)


def _wall_if_any(page, target_url: str) -> Optional[dict]:
    """当前页是不是风控验证墙;是则返回取证 dict(形状与 cookie 检测那套一致)。

    **URL 是硬判据**(``is_wall_url``),正文只用来分型(扫码 / 限流)——两者的运营动作
    不同。读不出正文不影响判定:判定失败宁可当没撞墙,也不能因为取证失败漏掉真墙。
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
        "target_url": target_url,
        "landed_url": landed,
        "page_text": text,
    }
    logger.warning(
        f"[matrix_interact] 撞风控墙 type={wall['wall_type']} landed={landed} "
        f"text={text[:60]!r}"
    )
    return wall


async def _handle_wall(account_id: int, wall: dict) -> None:
    """撞墙处置:该号置 ``cookie_status='restricted'`` + 事件落 ``risk_events``。

    ``restricted`` ≠ ``invalid``:cookie 没坏,是账号被挂了验证墙,运营动作是拿手机小红书
    App 扫码(``scan_qr``)或干脆晾着别再碰(``rate_limit``),不是重新登录。
    **绝不上抛**:处置失败不该把"已经完成的那几篇"的结果也拖没。

    没复用 ``interaction_backfill._handle_wall`` 的唯一理由是 ``source`` —— 它写死了那个
    模块的 JOB_KIND,复用会把本 kind 的墙记到互动补量名下,事后归因直接查错方向
    (与 ``note_collection_batch`` 同款取舍)。
    """
    try:
        async with get_session() as session:
            account = await session.get(XhsAccount, account_id)
            if account is not None:
                account.cookie_status = "restricted"
                account.last_check_at = datetime.utcnow()
                await session.commit()
    except Exception:  # noqa: BLE001 — 处置失败不吞结果,只告警
        logger.exception(
            f"[matrix_interact] 置 restricted 失败(忽略)account_id={account_id}"
        )
    await risk_events.record_wall(get_session, account_id, wall, JOB_KIND)
