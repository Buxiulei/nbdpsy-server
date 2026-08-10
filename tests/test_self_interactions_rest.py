"""GET /api/accounts/{id}/self-interactions:按笔记聚合**我们自己矩阵制造的**互动。

内容线拿平台指标做分析前要先把自家互动减掉,否则一篇笔记 6 个赞里 5 个是矩阵号刷的,
读出来的"这个选题反响不错"完全是自己跟自己说话。本端点就是那个减数。

三条口径在这里钉死,每条都对应一次会算错数的坑:

1. **done 与 skipped 都算**。``skipped`` 的语义是"去点的时候平台上已经是目标态"——
   对这个 actor 来说赞**就在那篇笔记上**,只是不是这一次点的。本仓两处既有代码
   (``interaction_backfill`` / ``matrix_interact`` 的 ``_COMPLETE_STATUSES``)早就是这个
   口径:"done 与 skipped 都算到位,两者平台状态相同"。只数 done 会漏掉一大半
   (生产实测 like 401 done vs 834 skipped),减出来的数还是虚高的;
2. **评论的 note_id 靠发布任务回填**。``note_comment_task`` 的 payload 里 note_id 是发布
   当场写的,那时平台 id 还没回来 —— 生产 123 条 done 里 **123 条全是空串**,不走
   ``source_publish_job_id`` → ``publish_jobs.note_id`` 这条回填,评论数恒为 0;
3. **回填不了的评论要报出来**。发布任务自己也没拿到 note_id 时这条评论无处可挂
   (生产 13/123),不能悄悄丢——那是 11% 的静默少算。挂在 coverage.unresolved_comments。
"""

import json
from datetime import datetime, timedelta

import app.core.db as db_module
from app.models import BrowserJob, NoteInteraction, PublishedNote, PublishJob
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]
_NOW = datetime(2026, 8, 10, 12, 0, 0)


async def _seed_note(account_id: int, note_id: str, title: str) -> None:
    async with db_module.async_session() as s:
        s.add(PublishedNote(
            account_id=account_id, note_id=note_id, title=title,
            published_at=_NOW - timedelta(days=30), sync_status="linked",
        ))
        await s.commit()


async def _seed_interaction(
    actor: int, note_id: str, action: str, *, status="done", days_ago=1
) -> None:
    async with db_module.async_session() as s:
        s.add(NoteInteraction(
            actor_account_id=actor, note_id=note_id, action=action,
            status=status, done_at=_NOW - timedelta(days=days_ago),
        ))
        await s.commit()


async def _seed_comment_job(
    actor: int, *, note_id="", publish_job_id=None, status="done",
    commented=True, days_ago=1, kind="note_comment_task",
) -> str:
    """一条评论任务台账行。``note_id=""`` 复刻生产真实形态(发布当场没拿到平台 id)。"""
    import uuid

    job_id = uuid.uuid4().hex
    at = _NOW - timedelta(days=days_ago)
    async with db_module.async_session() as s:
        s.add(BrowserJob(
            id=job_id, kind=kind, account_id=actor, operator_id=0,
            payload=json.dumps(
                {"note_id": note_id, "source_publish_job_id": publish_job_id,
                 "text": "一条评论"},
                ensure_ascii=False,
            ),
            status=status,
            result=json.dumps({"commented": commented}, ensure_ascii=False),
            created_at=at, updated_at=at,
        ))
        await s.commit()
    return job_id


async def _seed_publish_job(account_id: int, note_id: str | None) -> int:
    async with db_module.async_session() as s:
        job = PublishJob(
            account_id=account_id, title="发布过的一篇", content="正文",
            images_json="[]", topics_json="[]", status="published", note_id=note_id,
        )
        s.add(job)
        await s.commit()
        return job.id


async def _grant(op_id: int, *account_ids: int) -> None:
    async with db_module.async_session() as s:
        for acc in account_ids:
            await operator_service.grant_access(s, op_id, acc, op_id)


def _note_row(payload: dict, note_id: str) -> dict:
    return next(n for n in payload["notes"] if n["note_id"] == note_id)


async def test_aggregates_likes_collects_and_actors_per_note(tmp_path, monkeypatch):
    """一篇笔记聚出赞/藏各几个、都是谁干的、第一次与最后一次是什么时候。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        await _seed_note(owner, "nid-1", "一篇笔记")
        await _seed_interaction(101, "nid-1", "like", days_ago=5)
        await _seed_interaction(102, "nid-1", "like", days_ago=1)
        await _seed_interaction(101, "nid-1", "collect", days_ago=5)

        r = await c.get(f"/api/accounts/{owner}/self-interactions",
                        headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        note = _note_row(r.json(), "nid-1")
        assert note["title"] == "一篇笔记"
        assert (note["self_likes"], note["self_collects"], note["self_comments"]) == (2, 1, 0)
        assert note["actor_account_ids"] == [101, 102]
        assert note["first_at"] < note["last_at"]


async def test_skipped_counts_as_present_but_error_does_not(tmp_path, monkeypatch):
    """``skipped`` = 平台上这个号的赞本来就在 → 要算;``error`` = 那一下没成 → 不算。

    只数 done 会把矩阵刷的量少算一大半(生产 like 401 done vs 834 skipped),
    减完剩下的"自然互动"仍然虚高 —— 而这个端点存在的全部意义就是把那部分减干净。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        await _seed_note(owner, "nid-1", "一篇笔记")
        await _seed_interaction(101, "nid-1", "like", status="done")
        await _seed_interaction(102, "nid-1", "like", status="skipped")
        await _seed_interaction(103, "nid-1", "like", status="error")

        r = await c.get(f"/api/accounts/{owner}/self-interactions",
                        headers=bearer(ADMIN_KEY))
        note = _note_row(r.json(), "nid-1")
        assert note["self_likes"] == 2
        assert note["actor_account_ids"] == [101, 102], "error 的 actor 混进来了"


async def test_comment_note_id_backfilled_via_source_publish_job(tmp_path, monkeypatch):
    """评论的 note_id **空串**时经 source_publish_job_id → publish_jobs.note_id 补齐。

    这不是边角情况:生产 123 条 done 的 note_comment_task **全部**是空串(评论任务在发布
    当场登记,那时平台 id 还没回来)。不走这条回填,self_comments 恒为 0。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        await _seed_note(owner, "nid-1", "一篇笔记")
        pj = await _seed_publish_job(owner, "nid-1")
        await _seed_comment_job(201, note_id="", publish_job_id=pj)
        # payload 里直接带了 note_id 的(手工登记场景)照样认
        await _seed_comment_job(202, note_id="nid-1")

        r = await c.get(f"/api/accounts/{owner}/self-interactions",
                        headers=bearer(ADMIN_KEY))
        note = _note_row(r.json(), "nid-1")
        assert note["self_comments"] == 2
        assert note["actor_account_ids"] == [201, 202]


async def test_unfinished_or_uncommented_jobs_do_not_count(tmp_path, monkeypatch):
    """没跑完 / 跑完了但没评上(commented=false)的任务不算 —— 平台上没有这条评论。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        await _seed_note(owner, "nid-1", "一篇笔记")
        await _seed_comment_job(201, note_id="nid-1", status="running")
        await _seed_comment_job(202, note_id="nid-1", status="error", commented=False)
        await _seed_comment_job(203, note_id="nid-1", commented=False)

        r = await c.get(f"/api/accounts/{owner}/self-interactions",
                        headers=bearer(ADMIN_KEY))
        assert r.json()["notes"] == []


async def test_unresolvable_comments_are_reported_not_dropped(tmp_path, monkeypatch):
    """发布任务自己也没 note_id → 这条评论无处可挂,进 coverage 而不是被悄悄丢掉。

    生产 13/123 条如此。静默丢等于评论数少算 11%,而调用方拿这个数去减平台指标,
    少算的部分会被当成"自然评论"。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        await _seed_note(owner, "nid-1", "一篇笔记")
        orphan_pj = await _seed_publish_job(owner, None)  # 发布任务也没拿到 note_id
        await _seed_comment_job(201, note_id="", publish_job_id=orphan_pj)
        await _seed_comment_job(202, note_id="", publish_job_id=None)

        r = await c.get(f"/api/accounts/{owner}/self-interactions",
                        headers=bearer(ADMIN_KEY))
        data = r.json()
        assert data["notes"] == []
        assert data["coverage"]["unresolved_comments"] == 2


async def test_since_filters_and_other_accounts_notes_excluded(tmp_path, monkeypatch):
    """since 之前的互动不计;别的号名下的笔记不出现在本号结果里。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        other = await seed_account("别的号", "u-other", _COOKIES)
        await _seed_note(owner, "nid-1", "本号的")
        await _seed_note(other, "nid-2", "别人的")
        await _seed_interaction(101, "nid-1", "like", days_ago=30)  # 窗口外
        await _seed_interaction(102, "nid-1", "like", days_ago=1)   # 窗口内
        await _seed_interaction(103, "nid-2", "like", days_ago=1)

        since = (_NOW - timedelta(days=7)).strftime("%Y-%m-%d")
        r = await c.get(f"/api/accounts/{owner}/self-interactions?since={since}",
                        headers=bearer(ADMIN_KEY))
        data = r.json()
        assert [n["note_id"] for n in data["notes"]] == ["nid-1"]
        assert _note_row(data, "nid-1")["self_likes"] == 1
        assert data["since"] == since

        # 不传 since = 全量
        full = (await c.get(f"/api/accounts/{owner}/self-interactions",
                            headers=bearer(ADMIN_KEY))).json()
        assert _note_row(full, "nid-1")["self_likes"] == 2
        assert full["since"] is None


async def test_bad_since_is_rejected(tmp_path, monkeypatch):
    """since 不是 YYYY-MM-DD → 400,不静默当成"没传"。

    静默忽略的话调用方以为自己只取了近一周,拿到的却是开天辟地以来的全量,
    减出来的数会大得莫名其妙。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        r = await c.get(f"/api/accounts/{owner}/self-interactions?since=上周",
                        headers=bearer(ADMIN_KEY))
        assert r.status_code == 400, r.text


async def test_requires_access_and_existing_account(tmp_path, monkeypatch):
    """无该号授权 → 403;账号不存在 → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        op_key = "op-self-denied"
        op_id = await make_operator(op_key)
        assert (await c.get(f"/api/accounts/{owner}/self-interactions",
                            headers=bearer(op_key))).status_code == 403

        await _grant(op_id, owner)
        assert (await c.get(f"/api/accounts/{owner}/self-interactions",
                            headers=bearer(op_key))).status_code == 200
        assert (await c.get("/api/accounts/99999/self-interactions",
                            headers=bearer(ADMIN_KEY))).status_code == 404


async def test_coverage_block_states_the_ledger_windows(tmp_path, monkeypatch):
    """coverage 说清两条台账各自从哪天开始有数、以及人工互动不在内。

    没有它,调用方会把"2026-08-02 之前一个赞都没有"读成"那段时间没人刷",
    实际是那之前根本没有这张台账。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("号主", "u-owner", _COOKIES)
        cov = (await c.get(f"/api/accounts/{owner}/self-interactions",
                           headers=bearer(ADMIN_KEY))).json()["coverage"]
        assert cov["likes_collects_since"] == "2026-08-02"
        assert cov["comments_since"] == "2026-08-01"
        assert "人工" in cov["note"]
