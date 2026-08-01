"""笔记核心目的(note_purpose)单测:受控词表分类 + 手工笔记正文回填(不起真浏览器)。

设计 docs/design/2026-08-01-note-purpose-design.md。锁住的正是那几条**硬纪律**:

- **节流**:每轮最多开 ``NOTE_PURPOSE_BACKFILL_LIMIT`` 次编辑页,优先最近发布的
  ——同号一小时 5 次会话就会被平台从"扫码验证"打成"请求太频繁",这条是风控约束不是偏好;
- **已抓过的不重开页**:``content_fetched_at`` 非空即拿库里的正文重分类,LLM 挂了重试
  也不白烧浏览器会话;
- **已有目的的不重复抓**、**私密笔记(含 permission_code 未知)一律跳过**;
- **LLM 只分类不生成**:词表外的回复归「其他」,**绝不自造类别**;LLM 不可达 →
  note_purpose 留 NULL **不阻断**(正文照样落库),下轮重试;
- **purpose_source 区分 declared / inferred**:发布时声明的与事后推断的可信度不同;
- REST:``content_text`` **只在单条给**,列表不给(几百篇正文会把响应撑爆)。

patch 纪律:打在被测模块的命名空间(``svc.OpenAI`` / ``svc._fetch_sync`` /
``svc.classify_purpose``),不是源模块。
"""

import sqlite3
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import note_ledger
from app.services import note_purpose as svc
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


# ---------------- 夹具 ----------------


@pytest_asyncio.fixture
async def wired_db(tmp_path, monkeypatch):
    """临时文件库 + monkeypatch 全局 engine/async_session;yield 库路径。

    sync 侧(T0 直连 sqlite3)与 async 侧(execute / get_session)落在同一个库上。
    """
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    db_path = str(tmp_path / "purpose.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", session_factory)
    try:
        yield db_path
    finally:
        await engine.dispose()


async def _add_note(
    account_id: int = 1,
    note_id: str = "n1",
    title: str = "标题",
    permission_code: int | None = 0,
    note_purpose: str | None = None,
    content_text: str | None = None,
    content_fetched_at: datetime | None = None,
    platform_published_at: datetime | None = None,
) -> int:
    """造一行台账;返回行 id。"""
    async with db_module.async_session() as s:
        row = PublishedNote(
            account_id=account_id,
            note_id=note_id,
            title=title,
            published_at=platform_published_at or datetime(2026, 7, 1, 0, 0, 0),
            platform_published_at=platform_published_at,
            permission_code=permission_code,
            note_purpose=note_purpose,
            content_text=content_text,
            content_fetched_at=content_fetched_at,
            sync_status="orphan",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _row(row_id: int) -> PublishedNote:
    async with db_module.async_session() as s:
        return await s.get(PublishedNote, row_id)


@pytest.fixture
def no_browser(monkeypatch):
    """禁掉真浏览器:``_fetch_sync`` 换成假的,记录每轮抓了哪些 note_id。

    返回 ``(calls, set_result)``:calls 是逐轮抓取的 note_id 列表(用于断言节流),
    set_result 可注入下一轮的返回内容。
    """
    calls: list[list[str]] = []
    scripted: dict[str, dict] = {}

    def fake_fetch(account_id, cookies, note_ids):
        calls.append(list(note_ids))
        return {
            nid: scripted.get(nid, {"title": f"平台标题-{nid}", "content_text": f"正文-{nid}"})
            for nid in note_ids
        }

    async def fake_cookies(_account_id):
        return _COOKIES

    monkeypatch.setattr(svc, "_fetch_sync", fake_fetch)
    monkeypatch.setattr(svc, "load_account_cookies", fake_cookies)
    return calls, scripted


@pytest.fixture
def fake_llm(monkeypatch):
    """把 ``classify_purpose`` 换成假的:记录入参,按脚本返回。

    默认一律返回「概念解读」;把 ``script["return"]`` 改成 None 即模拟 LLM 不可达。
    """
    seen: list[tuple[str, str]] = []
    script: dict = {"return": "概念解读"}

    def fake_classify(title, content_text):
        seen.append((title, content_text))
        return script["return"]

    monkeypatch.setattr(svc, "classify_purpose", fake_classify)
    return seen, script


# ---------------- 挑篇:节流 / 跳过条件 ----------------


async def test_pick_targets_limits_and_prefers_recent(wired_db):
    """每轮有篇数上限,且**优先最近发布的**(旧的个人记录价值低)。"""
    for i in range(5):
        await _add_note(
            note_id=f"n{i}", platform_published_at=datetime(2026, 7, 1 + i, 0, 0, 0)
        )

    async with db_module.async_session() as s:
        targets = await svc.pick_backfill_targets(s, 1, None, 2)

    assert [t["note_id"] for t in targets] == ["n4", "n3"]


async def test_pick_targets_skips_private_and_unknown_visibility(wired_db):
    """只回填公开笔记:仅自己可见(1)与**可见性未知(NULL)**一律不抓。"""
    await _add_note(note_id="pub", permission_code=0)
    await _add_note(note_id="private", permission_code=1)
    await _add_note(note_id="unknown", permission_code=None)

    async with db_module.async_session() as s:
        targets = await svc.pick_backfill_targets(s, 1, None, 10)

    assert [t["note_id"] for t in targets] == ["pub"]


async def test_pick_targets_skips_already_purposed(wired_db):
    """已经有 note_purpose 的不再抓(自动挑篇口径)。"""
    await _add_note(note_id="done", note_purpose="推介咨询师")
    await _add_note(note_id="todo")

    async with db_module.async_session() as s:
        targets = await svc.pick_backfill_targets(s, 1, None, 10)

    assert [t["note_id"] for t in targets] == ["todo"]


async def test_pick_targets_explicit_note_id_allows_reclassify(wired_db):
    """显式点名某篇 → 只放宽"已有目的"这一条(可重新分类),公开性仍照旧。"""
    await _add_note(note_id="done", note_purpose="推介咨询师")
    await _add_note(note_id="private", permission_code=1)

    async with db_module.async_session() as s:
        picked = await svc.pick_backfill_targets(s, 1, "done", 10)
        refused = await svc.pick_backfill_targets(s, 1, "private", 10)

    assert [t["note_id"] for t in picked] == ["done"]
    assert refused == []


async def test_pick_targets_skips_rows_without_note_id(wired_db):
    """pending_id 行(还没补到 note_id)进不去深链,不挑。"""
    await _add_note(note_id=None)

    async with db_module.async_session() as s:
        assert await svc.pick_backfill_targets(s, 1, None, 10) == []


# ---------------- execute:节流真的生效 / 复用已抓正文 ----------------


async def test_execute_opens_at_most_limit_pages(wired_db, monkeypatch, no_browser, fake_llm):
    """节流是硬要求:5 篇待补、上限 2 → 这一轮只开 2 次编辑页。"""
    calls, _ = no_browser
    monkeypatch.setattr(svc.settings, "NOTE_PURPOSE_BACKFILL_LIMIT", 2)
    for i in range(5):
        await _add_note(
            note_id=f"n{i}", platform_published_at=datetime(2026, 7, 1 + i, 0, 0, 0)
        )

    result = await svc.execute(1, {})

    assert calls == [["n4", "n3"]]  # 只抓最近两篇
    assert result["picked"] == 2 and result["fetched"] == 2 and result["classified"] == 2
    assert "error" not in result


async def test_execute_reuses_stored_content_without_browser(wired_db, no_browser, fake_llm):
    """已经抓过正文的(content_fetched_at 非空)**不再开页**,直接拿库里那份重分类。"""
    calls, _ = no_browser
    seen, _script = fake_llm
    row_id = await _add_note(
        note_id="n1",
        title="台账标题",
        content_text="库里存着的正文",
        content_fetched_at=datetime(2026, 7, 20, 0, 0, 0),
    )

    result = await svc.execute(1, {})

    assert calls == []  # 一次浏览器都没起
    assert seen == [("台账标题", "库里存着的正文")]
    assert result["fetched"] == 0 and result["classified"] == 1
    row = await _row(row_id)
    assert row.note_purpose == "概念解读"
    assert row.content_fetched_at == datetime(2026, 7, 20, 0, 0, 0)  # 不被刷新


async def test_execute_writes_inferred_source(wired_db, no_browser, fake_llm):
    """回填出来的目的一律 purpose_source='inferred'(机器猜的,与声明的可信度不同)。"""
    row_id = await _add_note(note_id="n1")

    await svc.execute(1, {})

    row = await _row(row_id)
    assert row.note_purpose == "概念解读"
    assert row.purpose_source == svc.SOURCE_INFERRED == "inferred"
    assert row.content_text == "正文-n1" and row.content_fetched_at is not None


async def test_execute_prefers_platform_title_for_classification(
    wired_db, no_browser, fake_llm
):
    """分类用编辑页里读到的**平台当前标题**(台账 title 会过期)。"""
    seen, _script = fake_llm
    await _add_note(note_id="n1", title="台账里的旧标题")

    await svc.execute(1, {})

    assert seen == [("平台标题-n1", "正文-n1")]


async def test_execute_llm_unreachable_keeps_null_and_stores_content(
    wired_db, no_browser, fake_llm
):
    """LLM 不可达 → note_purpose 留 NULL **不阻断**;正文照样落库,下轮不必再开浏览器。"""
    _seen, script = fake_llm
    script["return"] = None
    row_id = await _add_note(note_id="n1")

    result = await svc.execute(1, {})

    assert "error" not in result  # 不是失败:任务照常 done
    assert result["classified"] == 0 and result["unclassified"] == 1
    row = await _row(row_id)
    assert row.note_purpose is None and row.purpose_source is None
    assert row.content_text == "正文-n1" and row.content_fetched_at is not None


async def test_execute_empty_body_classifies_by_title(wired_db, no_browser, fake_llm):
    """纯图笔记(正文为空)→ 只用标题分类;空串也落库(算"看过了",别再开一次页)。"""
    _calls, scripted = no_browser
    seen, _script = fake_llm
    scripted["n1"] = {"title": "海马体，打钱！", "content_text": ""}
    row_id = await _add_note(note_id="n1")

    result = await svc.execute(1, {})

    assert seen == [("海马体，打钱！", "")]
    assert result["classified"] == 1
    row = await _row(row_id)
    assert row.content_text == "" and row.content_fetched_at is not None


async def test_execute_single_fetch_failure_does_not_block_others(
    wired_db, monkeypatch, no_browser, fake_llm
):
    """一篇进不去(多半是被删了)只记进 failed,不拖垮同批其余篇。"""
    _calls, scripted = no_browser
    monkeypatch.setattr(svc.settings, "NOTE_PURPOSE_BACKFILL_LIMIT", 2)
    scripted["gone"] = {"error": "editor_not_ready: 更新页始终没渲染出「内容设置」"}
    gone_id = await _add_note(
        note_id="gone", platform_published_at=datetime(2026, 7, 5, 0, 0, 0)
    )
    ok_id = await _add_note(
        note_id="ok", platform_published_at=datetime(2026, 7, 4, 0, 0, 0)
    )

    result = await svc.execute(1, {})

    assert result["failed"] == [
        {"note_id": "gone", "reason": "editor_not_ready: 更新页始终没渲染出「内容设置」"}
    ]
    assert result["classified"] == 1 and result["purposes"] == {"ok": "概念解读"}
    assert (await _row(gone_id)).content_fetched_at is None
    assert (await _row(ok_id)).note_purpose == "概念解读"


async def test_execute_without_cookies_errors(wired_db, monkeypatch, fake_llm):
    """账号没有可用 cookie → error,不去起浏览器。"""
    async def no_cookies(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", no_cookies)
    await _add_note(note_id="n1")

    result = await svc.execute(1, {})

    assert result["error"].startswith("账号无可用 cookie")


async def test_execute_explicit_unknown_note_errors(wired_db, no_browser, fake_llm):
    """手工点名的笔记不在台账里(或不是公开笔记)→ error,别静默当成功。"""
    result = await svc.execute(1, {"note_id": "nope"})

    assert result["error"].startswith("note_not_eligible")


async def test_execute_nothing_to_do_is_done_not_error(wired_db, no_browser, fake_llm):
    """自动挑篇挑不到任何一篇 → 计数全 0 的 done(不是 error:没得可干不是失败)。"""
    result = await svc.execute(1, {})

    assert result == {"picked": 0, "fetched": 0, "classified": 0, "unclassified": 0,
                      "purposes": {}, "failed": []}


# ---------------- 受控词表:LLM 只分类不自造 ----------------


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, reply, seen):
        self._reply = reply
        self._seen = seen

    def create(self, **kwargs):
        self._seen.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        return _FakeResponse(self._reply)


def _fake_openai(monkeypatch, reply):
    """把 ``svc.OpenAI`` 换成假的,返回收到的请求参数列表。"""
    seen: list[dict] = []

    class _FakeClient:
        def __init__(self, api_key=None, base_url=None):
            self.chat = type("_Chat", (), {"completions": _FakeCompletions(reply, seen)})()

    monkeypatch.setattr(svc, "OpenAI", _FakeClient)
    monkeypatch.setattr(svc.settings, "LLM_API_KEY", "test-key")
    return seen


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("概念解读", "概念解读"),
        ("  案例剖析\n", "案例剖析"),          # 空白归一后仍是词表内的词
        ("类别:热点分析", "热点分析"),          # 爱带壳,壳里只有一个词表内的词
        ("职场干货", "其他"),                   # **自造类别 → 其他**
        ("", "其他"),                           # 空回复也不许留白
        ("概念解读 或者 案例剖析", "其他"),      # 同时提到多个 = 没分清
    ],
)
def test_classify_normalizes_into_vocabulary(monkeypatch, reply, expected):
    """LLM 回复一律收进受控词表,**词表外归「其他」,绝不自造类别**。"""
    _fake_openai(monkeypatch, reply)

    assert svc.classify_purpose("标题", "正文") == expected
    assert expected in svc.PURPOSE_VOCABULARY


def test_classify_returns_none_when_llm_unreachable(monkeypatch):
    """LLM 抛异常 → None(留 NULL 下轮重试),**不是**「其他」。"""
    _fake_openai(monkeypatch, RuntimeError("connection reset"))

    assert svc.classify_purpose("标题", "正文") is None


def test_classify_returns_none_without_api_key(monkeypatch):
    """没配 LLM_API_KEY → None,连请求都不发。"""
    monkeypatch.setattr(svc.settings, "LLM_API_KEY", "")

    assert svc.classify_purpose("标题", "正文") is None


def test_classify_uses_title_when_body_empty(monkeypatch):
    """正文为空(纯图笔记)→ 提示词里仍带标题,并注明这是纯图笔记。"""
    seen = _fake_openai(monkeypatch, "个人记录")

    assert svc.classify_purpose("宝宝出去玩吗", "") == "个人记录"
    prompt = seen[0]["messages"][0]["content"]
    assert "宝宝出去玩吗" in prompt and "纯图笔记" in prompt


def test_classify_returns_none_when_title_and_body_empty(monkeypatch):
    """标题正文都空 → 无从分类,None(不发请求,也不瞎填「其他」)。"""
    seen = _fake_openai(monkeypatch, "其他")

    assert svc.classify_purpose("", "") is None
    assert seen == []


# ---------------- 路径 A:发布时声明(T0)----------------


def _add_published_job(db_path: str, job_id: int, note_purpose: str | None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_by, created_at, note_purpose)"
            " VALUES (?, 3, '标题', '正文', '[]', '[]', 'published', 0, 9,"
            " '2026-07-30 08:00:00', ?)",
            (job_id, note_purpose),
        )
        conn.commit()


def _ledger_rows(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM published_notes").fetchall()]


async def test_t0_declared_purpose_lands_with_source(wired_db):
    """发布时声明的目的 T0 当场写台账,并记 purpose_source='declared'(权威)。"""
    _add_published_job(wired_db, 77, "推介咨询师")

    note_ledger.record_published_note(wired_db, 77)

    row = _ledger_rows(wired_db)[0]
    assert row["note_purpose"] == "推介咨询师"
    assert row["purpose_source"] == "declared"


async def test_t0_without_declared_purpose_leaves_both_null(wired_db):
    """没声明就两列都留 NULL —— **绝不在 T0 瞎猜一个**,交给回填链路推断。"""
    _add_published_job(wired_db, 78, None)

    note_ledger.record_published_note(wired_db, 78)

    row = _ledger_rows(wired_db)[0]
    assert row["note_purpose"] is None and row["purpose_source"] is None


async def test_t0_blank_purpose_treated_as_unset(wired_db):
    """空白串等同没声明(不落一个空目的 + 假的 declared)。"""
    _add_published_job(wired_db, 79, "   ")

    note_ledger.record_published_note(wired_db, 79)

    row = _ledger_rows(wired_db)[0]
    assert row["note_purpose"] is None and row["purpose_source"] is None


# ---------------- 任务登记:去重 / 没得可填就不登记 ----------------


async def test_schedule_enqueues_when_targets_exist(wired_db):
    await _add_note(note_id="n1")

    job_id = await svc.schedule_backfill_if_needed(1)

    assert job_id is not None
    async with db_module.async_session() as s:
        row = await s.get(BrowserJob, job_id)
    assert row.kind == svc.JOB_KIND and row.account_id == 1 and row.status == "queued"


async def test_schedule_skips_when_nothing_to_backfill(wired_db):
    """没有可回填的笔记就别登记任务(不然队列里全是空跑的浏览器任务)。"""
    await _add_note(note_id="n1", note_purpose="概念解读")

    assert await svc.schedule_backfill_if_needed(1) is None


async def test_schedule_dedups_in_flight_job(wired_db):
    """该号已有在途回填任务 → 不重复登记(同步会被反复触发,不去重就堆成会话洪水)。"""
    await _add_note(note_id="n1")
    first = await svc.schedule_backfill_if_needed(1)

    assert first is not None
    assert await svc.schedule_backfill_if_needed(1) is None


async def test_schedule_never_raises(wired_db, monkeypatch):
    """登记炸了也只吞成 None —— 绝不把已经落好库的台账同步结果拖成 error。"""
    async def boom(*_args, **_kwargs):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(svc.browser_jobs_repo, "enqueue", boom)
    await _add_note(note_id="n1")

    assert await svc.schedule_backfill_if_needed(1) is None


# ---------------- REST:列表不给正文,单条给 ----------------


def _api_role(monkeypatch) -> None:
    """置 NBDPSY_ROLE=api:start_* 只登记台账,不在本进程派执行(不会起浏览器)。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def test_rest_list_omits_content_but_detail_gives_it(tmp_path, monkeypatch):
    """``content_text`` 只在单条端点返回:一页 200 篇正文会把列表响应撑爆。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        account_id = await seed_account("号A", "u1", _COOKIES)
        await _add_note(
            account_id=account_id, note_id="n1", note_purpose="概念解读",
            content_text="这是正文", content_fetched_at=datetime(2026, 7, 20, 0, 0, 0),
        )
        async with db_module.async_session() as s:
            row = (await s.execute(select(PublishedNote))).scalars().one()
            row.purpose_source = "inferred"
            await s.commit()

        listed = await c.get(
            f"/api/accounts/{account_id}/published-notes", headers=bearer(ADMIN_KEY)
        )
        detail = await c.get("/api/published-notes/n1", headers=bearer(ADMIN_KEY))

    note = listed.json()["notes"][0]
    assert "content_text" not in note
    assert note["note_purpose"] == "概念解读" and note["purpose_source"] == "inferred"
    assert note["content_fetched_at"] == "2026-07-20T00:00:00+00:00"
    assert detail.json()["note"]["content_text"] == "这是正文"


async def test_rest_backfill_enqueue_and_poll(tmp_path, monkeypatch):
    """回填端点:202 给 backfill_id,轮询能查到;跨 kind 的 id 互查 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        account_id = await seed_account("号A", "u1", _COOKIES)

        started = await c.post(
            f"/api/accounts/{account_id}/note-purpose-backfills",
            json={"note_id": "n1"},
            headers=bearer(ADMIN_KEY),
        )
        backfill_id = started.json()["backfill_id"]
        polled = await c.get(
            f"/api/note-purpose-backfills/{backfill_id}", headers=bearer(ADMIN_KEY)
        )
        crossed = await c.get(
            f"/api/note-ledger-syncs/{backfill_id}", headers=bearer(ADMIN_KEY)
        )

    assert started.status_code == 202 and started.json()["status"] == "queued"
    assert polled.json()["status"] == "queued"
    assert crossed.status_code == 404


async def test_rest_backfill_body_optional_and_rbac(tmp_path, monkeypatch):
    """body 可省(自动挑篇);未知账号 404;无 apikey 401。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        account_id = await seed_account("号A", "u1", _COOKIES)

        bare = await c.post(
            f"/api/accounts/{account_id}/note-purpose-backfills", headers=bearer(ADMIN_KEY)
        )
        missing = await c.post(
            "/api/accounts/9999/note-purpose-backfills", headers=bearer(ADMIN_KEY)
        )
        anon = await c.post(f"/api/accounts/{account_id}/note-purpose-backfills")

    assert bare.status_code == 202
    assert missing.status_code == 404
    assert anon.status_code == 401
