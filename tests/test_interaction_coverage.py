"""GET /api/interaction-coverage:矩阵互刷完成率总账(池内每篇公开笔记 × 其余每个号)。

老板 KPI 是"每篇公开笔记都被其余每个号点赞+收藏,完成率 100%",而运营此前靠逐号翻
self-interactions 手拼分母报数,已经两次拿指标视图当台账分母翻车。所以这里钉死的全是
**算错了看不出来**的那几条口径:

1. **分母是格子不是笔记**:一格 = (一篇公开笔记, 一个非作者的池内号)。作者自己不算一格
   (自己给自己点赞不算数,与 plan_round 的 ``n.account_id != aid`` 同口径);
2. **done 与 skipped 都算到位,error 不算**(生产 1215 done vs 1893 skipped,只数 done
   会少算一大半);**池外 actor 的行不算**(退出矩阵的号点过的赞不再顶 KPI);
3. **不进分母的笔记不能消失**:没 note_id / 非公开进 unschedulable,作者号已退出矩阵的
   进顶层 unowned_notes —— 悄悄吞掉就会让运营数不清平台上的卡片去哪了,进而不信整张表;
4. **totals 的完成率按格子重算不是按各号完成率求平均** —— 平均会让 7 篇的新号和 70 篇的
   主力号一样重,报出来的矩阵完成率和实际欠账量对不上。
"""

from datetime import datetime, timedelta

import app.core.db as db_module
from app.models import NoteInteraction, PublishedNote
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]
_NOW = datetime(2026, 8, 13, 12, 0, 0)
_PATH = "/api/interaction-coverage"


async def _seed_note(
    account_id: int,
    note_id: str | None,
    *,
    permission_code: int | None = 0,
    deleted: bool = False,
) -> None:
    """一行发布台账。``note_id=None`` 复刻"台账行在、平台 id 还没同步回来"的形态。"""
    async with db_module.async_session() as s:
        s.add(PublishedNote(
            account_id=account_id, note_id=note_id, title=f"笔记-{note_id}",
            permission_code=permission_code,
            published_at=_NOW - timedelta(days=30),
            deleted_at=_NOW - timedelta(days=1) if deleted else None,
            sync_status="linked",
        ))
        await s.commit()


async def _seed_interaction(
    actor: int, note_id: str, action: str = "like", *, status: str = "done"
) -> None:
    async with db_module.async_session() as s:
        s.add(NoteInteraction(
            actor_account_id=actor, note_id=note_id, action=action,
            status=status, done_at=_NOW - timedelta(days=1),
        ))
        await s.commit()


async def _seed_pool() -> tuple[int, int, int]:
    """三个号的池:甲(笔记作者)/ 乙 / 丙。"""
    return (
        await seed_account("甲", "u-a", _COOKIES),
        await seed_account("乙", "u-b", _COOKIES),
        await seed_account("丙", "u-c", _COOKIES),
    )


def _row(body: dict, account_id: int) -> dict:
    return next(a for a in body["accounts"] if a["account_id"] == account_id)


def _actor(body: dict, account_id: int) -> dict:
    return next(a for a in body["actors"] if a["account_id"] == account_id)


# ---------------- 鉴权 ----------------


async def test_requires_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as c:
        assert (await c.get(_PATH)).status_code == 401


async def test_operator_sees_only_authorized_accounts(tmp_path, monkeypatch):
    """非 admin 只看得到自己被授权的号的账;但 **actors 出勤表仍是全池**。

    收窄 actors 会让某个号的 pending 凭空少掉,而"谁还没去点"正是这张表要回答的问题。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_note(acc_b, "n2")
        op_key = "op-coverage"
        op_id = await make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc_a, None)
            await s.commit()

        body = (await c.get(_PATH, headers=bearer(op_key))).json()

        assert [a["account_id"] for a in body["accounts"]] == [acc_a]
        assert body["totals"]["public_notes"] == 1
        assert len(body["actors"]) == 3


# ---------------- 完成率算术 ----------------


async def test_completion_arithmetic(tmp_path, monkeypatch):
    """一格 = (公开笔记, 非作者的池内号);完成率 = 已覆盖格 / 应做格,保留 1 位小数。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_note(acc_a, "n2")
        await _seed_interaction(acc_b, "n1")
        await _seed_interaction(acc_c, "n1", "collect", status="skipped")

        r = await c.get(_PATH, headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["pool_size"] == 3
        row = _row(body, acc_a)
        assert row["name"] == "甲"
        assert row["public_notes"] == 2
        assert row["combos_total"] == 4, "2 篇 × (池 3 - 作者自己 1) = 4 格"
        assert row["combos_covered"] == 2
        assert row["combos_pending"] == 2
        assert row["completion_pct"] == 50.0
        assert body["generated_at"] and body["note"]


async def test_account_without_public_notes_is_full(tmp_path, monkeypatch):
    """没有公开笔记的号 = 没有应做的活 = 没有欠账(100.0)。

    给 0.0 会让"这个号还没发过公开笔记"在看板上长得和"一格都没做"一模一样,
    而这两件事的运营动作完全相反。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        _acc_a, acc_b, _acc_c = await _seed_pool()

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        row = _row(body, acc_b)
        assert (row["combos_total"], row["combos_covered"]) == (0, 0)
        assert row["completion_pct"] == 100.0


async def test_totals_recomputed_by_combo_not_averaged(tmp_path, monkeypatch):
    """totals 按格子重算:各号完成率求平均会得出 66.7,而真实欠账口径是 25.0。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")          # 甲 1 篇,两个号都做了 → 100%
        await _seed_interaction(acc_b, "n1")
        await _seed_interaction(acc_c, "n1")
        for note_id in ("n2", "n3", "n4"):     # 乙 3 篇,一格没做 → 0%
            await _seed_note(acc_b, note_id)

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert _row(body, acc_a)["completion_pct"] == 100.0
        assert _row(body, acc_b)["completion_pct"] == 0.0
        assert _row(body, acc_c)["completion_pct"] == 100.0  # 没笔记
        assert body["totals"]["combos_total"] == 8
        assert body["totals"]["combos_covered"] == 2
        assert body["totals"]["completion_pct"] == 25.0


# ---------------- 分母口径 ----------------


async def test_nonpublic_and_missing_note_id_stay_out_of_denominator(
    tmp_path, monkeypatch
):
    """非公开与没 note_id 的笔记不进分母,但**必须**在 unschedulable 里报出来。

    permission_code 为 NULL 是"未知"不是公开(与 plan_round 同口径:写 != 1 会把未知
    当公开)—— 未知的那篇归 nonpublic,宁可漏做也不能对一篇可能私密的笔记去点赞。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, _acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")                          # 进分母
        await _seed_note(acc_a, "n2", permission_code=1)       # 私密
        await _seed_note(acc_a, "n3", permission_code=None)    # 未知 = 不算公开
        await _seed_note(acc_a, None)                          # 台账行在,平台 id 没回来

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        row = _row(body, acc_a)
        assert row["public_notes"] == 1
        assert row["combos_total"] == 2
        assert row["unschedulable"] == {"ledger_no_note_id": 1, "nonpublic": 2}
        assert body["totals"]["unschedulable"] == {"ledger_no_note_id": 1, "nonpublic": 2}


async def test_deleted_notes_are_out_of_everything(tmp_path, monkeypatch):
    """已删除的笔记平台上没有了,既不进分母也不进 unschedulable —— 算它就永远到不了 100%。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, _acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1", deleted=True)
        await _seed_note(acc_a, "n2", permission_code=1, deleted=True)

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        row = _row(body, acc_a)
        assert (row["public_notes"], row["combos_total"]) == (0, 0)
        assert row["unschedulable"] == {"ledger_no_note_id": 0, "nonpublic": 0}


async def test_notes_of_retired_account_reported_not_dropped(tmp_path, monkeypatch):
    """作者号已退出矩阵(账号行没了)的笔记:不进任何账号行,但顶层 unowned_notes 报数。

    生产真实存在(号9 账号行被移出系统,10 篇笔记还在库里)。悄悄吞掉的话,运营对着
    平台上还在的卡片数不出来源,整张表就不可信了。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, _acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_note(9999, "n-retired")  # 池里没有 9999 这个号

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert body["unowned_notes"] == 1
        assert body["totals"]["public_notes"] == 1
        assert all(a["account_id"] != 9999 for a in body["accounts"])


# ---------------- 覆盖口径 ----------------


async def test_skipped_counts_but_error_does_not(tmp_path, monkeypatch):
    """skipped = 去点时平台上已经是目标态 → 这一格到位;error = 那一下没成 → 不算。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_interaction(acc_b, "n1", status="skipped")
        await _seed_interaction(acc_c, "n1", status="error")

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert _row(body, acc_a)["combos_covered"] == 1
        assert _actor(body, acc_b)["done_skipped_combos"] == 1
        assert _actor(body, acc_c)["done_skipped_combos"] == 0


async def test_author_is_not_an_expected_actor(tmp_path, monkeypatch):
    """作者自己那一格根本不存在:既不算应做,他自己点的赞也不算已覆盖。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, _acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_interaction(acc_a, "n1")  # 甲给自己点赞

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        row = _row(body, acc_a)
        assert row["combos_total"] == 2 and row["combos_covered"] == 0
        assert row["zero_touch_notes"] == 1
        assert _actor(body, acc_a)["done_skipped_combos"] == 0


async def test_actor_outside_pool_does_not_count(tmp_path, monkeypatch):
    """退出矩阵的号点过的赞不再顶 KPI(KPI 说的是"池内每个账号")。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, _acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_interaction(9999, "n1")

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert _row(body, acc_a)["combos_covered"] == 0
        assert all(a["account_id"] != 9999 for a in body["actors"])


async def test_zero_touch_notes_counted(tmp_path, monkeypatch):
    """零互动笔记 = 分母内一格都没到位的篇(在选篇队列尾部,会被覆盖)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, _acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_note(acc_a, "n2")
        await _seed_note(acc_a, "n3")
        await _seed_interaction(acc_b, "n1")               # n1 有一格
        await _seed_interaction(acc_b, "n2", status="error")  # n2 试过没成,仍是零互动

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert _row(body, acc_a)["zero_touch_notes"] == 2
        assert body["totals"]["zero_touch_notes"] == 2


# ---------------- actor 出勤 ----------------


async def test_actor_attendance_counts_own_notes_out(tmp_path, monkeypatch):
    """谁欠多少活:应做 = 分母里**不是他自己写的**那些篇,已做 = 他覆盖的格数。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc_a, acc_b, acc_c = await _seed_pool()
        await _seed_note(acc_a, "n1")
        await _seed_note(acc_a, "n2")
        await _seed_note(acc_b, "n3")
        await _seed_interaction(acc_b, "n1")
        await _seed_interaction(acc_b, "n2", "collect", status="skipped")

        body = (await c.get(_PATH, headers=bearer(ADMIN_KEY))).json()

        assert _actor(body, acc_b) == {
            "account_id": acc_b, "name": "乙",
            "done_skipped_combos": 2, "pending_combos": 0,
        }
        # 甲名下 2 篇是自己的,只欠乙那 1 篇;丙三篇全欠
        assert _actor(body, acc_a)["pending_combos"] == 1
        assert _actor(body, acc_c)["pending_combos"] == 3
        # 出勤表与账号表同源:各 actor 已做数之和 = 全池已覆盖格数
        assert sum(a["done_skipped_combos"] for a in body["actors"]) == (
            body["totals"]["combos_covered"]
        )
