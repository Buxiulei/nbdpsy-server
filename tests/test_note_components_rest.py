"""note-components 分组 REST(4 端点)测试:合集/活动列表(只读)+ 三组件设置(202)+ 轮询。

隔离手法同 test_published_notes_rest.py:rest_client 跑真实 lifespan(隔离库)+
``NBDPSY_ROLE=api``(只登记台账不派执行,不起浏览器)。两个 GET 端点会真起浏览器,
故把服务层的 ``read_catalog`` 打桩掉 —— 测的是鉴权/落脚点选取/筛选/错误映射这几层。

覆盖:
- 鉴权缺失 → 401;越权 operator → 403;未知账号 → 404(都不登记任务)。
- 请求体校验:note_id 必填;三个组件 id **一个都不给 → 422**(那样这次编辑什么都不改,
  却要真提交一次全量覆盖的发布,纯风险零收益)。
- 登记内容:kind=note_components、payload 四件套齐全、记到发起人名下(配额依据)。
- 轮询:queued 可查;done 带 **result_status + 逐项 applied/failed**;部分生效不得报成
  全成;error 带 reason;僵死 unknown 标记 → unknown;跨 kind 的 id → 404。
- note_components 非幂等:不得进 _IDEMPOTENT_KINDS。
- 目录端点:note_id 省略时自动取台账里最近一篇有 note_id 的笔记;台账里一篇都没有 → 400;
  浏览器侧失败 → 502(**不静默返回空列表**);活动按默认心理学词表筛,`*` 不过滤。
- 防漂移:4 条新路由在 manifest 与实际注册路由里双向全等。
"""

import json
from datetime import datetime

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo, note_components, operator_service
from tests.rest_helpers import (
    ADMIN_KEY,
    bearer,
    make_operator,
    rest_client,
    seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

_NEW_ROUTES = {
    ("GET", "/api/accounts/{account_id}/collections"),
    ("GET", "/api/accounts/{account_id}/activities"),
    ("POST", "/api/accounts/{account_id}/note-components"),
    ("GET", "/api/note-components/{job_id}"),
}

_CATALOG = {
    "collections": [
        {"id": "c1", "name": "咨询师简介", "desc": "全员北大临床心理硕博", "note_num": 10}
    ],
    "activities": [
        {"id": "43561", "name": "身边的心理学", "desc": "聊聊情绪"},
        {"id": "999", "name": "howto穿出自我", "desc": "穿搭灵感大赏"},
    ],
}


def _api_role(monkeypatch) -> None:
    """置 NBDPSY_ROLE=api:start_components 只登记台账,不在本进程起浏览器。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")


def _stub_catalog(monkeypatch, result=None) -> dict:
    """把服务层的目录读取打桩掉(它会起真浏览器);返回记录调用参数的 dict。"""
    seen: dict = {}

    async def fake_read(account_id, note_id):
        seen.update({"account_id": account_id, "note_id": note_id})
        return dict(result if result is not None else _CATALOG)

    monkeypatch.setattr(note_components, "read_catalog", fake_read)
    return seen


async def _grant(op_id: int, account_id: int) -> None:
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, account_id, None)
        await s.commit()


async def _seed_note(account_id: int, note_id: str, published_at: datetime) -> None:
    async with db_module.async_session() as s:
        s.add(
            PublishedNote(
                account_id=account_id, note_id=note_id, title="某篇笔记",
                published_at=published_at, sync_status="linked",
                first_seen_at=published_at, last_synced_at=published_at,
            )
        )
        await s.commit()


async def _seed_job(job_id: str, kind: str, account_id: int, status: str, result) -> None:
    async with db_module.async_session() as s:
        s.add(
            BrowserJob(
                id=job_id, kind=kind, account_id=account_id, operator_id=0,
                payload="{}", status=status,
                result=json.dumps(result, ensure_ascii=False) if result else None,
            )
        )
        await s.commit()


# ---------------- 非幂等纪律 ----------------


def test_note_components_is_not_idempotent_kind():
    """绝不能进 _IDEMPOTENT_KINDS:提交是全量覆盖,且关联活动会把话题**再追加一遍**。"""
    assert "note_components" not in browser_jobs_repo._IDEMPOTENT_KINDS


# ---------------- POST /api/accounts/{id}/note-components ----------------


async def test_missing_apikey_401(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件鉴权号", "uNcAuth", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n1", "collection_id": "c1"},
        )
        assert r.status_code == 401


async def test_start_202_and_payload(tmp_path, monkeypatch):
    """202 回 job_id;payload 带 note_id + 三组件,记发起人名下。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件号", "uNc", _COOKIES)
        op_key = "op-nc-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={
                "note_id": "6a4ce556",
                "collection_id": "c1",
                "quoted_note_id": "n-quote",
                "activity_id": "43561",
            },
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["job_id"]

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, body["job_id"])
        assert row.kind == "note_components"
        assert row.account_id == acc and row.operator_id == op_id
        payload = json.loads(row.payload)
        assert payload == {
            "note_id": "6a4ce556", "collection_id": "c1",
            "quoted_note_id": "n-quote", "activity_id": "43561",
        }

        poll = await c.get(
            f"/api/note-components/{body['job_id']}", headers=bearer(op_key)
        )
        assert poll.json() == {"status": "queued"}


async def test_request_validation(tmp_path, monkeypatch):
    """note_id 必填;三个组件 id 一个都不给 → 422(这次编辑什么都不会改)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件校验号", "uNcValid", _COOKIES)
        url = f"/api/accounts/{acc}/note-components"

        bad_bodies = [
            {"collection_id": "c1"},                       # 缺 note_id
            {"note_id": "", "collection_id": "c1"},        # note_id 空
            {"note_id": "n1"},                             # 一个组件都没给
            {"note_id": "n1", "collection_id": None, "activity_id": None},
        ]
        for body in bad_bodies:
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, f"未被拒: {body!r} -> {r.text}"

        # 只给一个组件也放行
        r = await c.post(
            url, json={"note_id": "n1", "activity_id": "43561"}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text


async def test_start_denied_and_unknown(tmp_path, monkeypatch):
    """越权 → 403;未知账号 → 404;两者都不登记任务。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件越权号", "uNcDeny", _COOKIES)
        body = {"note_id": "n1", "collection_id": "c1"}
        other_key = "op-nc-denied-01"
        await make_operator(other_key)

        assert (await c.post(
            f"/api/accounts/{acc}/note-components", json=body, headers=bearer(other_key)
        )).status_code == 403
        assert (await c.post(
            "/api/accounts/999999/note-components", json=body, headers=bearer(ADMIN_KEY)
        )).status_code == 404

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            assert await s.scalar(select(func.count()).select_from(BrowserJob)) == 0


# ---------------- GET /api/note-components/{job_id} ----------------


async def test_poll_partially_applied_is_not_reported_as_done(tmp_path, monkeypatch):
    """部分生效:外层 status=done(任务跑完了)但 **result_status=partially_applied**。

    合成一个键会让"只成了一半"被读成"全成了" —— 这条产品线的失败是静默的,必须分开报。
    """
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件部分号", "uNcPart", _COOKIES)
        await _seed_job(
            "nc-part-1", "note_components", acc, "done",
            {
                "status": "partially_applied",
                "applied": {"collection": False, "activity": True},
                "failed": [{"component": "collection", "reason": "collection_not_applied: …"}],
                "submitted": True,
                "permission_before": "公开可见", "permission_after": "公开可见",
                "permission_preserved": True,
                "body_appended": "#心理学小课堂[话题]#",
                "topics_injected": ["心理学小课堂"],
            },
        )
        body = (await c.get(
            "/api/note-components/nc-part-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["status"] == "done"
        assert body["result_status"] == "partially_applied"
        assert body["applied"] == {"collection": False, "activity": True}
        assert body["failed"][0]["component"] == "collection"
        assert body["topics_injected"] == ["心理学小课堂"]
        assert body["permission_preserved"] is True


async def test_poll_permission_alarm_fields(tmp_path, monkeypatch):
    """权限被改动:permission_preserved=false + permission_restored 一起下发(不能只报成功)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件权限号", "uNcPerm", _COOKIES)
        await _seed_job(
            "nc-perm-1", "note_components", acc, "done",
            {
                "status": "done", "applied": {"activity": True}, "failed": [],
                "permission_before": "公开可见", "permission_after": "仅自己可见",
                "permission_preserved": False,
                "permission_restored": {"ok": False, "reason": "note_not_locatable"},
            },
        )
        body = (await c.get(
            "/api/note-components/nc-perm-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["permission_preserved"] is False
        assert body["permission_restored"]["ok"] is False


async def test_poll_error_unknown_and_cross_kind(tmp_path, monkeypatch):
    """一项都没生效 → error + reason;僵死 unknown 标记 → unknown;跨 kind 的 id → 404。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件终态号", "uNcTerm", _COOKIES)
        await _seed_job(
            "nc-err-1", "note_components", acc, "error",
            {"error": "note_components_all_failed: 请求的组件一项都没确认生效"},
        )
        await _seed_job(
            "nc-unknown-1", "note_components", acc, "error",
            {"error": "执行进程中断,任务结果未知(unknown)", "unknown": True},
        )
        await _seed_job("vis-1", "note_visibility", acc, "done", {"status": "done"})

        err = (await c.get(
            "/api/note-components/nc-err-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert err["status"] == "error" and "all_failed" in err["reason"]

        unk = (await c.get(
            "/api/note-components/nc-unknown-1", headers=bearer(ADMIN_KEY)
        )).json()
        assert unk["status"] == "unknown"  # 绝不冒充普通失败诱导重试

        assert (await c.get(
            "/api/note-components/vis-1", headers=bearer(ADMIN_KEY)
        )).status_code == 404
        assert (await c.get(
            "/api/note-components/nope", headers=bearer(ADMIN_KEY)
        )).status_code == 404


async def test_poll_denied_403(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件轮询越权号", "uNcPollDeny", _COOKIES)
        await _seed_job("nc-other-1", "note_components", acc, "done", {"status": "done"})
        other_key = "op-nc-poll-denied-01"
        await make_operator(other_key)
        r = await c.get("/api/note-components/nc-other-1", headers=bearer(other_key))
        assert r.status_code == 403


# ---------------- GET 合集 / 活动列表 ----------------


async def test_collections_uses_latest_ledger_note_as_entry(tmp_path, monkeypatch):
    """没传 note_id → 自动取台账里**最近一篇有 note_id** 的笔记当打开编辑器的落脚点。"""
    _api_role(monkeypatch)
    seen = _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("目录号", "uCat", _COOKIES)
        await _seed_note(acc, "old-note", datetime(2026, 7, 1))
        await _seed_note(acc, "new-note", datetime(2026, 7, 30))

        r = await c.get(f"/api/accounts/{acc}/collections", headers=bearer(ADMIN_KEY))

        assert r.status_code == 200, r.text
        assert r.json()["collections"][0]["id"] == "c1"
        assert r.json()["note_id"] == "new-note"
        assert seen == {"account_id": acc, "note_id": "new-note"}


async def test_collections_explicit_note_id_wins(tmp_path, monkeypatch):
    _api_role(monkeypatch)
    seen = _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("目录指定号", "uCatPin", _COOKIES)
        await _seed_note(acc, "ledger-note", datetime(2026, 7, 1))

        r = await c.get(
            f"/api/accounts/{acc}/collections?note_id=chosen", headers=bearer(ADMIN_KEY)
        )

        assert r.status_code == 200
        assert seen["note_id"] == "chosen"


async def test_collections_without_any_ledger_note_is_400(tmp_path, monkeypatch):
    """台账里一篇带 note_id 的笔记都没有 → 给不出落脚点,400 明说,不去起浏览器。"""
    _api_role(monkeypatch)

    async def boom(*_a, **_kw):
        raise AssertionError("给不出落脚点时不应起浏览器")

    monkeypatch.setattr(note_components, "read_catalog", boom)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("空台账号", "uCatEmpty", _COOKIES)
        r = await c.get(f"/api/accounts/{acc}/collections", headers=bearer(ADMIN_KEY))
        assert r.status_code == 400
        assert "note_id" in r.text


async def test_catalog_browser_failure_is_502_not_empty_list(tmp_path, monkeypatch):
    """浏览器侧失败 → 502 带原因;**绝不静默返回空列表**(那会被读成"这号没有合集")。"""
    _api_role(monkeypatch)
    _stub_catalog(monkeypatch, {"error": "browser_start_failed: camoufox 起不来"})
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("目录失败号", "uCatErr", _COOKIES)
        r = await c.get(
            f"/api/accounts/{acc}/collections?note_id=n1", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 502
        assert "browser_start_failed" in r.text


async def test_activities_default_keywords_exclude_false_positive(tmp_path, monkeypatch):
    """默认心理学词表筛掉「howto穿出自我」(实测假阳性:它其实是穿搭活动)。"""
    _api_role(monkeypatch)
    _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("活动号", "uAct", _COOKIES)

        r = await c.get(
            f"/api/accounts/{acc}/activities?note_id=n1", headers=bearer(ADMIN_KEY)
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert [a["id"] for a in body["activities"]] == ["43561"]
        assert body["activities"][0]["matched_keywords"] == ["心理", "情绪"]
        assert body["total"] == 2  # 筛选前的总数照实给
        assert "心理" in body["keywords"]


async def test_activities_star_disables_filter(tmp_path, monkeypatch):
    """keywords=* → 不过滤,原样全给(调用方自己判定)。"""
    _api_role(monkeypatch)
    _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("活动全量号", "uActAll", _COOKIES)

        body = (await c.get(
            f"/api/accounts/{acc}/activities?note_id=n1&keywords=*",
            headers=bearer(ADMIN_KEY),
        )).json()

        assert len(body["activities"]) == 2 and body["keywords"] == []


async def test_activities_custom_keywords(tmp_path, monkeypatch):
    """自定义词表(逗号分隔)覆盖默认表。"""
    _api_role(monkeypatch)
    _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("活动自定义号", "uActKw", _COOKIES)

        body = (await c.get(
            f"/api/accounts/{acc}/activities?note_id=n1&keywords=穿搭,灵感",
            headers=bearer(ADMIN_KEY),
        )).json()

        assert [a["id"] for a in body["activities"]] == ["999"]


async def test_catalog_rbac(tmp_path, monkeypatch):
    """两个 GET 也走鉴权:无 apikey → 401;越权 → 403;未知账号 → 404。"""
    _api_role(monkeypatch)
    _stub_catalog(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("目录越权号", "uCatDeny", _COOKIES)
        other_key = "op-cat-denied-01"
        await make_operator(other_key)

        assert (await c.get(
            f"/api/accounts/{acc}/collections?note_id=n1"
        )).status_code == 401
        assert (await c.get(
            f"/api/accounts/{acc}/activities?note_id=n1", headers=bearer(other_key)
        )).status_code == 403
        assert (await c.get(
            "/api/accounts/999999/collections?note_id=n1", headers=bearer(ADMIN_KEY)
        )).status_code == 404


# ---------------- 发布链路带三组件 ----------------


async def test_publish_job_carries_components(tmp_path, monkeypatch):
    """POST /api/publish-jobs 的三组件字段落到 publish_jobs 的三列(不传即 NULL)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        from app.models.publish_job import PublishJob

        acc = await seed_account("发布组件号", "uPubNc", _COOKIES)
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "标题", "content": "正文",
                "images": ["https://x/1.png"],
                "collection_id": "c1", "activity_id": "43561",
            },
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
        assert job.collection_id == "c1"
        assert job.activity_id == "43561"
        assert job.quoted_note_id is None  # 不传即不设置


# ---------------- 防漂移(局部子集) ----------------


def test_manifest_covers_new_routes():
    """4 条新路由在 manifest 与实际注册路由里双向全等。"""
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


# ---------------- alembic 迁移 up/down ----------------


def test_publish_jobs_components_migration(monkeypatch, tmp_path):
    """三组件三列随 upgrade head 加上、随 downgrade 退掉,既有列一列不动。"""
    import pathlib
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from app.core.config import settings

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    db_file = str(tmp_path / "mig_nc.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    cfg = Config(str(repo_root / "alembic.ini"))

    def columns() -> set[str]:
        conn = sqlite3.connect(db_file)
        try:
            return {r[1] for r in conn.execute("PRAGMA table_info(publish_jobs)")}
        finally:
            conn.close()

    command.upgrade(cfg, "head")
    after_up = columns()
    assert {"collection_id", "quoted_note_id", "activity_id"} <= after_up
    # 只加列不删不改:既有列全在
    assert {"title", "content", "images_json", "topics_json", "status"} <= after_up

    command.downgrade(cfg, "b8e2f14c7a09")
    after_down = columns()
    assert not ({"collection_id", "quoted_note_id", "activity_id"} & after_down)
    assert {"title", "content", "images_json", "topics_json", "status"} <= after_down
