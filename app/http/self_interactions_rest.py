"""self-interactions 分组 REST(1 端点):按笔记聚合**我们自己矩阵制造的**互动。

内容线拿平台指标做分析之前得先把自家互动减掉:一篇笔记 6 个赞里 5 个是矩阵号刷的,
不减就把"自己跟自己说话"读成了"这个选题反响不错"。本端点就是那个减数。

**纯 DB 读,不起浏览器**:数据全部来自两张已有的台账,一次调用零会话成本、零风控暴露。

数据来自两处,拼在一起才完整:

- **赞 / 藏** —— ``note_interactions``(互动补量与矩阵互动写的行,一次动作一行);
- **评论** —— ``browser_jobs`` 里 ``kind='note_comment_task'`` 的行(发布后的延时评论)。

三条口径各自对应一次会算错数的坑,改之前先读明白:

1. **``done`` 与 ``skipped`` 都算**。``skipped`` 的语义是"去点的时候平台上已经是目标态"
   —— 对那个号来说赞**就在那篇笔记上**,只是不是这一次点的。本仓两处既有代码
   (``interaction_backfill`` / ``matrix_interact`` 的 ``_COMPLETE_STATUSES``)早已是这个
   口径:"done 与 skipped 都算到位,两者平台状态相同"。只数 ``done`` 会少算一大半
   (生产实测 like 是 401 done 对 834 skipped),减完剩下的"自然互动"依旧虚高;
2. **评论的 note_id 靠发布任务回填**。评论任务是发布成功那一刻登记的,那时平台 note_id
   还没回来,所以 payload 里存的是空串 —— 生产 123 条 done **全部**如此。必须经
   ``payload.source_publish_job_id`` → ``publish_jobs.note_id`` 补齐,否则评论数恒为 0;
3. **回填不了的评论要报出来**。发布任务自己也没拿到 note_id 时这条评论无处可挂
   (生产 13/123),不能悄悄丢:那是 11% 的静默少算,而少算的部分会被调用方当成自然评论。
   挂在 ``coverage.unresolved_comments``。

**盖不到的**:人在小红书 App 里手点的赞/评论不在任何台账里,本端点看不见(见 coverage.note)。
所以这个数是**下界**——减完仍可能残留人工制造的互动。
"""

import json
from datetime import date, datetime

from fastapi import APIRouter
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.browser_job import BrowserJob
from app.models.note_interaction import NoteInteraction
from app.models.publish_job import PublishJob
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount

router = APIRouter()

# 评论任务的 kind(发布后延时评论;手工单篇评论走 note_comment,那条链路生产至今 0 行)
_COMMENT_JOB_KIND = "note_comment_task"
# 算作"这个赞/藏现在在平台上"的状态。**skipped 必须在内**,理由见模块 docstring 第 1 条;
# error 不在内(那一下没成)。与 interaction_backfill._COMPLETE_STATUSES 同口径。
_PRESENT_STATUSES = ("done", "skipped")
_COUNTED_ACTIONS = ("like", "collect")

# 两张台账各自的起始日(生产库实测最早一行)。给调用方判断"这段时间没数"是真没刷过、
# 还是那时候压根还没有这张表 —— 后者读成前者就会得出"八月前完全没有自家互动"的错结论。
_LIKES_COLLECTS_SINCE = "2026-08-02"
_COMMENTS_SINCE = "2026-08-01"
_COVERAGE_NOTE = (
    "仅覆盖**经 API 触发**的互动;人在小红书 App 里手点的赞/收藏/评论不落任何台账,"
    "本端点看不见。所以这些数是**下界**,减完仍可能残留人工制造的互动。"
)

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts/{account_id}/self-interactions",
        "summary": "按笔记聚合**自家矩阵制造的**赞/藏/评论(做数据清洗时的减数,纯 DB 读)",
        "admin_only": False,
        "params": {
            "account_id": "path,int(**笔记所属的号**,不是去互动的号)",
            "since": "query,str|None(YYYY-MM-DD,只算这天 00:00 UTC 之后的互动;不传=全量)",
        },
        "returns": "{account_id, since, notes:[{note_id, title, self_likes, self_collects, "
                   "self_comments, actor_account_ids[], first_at, last_at}], "
                   "coverage:{likes_collects_since, comments_since, unresolved_comments, note}}",
        "errors": "400=since 不是 YYYY-MM-DD;403=无该号授权;404=账号不存在",
        "notes": "**用途**:平台指标里混着自家矩阵刷的量,分析前要减掉它 —— 一篇 6 个赞里 5 个"
                 "是矩阵号点的,不减就把自己跟自己说话读成了选题反响好。"
                 "**纯 DB 读**:不起浏览器、不消耗任何账号的会话额度,可随意调。"
                 "⚠️ 四条读数纪律:"
                 "① **只列有自家互动的笔记**,没出现在 notes 里 = 该窗口内这篇没有 API 触发的"
                 "自家互动(不是「查不到这篇」);"
                 "② **计数口径是「现在在平台上」而不是「这次点成了」**:互动台账里 done(点成了)"
                 "与 skipped(去点时平台上已经是这个状态)都算 —— 对那个号来说赞就在那篇笔记上,"
                 "只是不是这一次点的;error(那一下没成)不算。这与互动补量回执里 liked/collected "
                 "的口径一致;"
                 "③ **coverage 的两个起始日是台账起点不是业务起点**:那之前的赞藏评论没有记录,"
                 "读成「那时候没刷过」就错了;"
                 "④ **unresolved_comments 是全矩阵口径的少算量**:这些评论确实发出去了,但连"
                 "发布任务都没拿到平台 note_id,挂不到任何一篇上。本号的 self_comments 最多可能"
                 "少这么多 —— 它不为零时别把 self_comments 当精确值。"
                 "actor_account_ids 是**去互动的那些号**(升序);first_at/last_at 是该篇自家"
                 "互动的首末时刻(naive UTC)。"
                 "**手工单篇评论(POST /api/accounts/{id}/note-comments)不在统计内**——"
                 "那条链路的台账 kind 不同,且生产至今没有行;真开始用了要回来补口径。",
    },
]


def _parse_since(since: str | None) -> datetime | None:
    """``YYYY-MM-DD`` → 当天 00:00 的 naive UTC 时刻;不传给 None(全量)。

    格式不对就报错,**绝不静默当成没传** —— 那样调用方以为自己取的是近一周,拿到的却是
    开天辟地以来的全量,减出来的数会大得莫名其妙且无从察觉。
    """
    if since is None:
        return None
    try:
        return datetime.combine(date.fromisoformat(since), datetime.min.time())
    except ValueError:
        raise ValueError(
            f"since={since!r} 不是合法日期,要 YYYY-MM-DD(如 2026-08-01)"
        ) from None


def _job_field(raw: str | None, key: str):
    """从 payload / result 的 JSON 串里取一个键;存坏了当没有(一行坏数据不该打死整批)。"""
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    return parsed.get(key) if isinstance(parsed, dict) else None


@router.get("/api/accounts/{account_id}/self-interactions")
async def list_self_interactions_endpoint(
    account_id: int, since: str | None = None
) -> dict:
    """按笔记聚合该号名下笔记收到的自家互动(赞/藏来自互动台账,评论来自评论任务台账)。"""
    operator = current_operator()
    cutoff = _parse_since(since)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")

        # 本号名下的笔记:note_id → 标题。待补 id 的行(note_id 为 NULL)没法与互动台账
        # 对上,天然落在外面。
        titles = dict((await session.execute(
            select(PublishedNote.note_id, PublishedNote.title)
            .where(PublishedNote.account_id == account_id)
            .where(PublishedNote.note_id.is_not(None))
        )).all())

        stmt = (
            select(NoteInteraction)
            .where(NoteInteraction.note_id.in_(list(titles)))
            .where(NoteInteraction.status.in_(_PRESENT_STATUSES))
            .where(NoteInteraction.action.in_(_COUNTED_ACTIONS))
        )
        if cutoff is not None:
            stmt = stmt.where(NoteInteraction.done_at >= cutoff)
        interactions = list((await session.execute(stmt)).scalars().all())

        job_stmt = (
            select(BrowserJob)
            .where(BrowserJob.kind == _COMMENT_JOB_KIND)
            .where(BrowserJob.status == "done")
        )
        if cutoff is not None:
            # 用 updated_at 而不是 created_at:评论任务是**排期**登记的(登记后延时几分钟到
            # 半小时才执行),created_at 是排期时刻,updated_at 才是评论真的发出去的时刻。
            job_stmt = job_stmt.where(BrowserJob.updated_at >= cutoff)
        jobs = [
            j for j in (await session.execute(job_stmt)).scalars().all()
            if _job_field(j.result, "commented")
        ]

        # 评论的 note_id 回填:payload 里几乎恒为空串(评论任务在发布当场登记,那时平台
        # note_id 还没回来),必须经发布任务补。批量查一次,不逐条打库。
        pending = {
            _job_field(j.payload, "source_publish_job_id") for j in jobs
            if not _job_field(j.payload, "note_id")
        }
        pending.discard(None)
        publish_note_ids = dict((await session.execute(
            select(PublishJob.id, PublishJob.note_id).where(PublishJob.id.in_(pending))
        )).all()) if pending else {}

    buckets: dict[str, dict] = {}

    def _bucket(note_id: str) -> dict:
        return buckets.setdefault(note_id, {
            "note_id": note_id,
            "title": titles.get(note_id),
            "self_likes": 0, "self_collects": 0, "self_comments": 0,
            "actors": set(), "first_at": None, "last_at": None,
        })

    def _stamp(bucket: dict, actor: int | None, at: datetime | None) -> None:
        if actor is not None:
            bucket["actors"].add(actor)
        if at is None:
            return
        if bucket["first_at"] is None or at < bucket["first_at"]:
            bucket["first_at"] = at
        if bucket["last_at"] is None or at > bucket["last_at"]:
            bucket["last_at"] = at

    for row in interactions:
        bucket = _bucket(row.note_id)
        bucket["self_likes" if row.action == "like" else "self_collects"] += 1
        _stamp(bucket, row.actor_account_id, row.done_at)

    unresolved = 0
    for job in jobs:
        note_id = _job_field(job.payload, "note_id") or publish_note_ids.get(
            _job_field(job.payload, "source_publish_job_id")
        )
        if not note_id:
            # 连发布任务都没拿到平台 id:这条评论确实发出去了却挂不到任何一篇上。
            # **不能悄悄丢** —— 那是静默少算,调用方会把它当成自然评论。
            unresolved += 1
            continue
        if note_id not in titles:
            continue  # 评论的是别的号的笔记,不属于本号的自家互动
        bucket = _bucket(note_id)
        bucket["self_comments"] += 1
        _stamp(bucket, job.account_id, job.updated_at)

    # 最近有动静的排前面。两趟稳定排序:先按 note_id 升序定基线,再按时刻降序 —— 一趟排
    # 会在 last_at 相同(或都为空)时拿 None 去比大小炸掉,而 reverse=True 还会把并列项的
    # note_id 顺序一起倒过来。
    ordered = sorted(buckets.values(), key=lambda x: x["note_id"])
    ordered.sort(key=lambda x: x["last_at"] or datetime.min, reverse=True)
    notes = [
        {
            "note_id": b["note_id"],
            "title": b["title"],
            "self_likes": b["self_likes"],
            "self_collects": b["self_collects"],
            "self_comments": b["self_comments"],
            "actor_account_ids": sorted(b["actors"]),
            "first_at": b["first_at"].isoformat() if b["first_at"] else None,
            "last_at": b["last_at"].isoformat() if b["last_at"] else None,
        }
        for b in ordered
    ]
    return {
        "account_id": account_id,
        "since": since,
        "notes": notes,
        "coverage": {
            "likes_collects_since": _LIKES_COLLECTS_SINCE,
            "comments_since": _COMMENTS_SINCE,
            "unresolved_comments": unresolved,
            "note": _COVERAGE_NOTE,
        },
    }
