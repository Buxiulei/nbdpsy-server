"""矩阵互动单测(不起真浏览器),锁设计第五节验收 1 的四项 + 台账纪律。

- 矩阵选号:全部 cookie_status='valid' 排除发布者本人(失效/未知 cookie 号不派);
- 标题匹配定位:命中才点,匹配不到抛错放弃(**绝不默认取第一篇**);
- 已赞/已藏跳过分支:图标读到 #liked / #collected 记 skipped 且一次都不点;
- 成败判定:两个动作全失败必落 error(评论移走后 not_requested 状态一并取消,
  判据改为直接对全部动作取 any——回归锁死"永远落不下 error"的老缺陷不复发);
- 延时排期:payload 的 not_before 未到点则不派发(执行方不 sleep 等待);
- matrix_interact 非幂等,不得进 _IDEMPOTENT_KINDS(重复执行会取消已点的赞)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.browser import matrix_interact as browser_mi
from app.services import browser_jobs_repo as repo
from app.services import matrix_interact as svc


# ---------------- 测试替身 ----------------


class _FakeElement:
    """假元素:只提供定位/读文本/取矩形这几件被测代码真用到的能力。"""

    def __init__(self, text: str = "", box: dict | None = None):
        self._text = text
        self._box = box or {"x": 100.0, "y": 200.0, "width": 240.0, "height": 320.0}

    def inner_text(self) -> str:
        return self._text

    def bounding_box(self) -> dict:
        return self._box


class _FakeLocator:
    def __init__(self, ok: bool = True):
        self._ok = ok

    @property
    def first(self):
        return self

    def wait_for(self, **_kw):
        if not self._ok:
            raise RuntimeError("等待超时")


class _FakePage:
    """假 page:query_selector(_all) / locator / evaluate / url 四件套。"""

    def __init__(self, cards=(), elements=None, evaluate=None, locator_ok=True):
        self._cards = list(cards)
        self._elements = elements or {}
        self._evaluate = evaluate or (lambda js, arg: None)
        self._locator_ok = locator_ok
        self.url = "https://www.xiaohongshu.com/explore/abc?xsec_token=T"

    def locator(self, _sel):
        return _FakeLocator(self._locator_ok)

    def query_selector_all(self, _sel):
        return self._cards

    def query_selector(self, sel):
        return self._elements.get(sel)

    def evaluate(self, js, arg=None):
        return self._evaluate(js, arg)


class _FakeHuman:
    """假拟人层:记录动作,断言"该点的点了 / 不该点的一次没点"。"""

    def __init__(self):
        self.navigated = None
        self.clicks = []
        self.typed = []

    def navigate(self, url, **_kw):
        self.navigated = url

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        pass

    def scroll_to_element(self, _el):
        pass

    def click(self, target, **_kw):
        self.clicks.append(target)

    def type_text(self, target, text, **_kw):
        self.typed.append((target, text))


# ---------------- 标题匹配定位 ----------------


def test_title_matches_exact_and_truncated():
    """完整包含命中;卡片截断成省略号时按 ≥8 字前缀命中;短前缀/异题不命中。"""
    title = "焦虑发作时的五个自救动作"
    assert browser_mi._title_matches("焦虑发作时的五个自救动作\n1.2万", title)
    assert browser_mi._title_matches("焦虑发作时的五个自...", title)
    assert not browser_mi._title_matches("焦虑发...", title)  # 前缀太短,不认
    assert not browser_mi._title_matches("拖延症的三个成因", title)
    assert not browser_mi._title_matches("焦虑发作时的五个自救动作", "")


def test_open_note_by_title_clicks_matched_card():
    """按标题匹配到第几张就点第几张(不是第一张),点的是卡片上部封面区。"""
    target = "边界感是练出来的"
    cards = [
        _FakeElement("别人的情绪不是你的责任\n860"),
        _FakeElement(f"{target}\n1203"),
    ]
    page = _FakePage(cards=cards)
    human = _FakeHuman()

    url = browser_mi._open_note_by_title(page, human, "u123", target)

    assert human.navigated.endswith("/user/profile/u123")
    assert url == page.url
    assert len(human.clicks) == 1
    box = cards[1].bounding_box()
    x, y = human.clicks[0]
    assert x == box["x"] + box["width"] * 0.5
    assert y == box["y"] + box["height"] * 0.35  # 上部封面区,不点底部作者行


def test_open_note_by_title_gives_up_when_no_match():
    """匹配不到标题 → 抛 note_not_found 放弃,绝不退而求其次点第一篇。"""
    cards = [_FakeElement("完全不相干的另一篇笔记标题"), _FakeElement("再来一篇也不相干")]
    page = _FakePage(cards=cards)
    human = _FakeHuman()

    with pytest.raises(browser_mi.MatrixInteractError) as exc:
        browser_mi._open_note_by_title(page, human, "u123", "目标笔记的标题在这里")

    assert exc.value.reason.startswith("note_not_found")
    assert human.clicks == []  # 一次都没点


# ---------------- 已赞 / 已藏跳过分支 ----------------


def test_like_skipped_when_already_liked():
    """图标是 #liked(不是看 class 里的 like-active)→ 记 skipped 且一次都不点。"""
    page = _FakePage(
        elements={".engage-bar .like-wrapper": _FakeElement()},
        evaluate=lambda js, arg: "#liked",
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "skipped"
    assert human.clicks == []


def test_collect_skipped_when_already_collected():
    """收藏同构:#collected → skipped,不点。"""
    page = _FakePage(
        elements={".engage-bar .collect-wrapper": _FakeElement()},
        evaluate=lambda js, arg: "#collected",
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "收藏", browser_mi._COLLECT_SELECTORS, "#collect", "#collected"
    )

    assert result["status"] == "skipped"
    assert human.clicks == []


def test_like_clicks_and_verifies_icon_flip():
    """未赞(#like)→ 拟人点击 → 复核图标变 #liked 才算 done。"""
    element = _FakeElement()
    state = {"href": "#like"}

    def fake_evaluate(_js, _arg):
        href = state["href"]
        state["href"] = "#liked"  # 点击后下一次读到已赞
        return href

    page = _FakePage(
        elements={".engage-bar .like-wrapper": element}, evaluate=fake_evaluate
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "done"
    assert human.clicks == [element]


def test_like_icon_unreadable_does_not_click():
    """图标读不出来(状态未知)就不点:盲点可能把已有的赞取消掉。"""
    page = _FakePage(
        elements={".engage-bar .like-wrapper": _FakeElement()},
        evaluate=lambda js, arg: None,
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "error" and "unreadable" in result["reason"]
    assert human.clicks == []


# ---------------- 成败判定(评论移除后)----------------


def _patch_interact(monkeypatch, icon_result: dict) -> None:
    """把 interact_with_note 的定位/浏览/图标动作都换成替身,只留成败判定这一层。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "_icon_action", lambda *a, **k: icon_result)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())


def test_interact_has_no_comment_step(monkeypatch):
    """矩阵互动只剩点赞 + 收藏两个动作:actions 恒为这两条,不含 comment。

    评论是**结构上**移除的,不是靠传空文案绕过——所以 comment 这个键压根不该出现,
    也不该再有任何 not_requested 状态(它正是老成败判定漏洞的载体)。
    """
    _patch_interact(monkeypatch, {"status": "done"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert set(result["actions"]) == {"like", "collect"}
    assert all(
        a["status"] != "not_requested" for a in result["actions"].values()
    )
    assert "error" not in result


def test_both_actions_failed_falls_to_error(monkeypatch):
    """点赞收藏双双失败 → 必须落 error,绝不能显示 done。

    回归老缺陷:旧判定先剔掉 not_requested 再要求"剔剩的非空"才判失败,一旦所有动作
    都可缺席,error 就永远落不下来,错误上报被彻底架空。评论移走后两个动作无条件各跑
    一次,判据直接对全部动作取 any,不存在可剔空的集合。
    """
    _patch_interact(monkeypatch, {"status": "error", "reason": "点不动"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert result["actions"]["like"]["status"] == "error"
    assert result["actions"]["collect"]["status"] == "error"
    assert result.get("error") == "点赞与收藏均失败"


def test_action_exception_still_counts_as_failure(monkeypatch):
    """动作抛异常被 except 兜成 error 写回 actions,照样参与判定 → 整体 error。

    这条锁死"异常动作没进 actions 导致集合为空、于是不判失败"的另一条退路。
    """
    def _boom(*a, **k):
        raise RuntimeError("页面炸了")

    _patch_interact(monkeypatch, {"status": "done"})
    monkeypatch.setattr(browser_mi, "_icon_action", _boom)

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert set(result["actions"]) == {"like", "collect"}
    assert all(a["status"] == "error" for a in result["actions"].values())
    assert result.get("error") == "点赞与收藏均失败"


def test_one_action_succeeds_is_not_error(monkeypatch):
    """一个成功一个失败 → 不落 error(动作互不阻断,有成果就不算整体失败)。"""
    calls = {"n": 0}

    def _alternating(*a, **k):
        calls["n"] += 1
        return {"status": "done"} if calls["n"] == 1 else {"status": "error",
                                                           "reason": "点不动"}

    _patch_interact(monkeypatch, {"status": "done"})
    monkeypatch.setattr(browser_mi, "_icon_action", _alternating)

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert "error" not in result


def test_already_liked_and_collected_counts_as_success(monkeypatch):
    """已赞已藏(skipped)→ 目标本就达成,不得落 error。"""
    _patch_interact(monkeypatch, {"status": "skipped", "reason": "已激活"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert "error" not in result


# ---------------- 独立评论(comment_on_note)----------------


@pytest.mark.parametrize("text", ["", "   ", None])
def test_comment_empty_text_is_error_not_skip(text):
    """空文案 → error(评论独立后没有"这次没要求做"这回事),且不碰页面任何元素。"""
    page = _FakePage(elements={"boom": None})
    human = _FakeHuman()

    result = browser_mi._do_comment(page, human, text)

    assert result["status"] == "error"
    assert "comment_text_empty" in result["reason"]
    assert human.clicks == [] and human.typed == []


class _FakeClock:
    """假时钟:sleep 不真睡只推进虚拟时间,让 _do_comment 的轮询超时分支秒级跑完。"""

    def __init__(self):
        self._now = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


def _comment_page(posted: dict) -> _FakePage:
    """造一个"评论链路全程顺畅"的假页面,只有最后的复核结果由 posted 决定。"""
    elements = {
        browser_mi._COMMENT_ENTRY_SELECTORS[0]: _FakeElement("评论"),
        browser_mi._TEXTAREA: _FakeElement(),
        browser_mi._SUBMIT: _FakeElement(),
    }

    def _evaluate(js, arg=None):
        if js is browser_mi._TEXTAREA_READY_JS:
            return {"ready": True, "reason": "ok"}
        if js is browser_mi._SUBMIT_STATE_JS:
            return {"found": True, "gray": False}
        if js is browser_mi._COMMENT_POSTED_JS:
            return dict(posted)
        raise AssertionError(f"未预期的 evaluate: {js[:40]!r}")

    return _FakePage(elements=elements, evaluate=_evaluate)


@pytest.mark.parametrize("cleared", [False, True])
def test_comment_listed_is_done_regardless_of_cleared(monkeypatch, cleared):
    """listed=True 即 done —— cleared 是前端表现(残留空白/清空延迟)不能当判据。

    cleared=False 那条是本次修复的核心用例:7 条真发出去的评论曾因此被记 error。
    """
    monkeypatch.setattr(browser_mi, "time", _FakeClock())
    page = _comment_page({"cleared": cleared, "listed": True})
    human = _FakeHuman()

    result = browser_mi._do_comment(page, human, "写得真好")

    assert result["status"] == "done"
    assert "reason" not in result
    # cleared 只作附加信息随结果带出,供日后排查前端清空行为
    assert result["cleared"] is cleared
    assert human.typed == [(page.query_selector(browser_mi._TEXTAREA), "写得真好")]


def test_comment_not_listed_is_error(monkeypatch):
    """listed=False → error(不能松:这条防的是"点了发送但根本没发出去")。"""
    monkeypatch.setattr(browser_mi, "time", _FakeClock())
    page = _comment_page({"cleared": True, "listed": False})

    result = browser_mi._do_comment(page, _FakeHuman(), "写得真好")

    assert result["status"] == "error"
    assert "comment_unverified" in result["reason"]
    assert result["cleared"] is True


def test_comment_on_note_success(monkeypatch):
    """评论发出并复核 → {note_url, commented:True},无 error 键(台账落 done)。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "_do_comment",
                        lambda *a, **k: {"status": "done"})
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert result == {
        "note_url": "https://www.xiaohongshu.com/explore/x", "commented": True
    }


def test_comment_on_note_failure_carries_error_and_url(monkeypatch):
    """评论没发出 → 带 error 键(台账落 error)且仍给 note_url 供人工核对。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(
        browser_mi, "_do_comment",
        lambda *a, **k: {"status": "error", "reason": "comment_unverified: 没复核到"},
    )
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert "comment_unverified" in result["error"]
    # 非幂等链路,人工核对是重试前的必要步骤,所以失败也要把链接交出去
    assert result["note_url"] == "https://www.xiaohongshu.com/explore/x"
    assert "commented" not in result


# ---------------- 矩阵选号 + 登记(schedule_matrix_interact) ----------------


@pytest.fixture
def matrix_db(tmp_path):
    """建一个带全部表的临时 sqlite 文件库,返回路径(sync 侧直连用)。"""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401  触发模型注册
    from app.core.db import Base

    db_path = str(tmp_path / "matrix.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _add_account(db_path: str, account_id: int, name: str, cookie_status: str,
                 user_id: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO xhs_accounts (id, name, user_id, status, cookie_status, created_at)"
            " VALUES (?, ?, ?, 'unknown', ?, ?)",
            (account_id, name, user_id, cookie_status, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _add_published_job(db_path: str, job_id: int, account_id: int, title: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_at)"
            " VALUES (?, ?, ?, '正文', '[]', '[]', 'published', 0, ?)",
            (job_id, account_id, title, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _read_jobs(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM browser_jobs WHERE kind='matrix_interact' ORDER BY account_id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_schedule_selects_valid_accounts_excluding_publisher(matrix_db):
    """矩阵 = 全部 cookie_status='valid' 排除发布者本人;失效/未知 cookie 号不派。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_account(matrix_db, 3, "失效号", "invalid")
    _add_account(matrix_db, 4, "矩阵号B", "valid")
    _add_account(matrix_db, 5, "未检号", "unknown")
    _add_published_job(matrix_db, 77, 1, "边界感是练出来的")

    job_ids = svc.schedule_matrix_interact(matrix_db, 77)

    rows = _read_jobs(matrix_db)
    assert len(job_ids) == 2
    assert [r["account_id"] for r in rows] == [2, 4]  # 发布者 1 / 失效 3 / 未检 5 都不在
    assert all(r["status"] == "queued" and r["operator_id"] == 0 for r in rows)


def test_schedule_payload_carries_locator_and_window(matrix_db):
    """payload 带主页定位三件套 + 窗口内随机 not_before;**不再有 comment 字段**。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 88, 1, "焦虑发作时的五个自救动作")

    before = datetime.utcnow()
    svc.schedule_matrix_interact(matrix_db, 88)

    payload = repo.get_job_sync(matrix_db, _read_jobs(matrix_db)[0]["id"])["payload"]
    assert payload["publisher_user_id"] == "pub-uid"
    assert payload["title"] == "焦虑发作时的五个自救动作"
    assert payload["source_publish_job_id"] == 88
    # 评论已从矩阵互动移除(独立走 note_comment),payload 里不该再有这个字段
    assert "comment" not in payload
    not_before = datetime.fromisoformat(payload["not_before"])
    assert before <= not_before <= before + timedelta(seconds=svc.WINDOW_SECONDS + 1)


def test_schedule_is_idempotent_per_publish_job(matrix_db):
    """同一发布重复调不重复登记(钩子幂等)。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 99, 1, "拖延的三个成因")

    assert len(svc.schedule_matrix_interact(matrix_db, 99)) == 1
    assert svc.schedule_matrix_interact(matrix_db, 99) == []
    assert len(_read_jobs(matrix_db)) == 1


def test_schedule_skips_when_publisher_has_no_user_id(matrix_db):
    """发布者没有 user_id → 主页路径无从走起,直接放弃(不猜、不登记)。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id=None)
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 66, 1, "标题在这里")

    assert svc.schedule_matrix_interact(matrix_db, 66) == []
    assert _read_jobs(matrix_db) == []


def test_schedule_never_raises_on_broken_db():
    """登记绝不抛错阻断发布终态:库路径都坏了也只返回空表。"""
    assert svc.schedule_matrix_interact("/nonexistent/dir/nope.db", 1) == []


# ---------------- execute 契约 ----------------


async def test_execute_returns_error_when_payload_incomplete():
    """payload 缺定位信息 → 收敛成 {"error": ...},不抛出、不起浏览器。"""
    result = await svc.execute(2, {"title": "只有标题没有 user_id"})
    assert "error" in result


async def test_execute_converges_locate_failure(monkeypatch):
    """定位类失败(MatrixInteractError)收敛成 {"error": reason},不上抛。"""

    async def fake_load(_account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    def boom(*_args):
        raise browser_mi.MatrixInteractError("note_not_found: 没找到")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_interact_sync", boom)

    result = await svc.execute(2, {"publisher_user_id": "u1", "title": "标题"})
    assert result == {"error": "note_not_found: 没找到"}


# ---------------- 台账纪律:延时排期 + 非幂等 ----------------


@pytest_asyncio.fixture
async def jobs_db(tmp_path, monkeypatch):
    """临时 sqlite 文件库 + monkeypatch 全局 engine/async_session;yield 库文件路径。"""
    from app.core.db import Base

    import app.models  # noqa: F401

    db_path = str(tmp_path / "jobs.db")
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


async def test_list_dispatchable_holds_back_future_not_before(jobs_db):
    """未到点的延时任务不派发(执行方不许领了再 sleep 等,会占死浏览器闸)。"""
    future = (datetime.utcnow() + timedelta(seconds=300)).isoformat(sep=" ")
    past = (datetime.utcnow() - timedelta(seconds=5)).isoformat(sep=" ")
    later = await repo.enqueue(
        "matrix_interact", {"not_before": future}, operator_id=0, account_id=2)
    due = await repo.enqueue(
        "matrix_interact", {"not_before": past}, operator_id=0, account_id=3)
    plain = await repo.enqueue("note_export", {}, operator_id=1, account_id=4)

    ids = [r["id"] for r in await repo.list_dispatchable()]
    assert due in ids and plain in ids
    assert later not in ids


async def test_list_dispatchable_tolerates_broken_not_before(jobs_db):
    """not_before 值坏了按立即可派处理,不让任务永久卡死。"""
    jid = await repo.enqueue(
        "matrix_interact", {"not_before": "不是时间"}, operator_id=0, account_id=2)
    assert jid in [r["id"] for r in await repo.list_dispatchable()]


def test_matrix_interact_is_not_idempotent_kind():
    """matrix_interact 非幂等(重跑会取消已点的赞),不得进 _IDEMPOTENT_KINDS。"""
    assert "matrix_interact" not in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_matrix_interact_execute():
    """account_worker 按 kind 能解析到本服务的 execute(否则子进程会兜底置 error)。"""
    from app import account_worker

    assert account_worker._resolve_execute("matrix_interact") is not None
