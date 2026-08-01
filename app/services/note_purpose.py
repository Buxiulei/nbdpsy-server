"""笔记核心目的(note_purpose):受控词表 + LLM 分类 + 手工笔记正文回填。

设计 docs/design/2026-08-01-note-purpose-design.md。核心目的这个字段**是给调用笔记的
agent 读的** —— agent 得知道一篇笔记的意图,才能正确地引用它、评论它、给它排期。

两条填充路径:

- **路径 A(权威)**:发布时 ``POST /api/publish-jobs`` 传 ``note_purpose``,T0 发布当场
  随 ``related_counselor`` / ``generated_at`` 一起写进台账,``purpose_source='declared'``
  (在 ``app.services.note_ledger.record_published_note`` 里,不在本模块)。
- **路径 B(推断)**:手工发布的笔记本系统没发过,台账里连正文都没有。台账同步(T2)
  之后登记一条回填任务:只读进编辑页把正文读回来 → LLM 按受控词表分类 →
  ``purpose_source='inferred'``。

**三条硬纪律**:

1. **节流是硬要求**。每轮最多回填 ``NOTE_PURPOSE_BACKFILL_LIMIT`` 篇(默认 3),优先
   最近发布的,已有 ``note_purpose`` 的不再抓,与 ``browser_slot`` 闸共用并发上限不额外
   起并发。实测同号一小时内起 5 次会话就会从"扫码验证"被打成"请求太频繁",两个账号
   因此被弹墙。**已经抓过正文的篇不再开页**(``content_fetched_at`` 非空即跳过浏览器,
   直接拿库里的正文重分类),LLM 挂了重试也不会白烧浏览器会话。
2. **LLM 只做分类不生成内容**。输出必须落在受控词表内,拿不准填「其他」,不许自造类别;
   分类失败 / LLM 不可达 → ``note_purpose`` 留 NULL **不阻断**,下轮重试。
3. **只回填公开笔记**(``permission_code=0``)。私密的读者看不到、agent 也不会去操作它,
   不值得花一次浏览器会话;``permission_code`` 为 NULL 是**未知**,同样不抓。

收敛纪律照抄 ``note_export`` / ``note_ledger``:号锁 → 浏览器闸 → 线程内跑同步浏览器,
任何异常收敛成 ``{"error": reason}`` **绝不上抛**。回填**幂等**(纯只读抓取 + 按行 upsert),
在 ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里,僵死会自动重跑。
"""

import asyncio
from datetime import datetime

from loguru import logger
from openai import OpenAI  # 模块级导入,便于测试 monkeypatch note_purpose.OpenAI
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_purpose import fetch_note_contents
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# browser_jobs 的 kind(登记 / 派发 / 轮询三处同名)
JOB_KIND = "note_purpose_backfill"

# purpose_source 两个取值:声明的可直接信,推断的要留余地。NULL=未知。
SOURCE_DECLARED = "declared"
SOURCE_INFERRED = "inferred"

# 受控词表(设计 3.2)。**推荐而非强制枚举**:用户原话是"等等",说明会扩,故库里存
# 字符串、不做枚举约束;LLM 这一侧则严格收在表内(它最爱自造类别)。调用方按已知值
# 匹配,**遇到新值不要报错**。
PURPOSE_VOCABULARY = (
    "推介咨询师",
    "概念解读",
    "案例剖析",
    "热点分析",
    "互动引导",
    "个人记录",
    "其他",
)

# 拿不准时的落点:**绝不让 LLM 自造类别**,分不清就是「其他」。
FALLBACK_PURPOSE = "其他"

_VOCABULARY_HINT = "\n".join(
    f"- {word}:{desc}"
    for word, desc in (
        ("推介咨询师", "介绍某位咨询师,引导预约"),
        ("概念解读", "解释一个心理学概念"),
        ("案例剖析", "拆解一个具体场景或来访情境"),
        ("热点分析", "从心理学视角分析社会热点"),
        ("互动引导", "引导关注/收藏/私信一类的功能性笔记"),
        ("个人记录", "与心理科普无关的个人生活内容"),
        ("其他", "以上都不是"),
    )
)

# 喂给 LLM 的正文上限(字符):分类只需要看个开头,整篇喂进去纯烧 token。
_CONTENT_CHARS_FOR_LLM = 800


# ---------------- LLM 分类(只分类,不生成内容) ----------------


def classify_purpose(title: str, content_text: str) -> str | None:
    """按受控词表给一篇笔记分类;返回词表内的词,**分不了返回 None**。

    None 与「其他」是两件事,不许混:

    - ``None`` = **这次没分成**(没配 key / LLM 不可达 / 标题正文都是空)。调用方据此
      让 ``note_purpose`` 留 NULL,下轮重试;
    - ``"其他"`` = **分过了,就是不属于任何已知类别**(含 LLM 自造了个新词的情况)。

    正文为空(纯图笔记)时只用标题分类 —— 标题也空才放弃。
    """
    title = (title or "").strip()
    content_text = (content_text or "").strip()
    if not title and not content_text:
        logger.info("[note_purpose] 标题与正文都是空,无从分类(留 NULL 待下轮)")
        return None
    if not settings.LLM_API_KEY:
        logger.info("[note_purpose] 未配 LLM_API_KEY,跳过分类(留 NULL 待下轮)")
        return None

    body = content_text[:_CONTENT_CHARS_FOR_LLM] or "(这是一篇纯图笔记,没有正文)"
    prompt = (
        "你在给一批小红书心理科普笔记打标签,判断每篇笔记的**核心目的**。\n"
        "## 候选类别(只能选其中一个)\n"
        f"{_VOCABULARY_HINT}\n"
        "## 待判断的笔记\n"
        f"标题:{title or '(无标题)'}\n"
        f"正文:{body}\n"
        "## 输出要求\n"
        "只输出上面列出的**一个**类别词,不要解释、不要标点、不要自造新类别;"
        "拿不准就输出「其他」。"
    )
    try:
        client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=settings.LLM_TIMEOUT,
        )
        content = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 — LLM 不可达不阻断回填,留 NULL 下轮重试
        logger.warning(f"[note_purpose] LLM 分类失败(留 NULL 待下轮): {exc}")
        return None
    return normalize_purpose(content)


def normalize_purpose(raw: str) -> str:
    """把 LLM 的回复收进受控词表;**词表外一律归「其他」**。

    三档判定:整段回复正好是某个词 → 用它;回复里恰好出现一个词表内的词(它爱带
    「类别:概念解读」这种壳)→ 用那个;其余(空回复 / 自造词 / 同时提到多个)→「其他」。
    """
    text = " ".join((raw or "").split())
    if text in PURPOSE_VOCABULARY:
        return text
    hits = [word for word in PURPOSE_VOCABULARY if word in text]
    if len(hits) == 1:
        return hits[0]
    logger.info(f"[note_purpose] LLM 回复 {text[:40]!r} 不在受控词表内,归「{FALLBACK_PURPOSE}」")
    return FALLBACK_PURPOSE


# ---------------- 挑篇(节流的落点) ----------------


async def pick_backfill_targets(
    session, account_id: int, note_id: str | None, limit: int
) -> list[dict]:
    """挑这一轮要回填的笔记,返回脱离 session 的纯 dict 列表(**已按节流截断**)。

    筛选条件(自动挑篇):

    - 有 ``note_id``(pending_id 行还没补到 id,深链进不去);
    - ``permission_code == 0`` 公开笔记 —— **NULL 是未知,同样不抓**(纪律 3);
    - ``note_purpose`` 还是 NULL(已经有目的的不重复抓);
    - 按平台发布时间倒序取前 ``limit`` 篇(旧的个人记录价值低,先补最近的)。

    ``note_id`` 显式指定时(运营手工补录某篇)只放宽"已有目的"这一条 —— 明确点名要重
    分类的那篇,理应能重来;公开性与 id 两条仍照旧,那是风控与可行性约束,不是偏好。
    """
    stmt = select(PublishedNote).where(
        PublishedNote.account_id == account_id,
        PublishedNote.note_id.is_not(None),
        PublishedNote.permission_code == 0,
    )
    if note_id:
        stmt = stmt.where(PublishedNote.note_id == note_id)
    else:
        stmt = stmt.where(PublishedNote.note_purpose.is_(None))
    rows = (
        (
            await session.execute(
                stmt.order_by(
                    # 平台时间是权威发布时刻;为空的行(没同步到)排在后面,再按本机时刻兜底
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
            "title": row.title or "",
            "content_text": row.content_text,
            "content_fetched_at": row.content_fetched_at,
        }
        for row in rows
    ]


def _limit_of(payload: dict) -> int:
    """本轮回填篇数上限:payload 显式给了就用它,否则用配置(**绝不无上限**)。"""
    raw = payload.get("limit")
    try:
        limit = int(raw) if raw is not None else int(settings.NOTE_PURPOSE_BACKFILL_LIMIT)
    except (TypeError, ValueError):
        limit = int(settings.NOTE_PURPOSE_BACKFILL_LIMIT)
    return max(1, limit)


# ---------------- 任务登记(T2 钩子 / REST 手工触发) ----------------


async def schedule_backfill_if_needed(account_id: int) -> str | None:
    """台账同步之后登记一条回填任务;没得可填或已有在途任务则不登记(返回 None)。

    去重是必须的:同步会被反复触发(每次发布 T1 + 手工 + 定时),不去重就会在队列里堆
    一串同号回填任务,一条条排着开浏览器 —— 那正是纪律 1 要防的会话频次。

    **绝不抛错**:登记回填是同步的事后副作用,炸了不能影响同步自身的结果。
    """
    try:
        async with get_session() as session:
            pending = await session.scalar(
                select(BrowserJob.id).where(
                    BrowserJob.kind == JOB_KIND,
                    BrowserJob.account_id == account_id,
                    BrowserJob.status.in_(("queued", "running")),
                )
            )
            if pending is not None:
                logger.info(
                    f"[note_purpose] 账号{account_id} 已有在途回填任务 {pending},不重复登记"
                )
                return None
            targets = await pick_backfill_targets(
                session, account_id, None, int(settings.NOTE_PURPOSE_BACKFILL_LIMIT)
            )
        if not targets:
            return None
        # operator_id=0(非请求上下文的进程内直调):不占运营的未终态配额
        job_id = await browser_jobs_repo.enqueue(JOB_KIND, {}, 0, account_id=account_id)
        logger.info(
            f"[note_purpose] 账号{account_id} 已登记核心目的回填任务 {job_id}"
            f"(本轮候选 {len(targets)} 篇)"
        )
        return job_id
    except Exception as exc:  # noqa: BLE001 — 登记失败绝不影响台账同步结果
        logger.warning(f"[note_purpose] 登记回填任务失败 account_id={account_id}(忽略): {exc}")
        return None


def start_backfill(account_id: int, note_id: str | None = None) -> str:
    """REST 手工触发一次回填;登记 browser_jobs 台账,返回轮询 id。

    与 ``schedule_backfill_if_needed`` 的差别:手工触发**不去重**(运营点了就该跑),
    可指定 ``note_id`` 补录某一篇。
    """
    payload = {"note_id": note_id} if note_id else {}
    job_id = browser_jobs_repo.enqueue_from_request(
        JOB_KIND, payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


# ---------------- 契约执行(account_worker 子进程消费) ----------------


async def execute(account_id: int, payload: dict) -> dict:
    """回填一批笔记的正文与核心目的(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"picked", "fetched", "classified", "unclassified", "purposes", "failed"}``;
    账号无 cookie / 指定的笔记不符合回填条件 / 任何异常 → ``{"error": reason}``,**不抛出**。

    LLM 挂了不算失败:那批笔记的正文照样落库,``note_purpose`` 留 NULL 记在
    ``unclassified`` 里,下轮**不必再开浏览器**就能重分类。
    """
    payload = payload or {}
    note_id = str(payload.get("note_id") or "").strip() or None
    limit = _limit_of(payload)
    try:
        async with get_session() as session:
            targets = await pick_backfill_targets(session, account_id, note_id, limit)
        if not targets:
            if note_id:
                return {
                    "error": f"note_not_eligible: 笔记 {note_id} 不在本号台账里,"
                             f"或不是公开笔记(只回填 permission_code=0 的公开笔记)"
                }
            return _empty_result()

        # 已经抓过正文的不再开页(纪律 1):库里那份直接拿来重分类
        need_fetch = [t for t in targets if t["content_fetched_at"] is None]
        contents: dict[str, dict] = {}
        if need_fetch:
            cookies = await load_account_cookies(account_id)
            if not cookies:
                return {"error": "账号无可用 cookie,跳过核心目的回填"}
            # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
            async with account_locks.get(account_id):
                # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含落库/分类)。
                async with browser_slot():
                    contents = await asyncio.to_thread(
                        _fetch_sync,
                        account_id,
                        cookies,
                        [t["note_id"] for t in need_fetch],
                    )
        return await _classify_and_store(account_id, targets, contents)
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"核心目的回填任务异常 account_id={account_id}")
        return {"error": f"核心目的回填任务异常:{exc}"}


def _empty_result() -> dict:
    return {"picked": 0, "fetched": 0, "classified": 0, "unclassified": 0,
            "purposes": {}, "failed": []}


def _fetch_sync(account_id: int, cookies: list[dict], note_ids: list[str]) -> dict:
    """同一线程内:建 SyncClient → start → 逐篇只读抓正文 → stop 收尾(finally 防泄漏)。

    headed 真屏沿用 SyncClient 默认;纯只读不看图,``block_images`` 省内存(同 note_export)。
    """
    client = SyncClient(account_id, cookies, block_images=True)
    try:
        start = client.start()
        if not start.get("success"):
            # 浏览器起不来:整批都没抓成,逐篇记同一个原因(调用方按篇上报)
            reason = f"browser_start_failed: {start.get('error')}"
            return {note_id: {"error": reason} for note_id in note_ids}
        return fetch_note_contents(client.page, account_id, note_ids)
    finally:
        client.stop()


async def _classify_and_store(
    account_id: int, targets: list[dict], contents: dict[str, dict]
) -> dict:
    """逐篇分类并落库;返回计数与逐篇结果。

    落库规则:

    - 这轮新抓到正文 → 写 ``content_text`` + ``content_fetched_at``(**空正文也写**:
      纯图笔记也算"看过了",不必为它再开一次编辑页);
    - 分类出了词 → 写 ``note_purpose`` + ``purpose_source='inferred'``;
      分不出来(LLM 挂了)→ 两列都不动,留 NULL 下轮重试(**不阻断**)。
    """
    now = datetime.utcnow()
    fetched = classified = unclassified = 0
    purposes: dict[str, str] = {}
    failed: list[dict] = []

    async with get_session() as session:
        for target in targets:
            note_id = target["note_id"]
            got = contents.get(note_id)
            if got is not None and "error" in got:
                failed.append({"note_id": note_id, "reason": got["error"]})
                continue
            row = await session.get(PublishedNote, target["id"])
            if row is None:  # 抓取期间行被删掉了(理论上不会,台账只增不删)
                failed.append({"note_id": note_id, "reason": "台账行已不存在"})
                continue

            if got is not None:
                row.content_text = got.get("content_text") or ""
                row.content_fetched_at = now
                fetched += 1
                # 平台当前标题比台账 title 权威(运营可能在 App 里改过),分类用它
                title = got.get("title") or target["title"]
            else:
                title = target["title"]

            purpose = await asyncio.to_thread(
                classify_purpose, title, row.content_text or ""
            )
            if purpose is None:
                unclassified += 1
                continue
            row.note_purpose = purpose
            row.purpose_source = SOURCE_INFERRED
            purposes[note_id] = purpose
            classified += 1
        await session.commit()

    stats = {
        "picked": len(targets),
        "fetched": fetched,
        "classified": classified,
        "unclassified": unclassified,
        "purposes": purposes,
        "failed": failed,
    }
    logger.info(f"[note_purpose] 账号{account_id} 核心目的回填:{stats}")
    return stats
