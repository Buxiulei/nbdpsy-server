"""按账号的互动日上限:恢复期账号需要爬坡,不该一上来就满配额。

2026-08-13 立:李牧阳_北大心理连败 96 次被隔离一周(软风控形态:登录态在、
发布者主页渲染不出卡片),重登回池后若立刻每天顶满 20 篇,是典型的**行为突变**
——绝对频次没超红线,但模式突变本身就是风控特征。全局 NOTE_INTERACTION_DAILY_LIMIT
改不了单个号,故加 per-account 覆盖值。

这个能力不止为它:任何新号加入矩阵,头几天同样该爬坡。
"""

import pytest

from app.core.config import settings
from app.services import interaction_backfill as svc

from tests.test_interaction_backfill import _add_account, _add_note, _plan  # noqa: F401
from tests.test_interaction_backfill import wired_db  # noqa: F401


@pytest.mark.asyncio
async def test_account_limit_overrides_global(wired_db, monkeypatch):
    """账号设了 interaction_daily_limit 就用它,不用全局值。"""
    monkeypatch.setattr(settings, "NOTE_INTERACTION_DAILY_LIMIT", 20)
    await _add_account(1, interaction_daily_limit=3)
    await _add_account(2)
    for i in range(6):
        await _add_note(2, f"n{i}", permission_code=0)

    plan = await _plan(svc.SCOPE_ALL, actor=1)

    # 一轮最多 5 篇,但该号日上限 3 → 只能拿 3 篇
    assert len(plan["targets"]) == 3


@pytest.mark.asyncio
async def test_null_account_limit_falls_back_to_global(wired_db, monkeypatch):
    """没设账号值就用全局值(既有行为不变)。"""
    monkeypatch.setattr(settings, "NOTE_INTERACTION_DAILY_LIMIT", 20)
    await _add_account(1)          # interaction_daily_limit 为 None
    await _add_account(2)
    for i in range(6):
        await _add_note(2, f"n{i}", permission_code=0)

    plan = await _plan(svc.SCOPE_ALL, actor=1)

    # 全局 20 不设限 → 受每轮 5 篇上限
    assert len(plan["targets"]) == 5


@pytest.mark.asyncio
async def test_zero_or_negative_account_limit_falls_back(wired_db, monkeypatch):
    """账号值 <=0 视为没设(方向反了的兜底比没有兜底危险,与 note_cap 同一条教训)。"""
    monkeypatch.setattr(settings, "NOTE_INTERACTION_DAILY_LIMIT", 20)
    await _add_account(1, interaction_daily_limit=0)
    await _add_account(2)
    for i in range(6):
        await _add_note(2, f"n{i}", permission_code=0)

    plan = await _plan(svc.SCOPE_ALL, actor=1)

    assert len(plan["targets"]) == 5
