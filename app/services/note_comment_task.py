"""发布后评论互动:发布成功钩子登记延时评论任务(kind=``note_comment_task``)。

话术池与分配规则见 ``app.services.comment_phrases``(《评论互动话术池-v5》的落地);
浏览器动作与执行契约**完全复用**已真号验证过的 ``app.services.note_comment.execute``
(kind 映射在 ``account_worker._resolve_execute``),本模块只管选号、选话术、排期、登记。

一篇笔记发布成功后产生两类评论::

    ① 笔记所属账号本人  → 一条预约引导,**最先发**,占住第一条评论位
    ② 其他矩阵号        → 各补一条本号定位的专业视角,零引流指向

分层与 ``matrix_interact`` / ``note_ledger`` 一致:

- ``schedule_note_comments``(sync,发布 published 钩子调,与 ``schedule_matrix_interact``
  同址同模式):按 ``source_publish_job_id`` 幂等;**绝不抛错**阻断发布终态。
- **延时靠落库排期,不靠进程内 sleep**:执行时刻写进 payload 的 ``not_before``,
  派发侧(``browser_jobs_repo.list_dispatchable``)按它过滤未到点的行。领了任务再干等
  会占死全局浏览器闸 ``browser_slot``,把 cookie_check / note_export / 发布一起堵住。
- **评论之间要有随机间隔**(v5 第六节):六个号同时涌进评论区和刷屏没区别。所属账号那条
  先排,矩阵号在其后的窗口内散开且两两至少隔 ``MIN_GAP_SECONDS``。
  ``not_before`` 只是下界,派发顺序不由它保证 —— 拉开的间距足够大,先后倒挂只是概率
  事件,不值得为它引入跨任务的顺序依赖(那会让任一条卡住就拖住整串)。
- **笔记进主页有延迟**:所属账号那条也要等 ``OWNER_DELAY_MIN`` 起步,免得主页上还没有
  这篇。定位不到就是失败,**不重试、不阻断**(评论非幂等,重试可能变成刷屏)。

``note_comment_task`` 与 ``note_comment`` 同样**非幂等**:重复执行会再发一条一模一样的
评论,故不进 ``browser_jobs_repo._IDEMPOTENT_KINDS`` —— 僵死置 error + 结果未知指引,
绝不自动重跑。
"""

import random
import sqlite3
from datetime import datetime, timedelta

from loguru import logger

from app.services import browser_jobs_repo, comment_phrases
from app.services.counselor_quote import parse_counselor_from_title

# 所属账号那条的起步延迟(秒):等笔记进发布者主页,太早去找必然定位不到。
OWNER_DELAY_MIN = 90
OWNER_DELAY_MAX = 240

# 矩阵号相对所属账号那条再往后推的保底间隔(秒):给第一条评论留出发出去的时间,
# 让它稳稳占住第一条评论位。
MATRIX_LEAD_SECONDS = 240

# 矩阵号散开的窗口(秒)与两条评论之间的最小间隔(秒)。
MATRIX_WINDOW_SECONDS = 1800
MIN_GAP_SECONDS = 90


def schedule_note_comments(db_path: str, publish_job_id: int) -> list[str]:
    """为一篇刚发布成功的笔记登记评论任务;返回登记的 job id 列表(未登记则空表)。

    幂等:该 publish job 已登记过则跳过。**绝不抛错** —— 发布终态已先行落库,登记是
    事后副作用,任何异常只告警(与 ``schedule_matrix_interact`` 同款纪律)。
    """
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT id, account_id, title, note_id, related_counselor"
                " FROM publish_jobs WHERE id=?",
                (publish_job_id,),
            ).fetchone()
            if job is None or not (job["title"] or "").strip():
                logger.warning(
                    f"[note_comment_task] 发布 job={publish_job_id} 不存在或无标题,不登记评论"
                )
                return []
            publisher_id = job["account_id"]
            publisher = conn.execute(
                "SELECT id, name, nickname, user_id FROM xhs_accounts WHERE id=?",
                (publisher_id,),
            ).fetchone()
            # 主页路径定位依赖发布者 user_id;没有就无从进主页,直接放弃(不猜)
            if publisher is None or not (publisher["user_id"] or "").strip():
                logger.warning(
                    f"[note_comment_task] 发布者账号{publisher_id} 无 user_id,"
                    f"无法走主页路径,跳过 job={publish_job_id} 的评论任务"
                )
                return []
            dup = conn.execute(
                "SELECT id FROM browser_jobs WHERE kind='note_comment_task'"
                " AND json_extract(payload, '$.source_publish_job_id')=? LIMIT 1",
                (publish_job_id,),
            ).fetchone()
            if dup is not None:
                logger.info(
                    f"[note_comment_task] 发布 job={publish_job_id} 已登记过评论任务,跳过"
                )
                return []
            # 矩阵 = 全部 cookie_status='valid' 的账号排除发布者本人(与 matrix_interact
            # 同口径);发布者本人**不看 cookie_status** —— 他刚发成功,cookie 显然可用,
            # 没必要被巡检进度卡掉这条唯一能发转化引导的评论。
            matrix = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, name, nickname FROM xhs_accounts"
                    " WHERE cookie_status='valid' AND id != ? ORDER BY id",
                    (publisher_id,),
                ).fetchall()
            ]
            history = _phrase_history(conn)

        counselor = _resolve_counselor(job["related_counselor"], job["title"])
        assigned = comment_phrases.assign_phrases(
            owner={
                "account_id": publisher_id,
                "name": publisher["name"],
                "nickname": publisher["nickname"],
            },
            matrix=[
                {
                    "account_id": row["id"],
                    "name": row["name"],
                    "nickname": row["nickname"],
                }
                for row in matrix
            ],
            counselor=counselor,
            history=history,
        )
        if not assigned:
            logger.warning(
                f"[note_comment_task] 发布 job={publish_job_id} 没分到任何话术,不登记"
            )
            return []

        schedule = _comment_times(datetime.utcnow(), len(assigned))
        job_ids: list[str] = []
        for item, not_before in zip(assigned, schedule):
            payload = {
                "source_publish_job_id": publish_job_id,
                "publisher_account_id": publisher_id,
                "publisher_user_id": publisher["user_id"],
                "title": job["title"],
                # 定位优先 note_id(主页卡片链接里带笔记 id,比标题稳 —— 台账 title 会
                # 过期);发布当场没回填到就留空,由 note_comment 回退标题匹配
                "note_id": job["note_id"] or "",
                "text": item["text"],
                # 模板原文(未渲染姓名)单独存一份:跨笔记去重按模板算,渲染后的文案
                # 带咨询师姓名,每篇都不一样,拿它去重等于没去重
                "template": item["template"],
                "position": item["position"],
                "not_before": not_before.isoformat(sep=" "),
            }
            # operator_id=0(非请求上下文的进程内直调):不记到发布者名下,否则这批
            # queued 行会占掉运营的 OPERATOR_PENDING_QUOTA 未终态配额长达一个窗口。
            job_ids.append(
                browser_jobs_repo.enqueue_sync(
                    db_path,
                    "note_comment_task",
                    payload,
                    0,
                    account_id=item["account_id"],
                )
            )
        logger.info(
            f"[note_comment_task] 发布 job={publish_job_id} 已登记 {len(job_ids)} 条评论"
            f"任务(所属账号 {publisher_id} 首发,咨询师 {counselor or '(通用版)'},"
            f"窗口 {MATRIX_WINDOW_SECONDS}s)"
        )
        return job_ids
    except Exception as exc:  # noqa: BLE001 — 登记绝不阻断发布终态
        logger.warning(
            f"[note_comment_task] 登记评论任务失败 job={publish_job_id}"
            f"(忽略,不阻断发布): {exc}"
        )
        return []


def _resolve_counselor(related_counselor: str | None, title: str | None) -> str | None:
    """咨询师姓名三级降级:``related_counselor`` 字段 → 标题解析 → None(用通用版)。

    标题解析复用 ``counselor_quote.parse_counselor_from_title``(它认「粤语咨询师-」
    这类前缀变体,解析不出宁可返回 None 也不给个假名字)。
    """
    name = (related_counselor or "").strip()
    if name:
        return name
    return parse_counselor_from_title(title)


def _phrase_history(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """回读各账号历史用过的话术模板与次数 → ``{account_id: {模板: 次数}}``。

    这是**跨笔记不重复**的依据(v5 第六节)。统计的是登记过的全部任务,不管执行成没成功:
    发出去了当然不能再发,没发成功也说明这句在这个号上刚试过,轮下一句更稳妥。
    """
    history: dict[int, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT account_id, json_extract(payload, '$.template') AS template"
        " FROM browser_jobs WHERE kind='note_comment_task'"
    ).fetchall()
    for row in rows:
        account_id, template = row["account_id"], row["template"]
        if account_id is None or not template:
            continue
        counts = history.setdefault(account_id, {})
        counts[template] = counts.get(template, 0) + 1
    return history


def _comment_times(now: datetime, count: int) -> list[datetime]:
    """排 ``count`` 条评论的执行时刻:第一条(所属账号)最早,其余随机散开且互相拉开。

    第一条起步等 ``OWNER_DELAY_MIN~MAX``(笔记进主页有延迟);矩阵号那批在其后
    ``MATRIX_LEAD_SECONDS`` 起的 ``MATRIX_WINDOW_SECONDS`` 窗口内取随机时刻,排序后
    逐个抬到至少间隔 ``MIN_GAP_SECONDS`` —— 纯随机会挤出两条几乎同时的评论,那和六个号
    一起涌进来没区别。
    """
    owner_at = now + timedelta(seconds=random.uniform(OWNER_DELAY_MIN, OWNER_DELAY_MAX))
    times = [owner_at]
    previous = owner_at + timedelta(seconds=MATRIX_LEAD_SECONDS)
    base = previous
    offsets = sorted(
        random.uniform(0, MATRIX_WINDOW_SECONDS) for _ in range(max(count - 1, 0))
    )
    for offset in offsets:
        moment = max(base + timedelta(seconds=offset), previous)
        times.append(moment)
        previous = moment + timedelta(seconds=MIN_GAP_SECONDS)
    return times
