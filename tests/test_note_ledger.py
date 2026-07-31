"""发布笔记永久台账单测(不起真浏览器),锁设计第七节验收 1 的四项 + 台账纪律。

- 分页遍历终止条件:空批 / 滚动后无新响应 / 批数上限,三条都不许死循环;
- upsert 幂等:同一 note_id 跑两次不产生重复行,first_seen_at 不被改写;
- 关联不上留 NULL:标题重复、标题为空、同标题多条 published、目标 job 已被占用,
  一律不连、不回填(绝不猜);
- visible_time → published_at 转换(缺失/非法留 NULL,不拿北京时间串猜时区);
- 只读纪律:抓取全程不点击、不 evaluate(假 page 上这两个方法一被调用就断言失败)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

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

    def __init__(self, script):
        self._script = list(script)
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
        self._emit(_FakeResponse(_POSTED_URL, {"code": 0, "data": {"notes": notes}}))

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


def _note(note_id: str, **extra) -> dict:
    """构造一条接口原样字段的笔记(默认值取自真号实测响应)。"""
    base = {
        "id": note_id,
        "xsec_token": f"tok-{note_id}",
        "xsec_source": "pc_creatormng",
        "display_title": f"标题-{note_id}",
        "time": "2025-09-25 17:15",
        "visible_time": 1758791784,
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


# ---------------- visible_time → published_at ----------------


def test_published_at_from_visible_time():
    """visible_time(unix 秒)→ naive UTC datetime(真号实测值对应北京时间 17:16)。"""
    assert svc.published_at_of(_note("a")) == datetime(2025, 9, 25, 9, 16, 24)


@pytest.mark.parametrize("value", [None, "", "不是时间", {"a": 1}])
def test_published_at_null_when_unusable(value):
    """visible_time 缺失/非法 → 留 NULL(不拿北京时间串 time 去猜时区)。"""
    assert svc.published_at_of({"visible_time": value}) is None


def test_note_url_falls_back_without_token():
    """有 xsec 两件套就拼完整链接;没有 token 时退化成裸 explore 链接,不编造参数。"""
    assert svc._note_url("abc", "T", "pc_creatormng") == (
        "https://www.xiaohongshu.com/explore/abc?xsec_token=T&xsec_source=pc_creatormng"
    )
    assert svc._note_url("abc", None, None) == "https://www.xiaohongshu.com/explore/abc"


# ---------------- upsert 幂等 ----------------


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


async def test_upsert_is_idempotent(db):
    """同一账号同一 note_id 跑两次不产生重复行;刷新快照但不动 first_seen_at。"""
    first_run = datetime(2026, 7, 30, 10, 0, 0)
    second_run = datetime(2026, 7, 31, 10, 0, 0)
    notes = [_note("n1"), _note("n2")]

    stats1 = await svc.upsert_notes(db, 1, notes, first_run)
    updated = [_note("n1", likes=99, display_title="改过的标题"), _note("n2")]
    stats2 = await svc.upsert_notes(db, 1, updated, second_run)

    rows = await _rows(db)
    assert len(rows) == 2
    assert (stats1["inserted"], stats1["updated"]) == (2, 0)
    assert (stats2["inserted"], stats2["updated"]) == (0, 2)
    assert rows[0].first_seen_at == first_run  # 不被第二次同步改写
    assert rows[0].last_synced_at == second_run
    assert rows[0].likes == 99 and rows[0].title == "改过的标题"
    assert rows[0].note_url.startswith("https://www.xiaohongshu.com/explore/n1?")
    assert rows[0].note_type == "normal"
    assert rows[0].collects == 2 and rows[0].comments == 3
    assert rows[0].shares == 4 and rows[0].views == 5


async def test_upsert_skips_notes_without_id(db):
    """无 id 的条目落不了台账(幂等键缺失),跳过而不是插一行空 note_id。"""
    await svc.upsert_notes(db, 1, [{"display_title": "无 id"}, _note("n1")], datetime.utcnow())

    assert [r.note_id for r in await _rows(db)] == ["n1"]


# ---------------- 回连与回填 ----------------


async def _add_job(db, job_id: int, account_id: int, title: str, status: str = "published"):
    job = PublishJob(
        id=job_id,
        account_id=account_id,
        title=title,
        content="正文",
        images_json="[]",
        topics_json="[]",
        status=status,
    )
    db.add(job)
    await db.commit()
    return job


async def test_link_and_backfill_on_unique_title_match(db):
    """账号内标题唯一命中 → 连上发布任务与归档,并回填 note_id / published_at。"""
    await _add_job(db, 10, 1, "边界感是练出来的")
    db.add(ContentArchive(
        id=5, title="边界感是练出来的", content="正文", topics_json="[]", media_json="[]",
        kind="image_note", source_account_id=1, source_publish_job_id=10,
    ))
    await db.commit()

    stats = await svc.upsert_notes(
        db, 1, [_note("n1", display_title="边界感是练出来的")], datetime.utcnow()
    )

    row = (await _rows(db))[0]
    job = await db.get(PublishJob, 10)
    assert stats["linked"] == 1
    assert row.source_publish_job_id == 10 and row.content_archive_id == 5
    assert job.note_id == "n1"
    assert job.published_at == datetime(2025, 9, 25, 9, 16, 24)


async def test_link_null_when_title_duplicated_in_batch(db):
    """本批里两篇同标题 → 无法区分是哪一篇,两行都留 NULL、不回填(绝不猜)。"""
    await _add_job(db, 10, 1, "同一个标题")

    stats = await svc.upsert_notes(
        db,
        1,
        [_note("n1", display_title="同一个标题"), _note("n2", display_title="同一个标题")],
        datetime.utcnow(),
    )

    rows = await _rows(db)
    job = await db.get(PublishJob, 10)
    assert stats["linked"] == 0
    assert all(r.source_publish_job_id is None for r in rows)
    assert all(r.content_archive_id is None for r in rows)
    assert not (job.note_id or "")  # 回填也不做


async def test_link_null_when_title_empty(db):
    """空标题笔记(接口实测存在)没有可匹配的键 → 留 NULL。"""
    await _add_job(db, 10, 1, "")

    stats = await svc.upsert_notes(db, 1, [_note("n1", display_title="")], datetime.utcnow())

    assert stats["linked"] == 0
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_link_null_when_multiple_published_jobs_share_title(db):
    """库里同标题多条 published → 命中不唯一,留 NULL。"""
    await _add_job(db, 10, 1, "重复标题")
    await _add_job(db, 11, 1, "重复标题")

    stats = await svc.upsert_notes(
        db, 1, [_note("n1", display_title="重复标题")], datetime.utcnow()
    )

    assert stats["linked"] == 0
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_link_null_when_job_already_taken(db):
    """目标发布任务已被别的台账行认领 → 不抢(两篇同标题笔记跨批出现的情形)。"""
    await _add_job(db, 10, 1, "同一个标题")
    await svc.upsert_notes(db, 1, [_note("n1", display_title="同一个标题")], datetime.utcnow())

    stats = await svc.upsert_notes(
        db, 1, [_note("n2", display_title="同一个标题")], datetime.utcnow()
    )

    rows = await _rows(db)
    assert stats["linked"] == 0
    assert rows[0].source_publish_job_id == 10  # 先连上的那行不动
    assert rows[1].source_publish_job_id is None


async def test_link_ignores_non_published_and_other_accounts(db):
    """failed/canceled 的 job(实测有从未真正发布的归档)与别号的 job 都不参与匹配。"""
    await _add_job(db, 10, 1, "标题", status="failed")
    await _add_job(db, 11, 2, "标题")

    stats = await svc.upsert_notes(db, 1, [_note("n1", display_title="标题")], datetime.utcnow())

    assert stats["linked"] == 0
    assert (await _rows(db))[0].source_publish_job_id is None


async def test_backfill_does_not_overwrite_existing_note_id(db):
    """已有 note_id 的发布任务不被覆盖(尊重既有数据,只补空缺)。"""
    job = await _add_job(db, 10, 1, "标题")
    job.note_id = "已有的id"
    job.published_at = datetime(2026, 1, 1)
    await db.commit()

    await svc.upsert_notes(db, 1, [_note("n1", display_title="标题")], datetime.utcnow())

    job = await db.get(PublishJob, 10)
    assert job.note_id == "已有的id"
    assert job.published_at == datetime(2026, 1, 1)


# ---------------- 发布成功钩子(登记同步任务)----------------


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


def _add_published_job_sync(db_path: str, job_id: int, account_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_at)"
            " VALUES (?, ?, '标题', '正文', '[]', '[]', 'published', 0, ?)",
            (job_id, account_id, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _read_sync_jobs(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM browser_jobs WHERE kind='note_ledger_sync' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_schedule_registers_delayed_sync_for_publisher(ledger_db):
    """发布成功登记一条本账号的同步任务,排期延后(笔记进列表有延迟)。"""
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
    """发布任务不存在 → 不登记(账号都不知道是谁)。"""
    assert svc.schedule_note_ledger_sync(ledger_db, 404) is None
    assert _read_sync_jobs(ledger_db) == []


def test_schedule_never_raises_on_broken_db():
    """登记绝不抛错阻断发布终态:库路径都坏了也只返回 None。"""
    assert svc.schedule_note_ledger_sync("/nonexistent/dir/nope.db", 1) is None


# ---------------- execute 契约 + 台账纪律 ----------------


async def test_execute_returns_error_without_cookies(monkeypatch):
    """没 cookie 直接收敛成 {"error"},不起浏览器、不抛出。"""

    async def fake_load(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)

    assert "error" in await svc.execute(1, {})


async def test_execute_converges_fetch_failure(monkeypatch):
    """抓取语义失败(CreatorNoteListError)收敛成 {"error": reason},不上抛、不落库。"""

    async def fake_load(_account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    def boom(*_args):
        raise svc.CreatorNoteListError("no_posted_response: 没响应")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_fetch_sync", boom)

    assert await svc.execute(1, {}) == {"error": "no_posted_response: 没响应"}


def test_note_ledger_sync_is_idempotent_kind():
    """纯只读抓取 + upsert,僵死后可自动重跑。"""
    assert "note_ledger_sync" in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_note_ledger_execute():
    """account_worker 按 kind 能解析到本服务的 execute(否则子进程会兜底置 error)。"""
    from app import account_worker

    assert account_worker._resolve_execute("note_ledger_sync") is not None
