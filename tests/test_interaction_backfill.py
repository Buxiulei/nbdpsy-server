"""历史笔记互动补量单测:选篇 + 四层节流 + 撞墙即停 + 记账(不起真浏览器)。

设计 docs/design/2026-08-02-interaction-backfill-design.md。锁住的是那几条**风控约束**,
不是功能便利:

- **日上限 / 单轮上限**:每号每天 ≤ N 篇、每轮 ≤ M 篇,``limit`` 只能往小压不能放大;
- **已互动的跳过且不开浏览器**:台账有完整记录的篇根本不进候选,SyncClient 一次都不建;
- **只互动公开笔记**:``permission_code == 0`` 才做,``1``(私密)与 ``None``(未知)
  **一律不碰** —— 不确定就不做;
- **撞墙立刻中止**:剩余篇目一篇不碰,已完成的部分照常记账不回滚,actor 置 restricted
  并落 risk_events;
- **间隔抖动落在 [60, 240] 秒**:绝不连续快跑;
- 三种 scope(account / all / newcomer)的选篇口径,以及"新发现 > 最近发布 > 更早"的优先级;
- ``interaction_backfill`` **非幂等**,不得进 ``_IDEMPOTENT_KINDS``。

patch 纪律:打在**被测模块的命名空间**(``svc.SyncClient`` / ``svc.interact_with_note`` /
``svc.load_account_cookies``),不是源模块。
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.note_interaction import NoteInteraction
from app.models.published_note import PublishedNote
from app.models.risk_event import RiskEvent
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo as repo
from app.services import interaction_backfill as svc
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


# ---------------- 夹具 ----------------


@pytest_asyncio.fixture
async def wired_db(tmp_path, monkeypatch):
    """临时文件库 + monkeypatch 全局 engine/async_session;yield 库路径。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    db_path = str(tmp_path / "interaction.db")
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


async def _add_account(
    account_id: int, user_id: str = None, cookie_status: str = "valid"
) -> int:
    """造一个矩阵账号(默认 valid + 有 cookie,即可当互动方)。"""
    async with db_module.async_session() as s:
        s.add(
            XhsAccount(
                id=account_id,
                name=f"号{account_id}",
                user_id=user_id if user_id is not None else f"u-{account_id}",
                cookie_status=cookie_status,
                login_cookies="enc",
            )
        )
        await s.commit()
    return account_id


async def _add_note(
    account_id: int,
    note_id: str,
    title: str = "标题",
    permission_code: int | None = 0,
    first_seen_at: datetime | None = None,
    platform_published_at: datetime | None = None,
) -> int:
    """造一行台账笔记;返回行 id。"""
    async with db_module.async_session() as s:
        row = PublishedNote(
            account_id=account_id,
            note_id=note_id,
            title=title,
            published_at=platform_published_at or datetime(2026, 7, 1),
            platform_published_at=platform_published_at or datetime(2026, 7, 1),
            first_seen_at=first_seen_at or datetime(2026, 7, 1),
            permission_code=permission_code,
            sync_status="orphan",
        )
        s.add(row)
        await s.commit()
        return row.id


async def _add_interaction(
    actor_account_id: int,
    note_id: str,
    action: str = "like",
    status: str = "done",
    done_at: datetime | None = None,
) -> None:
    async with db_module.async_session() as s:
        s.add(
            NoteInteraction(
                actor_account_id=actor_account_id,
                note_id=note_id,
                action=action,
                status=status,
                done_at=done_at or datetime.utcnow(),
            )
        )
        await s.commit()


async def _mark_done(actor: int, note_id: str, when: datetime | None = None) -> None:
    """把某篇对某号标成"两个动作都做完了"(选篇应当跳过它)。"""
    for action in svc.ACTIONS:
        await _add_interaction(actor, note_id, action, "done", when)


async def _plan(scope, target=None, actor=None, limit=None, now=None) -> dict:
    async with db_module.async_session() as s:
        return await svc.plan_round(s, scope, target, actor, limit, now)


class _FakePage:
    """假 page:只提供 url 与 evaluate(撞墙判定与取证要用)。"""

    def __init__(self, url="https://www.xiaohongshu.com/explore/n1"):
        self.url = url

    def evaluate(self, script, *args):
        return "为保护账号安全,请使用已登录该账号的「小红书APP」扫码验证身份"


class _FakeClient:
    """假 SyncClient:记录建了几次(= 起了几次会话),page 可被测试改 url 模拟撞墙。"""

    instances: list["_FakeClient"] = []

    def __init__(self, account_id, cookies, block_images=False):
        self.account_id = account_id
        self.page = _FakePage()
        self.stopped = False
        _FakeClient.instances.append(self)

    def start(self):
        return {"success": True}

    def stop(self):
        self.stopped = True


@pytest.fixture
def no_browser(monkeypatch):
    """禁掉真浏览器与真等待;返回 ``(calls, sleeps, script)``。

    - ``calls``:逐篇被互动的 note_id(断言选篇与中止行为);
    - ``sleeps``:篇间抖动的秒数(断言第三层闸);
    - ``script``:``{note_id: 结果或 "wall"}``,注入某篇的返回。
    """
    _FakeClient.instances.clear()
    calls: list[str] = []
    sleeps: list[float] = []
    script: dict[str, object] = {}

    def fake_interact(page, account_id, publisher_user_id, title, note_id=None):
        calls.append(note_id)
        planned = script.get(note_id)
        if planned == "wall":
            # 真实撞墙的形态:互动过程中被重定向到验证页
            page.url = "https://www.xiaohongshu.com/website-login/captcha?verifyType=124"
            raise svc.MatrixInteractError("profile_not_loaded: 发布者主页未渲染出笔记卡片")
        if isinstance(planned, dict):
            return planned
        return {
            "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "actions": {"like": {"status": "done"}, "collect": {"status": "done"}},
        }

    monkeypatch.setattr(svc, "SyncClient", _FakeClient)
    monkeypatch.setattr(svc, "interact_with_note", fake_interact)
    monkeypatch.setattr(svc.time, "sleep", lambda s: sleeps.append(s))

    async def fake_cookies(account_id):
        return _COOKIES

    monkeypatch.setattr(svc, "load_account_cookies", fake_cookies)
    return calls, sleeps, script


# ---------------- 选篇:公开性 / scope / 优先级 ----------------


async def test_only_public_notes_are_picked(wired_db):
    """只互动 permission_code=0;私密(1)与**未知(None)一律不碰**。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "pub", permission_code=0)
    await _add_note(1, "private", permission_code=1)
    await _add_note(1, "unknown", permission_code=None)

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["pub"]


async def test_note_without_id_or_publisher_user_id_excluded(wired_db):
    """没有 note_id 的行(pending_id)进不去详情;作者没有 user_id 就无从进主页。"""
    await _add_account(1)
    await _add_account(2, user_id="")  # 号2 没有 user_id,它的笔记定位不了
    await _add_note(2, "n-no-user")
    async with db_module.async_session() as s:
        s.add(
            PublishedNote(
                account_id=1, note_id=None, title="待补 id",
                published_at=datetime(2026, 7, 1), permission_code=0,
            )
        )
        await s.commit()

    plan = await _plan(svc.SCOPE_ALL)
    assert plan["targets"] == []


async def test_scope_account_targets_only_that_account(wired_db):
    """scope=account:只互动被指定那个号的笔记,互动方是矩阵内其余号。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "a1")
    await _add_note(2, "b1")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["a1"]
    assert plan["actor_account_id"] == 2  # 不会挑号1 给自己点赞


async def test_scope_all_covers_every_account_but_not_self(wired_db):
    """scope=all:所有号的笔记都算,但**不给自己点赞**。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "a1")
    await _add_note(2, "b1")

    plan = await _plan(svc.SCOPE_ALL, actor=1)
    assert plan["actor_account_id"] == 1
    assert [t["note_id"] for t in plan["targets"]] == ["b1"]


async def test_scope_newcomer_fixes_actor_and_covers_others(wired_db):
    """scope=newcomer:互动方固定是新号,被互动的是其余所有号的笔记。"""
    await _add_account(1)
    await _add_account(2)
    await _add_account(3)
    await _add_note(1, "a1")
    await _add_note(2, "b1")
    await _add_note(3, "c1")

    plan = await _plan(svc.SCOPE_NEWCOMER, actor=3)
    assert plan["actor_account_id"] == 3
    assert sorted(t["note_id"] for t in plan["targets"]) == ["a1", "b1"]


async def test_scope_requires_matching_ids(wired_db):
    """scope 与必填 id 不匹配时给 reason,不瞎猜一个范围。"""
    await _add_account(1)
    assert (await _plan(svc.SCOPE_ACCOUNT))["reason"]
    assert (await _plan(svc.SCOPE_NEWCOMER))["reason"]


async def test_priority_new_discovery_then_recent(wired_db):
    """优先级:新发现的笔记 > 最近发布的 > 更早的。"""
    await _add_account(1)
    await _add_account(2)
    old = datetime(2026, 1, 1)
    await _add_note(1, "老帖", first_seen_at=old, platform_published_at=datetime(2025, 1, 1))
    await _add_note(1, "老帖但新发的", first_seen_at=old,
                    platform_published_at=datetime(2026, 6, 1))
    await _add_note(1, "刚发现的", first_seen_at=datetime(2026, 8, 2),
                    platform_published_at=datetime(2024, 1, 1))

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["刚发现的", "老帖但新发的", "老帖"]


async def test_actor_must_be_valid_with_cookies(wired_db):
    """互动方必须 cookie_status=valid 且有 cookie(与矩阵互动同口径)。"""
    await _add_account(1, cookie_status="unknown")
    await _add_account(2, cookie_status="restricted")
    await _add_note(1, "a1")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert plan["actor_account_id"] is None
    assert "互动账号" in plan["reason"]


# ---------------- 节流:日上限 / 单轮上限 / 跳过已互动 ----------------


async def test_round_limit_caps_targets(wired_db, monkeypatch):
    """单轮上限:一次最多 M 篇,``limit`` **只能往小压,不能放大**。"""
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_ROUND_LIMIT", 5)
    await _add_account(1)
    await _add_account(2)
    for i in range(10):
        await _add_note(1, f"n{i}")

    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1))["targets"]) == 5
    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1, limit=2))["targets"]) == 2
    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1, limit=100))["targets"]) == 5


async def test_daily_limit_blocks_actor(wired_db, monkeypatch):
    """日上限:今天开过页的篇数到量后,这个号今天不再派活。"""
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_DAILY_LIMIT", 3)
    await _add_account(1)
    await _add_account(2)
    for i in range(6):
        await _add_note(1, f"n{i}")
    # 号2 今天已经开过 3 篇的页(失败的也算 —— 闸闸的是页面访问,不是成功数)
    for i in range(3):
        await _add_interaction(2, f"done{i}", "like", "error")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert plan["actor_account_id"] is None
    assert "今日已达互动上限" in plan["reason"]


async def test_daily_remaining_caps_this_round(wired_db, monkeypatch):
    """当日剩余配额比单轮上限小时,按剩余的来(不许借明天的额度)。"""
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_DAILY_LIMIT", 4)
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_ROUND_LIMIT", 5)
    await _add_account(1)
    await _add_account(2)
    for i in range(10):
        await _add_note(1, f"n{i}")
    for i in range(3):
        await _mark_done(2, f"other{i}")

    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1))["targets"]) == 1


async def test_yesterday_usage_does_not_count(wired_db, monkeypatch):
    """日上限是**当天**的账:昨天做的不占今天的额度。"""
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_DAILY_LIMIT", 2)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n1")
    yesterday = datetime.utcnow() - timedelta(days=1)
    for i in range(5):
        await _mark_done(2, f"old{i}", yesterday)

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1))["actor_account_id"] == 2


async def test_completed_notes_are_skipped(wired_db):
    """两个动作都到位的篇不再进候选(**这条就是"不白开浏览器"的落点**)。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "done")
    await _add_note(1, "todo")
    await _mark_done(2, "done")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["todo"]


async def test_half_done_note_is_retried(wired_db):
    """只点了赞没收藏的篇仍要做完(冷却期外)。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "half")
    await _add_interaction(
        2, "half", "like", "done",
        datetime.utcnow() - timedelta(hours=svc.ERROR_RETRY_COOLDOWN_HOURS + 1),
    )

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["half"]


async def test_recent_failure_is_cooling_then_retriable(wired_db):
    """刚失败过的篇在冷却期内不再开页;冷却过了可以再试一次。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "flaky")
    await _add_interaction(2, "flaky", "like", "error", datetime.utcnow())
    assert (await _plan(svc.SCOPE_ACCOUNT, target=1))["targets"] == []

    async with db_module.async_session() as s:
        row = (await s.execute(select(NoteInteraction))).scalars().one()
        row.done_at = datetime.utcnow() - timedelta(
            hours=svc.ERROR_RETRY_COOLDOWN_HOURS + 1
        )
        await s.commit()
    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1))["targets"]) == 1


async def test_actor_with_least_usage_today_is_picked(wired_db):
    """不指定互动方时挑今天用得最少的那个号(号间公平,把负载摊开)。"""
    await _add_account(1)
    await _add_account(2)
    await _add_account(3)
    await _add_note(1, "n1")
    await _mark_done(2, "别的篇")

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1))["actor_account_id"] == 3


# ---------------- 执行:不开浏览器 / 抖动 / 撞墙 / 记账 ----------------


async def test_nothing_to_do_never_opens_browser(wired_db, no_browser):
    """没得可做时**一次会话都不起**,且这不是失败(不带 error 键)。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "done")
    await _mark_done(2, "done")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert result["picked"] == 0
    assert "error" not in result
    assert result["reason"]
    assert _FakeClient.instances == []


async def test_round_runs_in_one_session_with_jitter(wired_db, no_browser, monkeypatch):
    """一轮 = **一次会话**做多篇;篇间抖动落在 [60, 240] 秒。"""
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_ROUND_LIMIT", 3)
    calls, sleeps, _ = no_browser
    await _add_account(1)
    await _add_account(2)
    for i in range(3):
        await _add_note(1, f"n{i}")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    assert len(_FakeClient.instances) == 1  # 3 篇共用一个 camoufox,不是一篇起一次
    assert _FakeClient.instances[0].stopped is True
    assert len(calls) == 3
    assert result["liked"] == 3 and result["collected"] == 3
    assert len(sleeps) == 2  # 篇间才等,最后一篇之后不等
    assert all(svc.MIN_GAP_SECONDS <= s <= svc.MAX_GAP_SECONDS for s in sleeps)


async def test_budget_exhausted_leaves_rest_for_next_round(
    wired_db, no_browser, monkeypatch
):
    """时间预算用尽就收工,剩下的留给下一轮(别撞上子进程硬超时被强杀)。"""
    monkeypatch.setattr(svc, "ROUND_BUDGET_SECONDS", 0)
    calls, _, _ = no_browser
    await _add_account(1)
    await _add_account(2)
    for i in range(3):
        await _add_note(1, f"n{i}")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert len(calls) == 1
    assert result["picked"] == 3 and result["handled"] == 1


async def test_wall_aborts_round_and_keeps_finished_part(wired_db, no_browser):
    """撞墙:立刻中止不碰剩余篇,已完成的照常记账,号置 restricted 并落 risk_events。"""
    calls, _, script = no_browser
    await _add_account(1)
    await _add_account(2)
    for i in range(3):
        await _add_note(1, f"n{i}", first_seen_at=datetime(2026, 7, 3 - i))
    script["n1"] = "wall"  # 第二篇撞墙

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    assert calls == ["n0", "n1"]  # 第三篇一下都没碰
    assert "撞风控墙" in result["error"]
    async with db_module.async_session() as s:
        rows = (await s.execute(select(NoteInteraction))).scalars().all()
        account = await s.get(XhsAccount, 2)
        events = (await s.execute(select(RiskEvent))).scalars().all()
    # 已完成的那篇照常记账;撞墙那篇没被真正处理,不记账(免得白白进冷却期)
    assert {r.note_id for r in rows} == {"n0"}
    assert {r.action for r in rows} == set(svc.ACTIONS)
    assert account.cookie_status == "restricted"
    assert len(events) == 1 and events[0].wall_type == "scan_qr"


async def test_ledger_records_each_action_status(wired_db, no_browser):
    """逐动作记账:done / skipped(平台已是目标态)/ error 原样落库,原因记在 detail。"""
    _calls, _sleeps, script = no_browser
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    script["n0"] = {
        "note_url": "u",
        "actions": {
            "like": {"status": "skipped", "reason": "已点赞"},
            "collect": {"status": "error", "reason": "收藏_button_not_found"},
        },
    }

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert result["liked"] == 1 and result["collected"] == 0

    async with db_module.async_session() as s:
        rows = {
            r.action: r for r in (await s.execute(select(NoteInteraction))).scalars().all()
        }
    assert rows["like"].status == "skipped" and rows["like"].detail == "已点赞"
    assert rows["collect"].status == "error"


async def test_locate_failure_records_error_rows(wired_db, no_browser, monkeypatch):
    """定位失败(页确实开过了)两个动作都记 error,让它进冷却而不是每轮重开。"""
    def boom(page, account_id, publisher_user_id, title, note_id=None):
        raise svc.MatrixInteractError("note_not_found: 主页没有这篇")

    monkeypatch.setattr(svc, "interact_with_note", boom)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "gone")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert result["failed"] == 1
    async with db_module.async_session() as s:
        rows = (await s.execute(select(NoteInteraction))).scalars().all()
    assert {r.status for r in rows} == {"error"}
    assert len(rows) == 2


async def test_repeat_round_updates_same_ledger_row(wired_db, no_browser):
    """同号同篇同动作只有一行:重跑是更新那行,不叠加历史。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    await _add_interaction(
        2, "n0", "like", "error",
        datetime.utcnow() - timedelta(hours=svc.ERROR_RETRY_COOLDOWN_HOURS + 1),
    )

    await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    async with db_module.async_session() as s:
        rows = (await s.execute(select(NoteInteraction))).scalars().all()
    assert len(rows) == 2
    assert {r.status for r in rows} == {"done"}


async def test_execute_without_cookies_is_error(wired_db, no_browser, monkeypatch):
    """账号没 cookie 直接给 error,不去起浏览器。"""
    async def no_cookies(account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", no_cookies)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert "cookie" in result["error"]
    assert _FakeClient.instances == []


async def test_browser_start_failure_records_nothing(wired_db, no_browser, monkeypatch):
    """浏览器起不来:落 error 终态,但**一篇都不记账**(没开过页,不该吃掉配额)。"""
    class DeadClient(_FakeClient):
        def start(self):
            return {"success": False, "error": "camoufox 起不来"}

    monkeypatch.setattr(svc, "SyncClient", DeadClient)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert "browser_start_failed" in result["error"]
    async with db_module.async_session() as s:
        assert (await s.execute(select(NoteInteraction))).scalars().all() == []


# ---------------- 自动路径(台账同步发现手工新增笔记) ----------------


async def test_schedule_after_sync_enqueues_one_job_per_matrix_actor(wired_db):
    """同步发现新笔记 → 矩阵内其余号各一条任务,带 not_before 散开,不给自己派。"""
    await _add_account(1)
    await _add_account(2)
    await _add_account(3)
    await _add_note(1, "新发现")

    job_ids = await svc.schedule_after_sync(1)
    assert len(job_ids) == 2
    async with db_module.async_session() as s:
        rows = (await s.execute(select(BrowserJob))).scalars().all()
    assert {r.account_id for r in rows} == {2, 3}
    assert all(r.kind == svc.JOB_KIND for r in rows)
    assert all("not_before" in r.payload for r in rows)


async def test_schedule_after_sync_skips_in_flight_and_empty(wired_db):
    """已有在途任务的号不重复登记;没得可补的号不登记空转任务。"""
    await _add_account(1)
    await _add_account(2)
    await _add_account(3)
    await _add_note(1, "n0")
    await _mark_done(3, "n0")  # 号3 已经做完了这唯一一篇
    await repo.enqueue(svc.JOB_KIND, {}, 0, account_id=2)  # 号2 有在途任务

    assert await svc.schedule_after_sync(1) == []


async def test_schedule_after_sync_never_raises(wired_db, monkeypatch):
    """登记炸了只告警:它是台账同步的事后副作用,不能把同步结果拖成 error。"""
    async def boom(*args, **kwargs):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(svc, "plan_round", boom)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")

    assert await svc.schedule_after_sync(1) == []


# ---------------- 幂等性纪律 ----------------


def test_interaction_backfill_is_not_idempotent_kind():
    """僵死重跑会重复开页、吃掉当日配额、放大风控暴露,绝不能进 _IDEMPOTENT_KINDS。"""
    assert svc.JOB_KIND not in repo._IDEMPOTENT_KINDS


# ---------------- REST ----------------


@pytest.fixture
def api_role(monkeypatch):
    """置 NBDPSY_ROLE=api:REST 只登记台账,不在本进程派执行(不会起浏览器)。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _seed_matrix() -> None:
    await seed_account("号一", "u-1", _COOKIES)
    await seed_account("号二", "u-2", _COOKIES)
    async with db_module.async_session() as s:
        for account in (await s.execute(select(XhsAccount))).scalars().all():
            account.cookie_status = "valid"
        await s.commit()


async def test_rest_start_backfill_queues_job(tmp_path, monkeypatch, api_role):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        await _add_note(1, "n0")
        r = await client.post(
            "/api/interaction-backfills",
            json={"scope": "account", "target_account_id": 1},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["actor_account_id"] == 2

        poll = await client.get(
            f"/api/interaction-backfills/{body['job_id']}", headers=bearer(ADMIN_KEY)
        )
        assert poll.status_code == 200 and poll.json()["status"] == "queued"


async def test_rest_skips_when_nothing_to_do(tmp_path, monkeypatch, api_role):
    """没得可做时不建任务:job_id=null + status=skipped + reason。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        r = await client.post(
            "/api/interaction-backfills",
            json={"scope": "all"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job_id"] is None and body["status"] == "skipped" and body["reason"]


async def test_rest_requires_admin(tmp_path, monkeypatch, api_role):
    """补量消耗的是全矩阵的风控预算,不是人人可点的按钮。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        op_key = "op-key-backfill"
        await make_operator(op_key)
        r = await client.post(
            "/api/interaction-backfills",
            json={"scope": "all"},
            headers=bearer(op_key),
        )
        assert r.status_code == 403


async def test_rest_rejects_scope_id_mismatch(tmp_path, monkeypatch, api_role):
    """scope 与必填 id 不匹配是入参错误,入口就 422,不排队后才失败。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        for body in (
            {"scope": "account"},
            {"scope": "newcomer"},
            {"scope": "matrix"},
        ):
            r = await client.post(
                "/api/interaction-backfills", json=body, headers=bearer(ADMIN_KEY)
            )
            assert r.status_code == 422, (body, r.text)


async def test_rest_poll_404_on_wrong_kind(tmp_path, monkeypatch, api_role):
    """拿别的 kind 的 id 来查是 404,不返回一条别的任务的状态。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        other = await repo.enqueue("note_export", {}, 0, account_id=1)
        r = await client.get(
            f"/api/interaction-backfills/{other}", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 404
