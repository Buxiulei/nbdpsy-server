"""受众采集落库单测:采回来的 message 怎么变成库里的行与游标。

这里锁的是**增量能不能收敛**。游标写错的两种死法都很安静:

- 游标不往前推 → 每轮都从头翻 47 页,增量退化成全量,把该省的真号会话全烧回去;
- 游标推过头(比如按"本轮采到的最老一条"写)→ 中间那段永远补不回来,而库里看着挺满,
  没有任何报错提示你缺了两个月。

外加一条:**没有新事件也要刷 ``updated_at``**。不刷的话"这个号最近没人互动"会被调度器
读成"这个号还没采过",于是每轮都挑中它,把会话额度全烧在最冷清的号上。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.browser import audience_collect as ac
from app.models.audience_event import AudienceEvent
from app.models.audience_sync_state import AudienceSyncState
from app.services import audience_sync

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "audience" / "notification_messages.json"
)


def _real_messages() -> dict:
    """真号取证快照抽出的真实 message(likes / connections 各一批)。"""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return {
        ac.CHANNEL_LIKES: {"messages": data["likes"], "stopped_by": "exhausted",
                           "rounds": 3, "pages": 4},
        ac.CHANNEL_CONNECTIONS: {"messages": data["connections"],
                                 "stopped_by": "exhausted", "rounds": 1, "pages": 1},
    }


async def _states(db, account_id: int) -> dict[str, AudienceSyncState]:
    rows = (await db.execute(
        select(AudienceSyncState).where(AudienceSyncState.account_id == account_id)
    )).scalars().all()
    return {r.channel: r for r in rows}


@pytest.mark.asyncio
async def test_persist_writes_events_and_advances_cursor(db):
    """真实事件入库 + 游标推到**本轮最新**那条的时刻。"""
    channels = _real_messages()

    summary = await audience_sync.persist(db, 1, channels, full=True)

    events = (await db.execute(select(AudienceEvent))).scalars().all()
    assert len(events) == summary["inserted"] > 0
    assert {e.account_id for e in events} == {1}

    states = await _states(db, 1)
    assert set(states) == {ac.CHANNEL_LIKES, ac.CHANNEL_CONNECTIONS}
    likes_times = [
        int(m["time"]) for m in channels[ac.CHANNEL_LIKES]["messages"]
    ]
    # 游标 = 本轮见过的**最大** event_time。按最老一条写会让中间那段永远补不回来
    assert states[ac.CHANNEL_LIKES].last_event_time == max(likes_times)


@pytest.mark.asyncio
async def test_replay_is_idempotent_and_cursor_does_not_regress(db):
    """同一批重放:不产生新行,游标也不倒退。"""
    channels = _real_messages()
    first = await audience_sync.persist(db, 1, channels, full=True)
    before = (await _states(db, 1))[ac.CHANNEL_LIKES].last_event_time

    second = await audience_sync.persist(db, 1, channels, full=False)

    assert first["inserted"] > 0 and second["inserted"] == 0
    assert (await _states(db, 1))[ac.CHANNEL_LIKES].last_event_time == before


@pytest.mark.asyncio
async def test_cursor_never_goes_backwards_on_thin_round(db):
    """增量轮只采到几条较老的事件时,游标**不许**被拉回去。"""
    await audience_sync.persist(db, 1, _real_messages(), full=True)
    high = (await _states(db, 1))[ac.CHANNEL_LIKES].last_event_time

    await audience_sync.persist(db, 1, {
        ac.CHANNEL_LIKES: {"messages": [{
            "id": "老事件", "time": 1, "type": "liked/item",
            "user_info": {"userid": "u1"},
            "item_info": {"type": "note_info", "id": "n1", "content": "标题"},
        }], "stopped_by": "reached_known", "rounds": 1, "pages": 1},
    }, full=False)

    assert (await _states(db, 1))[ac.CHANNEL_LIKES].last_event_time == high


@pytest.mark.asyncio
async def test_empty_round_still_refreshes_updated_at(db):
    """一条新事件都没有的轮次也要刷 updated_at,否则调度器每轮都挑中这个冷清号。"""
    stale = datetime.utcnow() - timedelta(hours=9)
    db.add(AudienceSyncState(account_id=1, channel=ac.CHANNEL_LIKES,
                             last_event_time=500, updated_at=stale))
    await db.commit()

    await audience_sync.persist(db, 1, {
        ac.CHANNEL_LIKES: {"messages": [], "stopped_by": "reached_known",
                           "rounds": 1, "pages": 1},
    }, full=False)

    state = (await _states(db, 1))[ac.CHANNEL_LIKES]
    assert state.updated_at > stale
    assert state.last_event_time == 500  # 没新事件,游标原地不动


@pytest.mark.asyncio
async def test_full_round_stamps_last_full_sync_at(db):
    """全量轮盖 last_full_sync_at(回答"这个号的历史补齐过没有");增量轮不动它。"""
    await audience_sync.persist(db, 1, _real_messages(), full=True)
    stamped = (await _states(db, 1))[ac.CHANNEL_LIKES].last_full_sync_at
    assert stamped is not None

    await audience_sync.persist(db, 1, {
        ac.CHANNEL_LIKES: {"messages": [], "stopped_by": "reached_known",
                           "rounds": 1, "pages": 1},
    }, full=False)

    assert (await _states(db, 1))[ac.CHANNEL_LIKES].last_full_sync_at == stamped


@pytest.mark.asyncio
async def test_unparsable_messages_do_not_kill_the_round(db):
    """一条认不出的新型通知不该打死整轮:其余真实事件照常入库。"""
    channels = _real_messages()
    channels[ac.CHANNEL_LIKES]["messages"] = (
        [{"id": "x", "time": 9, "type": "poked/you"}]
        + channels[ac.CHANNEL_LIKES]["messages"]
    )

    summary = await audience_sync.persist(db, 1, channels, full=True)

    assert summary["inserted"] > 0
    assert summary["dropped"] == 1


@pytest.mark.asyncio
async def test_load_targets_returns_cursor_per_channel(db):
    """两条 channel 各自独立的游标;没有行的 channel 给 None(首采走全量)。"""
    db.add(AudienceSyncState(account_id=1, channel=ac.CHANNEL_LIKES,
                             last_event_time=777, updated_at=datetime.utcnow()))
    await db.commit()

    targets = await audience_sync.load_targets(db, 1)

    assert targets == {ac.CHANNEL_LIKES: 777, ac.CHANNEL_CONNECTIONS: None}


def test_job_kind_is_wired_and_idempotent():
    """接线自查:account_worker 认得这个 kind,且它进了幂等集合。

    ``audience_sync`` 是**纯只读**(通知页只导航+滚动)+ ``ON CONFLICT DO NOTHING`` 入库 +
    游标只进不退,所以僵死后自动重跑是安全的:最坏结果是把同一段重采一遍。
    漏进幂等集合的话,一次子进程僵死就会把这个号的采集永久卡在 error 上。
    """
    from app import account_worker
    from app.services import browser_jobs_repo

    assert account_worker._resolve_execute(audience_sync.JOB_KIND) is not None
    assert audience_sync.JOB_KIND in browser_jobs_repo._IDEMPOTENT_KINDS


@pytest.mark.asyncio
async def test_connections_channel_parsed_with_its_own_shape(db):
    """connections 走另一套字段(``user`` / ``images``),落库后 event_type 是 follow。"""
    await audience_sync.persist(db, 7, {
        ac.CHANNEL_CONNECTIONS: _real_messages()[ac.CHANNEL_CONNECTIONS],
    }, full=True)

    events = (await db.execute(select(AudienceEvent))).scalars().all()
    assert events and {e.event_type for e in events} == {"follow"}
    assert all(e.target_note_id is None for e in events)
    assert all(e.actor_nickname for e in events)
