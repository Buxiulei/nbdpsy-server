"""受众行为库 REST(5 端点)测试:口径 + 鉴权 + 合规默认。

三条最容易出错、错了又看不出来的口径在这里钉死:

1. **自家号默认排除**。不排的话"最活跃的受众"永远是自家矩阵号(它们互刷出来的互动量
   碾压真实受众),整个库读起来就是一堆废话。排除名单**现查 xhs_accounts**,不硬编码;
2. **潜客分在整个可见人群里归一化,再按 filter 筛**。反过来做(先筛再归一)会让同一个人
   在"看全部"和"只看粉丝"两个视图里拿到两个分数,运营立刻就不信这个数了;
3. **漏斗五层是划分**:各层人数加起来必须等于总人数,否则那张图就是错的。
"""

from datetime import datetime, timedelta, timezone

import app.core.db as db_module
from app.models import AudienceEvent
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


def _epoch(days_ago: float) -> int:
    return int(
        (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    )


async def _seed_events(rows: list[dict]) -> None:
    async with db_module.async_session() as s:
        for i, row in enumerate(rows):
            s.add(AudienceEvent(
                account_id=row["account_id"],
                platform_event_id=row.get("event_id") or f"evt-{i}",
                actor_userid=row["actor"],
                actor_nickname=row.get("nickname") or f"昵称-{row['actor']}",
                actor_image=row.get("image"),
                event_type=row.get("event_type") or "like_note",
                target_note_id=row.get("note_id"),
                target_note_title=row.get("note_title"),
                fstatus=row.get("fstatus") or "none",
                event_time=row["event_time"],
                raw_json="{}",
            ))
        await s.commit()


async def _seed_matrix() -> tuple[int, int]:
    """两个自家号(user_id = self-1 / self-2)。"""
    return (
        await seed_account("号一", "self-1", _COOKIES),
        await seed_account("号二", "self-2", _COOKIES),
    )


async def _seed_typical(account_a: int, account_b: int) -> None:
    """一份有代表性的受众:高潜陌生人 / 浅粉丝 / 互关自己人 / 自家号互刷。"""
    await _seed_events([
        # 陌生高频 + 跨两个号 + 有收藏 → 该排最前
        {"account_id": account_a, "actor": "高潜", "event_time": _epoch(1),
         "event_type": "fav_note", "note_id": "n1", "note_title": "过度换气",
         "fstatus": "none"},
        {"account_id": account_a, "actor": "高潜", "event_time": _epoch(2),
         "event_type": "like_note", "note_id": "n1", "note_title": "过度换气",
         "fstatus": "none"},
        {"account_id": account_b, "actor": "高潜", "event_time": _epoch(3),
         "event_type": "like_note", "note_id": "n2", "note_title": "依恋修复",
         "fstatus": "none"},
        # 已关注但只来过一次
        {"account_id": account_a, "actor": "浅粉丝", "event_time": _epoch(5),
         "event_type": "like_comment", "note_id": "n1", "note_title": "过度换气",
         "fstatus": "fans"},
        # 互关自己人(真人,不是自家号)
        {"account_id": account_a, "actor": "同行", "event_time": _epoch(200),
         "event_type": "like_note", "note_id": "n2", "note_title": "依恋修复",
         "fstatus": "both"},
        # 自家号互刷:入库了,但分析默认必须看不见
        {"account_id": account_b, "actor": "self-1", "event_time": _epoch(1),
         "event_type": "like_note", "note_id": "n2", "note_title": "依恋修复",
         "fstatus": "both"},
    ])


# ---------------- 鉴权 ----------------


async def test_endpoints_require_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        for path in ("/api/audience/overview", "/api/audience/actors",
                     "/api/audience/actors/x", "/api/audience/funnel",
                     "/api/audience/segments"):
            assert (await client.get(path)).status_code == 401, path


async def test_operator_sees_only_authorized_accounts(tmp_path, monkeypatch):
    """非 admin 只看得到自己被授权的号收到的互动(与 GET /api/accounts 同门)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_events([
            {"account_id": acc_a, "actor": "甲的受众", "event_time": _epoch(1)},
            {"account_id": acc_b, "actor": "乙的受众", "event_time": _epoch(1)},
        ])
        op_key = "op-audience"
        op_id = await make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, acc_a, None)
            await s.commit()

        body = (await client.get("/api/audience/actors", headers=bearer(op_key))).json()

        assert [a["actor_userid"] for a in body["actors"]] == ["甲的受众"]


# ---------------- overview ----------------


async def test_overview_excludes_self_accounts_by_default(tmp_path, monkeypatch):
    """自家号互刷的互动照常入库,但**默认从受众分析里剔除**。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/overview", headers=bearer(ADMIN_KEY))).json()

        assert body["total_actors"] == 3, "自家号 self-1 没被排掉"
        assert body["self_exclude"]["enabled"] is True
        assert body["self_exclude"]["account_userids"] == 2
        # total_events 与 total_actors 同口径(都已剔除自家号):6 条事件里那条自家号互刷
        # 照常在库里躺着,只是不进这份分析
        assert body["total_events"] == 5


async def test_overview_can_include_self_accounts(tmp_path, monkeypatch):
    """排查"自家互刷量有多大"时要看得见它们 —— 显式关掉排除。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/overview?exclude_self=false",
            headers=bearer(ADMIN_KEY))).json()

        assert body["total_actors"] == 4
        assert body["self_exclude"]["enabled"] is False


async def test_overview_relation_distribution_and_new_actors(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/overview", headers=bearer(ADMIN_KEY))).json()

        assert body["by_fstatus"] == {"none": 1, "fans": 1, "both": 1}
        # 「新增互动者」按**首次**互动落在窗口内算:同行那位 200 天前就来过,不算新增
        assert body["new_actors_7d"] == 2
        assert body["new_actors_30d"] == 2
        assert body["scoring"]["calibration"], "打分口径必须自带「待校准」声明"


# ---------------- actors ----------------


async def test_actors_sorted_by_score_and_scored_over_whole_population(
    tmp_path, monkeypatch
):
    """默认按潜客分降序;分数在**整个可见人群**里归一化,不随 filter 变。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        everyone = (await client.get(
            "/api/audience/actors", headers=bearer(ADMIN_KEY))).json()
        filtered = (await client.get(
            "/api/audience/actors?fstatus=none", headers=bearer(ADMIN_KEY))).json()

        assert [a["actor_userid"] for a in everyone["actors"]][0] == "高潜"
        assert everyone["total"] == 3
        # 同一个人在两个视图里必须是同一个分数
        top = next(a for a in everyone["actors"] if a["actor_userid"] == "高潜")
        assert filtered["actors"][0]["potential_score"] == top["potential_score"]
        assert filtered["total"] == 1


async def test_actors_filter_by_event_type_and_sort_options(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        faved = (await client.get(
            "/api/audience/actors?event_type=fav_note",
            headers=bearer(ADMIN_KEY))).json()
        recent = (await client.get(
            "/api/audience/actors?sort=recent", headers=bearer(ADMIN_KEY))).json()
        events = (await client.get(
            "/api/audience/actors?sort=events", headers=bearer(ADMIN_KEY))).json()

        assert [a["actor_userid"] for a in faved["actors"]] == ["高潜"]
        assert recent["actors"][0]["actor_userid"] == "高潜"
        assert recent["actors"][-1]["actor_userid"] == "同行"
        assert events["actors"][0]["event_count"] == 3


async def test_actors_filter_by_followed(tmp_path, monkeypatch):
    """followed 是「他关注了我没有」这一刀(fans+both),比 fstatus 粗一档,漏斗上下游看它。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        yes = (await client.get(
            "/api/audience/actors?followed=true", headers=bearer(ADMIN_KEY))).json()
        no = (await client.get(
            "/api/audience/actors?followed=false", headers=bearer(ADMIN_KEY))).json()

        assert sorted(a["actor_userid"] for a in yes["actors"]) == ["同行", "浅粉丝"]
        assert [a["actor_userid"] for a in no["actors"]] == ["高潜"]
        assert yes["total"] + no["total"] == 3, "两半加起来必须等于全体"


async def test_actors_rejects_unknown_params(tmp_path, monkeypatch):
    """认不出的取值一律 400。

    排序键静默按默认排 → 调用方拿到的顺序不是他要的;筛选键静默返空 → 空列表会被读成
    「确实没有这类人」。两种都是"看不出来的错",所以宁可报错。
    """
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        for query in ("sort=乱写", "fstatus=friend", "event_type=liked/item", "limit=0"):
            r = await client.get(
                f"/api/audience/actors?{query}", headers=bearer(ADMIN_KEY))
            assert r.status_code == 400, query


async def test_actors_paginate(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        page = (await client.get(
            "/api/audience/actors?limit=1&offset=1", headers=bearer(ADMIN_KEY))).json()

        assert page["total"] == 3 and len(page["actors"]) == 1


# ---------------- 单人纵向轨迹 ----------------


async def test_actor_trajectory_aggregates_across_accounts(tmp_path, monkeypatch):
    """单人轨迹:时间线 + 跨自家号分布 + 关系演变 + 潜客分明细。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/actors/高潜", headers=bearer(ADMIN_KEY))).json()

        assert body["actor_userid"] == "高潜"
        assert body["event_count"] == 3
        assert [e["event_type"] for e in body["timeline"]] == [
            "fav_note", "like_note", "like_note",
        ], "时间线必须最近的在前"
        assert {b["account_id"] for b in body["by_account"]} == {acc_a, acc_b}
        assert sum(b["event_count"] for b in body["by_account"]) == 3
        # 潜客分明细可解释:五个维度各带 raw / normalized / weight
        assert set(body["score_detail"]) == {
            "frequency", "cross_account", "recency", "depth", "relation",
        }
        assert body["funnel_layer"] == "stranger_frequent"


async def test_actor_trajectory_score_matches_list_view(tmp_path, monkeypatch):
    """单人页的分数与列表页一致 —— 单人页若只拿自己归一化,分数恒为 0,那是纯误导。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        listed = (await client.get(
            "/api/audience/actors", headers=bearer(ADMIN_KEY))).json()["actors"]
        top = next(a for a in listed if a["actor_userid"] == "高潜")
        detail = (await client.get(
            "/api/audience/actors/高潜", headers=bearer(ADMIN_KEY))).json()

        assert detail["potential_score"] == top["potential_score"] > 0


async def test_actor_relation_history_shows_changes_only(tmp_path, monkeypatch):
    """关系演变只记**变化点**:一串重复的 none 不该刷屏。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, _acc_b = await _seed_matrix()
        await _seed_events([
            {"account_id": acc_a, "actor": "路人转粉", "event_time": _epoch(9),
             "fstatus": "none"},
            {"account_id": acc_a, "actor": "路人转粉", "event_time": _epoch(8),
             "fstatus": "none"},
            {"account_id": acc_a, "actor": "路人转粉", "event_time": _epoch(2),
             "fstatus": "fans"},
        ])

        body = (await client.get(
            "/api/audience/actors/路人转粉", headers=bearer(ADMIN_KEY))).json()

        assert [h["fstatus"] for h in body["relation_history"]] == ["none", "fans"]


async def test_unknown_actor_404(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        r = await client.get("/api/audience/actors/查无此人", headers=bearer(ADMIN_KEY))
        assert r.status_code == 404


async def test_self_account_actor_is_not_reachable_by_default(tmp_path, monkeypatch):
    """自家号默认排除对单人页同样生效(否则从列表看不见、直接敲 url 却能看见)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        assert (await client.get(
            "/api/audience/actors/self-1", headers=bearer(ADMIN_KEY))).status_code == 404
        assert (await client.get(
            "/api/audience/actors/self-1?exclude_self=false",
            headers=bearer(ADMIN_KEY))).status_code == 200


# ---------------- 漏斗 / 切片 ----------------


async def test_funnel_layers_sum_to_total(tmp_path, monkeypatch):
    """五层是划分:各层人数加起来等于总人数,否则那张图就是错的。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/funnel", headers=bearer(ADMIN_KEY))).json()

        assert sum(layer["count"] for layer in body["layers"]) == body["total"] == 3
        by_layer = {layer["layer"]: layer for layer in body["layers"]}
        assert by_layer["stranger_frequent"]["count"] == 1
        assert by_layer["follower_shallow"]["count"] == 1
        assert by_layer["mutual"]["count"] == 1
        # 每层都要有代表人物,不然运营只看得到数字看不到人
        assert by_layer["stranger_frequent"]["examples"][0]["actor_userid"] == "高潜"
        assert body["frequent_event_threshold"] >= 1


async def test_funnel_layers_always_all_five(tmp_path, monkeypatch):
    """空层也要列出来(count=0):少一层的漏斗图会被读成"这层没这个概念"。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _seed_matrix()
        body = (await client.get(
            "/api/audience/funnel", headers=bearer(ADMIN_KEY))).json()
        assert len(body["layers"]) == 5
        assert all(layer["count"] == 0 for layer in body["layers"])


async def test_segments_relation_activity_and_content(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        acc_a, acc_b = await _seed_matrix()
        await _seed_typical(acc_a, acc_b)

        body = (await client.get(
            "/api/audience/segments", headers=bearer(ADMIN_KEY))).json()

        assert body["by_relation"] == {"none": 1, "fans": 1, "both": 1}
        assert body["by_activity"]["low"] == 1  # 高潜 3 次
        assert body["by_activity"]["once"] == 2
        assert body["by_event_type"] == {"fav_note": 1, "like_note": 3, "like_comment": 1}
        top_note = body["content_preference"][0]
        assert top_note["note_id"] == "n1" and top_note["title"] == "过度换气"
        assert top_note["actor_count"] == 2 and top_note["event_count"] == 3
