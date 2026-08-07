"""published-notes 分组 REST(6 端点)测试:台账查询 / 台账同步 / 可见性切换。

隔离手法同 test_notes_rest.py:rest_client 跑真实 lifespan(隔离库)。**不起浏览器**靠
``NBDPSY_ROLE=api``——``browser_jobs_repo.inline_execution_enabled()`` 据它判定,置 api
后 start_* 只登记台账不派进程内执行,正是生产 api 进程的真实行为(执行交 worker)。
台账行与终态 browser_jobs 行直接经 ORM 造。

覆盖:
- 鉴权:无 apikey → 401(读写各一)。
- RBAC:越权 operator 读台账/发起同步/发起切换 → 403;未知账号 → 404。
- 序列化:permission_code 的 **null 与 0 不能混淆**(0 不得被 falsy 兜底成 null/缺失),
  sync_status / published_at / platform_published_at 逐字段对上。
- 分页:total 与 limit 无关;offset 翻页顺序为 published_at 降序。
- 单条:按 note_id 命中;pending_id 行(note_id 为 NULL)查不到 → 404。
- 异步契约:同步/切换/互动登记后能按 id 查到;跨 kind 的 id 互查 → 404。
- 请求体校验:target_privacy 只接受 0/1(2、"public"、true 一律 422),title 空 → 422。
- 轮询映射:done 带 result_status + permission_code(0 不丢);error+unknown 标记 → unknown。
- 防漂移:manifest 声明与实际注册的 6 条新路由双向全等。
"""

import json
from datetime import datetime

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

_NEW_ROUTES = {
    ("GET", "/api/accounts/{account_id}/published-notes"),
    ("GET", "/api/published-notes/{note_id}"),
    ("POST", "/api/accounts/{account_id}/note-ledger-syncs"),
    ("GET", "/api/note-ledger-syncs/{sync_id}"),
    ("POST", "/api/accounts/{account_id}/note-visibility-changes"),
    ("GET", "/api/note-visibility-changes/{change_id}"),
    ("POST", "/api/accounts/{account_id}/note-purpose-backfills"),
    ("GET", "/api/note-purpose-backfills/{backfill_id}"),
}


def _api_role(monkeypatch) -> None:
    """置 NBDPSY_ROLE=api:start_* 只登记台账,不在本进程派执行(不会起浏览器)。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _grant(op_id: int, account_id: int) -> None:
    """给 operator 授权某号。"""
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, account_id, None)
        await s.commit()


async def _seed_note(account_id: int, **kwargs) -> int:
    """直接经 ORM 造一行台账;返回行 id。"""
    async with db_module.async_session() as s:
        row = PublishedNote(account_id=account_id, **kwargs)
        s.add(row)
        await s.commit()
        return row.id


async def _seed_job(job_id: str, kind: str, account_id: int, status: str, result) -> None:
    """直接经 ORM 造一条终态 browser_jobs 行(模拟 worker 已跑完)。"""
    async with db_module.async_session() as s:
        s.add(
            BrowserJob(
                id=job_id,
                kind=kind,
                account_id=account_id,
                operator_id=0,
                payload="{}",
                status=status,
                result=json.dumps(result, ensure_ascii=False) if result else None,
            )
        )
        await s.commit()


# ---------------- 鉴权 ----------------


async def test_missing_apikey_401(tmp_path, monkeypatch):
    """无 apikey:读端点与写端点都 401(中间件层拦在业务前)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("鉴权号", "uAuth", _COOKIES)
        assert (await c.get(f"/api/accounts/{acc}/published-notes")).status_code == 401
        r = await c.post(
            f"/api/accounts/{acc}/note-visibility-changes",
            json={"note_id": "n1", "title": "标题", "target_privacy": 1},
        )
        assert r.status_code == 401


# ---------------- 台账查询:序列化与 RBAC ----------------


async def test_permission_code_zero_and_null_not_confused(tmp_path, monkeypatch):
    """permission_code:0(公开)与 null(未知)必须各自原样下发,绝不被 falsy 兜底混淆。

    这条是本组最要命的歧义——台账缺该字段时曾把用户刻意隐藏的笔记误判成公开笔记。
    """
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("码号", "uCode", _COOKIES)
        await _seed_note(
            acc, note_id="pub0", title="公开的", permission_code=0, permission_msg="",
            published_at=datetime(2026, 7, 20, 1, 0), sync_status="linked",
        )
        await _seed_note(
            acc, note_id="unk0", title="未知的", permission_code=None,
            published_at=datetime(2026, 7, 19, 1, 0), sync_status="orphan",
        )
        await _seed_note(
            acc, note_id="pri0", title="仅自己的", permission_code=1,
            permission_msg="仅自己可见",
            published_at=datetime(2026, 7, 18, 1, 0), sync_status="linked",
        )

        body = (await c.get(
            f"/api/accounts/{acc}/published-notes", headers=bearer(ADMIN_KEY)
        )).json()
        by_id = {n["note_id"]: n for n in body["notes"]}
        # 0 必须是整数 0 —— 不是 null、不是缺键
        assert by_id["pub0"]["permission_code"] == 0
        assert by_id["pub0"]["permission_code"] is not None
        assert by_id["unk0"]["permission_code"] is None
        assert "permission_code" in by_id["unk0"]  # 未知也要显式给 null,不是省略
        assert by_id["pri0"]["permission_code"] == 1
        assert by_id["pri0"]["permission_msg"] == "仅自己可见"

        # 口径随数据下发,且明确点破 null≠公开
        notes_meta = body["meta"]["field_notes"]
        assert "不等于公开" in notes_meta["permission_code"]
        assert "pending_id" in notes_meta["sync_status"]


async def test_note_view_fields_and_time_offsets(tmp_path, monkeypatch):
    """必需字段齐全;published_at 永不为空、platform_published_at 可空;时间带 +00:00。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("字段号", "uField", _COOKIES)
        await _seed_note(
            acc,
            note_id="n-full",
            title="全字段",
            note_url="https://www.xiaohongshu.com/explore/n-full?xsec_token=t",
            note_type="normal",
            published_at=datetime(2026, 7, 20, 1, 2, 3),
            platform_published_at=None,  # 平台权威时间可能没同步到
            permission_code=0,
            sync_status="pending_id",
            source_publish_job_id=None,
            content_archive_id=None,
            likes=7, collects=3, comments=1, shares=2, views=88,
        )

        note = (await c.get(
            f"/api/accounts/{acc}/published-notes", headers=bearer(ADMIN_KEY)
        )).json()["notes"][0]
        for key in (
            "note_id", "title", "note_url", "published_at", "platform_published_at",
            "permission_code", "permission_msg", "sync_status",
            "source_publish_job_id", "content_archive_id", "interaction",
        ):
            assert key in note, f"缺字段 {key}"
        # 本机记录的发布时刻永不为空,且带显式 UTC 偏移
        assert note["published_at"] == "2026-07-20T01:02:03+00:00"
        assert note["platform_published_at"] is None
        assert note["sync_status"] == "pending_id"
        assert note["interaction"] == {
            "likes": 7, "collects": 3, "comments": 1, "shares": 2, "views": 88
        }


async def test_list_pagination_and_order(tmp_path, monkeypatch):
    """total 与 limit 无关;按 published_at 降序,offset 能翻到后一页。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("翻页号", "uPage", _COOKIES)
        for day in (18, 20, 19):  # 入库序故意打乱,验证真按时间排而非入库序
            await _seed_note(
                acc, note_id=f"n{day}", title=f"第{day}篇",
                published_at=datetime(2026, 7, day, 1, 0),
            )

        first = (await c.get(
            f"/api/accounts/{acc}/published-notes",
            params={"limit": 2}, headers=bearer(ADMIN_KEY),
        )).json()
        assert first["total"] == 3  # total 数全表,不受 limit 截断
        assert first["limit"] == 2 and first["offset"] == 0
        assert [n["note_id"] for n in first["notes"]] == ["n20", "n19"]

        second = (await c.get(
            f"/api/accounts/{acc}/published-notes",
            params={"limit": 2, "offset": 2}, headers=bearer(ADMIN_KEY),
        )).json()
        assert [n["note_id"] for n in second["notes"]] == ["n18"]
        assert second["total"] == 3


async def test_list_denied_403(tmp_path, monkeypatch):
    """未授权该号的 operator 读台账 → 403。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("越权号", "uDeny", _COOKIES)
        await _seed_note(acc, note_id="secret", title="别人的笔记")
        other_key = "op-pub-denied-01"
        await make_operator(other_key)
        r = await c.get(
            f"/api/accounts/{acc}/published-notes", headers=bearer(other_key)
        )
        assert r.status_code == 403


# ---------------- 台账查询:单条 ----------------


async def test_get_single_note_and_pending_not_reachable(tmp_path, monkeypatch):
    """按 note_id 命中单条;pending_id 行(note_id 为 NULL)本端点查不到 → 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("单条号", "uOne", _COOKIES)
        await _seed_note(
            acc, note_id="abc123", title="有 id 的", permission_code=1,
            sync_status="linked", source_publish_job_id=None,
        )
        await _seed_note(acc, note_id=None, title="待补 id 的")  # pending_id

        body = (await c.get(
            "/api/published-notes/abc123", headers=bearer(ADMIN_KEY)
        )).json()
        assert body["note"]["title"] == "有 id 的"
        assert body["note"]["permission_code"] == 1
        assert body["meta"]["field_notes"]

        assert (await c.get(
            "/api/published-notes/nope", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_get_single_note_denied_403(tmp_path, monkeypatch):
    """单条读同样按行所属账号收窄:无该号授权 → 403。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("单条越权号", "uOneDeny", _COOKIES)
        await _seed_note(acc, note_id="hid1", title="他号的笔记")
        other_key = "op-pub-one-denied-01"
        await make_operator(other_key)
        r = await c.get("/api/published-notes/hid1", headers=bearer(other_key))
        assert r.status_code == 403


# ---------------- 台账同步(202 + 轮询)----------------


async def test_start_ledger_sync_202_and_poll(tmp_path, monkeypatch):
    """授权号 POST → 202 {sync_id, status:"queued"},随即能按 id 轮询到 queued。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("同步号", "uSync", _COOKIES)
        op_key = "op-sync-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-ledger-syncs", headers=bearer(op_key)
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["sync_id"]

        poll = await c.get(
            f"/api/note-ledger-syncs/{body['sync_id']}", headers=bearer(op_key)
        )
        assert poll.status_code == 200, poll.text
        # queue 段随排队态一起下发(排队可见性):这里只钉形状,细节见 tests/test_queue_status*.py
        body_poll = poll.json()
        assert body_poll["status"] == "queued"
        assert body_poll["queue"]["position"] == 1


async def test_start_ledger_sync_denied_and_unknown(tmp_path, monkeypatch):
    """越权 → 403;未知账号(admin)→ 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("同步越权号", "uSyncDeny", _COOKIES)
        other_key = "op-sync-denied-01"
        await make_operator(other_key)
        assert (await c.post(
            f"/api/accounts/{acc}/note-ledger-syncs", headers=bearer(other_key)
        )).status_code == 403
        assert (await c.post(
            "/api/accounts/999999/note-ledger-syncs", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_poll_ledger_sync_done_counts(tmp_path, monkeypatch):
    """done 行:计数原样并入响应;跨 kind 的 id 互查 → 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("同步终态号", "uSyncDone", _COOKIES)
        await _seed_job(
            "sync-done-1", "note_ledger_sync", acc, "done",
            {"note_count": 37, "refreshed": 30, "linked": 2, "orphan": 5,
             "ambiguous": 0, "pending_remaining": 1, "missing": 0},
        )
        body = (await c.get(
            "/api/note-ledger-syncs/sync-done-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert body["status"] == "done"
        assert body["note_count"] == 37 and body["linked"] == 2 and body["orphan"] == 5

        # 拿同步 id 去可见性端点查:kind 不符 → 404,不返回别的任务的状态
        assert (await c.get(
            "/api/note-visibility-changes/sync-done-1", headers=bearer(ADMIN_KEY)
        )).status_code == 404
        assert (await c.get(
            "/api/note-ledger-syncs/never-existed", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_poll_job_denied_403(tmp_path, monkeypatch):
    """轮询按台账行里的 account_id 收窄:无该号授权 → 403。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("轮询越权号", "uPollDeny", _COOKIES)
        await _seed_job("sync-other-1", "note_ledger_sync", acc, "done", {"note_count": 1})
        other_key = "op-poll-denied-01"
        await make_operator(other_key)
        r = await c.get(
            "/api/note-ledger-syncs/sync-other-1", headers=bearer(other_key)
        )
        assert r.status_code == 403


# ---------------- 可见性切换(202 + 轮询)----------------


async def test_start_visibility_change_202_and_payload(tmp_path, monkeypatch):
    """202 回 change_id;登记的 payload 带三件套 + operator_id(留痕来源)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("切换号", "uVis", _COOKIES)
        op_key = "op-vis-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-visibility-changes",
            json={"note_id": "vid1", "title": "要藏起来的笔记", "target_privacy": 1},
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        change_id = r.json()["change_id"]
        assert r.json()["status"] == "queued"

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, change_id)
        assert row.kind == "note_visibility"
        assert row.account_id == acc and row.operator_id == op_id
        payload = json.loads(row.payload)
        assert payload["note_id"] == "vid1"
        assert payload["title"] == "要藏起来的笔记"
        assert payload["target_privacy"] == 1
        # execute 契约签名拿不到请求上下文,visibility_changed_by 只能靠 payload 带过去
        assert payload["operator_id"] == op_id


async def test_visibility_request_validation(tmp_path, monkeypatch):
    """target_privacy 只接受 0/1:2 / "public" / true 一律 422;title 省略/为空则放行。

    另外三档(仅互关好友/部分人可见/部分人不可见)接口参数完全未验证,必须在入口就拒,
    不能排队一两分钟后才在浏览器层失败。

    title 2026-08-01 起**不再必填**:定位改成优先 note_id(浏览器层先拿 id 去平台列表里
    翻译出当前标题),台账 title 只是兜底 —— 而台账 title 会过期,拿它当必填反而误导。
    """
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("校验号", "uValid", _COOKIES)
        url = f"/api/accounts/{acc}/note-visibility-changes"
        base = {"note_id": "vid", "title": "标题"}

        for bad in (2, 3, -1, "public", True, None):
            r = await c.post(
                url, json={**base, "target_privacy": bad}, headers=bearer(ADMIN_KEY)
            )
            assert r.status_code == 422, f"target_privacy={bad!r} 未被拒: {r.text}"

        # 两档合法值都放行
        for good in (0, 1):
            r = await c.post(
                url, json={**base, "target_privacy": good}, headers=bearer(ADMIN_KEY)
            )
            assert r.status_code == 202, r.text

        # title 省略 / 为空都放行:定位主键是 note_id,标题只是兜底
        for body in (
            {"note_id": "vid", "target_privacy": 1},
            {"note_id": "vid", "title": "", "target_privacy": 1},
        ):
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 202, r.text

        # note_id 仍是必填(没它既定位不了也回读不了)
        r = await c.post(
            url, json={"note_id": "", "title": "标题", "target_privacy": 1},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422
        # note_id 是回读校验的依据,同样必填
        r = await c.post(
            url, json={"note_id": "", "title": "标题", "target_privacy": 1},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422


async def test_start_visibility_denied_and_unknown(tmp_path, monkeypatch):
    """越权 → 403;未知账号 → 404(都不登记任务)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("切换越权号", "uVisDeny", _COOKIES)
        body = {"note_id": "v", "title": "t", "target_privacy": 0}
        other_key = "op-vis-denied-01"
        await make_operator(other_key)
        assert (await c.post(
            f"/api/accounts/{acc}/note-visibility-changes",
            json=body, headers=bearer(other_key),
        )).status_code == 403
        assert (await c.post(
            "/api/accounts/999999/note-visibility-changes",
            json=body, headers=bearer(ADMIN_KEY),
        )).status_code == 404

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            count = await s.scalar(select(func.count()).select_from(BrowserJob))
        assert count == 0  # 拦在登记之前


async def test_poll_visibility_done_skipped_and_zero_code(tmp_path, monkeypatch):
    """done 时 result_status 区分真改(done)与本就是目标档位(skipped);code=0 不丢。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("切换终态号", "uVisDone", _COOKIES)
        await _seed_job(
            "vis-done-1", "note_visibility", acc, "done",
            {"status": "done", "permission_code": 0, "permission_msg": ""},
        )
        await _seed_job(
            "vis-skip-1", "note_visibility", acc, "done",
            {"status": "skipped", "permission_code": 1},
        )

        changed = (await c.get(
            "/api/note-visibility-changes/vis-done-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert changed["status"] == "done"
        assert changed["result_status"] == "done"
        # 切回公开就是 permission_code=0,绝不能被 falsy 兜底抹成 null
        assert changed["permission_code"] == 0
        assert changed["permission_msg"] == ""

        skipped = (await c.get(
            "/api/note-visibility-changes/vis-skip-1", headers=bearer(ADMIN_KEY)
        )).json()
        # 外层 status=done(任务跑完了)但内层 skipped(什么都没改),两者不能合成一个键
        assert skipped["status"] == "done" and skipped["result_status"] == "skipped"
        assert skipped["permission_code"] == 1


async def test_poll_visibility_error_and_unknown(tmp_path, monkeypatch):
    """普通失败 → error + reason;僵死恢复(带 unknown 标记)→ unknown,绝不冒充普通失败。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("切换失败号", "uVisErr", _COOKIES)
        await _seed_job(
            "vis-err-1", "note_visibility", acc, "error",
            {"error": "note_not_locatable: 同标题笔记不唯一"},
        )
        await _seed_job(
            "vis-unknown-1", "note_visibility", acc, "error",
            {"error": "执行进程中断,任务结果未知(unknown):请人工核对", "unknown": True},
        )

        err = (await c.get(
            "/api/note-visibility-changes/vis-err-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert err["status"] == "error"
        assert "note_not_locatable" in err["reason"]

        unknown = (await c.get(
            "/api/note-visibility-changes/vis-unknown-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert unknown["status"] == "unknown"  # 不是 error
        assert "人工核对" in unknown["reason"]


# ---------------- 防漂移(局部子集) ----------------


def test_manifest_covers_new_routes():
    """8 条新路由在 manifest 与实际注册路由里双向全等(全局防漂移在 test_manifest.py)。"""
    from app.http import ALL_MANIFEST_ENTRIES
    from app.server import create_app

    _HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    app = create_app()
    actual = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in ops
        if method.upper() in _HTTP_METHODS
    }
    declared = {(e["method"], e["path"]) for e in ALL_MANIFEST_ENTRIES}
    assert _NEW_ROUTES <= actual, f"未注册: {sorted(_NEW_ROUTES - actual)}"
    assert _NEW_ROUTES <= declared, f"manifest 漏写: {sorted(_NEW_ROUTES - declared)}"
