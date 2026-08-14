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

import json
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
    account_id: int, user_id: str = None, cookie_status: str = "valid",
    interaction_daily_limit: int | None = None,
) -> int:
    """造一个矩阵账号(默认 valid + 有 cookie,即可当互动方)。"""
    async with db_module.async_session() as s:
        s.add(
            XhsAccount(
                id=account_id,
                name=f"号{account_id}",
                user_id=user_id if user_id is not None else f"u-{account_id}",
                cookie_status=cookie_status,
                interaction_daily_limit=interaction_daily_limit,
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
    deleted_at: datetime | None = None,
) -> int:
    """造一行台账笔记;返回行 id。"""
    async with db_module.async_session() as s:
        row = PublishedNote(
            account_id=account_id,
            note_id=note_id,
            title=title,
            deleted_at=deleted_at,
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
    detail: str | None = None,
) -> None:
    async with db_module.async_session() as s:
        s.add(
            NoteInteraction(
                actor_account_id=actor_account_id,
                note_id=note_id,
                action=action,
                status=status,
                detail=detail,
                done_at=done_at or datetime.utcnow(),
            )
        )
        await s.commit()


async def _mark_done(actor: int, note_id: str, when: datetime | None = None) -> None:
    """把某篇对某号标成"两个动作都做完了"(选篇应当跳过它)。"""
    for action in svc.ACTIONS:
        await _add_interaction(actor, note_id, action, "done", when)


async def _mark_all_error(
    actor: int, note_id: str, detail: str, when: datetime | None = None
) -> None:
    """整篇失败的台账形状:定位失败时两个动作都记 error、同一个原因(见 _record_outcome)。"""
    for action in svc.ACTIONS:
        await _add_interaction(actor, note_id, action, "error", when, detail)


# 两种失败原因的真实形状(原文见 app/browser/matrix_interact.py 的 MatrixInteractError)
_PROFILE_DEAD = "profile_not_loaded: 发布者主页未渲染出笔记卡片(https://x/user/profile/u)"
_NOT_FOUND = "note_not_found: 发布者主页未找到 note_id='n' / 标题「t」的笔记卡"


async def _vote_not_found(actor: int, note_id: str) -> None:
    """某号报「这篇在发布者主页上找不到」(笔记熔断数的就是这种票)。

    时刻**刻意取在 error 冷却期之外**:落在冷却期内的话,那篇本来就会被冷却挡掉,
    断言就测不出熔断本身有没有生效(假绿)。
    """
    await _mark_all_error(
        actor,
        note_id,
        _NOT_FOUND,
        datetime.utcnow() - timedelta(hours=svc.ERROR_RETRY_COOLDOWN_HOURS + 1),
    )


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


async def test_deleted_notes_excluded_from_candidates(wired_db):
    """deleted_at 非空的笔记不派单:淘汰真删/平台收走补标后,调度与总账口径必须一致
    (coverage 端点按 deleted_at IS NULL 计分母,不滤则给死笔记白开页)。"""
    from datetime import datetime as _dt
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "alive", permission_code=0)
    await _add_note(1, "dead", permission_code=0, deleted_at=_dt(2026, 8, 13))

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["alive"]


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


# ---------------- 断路器A:actor 熔断(2026-08-13 事故) ----------------
#
# 事故:号12 自 08-08 起 96 连败,全部同一个 profile_not_loaded(登录态在,但打开任何
# 发布者主页都渲染不出笔记卡片)。它**没撞验证墙**,"撞墙即停"那套一次都没触发,风控台账
# 零记录,error 冷却一过就再试 —— 96 次白开的会话全是实打实的风控暴露。


async def test_actor_breaker_blocks_pinned_actor(wired_db, monkeypatch):
    """连败 N 次的号本轮不派活 —— **硬指定它也拦得住**。

    这正是号12 那条生产路径:任务登记时 actor 就定死了,execute 拿着自己的 id 重挑一次。
    只在"挑最闲的号"那条分支上熔断的话,这条路径一次都拦不到。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(2):  # 两篇整篇失败 = 4 行
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, now - timedelta(minutes=i + 1))

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2)
    assert plan["actor_account_id"] is None
    assert plan["targets"] == []
    assert "熔断" in plan["reason"]


async def test_actor_breaker_lets_healthy_actor_take_over(wired_db, monkeypatch):
    """半死号被跳过,健康号顶上 —— 熔断掐的是这个号,不是整条补量链路。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    await _add_account(2)  # 半死号
    await _add_account(3)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(2):  # 号2 今日用量 2
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, now - timedelta(minutes=i + 1))
    # 号3 今日用量 3,比号2 高 —— 不熔断的话"挑最闲的号"会挑中号2,这条断言才有鉴别力
    for i in range(3):
        await _mark_done(3, f"别的篇{i}")

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1))["actor_account_id"] == 3


async def test_actor_breaker_needs_n_consecutive_errors(wired_db, monkeypatch):
    """不到 N 次不熔断:偶发抖动不该让一个号停工(否则一次渲染失败就把号判死)。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 6)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(2):  # 4 行,差两行到 6
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, now - timedelta(minutes=i + 1))

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2))["actor_account_id"] == 2


async def test_actor_breaker_ignores_partial_failures(wired_db, monkeypatch):
    """赞成了藏没成**不算连败**:那是页内交互问题,不是账号半死,不该停这个号的工。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(3):
        await _add_interaction(2, f"半{i}", "like", "done", now - timedelta(minutes=i + 1))
        await _add_interaction(
            2, f"半{i}", "collect", "error", now - timedelta(minutes=i + 1),
            "收藏_button_not_found",
        )

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2))["actor_account_id"] == 2


async def test_actor_breaker_half_open_probes_exactly_one_note(wired_db, monkeypatch):
    """半开探测:最新那条 error 过了冷却期就放行一轮,**那一轮只做一篇**。

    没有半开就是死锁 —— 熔断的号永不被选中,也就永远产不出成功记录来复位。但探测也不能
    按整轮上限(5 篇)放:真让半死的号一次探 5 篇,每个冷却周期照旧白开 5 次页,
    断路器就只剩一半意义了。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_COOLDOWN_H", 12)
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_ROUND_LIMIT", 5)
    await _add_account(1)
    await _add_account(2)
    for i in range(5):  # 有 5 篇可做,不熔断的话会一次派满
        await _add_note(1, f"n{i}")
    stale = datetime.utcnow() - timedelta(hours=13)
    for i in range(2):
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, stale - timedelta(minutes=i))

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2)
    assert plan["actor_account_id"] == 2
    assert len(plan["targets"]) == 1


async def test_healthy_actor_still_gets_a_full_round(wired_db, monkeypatch):
    """探测成功复位后照常做满一轮 —— 半开的"只做一篇"不能漏到健康号头上。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    monkeypatch.setattr(svc.settings, "NOTE_INTERACTION_ROUND_LIMIT", 5)
    await _add_account(1)
    await _add_account(2)
    for i in range(5):
        await _add_note(1, f"n{i}")

    assert len((await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2))["targets"]) == 5


async def test_actor_breaker_resets_after_success(wired_db, monkeypatch):
    """成功即**自然复位**:最近 N 条不再全是 error,不需要任何显式复位逻辑。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(2):
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, now - timedelta(minutes=i + 2))
    # 半开那次探测成功了:最新一条是 done
    await _add_interaction(2, "探测成功的篇", "like", "done", now)

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1, actor=2))["actor_account_id"] == 2


# ---------------- 断路器B:笔记熔断(2026-08-13 事故) ----------------
#
# 事故:三篇 views=0/0/1 的笔记被全部 9 个 actor 报 note_not_found,各积了 16-18 个 error
# 还在被冷却重试。actor 的报告是准确的 —— 那几篇大概率被平台屏蔽,主页根本不展示。


async def test_note_breaker_suppresses_note_reported_by_k_actors(wired_db, monkeypatch):
    """≥K 个不同号报"主页找不到" → 该篇移出候选,并出现在 suppressed_notes 里。

    带出来是给**运营**看的:平台屏蔽不是系统能自己修的,不露出来就没人会去核实。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    await _add_account(1)
    for actor in (2, 3, 4):
        await _add_account(actor)
    await _add_note(1, "屏蔽篇")
    await _add_note(1, "正常篇")
    for actor in (2, 3, 4):
        await _vote_not_found(actor, "屏蔽篇")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["正常篇"]
    assert plan["suppressed_notes"] == ["屏蔽篇"]


async def test_note_breaker_needs_k_distinct_actors(wired_db, monkeypatch):
    """K-1 个号报找不到还不算数:一两个号看不到可能是它自己的会话问题。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    await _add_account(1)
    for actor in (2, 3, 4):
        await _add_account(actor)
    await _add_note(1, "存疑篇")
    for actor in (2, 3):
        await _vote_not_found(actor, "存疑篇")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["存疑篇"]
    assert plan["suppressed_notes"] == []


async def test_note_breaker_only_counts_note_not_found(wired_db, monkeypatch):
    """只认"主页找不到"这一种错:按钮点不动是页内交互问题,不是这篇被平台屏蔽。"""
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    await _add_account(1)
    for actor in (2, 3, 4):
        await _add_account(actor)
    await _add_note(1, "点不动的篇")
    stale = datetime.utcnow() - timedelta(hours=svc.ERROR_RETRY_COOLDOWN_HOURS + 1)
    for actor in (2, 3, 4):
        await _mark_all_error(actor, "点不动的篇", "note_card_no_box: 命中卡片坐标不可得", stale)

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["点不动的篇"]
    assert plan["suppressed_notes"] == []


async def test_note_breaker_ignores_votes_from_invalid_actor(wired_db, monkeypatch):
    """**失信号的票不算数**:号12 那种半死号对每篇都报找不到,票恒为真、不承载信息。

    它现在是 restricted(人工止血置的),历史票据此自动失效 —— 否则每篇笔记都白拿它
    一票,K 变相打了折,实际只需 K-1 个真信号就熔断。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    await _add_account(1)
    await _add_account(2)
    await _add_account(3)
    await _add_account(12, cookie_status="restricted")  # 半死号,已人工止血
    await _add_note(1, "存疑篇")
    for actor in (2, 3, 12):
        await _vote_not_found(actor, "存疑篇")

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["存疑篇"]
    assert plan["suppressed_notes"] == []


async def test_note_breaker_ignores_votes_from_tripped_actor(wired_db, monkeypatch):
    """K-1 个有资格的号 + 1 个**被断路器A 熔断**的号 → 不熔断。

    这也是两个断路器**结算顺序不能调**的原因:A 先跑完,B 才知道谁的票还算数。
    将来任何 actor 半死(还没来得及被人工置 restricted),先被 A 熔断,票随之失效。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    for actor in (2, 3, 4):
        await _add_account(actor)
    await _add_note(1, "存疑篇")
    for actor in (2, 3, 4):
        await _vote_not_found(actor, "存疑篇")
    # 号4 半死:再连败一篇,最近 4 行(这篇 2 行 + 存疑篇那 2 行)全是 error → 被 A 熔断
    await _mark_all_error(4, "dead", _PROFILE_DEAD, datetime.utcnow())

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert plan["actor_account_id"] in (2, 3)
    assert [t["note_id"] for t in plan["targets"]] == ["存疑篇"]
    assert plan["suppressed_notes"] == []


async def test_note_breaker_suppression_survives_note_cooldown_expiry(
    wired_db, monkeypatch
):
    """熔断是**永久**的:冷却期过了也不放它回候选池(平台屏蔽不会自己好)。

    恢复路径是人工的 —— 运营核实笔记恢复可见后,删掉该 note_id 的 error 台账行即可
    重新入池(见 plan_round docstring)。这里顺带把那条路径也验一遍。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    await _add_account(1)
    for actor in (2, 3, 4):
        await _add_account(actor)
    await _add_note(1, "屏蔽篇")
    for actor in (2, 3, 4):
        await _vote_not_found(actor, "屏蔽篇")  # 票本身已在冷却期之外

    assert (await _plan(svc.SCOPE_ACCOUNT, target=1))["targets"] == []

    # 运营核实后手工清票(DELETE ... WHERE note_id=? AND status='error')
    async with db_module.async_session() as s:
        for row in (await s.execute(select(NoteInteraction))).scalars().all():
            await s.delete(row)
        await s.commit()

    plan = await _plan(svc.SCOPE_ACCOUNT, target=1)
    assert [t["note_id"] for t in plan["targets"]] == ["屏蔽篇"]
    assert plan["suppressed_notes"] == []


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


async def test_tripped_actor_never_opens_browser(wired_db, no_browser, monkeypatch):
    """熔断的号**连一次会话都不起** —— 这才是断路器要省下的东西。

    号12 那 96 次连败每一次都真起了 camoufox、真进了主页,失败的只是最后一步;省掉的
    不是几秒 CPU,是 96 次实打实的风控暴露。任务登记时 actor 已定死,所以拦必须拦在
    execute 重挑那一次上。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_ACTOR_BREAKER_N", 4)
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    now = datetime.utcnow()
    for i in range(2):
        await _mark_all_error(2, f"dead{i}", _PROFILE_DEAD, now - timedelta(minutes=i + 1))

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})
    assert result["picked"] == 0
    assert "error" not in result  # 熔断不是失败,是"这轮不该派活"
    assert "熔断" in result["reason"]
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


_FORENSICS = {
    "url": "https://www.xiaohongshu.com/explore/n0?xsec_token=T",
    "title": "边界感是练出来的 - 小红书",
    "body_head": "正文头部",
    "engage_bar": False,
    "wrappers": [],
    "like": {"present": False},
    "collect": {"present": False},
}


async def test_ledger_detail_carries_forensics(wired_db, no_browser):
    """失败动作的 detail 带上失败当场的现场取证,**成功/跳过的 detail 一个字不变**。

    这张表是排查时最先被翻的地方;只写一句「收藏_button_not_found」等于什么都没说 ——
    到底是被踢到验证页、还是互动栏改版、还是按钮被盖住,分不出来就只能靠复现去猜。
    """
    _calls, _sleeps, script = no_browser
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    script["n0"] = {
        "note_url": "u",
        "actions": {
            "like": {"status": "skipped", "reason": "已点赞"},
            "collect": {
                "status": "error",
                "reason": "收藏_button_not_found",
                "forensics": dict(_FORENSICS),
            },
        },
    }

    await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    async with db_module.async_session() as s:
        rows = {
            r.action: r for r in (await s.execute(select(NoteInteraction))).scalars().all()
        }
    # 没取证的行照旧:成功路径的台账形状零变化
    assert rows["like"].detail == "已点赞"
    detail = rows["collect"].detail
    assert detail.startswith("收藏_button_not_found | forensics=")
    assert json.loads(detail.split("forensics=", 1)[1]) == _FORENSICS


async def test_ledger_detail_forensics_is_capped(wired_db, no_browser):
    """取证再大也不能把台账行撑爆:detail 里那段 JSON 有硬上限。"""
    _calls, _sleeps, script = no_browser
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    script["n0"] = {
        "note_url": "u",
        "actions": {
            "like": {"status": "done"},
            "collect": {"status": "error", "reason": "r",
                        "forensics": {"body_head": "长" * 9000}},
        },
    }

    await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    async with db_module.async_session() as s:
        rows = {
            r.action: r for r in (await s.execute(select(NoteInteraction))).scalars().all()
        }
    dumped = rows["collect"].detail.split("forensics=", 1)[1]
    assert len(dumped) == svc._DETAIL_FORENSICS_MAX


async def test_job_result_notes_carry_forensics(wired_db, no_browser):
    """整篇失败(点赞收藏均败)的现场随任务结果落 ``browser_jobs.result``。"""
    _calls, _sleeps, script = no_browser
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    script["n0"] = {
        "note_url": "u",
        "error": "点赞与收藏均失败",
        "forensics": dict(_FORENSICS),
        "actions": {
            "like": {"status": "error", "reason": "点不动", "forensics": dict(_FORENSICS)},
            "collect": {"status": "error", "reason": "点不动", "forensics": dict(_FORENSICS)},
        },
    }

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    assert result["notes"][0]["error"] == "点赞与收藏均失败"
    assert result["notes"][0]["forensics"] == _FORENSICS


async def test_partial_failure_forensics_reaches_job_result(wired_db, no_browser):
    """赞成了、藏没成 → 整篇不算失败,但藏那份现场也要能在任务结果里看到。

    ``notes`` 只摊平动作**状态**不带动作详情,不在这儿捞一把,单动作失败的现场就只落进
    ``note_interactions``,看任务结果的人根本看不到。
    """
    _calls, _sleeps, script = no_browser
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")
    script["n0"] = {
        "note_url": "u",
        "actions": {
            "like": {"status": "done"},
            "collect": {"status": "error", "reason": "点不动",
                        "forensics": dict(_FORENSICS)},
        },
    }

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    assert result["notes"][0]["error"] is None
    assert result["notes"][0]["forensics"] == _FORENSICS


async def test_successful_note_has_no_forensics(wired_db, no_browser):
    """全成功的篇目不带任何取证内容(成功路径零开销一路到任务结果)。"""
    await _add_account(1)
    await _add_account(2)
    await _add_note(1, "n0")

    result = await svc.execute(2, {"scope": svc.SCOPE_ACCOUNT, "target_account_id": 1})

    assert result["notes"][0]["forensics"] is None
    async with db_module.async_session() as s:
        rows = (await s.execute(select(NoteInteraction))).scalars().all()
    assert all(r.detail is None for r in rows)


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


async def _seed_matrix(count: int = 2) -> None:
    """灌 count 个 valid 矩阵号(默认两个:一个被互动、一个去互动)。"""
    for index in range(1, count + 1):
        await seed_account(f"号{index}", f"u-{index}", _COOKIES)
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


async def test_rest_response_carries_suppressed_notes(tmp_path, monkeypatch, api_role):
    """被笔记熔断踢掉的篇要出现在**触发回执**里。

    平台屏蔽不是系统能自己修的,只能靠人去核实;系统这边只会安静地不再调度它们 ——
    回执里不露出来,就没有任何地方会告诉运营"这几篇你得去看看"。
    """
    monkeypatch.setattr(svc.settings, "INTERACTION_NOTE_BREAKER_ACTORS", 3)
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix(4)
        await _add_note(1, "屏蔽篇")
        await _add_note(1, "正常篇")
        for actor in (2, 3, 4):
            await _vote_not_found(actor, "屏蔽篇")

        r = await client.post(
            "/api/interaction-backfills",
            json={"scope": "account", "target_account_id": 1},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        assert r.json()["suppressed_notes"] == ["屏蔽篇"]


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
