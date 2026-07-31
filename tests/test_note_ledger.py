"""发布笔记永久台账单测(不起真浏览器),锁设计第七节验收 1 的七项 + 台账纪律。

- **T0 发布当场即建台账行且内容侧字段齐全**——纯 DB,不依赖任何浏览器或同步;
- **同步全程失败时台账行仍完整保留 pending_id**——整个写入时序修订的核心保障;
- 分页遍历终止条件:空批 / 滚动后无新响应 / 批数上限,三条都不许死循环;
- 同步幂等:同一 note_id 跑两次不产生重复行,first_seen_at 不被改写;
- 认不准就留着不猜:标题不同 / 标题重复 / 平台时间超窗,一律留 pending_id;
- visible_time → platform_published_at 转换(缺失/非法留 NULL,不拿北京时间串猜时区);
- **T2 遇到列表里查不到的行只记录不删**;
- 只读纪律:抓取全程不点击、不 evaluate(假 page 上这两个方法一被调用就断言失败)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import sqlite3
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.db as db_module
from app.browser import creator_note_list as cnl
from app.models.content_archive import ContentArchive
from app.models.publish_job import PublishJob
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo as repo
from app.services import note_ledger as svc


# ---------------- 抓取层测试替身 ----------------


class _FakeResponse:
    """假响应:只提供被测代码真用到的 url / json()。"""

    def __init__(self, url: str, body, boom: bool = False):
        self.url = url
        self._body = body
        self._boom = boom

    def json(self):
        if self._boom:
            raise RuntimeError("响应体已失效")
        return self._body


_POSTED_URL = (
    "https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=0&page=0"
)


class _FakePage:
    """假 page:按脚本在 goto / scroll 时投递列表响应。

    ``script`` 每项是一批 notes(list)或 None(本次不产生任何列表响应);
    goto 消费一项,fake human 的 scroll 也消费一项。脚本耗尽后不再投递。
    """

    def __init__(self, script, notes_count=None):
        self._script = list(script)
        # 接口自报的笔记总数(data.tags[0].notes_count);None = 响应里没这项
        self._notes_count = notes_count
        self._handlers: list = []
        self.gotos: list[str] = []
        self.removed = False

    # --- playwright 事件接口 ---
    def on(self, event, fn):
        self._handlers.append((event, fn))

    def remove_listener(self, event, fn):
        self._handlers = [h for h in self._handlers if h != (event, fn)]
        self.removed = True

    # --- 导航 / 等待 ---
    def goto(self, url, **_kw):
        self.gotos.append(url)
        self.deliver_next()

    def wait_for_load_state(self, *_a, **_kw):
        pass

    def wait_for_timeout(self, _ms):
        time.sleep(0.01)  # 让轮询不空烧 CPU(真 page 上这里才会派发事件)

    # --- 只读纪律护栏:抓取层一旦点击/注入 JS,测试立即失败 ---
    def evaluate(self, *_a, **_kw):
        raise AssertionError("抓取层不得调用 page.evaluate(纯只读抓取)")

    def click(self, *_a, **_kw):
        raise AssertionError("抓取层不得点击页面(纯只读抓取)")

    # --- 脚本投递 ---
    def deliver_next(self):
        if not self._script:
            return
        notes = self._script.pop(0)
        if notes is None:
            return
        data: dict = {"notes": notes}
        if self._notes_count is not None:
            data["tags"] = [{"name": "所有笔记", "notes_count": self._notes_count}]
        self._emit(_FakeResponse(_POSTED_URL, {"code": 0, "data": data}))

    def _emit(self, response):
        for event, fn in list(self._handlers):
            if event == "response":
                fn(response)


class _FakeHuman:
    """假拟人层:scroll 触发下一批投递(模拟前端下拉加载再发一次分页请求)。"""

    def __init__(self, page):
        self.page = page
        self.scrolls = 0

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        self.scrolls += 1
        self.page.deliver_next()


@pytest.fixture
def fast_timeouts(monkeypatch):
    """把三档等待压到亚秒级,让终止条件用例秒过(逻辑不变,只缩时间)。"""
    monkeypatch.setattr(cnl, "_FIRST_BATCH_FAST_S", 0.2)
    monkeypatch.setattr(cnl, "_FIRST_BATCH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(cnl, "_NEXT_BATCH_TIMEOUT_S", 0.2)


@pytest.fixture
def fake_human(monkeypatch):
    """把抓取层的拟人层换成假的,并把实例暴露出来给断言用。"""
    created: list[_FakeHuman] = []

    def factory(page):
        human = _FakeHuman(page)
        created.append(human)
        return human

    monkeypatch.setattr(cnl, "SyncHumanActions", factory)
    return created


# 真号实测响应对应的 UTC 时刻(北京时间 2025-09-25 17:16)
_VISIBLE_TIME = 1758791784
_VISIBLE_DT = datetime(2025, 9, 25, 9, 16, 24)


def _note(note_id: str, **extra) -> dict:
    """构造一条接口原样字段的笔记(默认值取自真号实测响应)。"""
    base = {
        "id": note_id,
        "xsec_token": f"tok-{note_id}",
        "xsec_source": "pc_creatormng",
        "display_title": f"标题-{note_id}",
        "time": "2025-09-25 17:15",
        "visible_time": _VISIBLE_TIME,
        "type": "normal",
        "likes": 1,
        "collected_count": 2,
        "comments_count": 3,
        "shared_count": 4,
        "view_count": 5,
    }
    base.update(extra)
    return base


# ---------------- 分页遍历终止条件 ----------------


def test_paging_stops_on_empty_batch(fast_timeouts, fake_human):
    """某批返回空 notes → 页码超界,停止翻页(不再滚)。"""
    page = _FakePage([[_note("a"), _note("b")], [_note("c")], []])

    notes = cnl.fetch_posted_notes(page, account_id=1)

    assert [n["id"] for n in notes] == ["a", "b", "c"]
    assert fake_human[0].scrolls == 2  # 第二次滚出空批即停,不再继续
    assert page.removed  # 监听器已摘除


def test_paging_stops_when_no_new_response(fast_timeouts, fake_human):
    """滚动后等不到新的分页响应 → 前端没有更多可加载,停止(不无限滚)。"""
    page = _FakePage([[_note("a")], None])

    notes = cnl.fetch_posted_notes(page, account_id=1)

    assert [n["id"] for n in notes] == ["a"]
    assert fake_human[0].scrolls == 1


def test_paging_stops_at_max_pages(fast_timeouts, fake_human):
    """前端永远还有下一页时,批数上限兜底停止(防死循环)。"""
    page = _FakePage([[_note(f"n{i}")] for i in range(20)])

    notes = cnl.fetch_posted_notes(page, account_id=1, max_pages=3)

    assert [n["id"] for n in notes] == ["n0", "n1", "n2"]
    assert fake_human[0].scrolls == 2  # 首批来自 goto,再滚两次即达上限


class _LogRecorder:
    """假 logger:只记 warning 文案,其余级别吞掉(断言"抓不满有没有告警")。"""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg):
        self.warnings.append(str(msg))

    def info(self, _msg):
        pass

    def exception(self, _msg):
        pass

    def error(self, _msg):
        pass


def test_paging_retries_scroll_when_short_of_expected(fast_timeouts, fake_human):
    """没抓够接口自报的总数时,滚动没触发分页要重试 —— 修 37 篇只抓到 20 篇的缺口。

    脚本第二次滚动不产生响应,旧行为会当场收工只返 2 篇;现在因为 notes_count=3 还没抓够,
    继续滚,把第 3 篇捞回来。
    """
    page = _FakePage([[_note("a"), _note("b")], None, [_note("c")]], notes_count=3)

    notes = cnl.fetch_posted_notes(page, account_id=1)

    assert [n["id"] for n in notes] == ["a", "b", "c"]
    # 第 3 次滚动抓够了 3 篇,此时"无新分页"才被认定为到底
    assert fake_human[0].scrolls == 3


def test_paging_warns_when_short_of_expected(fast_timeouts, fake_human, monkeypatch):
    """重试完仍不足接口自报的总数 → **告警**,绝不静默当成"这号就这么多篇"。"""
    recorder = _LogRecorder()
    monkeypatch.setattr(cnl, "logger", recorder)
    page = _FakePage([[_note("a"), _note("b")], None, None, None], notes_count=37)

    notes = cnl.fetch_posted_notes(page, account_id=1)

    assert len(notes) == 2  # 抓不满也返回已抓到的(半份列表照样能刷已有台账行)
    assert any("少于接口自报的 37 篇" in w for w in recorder.warnings)


def test_paging_does_not_retry_when_expected_unknown(fast_timeouts, fake_human):
    """响应里没有 notes_count(期望未知)→ 维持旧行为,一次无响应即停,不空转重试。"""
    page = _FakePage([[_note("a")], None, [_note("b")]])

    notes = cnl.fetch_posted_notes(page, account_id=1)

    assert [n["id"] for n in notes] == ["a"]
    assert fake_human[0].scrolls == 1


def test_empty_first_batch_returns_no_notes(fast_timeouts, fake_human):
    """首批即空 = 该号没有笔记:直接返回空表,一次都不滚。"""
    page = _FakePage([[]])

    assert cnl.fetch_posted_notes(page, account_id=1) == []
    assert fake_human[0].scrolls == 0


def test_no_response_at_all_raises(fast_timeouts, fake_human):
    """始终没有列表响应(多半是 creator 域没登录)→ 抛错,绝不返回空当"该号无笔记"。"""
    page = _FakePage([None, None, None])

    with pytest.raises(cnl.CreatorNoteListError) as exc:
        cnl.fetch_posted_notes(page, account_id=1)

    assert exc.value.reason.startswith("no_posted_response")
    # 首访没响应会做一次 SSO 预热再重进笔记管理页
    assert any("publish/publish" in url for url in page.gotos)


def test_merge_dedups_and_drops_idless():
    """跨批去重(前端可能重复请求同一页);无 id 的条目丢弃(台账幂等键都没有)。"""
    merged = cnl._merge_batches(
        [[_note("a"), _note("b")], [_note("b"), _note("c")], [{"display_title": "无 id"}]]
    )

    assert [n["id"] for n in merged] == ["a", "b", "c"]


def test_collector_ignores_unrelated_and_broken_responses():
    """只认笔记列表接口;读不出 body / 无 data.notes 的一律忽略,不打断抓取。"""
    collector = cnl._PostedCollector()

    collector.handle(_FakeResponse("https://creator.xiaohongshu.com/api/other", {"x": 1}))
    collector.handle(_FakeResponse(_POSTED_URL, None, boom=True))
    collector.handle(_FakeResponse(_POSTED_URL, {"code": -1, "msg": "登录失效"}))
    collector.handle(_FakeResponse(_POSTED_URL, {"data": {"notes": [_note("a")]}}))

    assert collector.batches == [[_note("a")]]


# ---------------- 平台字段转换 ----------------


def test_platform_published_at_from_visible_time():
    """visible_time(unix 秒)→ naive UTC datetime(真号实测值对应北京时间 17:16)。"""
    assert svc.platform_published_at_of(_note("a")) == _VISIBLE_DT


@pytest.mark.parametrize("value", [None, "", "不是时间", {"a": 1}])
def test_platform_published_at_null_when_unusable(value):
    """visible_time 缺失/非法 → 留 NULL(不拿北京时间串 time 去猜时区)。"""
    assert svc.platform_published_at_of({"visible_time": value}) is None


def test_note_url_falls_back_without_token():
    """有 xsec 两件套就拼完整链接;没有 token 时退化成裸 explore 链接,不编造参数。"""
    assert svc._note_url("abc", "T", "pc_creatormng") == (
        "https://www.xiaohongshu.com/explore/abc?xsec_token=T&xsec_source=pc_creatormng"
    )
    assert svc._note_url("abc", None, None) == "https://www.xiaohongshu.com/explore/abc"


# ---------------- T0:发布当场落台账行 ----------------


@pytest.fixture
def ledger_db(tmp_path):
    """建一个带全部表的临时 sqlite 文件库,返回路径(sync 侧直连用)。"""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401  触发模型注册
    from app.core.db import Base

    db_path = str(tmp_path / "ledger.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _add_published_job_sync(
    db_path: str,
    job_id: int,
    account_id: int,
    title: str = "标题",
    created_by: int | None = 7,
    created_at: datetime | None = None,
) -> None:
    created = (created_at or datetime(2026, 7, 30, 8, 0, 0)).isoformat(sep=" ")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_by, created_at)"
            " VALUES (?, ?, ?, '正文', '[]', '[]', 'published', 0, ?, ?)",
            (job_id, account_id, title, created_by, created),
        )
        conn.commit()


def _add_archive_sync(db_path: str, archive_id: int, job_id: int, account_id: int) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO content_archive (id, title, content, topics_json, media_json,"
            " kind, source_account_id, source_publish_job_id, created_at, last_used_at,"
            " use_count) VALUES (?, '标题', '正文', '[]', '[]', 'image_note', ?, ?, ?, ?, 0)",
            (archive_id, account_id, job_id, now, now),
        )
        conn.commit()


def _read_ledger_rows(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM published_notes ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def test_t0_records_row_with_full_content_fields(ledger_db):
    """T0:发布成功当场即建台账行,内容侧字段齐全,平台侧留空待补——不碰浏览器。"""
    _add_published_job_sync(
        ledger_db, 77, 3, title="边界感是练出来的", created_by=9,
        created_at=datetime(2026, 7, 30, 8, 0, 0),
    )
    _add_archive_sync(ledger_db, 5, 77, 3)
    before = datetime.utcnow()

    row_id = svc.record_published_note(ledger_db, 77)

    rows = _read_ledger_rows(ledger_db)
    assert row_id is not None and len(rows) == 1
    row = rows[0]
    # 内容侧:全部写死
    assert row["account_id"] == 3
    assert row["title"] == "边界感是练出来的"
    assert row["generated_at"] == "2026-07-30 08:00:00"  # = publish_jobs.created_at
    assert row["operator_id"] == 9  # = publish_jobs.created_by
    assert row["source_publish_job_id"] == 77
    assert row["content_archive_id"] == 5
    assert datetime.fromisoformat(row["published_at"]) >= before  # 本机时钟,永不为空
    # 平台侧:当场拿不到,留空等同步补
    assert row["sync_status"] == "pending_id"
    assert row["note_id"] is None  # 必须是 NULL 不是空串(空串会撞联合唯一键)
    assert row["xsec_token"] is None and row["note_url"] is None
    assert row["platform_published_at"] is None and row["note_type"] is None


def test_t0_is_idempotent_per_publish_job(ledger_db):
    """同一发布重复调不产生第二行(钩子幂等)。"""
    _add_published_job_sync(ledger_db, 88, 3)

    assert svc.record_published_note(ledger_db, 88) is not None
    assert svc.record_published_note(ledger_db, 88) is None
    assert len(_read_ledger_rows(ledger_db)) == 1


def test_t0_without_archive_leaves_archive_id_null(ledger_db):
    """归档没落(实测有 4 条 published 无归档)→ content_archive_id 留 NULL,行照建。"""
    _add_published_job_sync(ledger_db, 99, 3)

    assert svc.record_published_note(ledger_db, 99) is not None
    assert _read_ledger_rows(ledger_db)[0]["content_archive_id"] is None


def test_t0_two_publishes_same_account_both_pending(ledger_db):
    """同账号两条待补 id 的行能共存(note_id 为 NULL,联合唯一键不冲突)。"""
    _add_published_job_sync(ledger_db, 1, 3, title="第一篇")
    _add_published_job_sync(ledger_db, 2, 3, title="第二篇")

    svc.record_published_note(ledger_db, 1)
    svc.record_published_note(ledger_db, 2)

    rows = _read_ledger_rows(ledger_db)
    assert len(rows) == 2
    assert all(r["sync_status"] == "pending_id" and r["note_id"] is None for r in rows)


def test_t0_skips_unknown_job(ledger_db):
    """发布任务不存在 → 不建行(账号都不知道是谁)。"""
    assert svc.record_published_note(ledger_db, 404) is None
    assert _read_ledger_rows(ledger_db) == []


def test_t0_never_raises_on_broken_db():
    """建台账绝不抛错阻断发布终态:库路径都坏了也只返回 None。"""
    assert svc.record_published_note("/nonexistent/dir/nope.db", 1) is None


# ---------------- T1 登记同步任务 ----------------


def _read_sync_jobs(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM browser_jobs WHERE kind='note_ledger_sync' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_schedule_registers_delayed_sync_for_publisher(ledger_db):
    """T1:登记一条本账号的同步任务,排期延后(笔记进列表有延迟)。"""
    _add_published_job_sync(ledger_db, 77, 3)
    before = datetime.utcnow()

    job_id = svc.schedule_note_ledger_sync(ledger_db, 77)

    rows = _read_sync_jobs(ledger_db)
    assert job_id is not None and len(rows) == 1
    assert rows[0]["account_id"] == 3 and rows[0]["status"] == "queued"
    assert rows[0]["operator_id"] == 0  # 不占运营的未终态配额
    payload = repo.get_job_sync(ledger_db, job_id)["payload"]
    assert payload["source_publish_job_id"] == 77
    not_before = datetime.fromisoformat(payload["not_before"])
    assert before + timedelta(seconds=svc.SYNC_DELAY_SECONDS) <= not_before


def test_schedule_is_idempotent_per_publish_job(ledger_db):
    """同一发布重复调不重复登记(钩子幂等)。"""
    _add_published_job_sync(ledger_db, 88, 3)

    assert svc.schedule_note_ledger_sync(ledger_db, 88) is not None
    assert svc.schedule_note_ledger_sync(ledger_db, 88) is None
    assert len(_read_sync_jobs(ledger_db)) == 1


def test_schedule_skips_unknown_job(ledger_db):
    """发布任务不存在 → 不登记。"""
    assert svc.schedule_note_ledger_sync(ledger_db, 404) is None
    assert _read_sync_jobs(ledger_db) == []


def test_schedule_never_raises_on_broken_db():
    """登记绝不抛错阻断发布终态:库路径都坏了也只返回 None。"""
    assert svc.schedule_note_ledger_sync("/nonexistent/dir/nope.db", 1) is None


# ---------------- 同步失败时台账行仍完好(修订的核心保障)----------------


@pytest_asyncio.fixture
async def wired_db(tmp_path, monkeypatch):
    """临时文件库 + monkeypatch 全局 engine/async_session;yield 库路径。

    sync 侧(T0)直连该文件,async 侧(sync_notes/execute)也落在同一个库上。
    """
    from app.core.db import Base

    import app.models  # noqa: F401

    db_path = str(tmp_path / "wired.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", session_factory)
    try:
        yield db_path
    finally:
        await engine.dispose()


async def test_ledger_row_survives_total_sync_failure(wired_db, monkeypatch):
    """同步全程失败(账号被挂验证墙之类),台账行仍完整保留在 pending_id。

    这是写入时序修订的核心保障:哪怕列表接口永远抓不到,"我们发过这篇笔记、什么内容、
    谁发的、什么时候发的"也已经在 T0 完整落库了。
    """
    _add_published_job_sync(wired_db, 77, 3, title="发出去了但同步挂了", created_by=9)
    svc.record_published_note(wired_db, 77)

    async def fake_load(_account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    def boom(*_args):
        raise svc.CreatorNoteListError("no_posted_response: 账号被挂验证墙")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_fetch_sync", boom)

    result = await svc.execute(3, {"source_publish_job_id": 77})

    assert result == {"error": "no_posted_response: 账号被挂验证墙"}
    rows = _read_ledger_rows(wired_db)
    assert len(rows) == 1
    assert rows[0]["sync_status"] == "pending_id"
    assert rows[0]["title"] == "发出去了但同步挂了"
    assert rows[0]["operator_id"] == 9 and rows[0]["source_publish_job_id"] == 77
    assert rows[0]["published_at"] and rows[0]["generated_at"]


# ---------------- T1/T2 同步:补 id / 建 orphan / 只记不删 ----------------


async def _rows(db, account_id: int = 1) -> list[PublishedNote]:
    return list(
        (
            await db.execute(
                select(PublishedNote)
                .where(PublishedNote.account_id == account_id)
                .order_by(PublishedNote.id)
            )
        ).scalars().all()
    )


async def _add_pending(
    db,
    account_id: int = 1,
    title: str = "标题-n1",
    published_at: datetime | None = None,
    job_id: int | None = None,
) -> PublishedNote:
    """造一条 T0 落下的 pending_id 台账行(可选带上对应的 published 发布任务)。"""
    if job_id is not None:
        db.add(PublishJob(
            id=job_id, account_id=account_id, title=title, content="正文",
            images_json="[]", topics_json="[]", status="published",
        ))
    row = PublishedNote(
        account_id=account_id,
        title=title,
        published_at=published_at or _VISIBLE_DT,
        generated_at=datetime(2026, 7, 30, 8, 0, 0),
        operator_id=9,
        source_publish_job_id=job_id,
        sync_status="pending_id",
    )
    db.add(row)
    await db.commit()
    return row


async def test_sync_fills_pending_row_and_backfills_job(db):
    """标题相等 + 平台时间在窗口内 + 候选唯一 → 补平台字段置 linked,并回填发布任务。"""
    await _add_pending(db, title="标题-n1", job_id=10)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    rows = await _rows(db)
    job = await db.get(PublishJob, 10)
    assert stats["linked"] == 1 and stats["orphan"] == 0
    assert len(rows) == 1  # 补在原行上,不新建
    assert rows[0].sync_status == "linked" and rows[0].note_id == "n1"
    assert rows[0].xsec_token == "tok-n1"
    assert rows[0].note_url.startswith("https://www.xiaohongshu.com/explore/n1?")
    assert rows[0].platform_published_at == _VISIBLE_DT
    assert rows[0].note_type == "normal"
    assert rows[0].likes == 1 and rows[0].collects == 2 and rows[0].views == 5
    # 内容侧字段不被同步改掉
    assert rows[0].operator_id == 9 and rows[0].generated_at == datetime(2026, 7, 30, 8, 0, 0)
    # 回填走台账行自带的 job id,不靠标题猜
    assert job.note_id == "n1" and job.published_at == rows[0].published_at


async def test_sync_is_idempotent(db):
    """同一批列表跑两次不产生重复行;刷新快照但不动 first_seen_at 与 published_at。"""
    await _add_pending(db, title="标题-n1", job_id=10)
    first_run = datetime(2026, 7, 30, 10, 0, 0)
    second_run = datetime(2026, 7, 31, 10, 0, 0)

    await svc.sync_notes(db, 1, [_note("n1")], first_run)
    row_after_first = (await _rows(db))[0]
    published_at = row_after_first.published_at
    first_seen = row_after_first.first_seen_at

    stats = await svc.sync_notes(db, 1, [_note("n1", likes=99)], second_run)

    rows = await _rows(db)
    assert len(rows) == 1
    assert (stats["refreshed"], stats["linked"], stats["orphan"]) == (1, 0, 0)
    assert rows[0].likes == 99 and rows[0].last_synced_at == second_run
    assert rows[0].first_seen_at == first_seen
    assert rows[0].published_at == published_at  # T0 时刻不被同步覆盖


async def test_sync_corrects_title_changed_on_platform(db):
    """运营在平台改过标题 → 同步时用 display_title 纠正过来。"""
    await _add_pending(db, title="标题-n1", job_id=10)
    await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    await svc.sync_notes(db, 1, [_note("n1", display_title="平台上改过的标题")], datetime.utcnow())

    assert (await _rows(db))[0].title == "平台上改过的标题"


async def test_sync_keeps_our_title_when_platform_title_empty(db):
    """平台 display_title 是空串(接口实测存在)→ 不覆盖 T0 记下的真标题。"""
    await _add_pending(db, title="标题-n1", job_id=10)
    await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    await svc.sync_notes(db, 1, [_note("n1", display_title="")], datetime.utcnow())

    assert (await _rows(db))[0].title == "标题-n1"


async def test_sync_creates_orphan_for_unknown_note(db):
    """列表里有、publish_jobs 里也查无此标题 → 真 orphan,两个外键都留 NULL。"""
    stats = await svc.sync_notes(db, 1, [_note("x1")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert rows[0].sync_status == "orphan"
    assert rows[0].source_publish_job_id is None and rows[0].content_archive_id is None
    assert rows[0].note_id == "x1"
    # 没有 T0 时刻可用,退而用平台时间(published_at 永不为空)
    assert rows[0].published_at == _VISIBLE_DT


# ---------------- 存量笔记:没有 T0 行,按标题回连 publish_jobs ----------------


async def _add_legacy_job(
    db,
    job_id: int,
    account_id: int = 1,
    title: str = "标题-n1",
    status: str = "published",
    started_at: datetime | None = None,
    archive_id: int | None = None,
) -> PublishJob:
    """造一条台账上线前的发布任务(没有对应 T0 台账行),可选带归档。"""
    job = PublishJob(
        id=job_id, account_id=account_id, title=title, content="正文",
        images_json="[]", topics_json="[]", status=status,
        started_at=started_at if started_at is not None else _VISIBLE_DT,
    )
    db.add(job)
    if archive_id is not None:
        db.add(ContentArchive(
            id=archive_id, title=title, content="正文", topics_json="[]", media_json="[]",
            kind="image_note", source_account_id=account_id, source_publish_job_id=job_id,
        ))
    await db.commit()
    return job


async def test_sync_links_legacy_note_to_publish_job(db):
    """存量场景:有 published 的 job、没有 T0 台账行 → 建行后按标题唯一回连,置 linked。

    这正是真号首次同步暴露的缺陷:20 篇全落 orphan,其中 9 篇本该连上自己的正文与媒体。
    """
    await _add_legacy_job(db, 10, title="标题-n1", archive_id=5)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    rows = await _rows(db)
    job = await db.get(PublishJob, 10)
    assert stats["linked_by_title"] == 1 and stats["orphan"] == 0
    assert rows[0].sync_status == "linked"
    assert rows[0].source_publish_job_id == 10
    assert rows[0].content_archive_id == 5  # 顺带挂上正文与媒体
    assert job.note_id == "n1"  # publish_jobs.note_id 不再全空
    assert job.published_at == rows[0].published_at


async def test_sync_heals_existing_orphan_row(db):
    """自愈:上线前那次同步落下的 orphan 行(已有 note_id),重跑时补上回连。

    生产库里已经躺着 20 行这样的 orphan,修复必须能把它们救回来,而不是只对新行生效。
    """
    db.add(PublishedNote(
        account_id=1, note_id="n1", title="标题-n1", published_at=_VISIBLE_DT,
        platform_published_at=_VISIBLE_DT, sync_status="orphan",
    ))
    await _add_legacy_job(db, 10, title="标题-n1", archive_id=5)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    rows = await _rows(db)
    assert len(rows) == 1 and stats["refreshed"] == 1 and stats["linked_by_title"] == 1
    assert rows[0].sync_status == "linked" and rows[0].source_publish_job_id == 10
    assert rows[0].content_archive_id == 5


async def test_sync_legacy_link_is_idempotent(db):
    """同一份数据跑两次:linked 行不被改回 orphan,也不重复回连。"""
    await _add_legacy_job(db, 10, title="标题-n1", archive_id=5)

    await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())
    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["linked_by_title"] == 0 and stats["orphan"] == 0  # 已连上,不再找
    assert len(rows) == 1 and rows[0].sync_status == "linked"
    assert rows[0].source_publish_job_id == 10


async def test_sync_legacy_stays_orphan_when_title_matches_many_jobs(db):
    """标题在 publish_jobs 里匹配到多条 → 无法区分,留 orphan + NULL(绝不猜)。"""
    await _add_legacy_job(db, 10, title="同一个标题")
    await _add_legacy_job(db, 11, title="同一个标题")

    stats = await svc.sync_notes(
        db, 1, [_note("n1", display_title="同一个标题")], datetime.utcnow()
    )

    rows = await _rows(db)
    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert rows[0].sync_status == "orphan"
    assert rows[0].source_publish_job_id is None and rows[0].content_archive_id is None


async def test_sync_legacy_prefers_job_inside_tight_window(db):
    """同标题两条 job:一条紧贴笔记发布时刻、一条差好几小时 → 认紧的那条。

    生产实测case:账号1 的"李冠阳"那篇同标题发过两次(job 68 差 4.4 小时、job 77 差
    246s,而实测发布耗时就是 160~284s)。只用 ±1 天的松窗口会同时命中两条判成认不准,
    白白丢掉一条本可确定的回连;紧窗口(30 分钟)内唯一即是强证据。
    """
    near = _VISIBLE_DT - timedelta(seconds=246)
    far = _VISIBLE_DT - timedelta(hours=4, minutes=26)
    await _add_legacy_job(db, 68, title="标题-n1", started_at=far, archive_id=38)
    await _add_legacy_job(db, 77, title="标题-n1", started_at=near, archive_id=39)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    row = (await _rows(db))[0]
    assert stats["linked_by_title"] == 1 and stats["orphan"] == 0
    assert row.source_publish_job_id == 77 and row.content_archive_id == 39


async def test_sync_legacy_stays_orphan_when_both_jobs_inside_tight_window(db):
    """两条同标题 job 都落在紧窗口内 → 真区分不了,放弃(不放宽也不取最近的那条)。"""
    await _add_legacy_job(db, 68, title="标题-n1", started_at=_VISIBLE_DT - timedelta(seconds=200))
    await _add_legacy_job(db, 77, title="标题-n1", started_at=_VISIBLE_DT - timedelta(seconds=250))

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    assert stats["linked_by_title"] == 0 and stats["orphan"] == 1
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_sync_legacy_stays_orphan_when_title_empty(db):
    """空标题笔记(实测 3 篇)没有可匹配的键 → 留 orphan + NULL。"""
    await _add_legacy_job(db, 10, title="")

    stats = await svc.sync_notes(db, 1, [_note("n1", display_title="")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert rows[0].source_publish_job_id is None


async def test_sync_legacy_stays_orphan_when_time_far_off(db):
    """标题对上但发布时间差得离谱(窗口外)→ 是另一篇同名笔记,留 orphan + NULL。"""
    far = _VISIBLE_DT + timedelta(seconds=svc.LEGACY_MATCH_WINDOW_SECONDS + 3600)
    await _add_legacy_job(db, 10, title="标题-n1", started_at=far)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert rows[0].source_publish_job_id is None


async def test_sync_legacy_ignores_non_published_jobs(db):
    """failed/canceled 的 job 从未真正发布(实测有 2 条这样的归档)→ 不参与回连。"""
    await _add_legacy_job(db, 10, title="标题-n1", status="failed")

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_sync_legacy_does_not_steal_taken_job(db):
    """目标 job 已被别的台账行认领 → 不抢(唯一约束下抢了会炸掉整次同步)。"""
    await _add_legacy_job(db, 10, title="同一个标题")
    db.add(PublishedNote(
        account_id=1, note_id="老笔记", title="同一个标题", published_at=_VISIBLE_DT,
        sync_status="linked", source_publish_job_id=10,
    ))
    await db.commit()

    stats = await svc.sync_notes(
        db, 1, [_note("n2", display_title="同一个标题")], datetime.utcnow()
    )

    rows = await _rows(db)
    new_row = [r for r in rows if r.note_id == "n2"][0]
    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert new_row.source_publish_job_id is None


async def test_sync_legacy_links_only_within_same_account(db):
    """别号的同标题 job 不参与回连(账号维度先收窄)。"""
    await _add_legacy_job(db, 10, account_id=2, title="标题-n1")

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    assert stats["orphan"] == 1 and stats["linked_by_title"] == 0
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_sync_leaves_pending_when_title_differs(db):
    """标题对不上 → 不认(留 pending_id),那篇列表笔记单独记成 orphan。"""
    await _add_pending(db, title="我们发的标题", job_id=10)

    stats = await svc.sync_notes(db, 1, [_note("n1", display_title="完全不同的标题")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["linked"] == 0 and stats["orphan"] == 1 and stats["pending_remaining"] == 1
    pending = [r for r in rows if r.sync_status == "pending_id"]
    assert len(pending) == 1 and pending[0].note_id is None


async def test_sync_leaves_pending_when_platform_time_out_of_window(db):
    """标题一样但平台时间差太远 → 不认(可能是很久以前的同名笔记),留 pending_id。"""
    far = _VISIBLE_DT + timedelta(seconds=svc.MATCH_WINDOW_SECONDS + 60)
    await _add_pending(db, title="标题-n1", published_at=far, job_id=10)

    stats = await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    assert stats["linked"] == 0 and stats["orphan"] == 1
    assert [r for r in await _rows(db) if r.sync_status == "pending_id"]


async def test_sync_leaves_pending_when_two_rows_share_title(db):
    """两条 pending 行同标题且都在窗口内 → 认不准,一条都不认(绝不猜)。"""
    await _add_pending(db, title="同一个标题", job_id=10)
    await _add_pending(db, title="同一个标题", job_id=11)

    stats = await svc.sync_notes(db, 1, [_note("n1", display_title="同一个标题")], datetime.utcnow())

    rows = await _rows(db)
    assert stats["linked"] == 0 and stats["pending_remaining"] == 2
    assert len([r for r in rows if r.sync_status == "pending_id"]) == 2
    assert (await db.get(PublishJob, 10)).note_id in (None, "")
    assert stats["ambiguous"] == 1  # 认不准,不建 orphan


async def test_sync_ambiguous_notes_create_no_orphan_rows(db):
    """两篇同标题笔记对两条同标题 pending 行:认不准就都留着,**也不建假 orphan 行**。

    这两篇明明是我们发的,只是分不清哪条 pending 对哪篇;建 orphan 等于往台账里塞两条
    明知是假的"非本系统发布",还会让行数翻倍。留着 pending 等下次同步更干净。
    """
    await _add_pending(db, title="同一个标题", job_id=10)
    await _add_pending(db, title="同一个标题", job_id=11)

    stats = await svc.sync_notes(
        db,
        1,
        [_note("n1", display_title="同一个标题"), _note("n2", display_title="同一个标题")],
        datetime.utcnow(),
    )

    assert stats["linked"] == 0 and stats["orphan"] == 0 and stats["ambiguous"] == 2
    assert stats["pending_remaining"] == 2
    assert len(await _rows(db)) == 2  # 没有多出任何行


async def test_sync_keeps_rows_missing_from_list(db):
    """台账里有、列表里查不到的行 → 只记录不删(笔记可能被删/被限流)。"""
    await _add_pending(db, title="标题-n1", job_id=10)
    await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    stats = await svc.sync_notes(db, 1, [], datetime.utcnow())

    rows = await _rows(db)
    assert stats["missing"] == 1
    assert len(rows) == 1  # 行还在
    assert rows[0].note_id == "n1" and rows[0].sync_status == "linked"


async def test_sync_skips_notes_without_id(db):
    """无 id 的列表条目落不了台账幂等键,跳过而不是建一行空 note_id。"""
    stats = await svc.sync_notes(db, 1, [{"display_title": "无 id"}], datetime.utcnow())

    assert stats["orphan"] == 0
    assert await _rows(db) == []


async def test_sync_does_not_overwrite_existing_job_note_id(db):
    """发布任务已有 note_id → 不覆盖(尊重既有数据,只补空缺)。"""
    await _add_pending(db, title="标题-n1", job_id=10)
    job = await db.get(PublishJob, 10)
    job.note_id = "已有的id"
    job.published_at = datetime(2026, 1, 1)
    await db.commit()

    await svc.sync_notes(db, 1, [_note("n1")], datetime.utcnow())

    job = await db.get(PublishJob, 10)
    assert job.note_id == "已有的id" and job.published_at == datetime(2026, 1, 1)


# ---------------- execute 契约 + 台账纪律 ----------------


async def test_execute_returns_error_without_cookies(monkeypatch):
    """没 cookie 直接收敛成 {"error"},不起浏览器、不抛出。"""

    async def fake_load(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)

    assert "error" in await svc.execute(1, {})


def test_note_ledger_sync_is_idempotent_kind():
    """纯只读抓取 + upsert,僵死后可自动重跑。"""
    assert "note_ledger_sync" in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_note_ledger_execute():
    """account_worker 按 kind 能解析到本服务的 execute(否则子进程会兜底置 error)。"""
    from app import account_worker

    assert account_worker._resolve_execute("note_ledger_sync") is not None
