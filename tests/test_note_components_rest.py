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
        # related_counselor 是推导引用笔记的由来,没传就是 None;显式给了 quoted_note_id
        # 时推导根本不跑,登记的就是调用方给的那个 id。
        assert payload == {
            "note_id": "6a4ce556", "collection_id": "c1", "collection_name": None,
            "remove_collection_id": None, "remove_collection_name": None,
            "quoted_note_id": "n-quote", "activity_id": "43561",
            "related_counselor": None, "set_original_declaration": False,
            "cover": None,
        }

        poll = await c.get(
            f"/api/note-components/{body['job_id']}", headers=bearer(op_key)
        )
        # queue 段随排队态一起下发(排队可见性):这里只钉形状,细节见 tests/test_queue_status*.py
        body_poll = poll.json()
        assert body_poll["status"] == "queued"
        assert body_poll["queue"]["position"] == 1


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


async def test_poll_error_still_exposes_per_component_reasons(tmp_path, monkeypatch):
    """任务以 error 收尾时,逐项失败原因**必须照样下发**。

    2026-08-03 运营上报:三组件"一项都没设上"恰恰以 error 收尾,而此前只有
    status=="done" 才下发 failed/applied —— 服务层明明写好了原因
    (quote_card_title_mismatch: 第 6 张卡与接口不一致),调用方一个字都拿不到,
    只能靠换外部变量盲测,实际连测三次全无信息。

    **失败时的逐项原因比成功时更值钱**,藏起来是这个接口最不该有的行为。
    """
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件失败号", "uNcErr", _COOKIES)
        await _seed_job(
            "nc-err-1", "note_components", acc, "error",
            {
                "status": "failed",
                "applied": {"quote": None},
                "failed": [{
                    "component": "quote",
                    "reason": "quote_card_title_mismatch: 第 6 张卡文案里没有平台标题,"
                              "卡片顺序与接口不一致,拒绝盲选",
                }],
                "submitted": False,
            },
        )
        body = (await c.get(
            "/api/note-components/nc-err-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["status"] == "error"          # 生命周期状态不变
        assert body["result_status"] == "failed"  # 内层结果也要给
        assert body["applied"] == {"quote": None}
        assert "quote_card_title_mismatch" in body["failed"][0]["reason"]


async def test_poll_running_has_no_result_keys(tmp_path, monkeypatch):
    """还在跑的时候不下发结果键(那时 result 本就没内容,给了反而像"已经有结论")。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("组件在跑号", "uNcRun", _COOKIES)
        await _seed_job("nc-run-1", "note_components", acc, "running", {})

        body = (await c.get(
            "/api/note-components/nc-run-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["status"] == "running"
        assert "failed" not in body and "result_status" not in body


# ---------------- 引用推导触发时机(2026-08-04 收口:仅显式意图才推导) ----------------
#
# 背景(运营 08-04 清单 P1-2):只传 collection_id 也会触发标题推导(规则 3),给推介
# 笔记顺带挂上跨账号的接待员引用,拖着整单一起失败。裁决(fable):编辑路径语义是
# "对已发布笔记只做被请求的事",推导仅在显式引用意图(related_counselor 非空,或
# quoted_note_id 显式给出)时进行;发布路径("按业务规则产出完整笔记")保持隐式推导不变。

from app.services import counselor_quote as _cq


def _spy_derive(monkeypatch, result=None):
    """打桩 resolve_for_published_note,记录是否被调用。"""
    seen = {"called": False}

    async def fake(session, account_id, note_id, related_counselor):
        seen["called"] = True
        return result

    monkeypatch.setattr(_cq, "resolve_for_published_note", fake)
    return seen


async def test_collection_only_never_derives_quote(tmp_path, monkeypatch):
    """只传 collection_id → 完全不进推导,payload 里 quoted_note_id=None。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        spy = _spy_derive(monkeypatch, result="不该被用到")
        acc = await seed_account("收口甲号", "uNcScope1", _COOKIES)
        op_key = "op-nc-scope-01"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "6a4c0001", "collection_id": "c1"},
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        assert spy["called"] is False

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, r.json()["job_id"])
        assert json.loads(row.payload)["quoted_note_id"] is None


async def test_related_counselor_still_triggers_derivation(tmp_path, monkeypatch):
    """显式传 related_counselor → 推导照跑,产物落 payload。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        spy = _spy_derive(monkeypatch, result="n-derived")
        acc = await seed_account("收口乙号", "uNcScope2", _COOKIES)
        op_key = "op-nc-scope-02"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "6a4c0002", "related_counselor": "李宇"},
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        assert spy["called"] is True

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, r.json()["job_id"])
        assert json.loads(row.payload)["quoted_note_id"] == "n-derived"


async def test_explicit_quoted_note_id_skips_derivation(tmp_path, monkeypatch):
    """显式 quoted_note_id → 推导不跑(现状回归锁,收口后依然成立)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        spy = _spy_derive(monkeypatch, result="不该被用到")
        acc = await seed_account("收口丙号", "uNcScope3", _COOKIES)
        op_key = "op-nc-scope-03"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "6a4c0003", "quoted_note_id": "n-explicit"},
            headers=bearer(op_key),
        )
        assert r.status_code == 202, r.text
        assert spy["called"] is False

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, r.json()["job_id"])
        assert json.loads(row.payload)["quoted_note_id"] == "n-explicit"


async def test_related_counselor_underivable_still_422(tmp_path, monkeypatch):
    """只传 related_counselor 且推不出 → 422 语义不变(收口后这一关仍可达)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _spy_derive(monkeypatch, result=None)
        acc = await seed_account("收口丁号", "uNcScope4", _COOKIES)
        op_key = "op-nc-scope-04"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "6a4c0004", "related_counselor": "查无此人"},
            headers=bearer(op_key),
        )
        assert r.status_code == 422
        assert "推导不出" in r.text


# ---------------- 移出合集(P0,2026-08-07)----------------


async def test_remove_collection_is_exclusive_with_join(tmp_path, monkeypatch):
    """加入与移出**同一次请求同时给 → 422**:语义相反,换合集的页面交互尚未取证。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("移出互斥号", "uNcRmEx", _COOKIES)
        url = f"/api/accounts/{acc}/note-components"
        r = await c.post(url, json={
            "note_id": "n1", "collection_id": "c1", "remove_collection_id": "c2",
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert "remove_collection_id" in r.text

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            assert await s.scalar(select(func.count()).select_from(BrowserJob)) == 0


async def test_remove_collection_name_alone_is_not_a_component(tmp_path, monkeypatch):
    """只给 remove_collection_name(没有 id)不构成组件请求 → 422(与 collection_name 同款)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("移出裸名号", "uNcRmNm", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-components", json={
            "note_id": "n1", "remove_collection_name": "咨询师简介",
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text


async def test_remove_collection_reaches_payload_without_drift(tmp_path, monkeypatch):
    """两个新字段按**同名**落进 payload:字段级漂移没有测试兜底,这里就是那道兜底。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("移出登记号", "uNcRmReg", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-components", json={
            "note_id": "n-target",
            "remove_collection_id": "6a69e9e316fb000000000001",
            "remove_collection_name": "咨询师简介",
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

        async with db_module.async_session() as s:
            from sqlalchemy import select

            row = await s.scalar(select(BrowserJob))
        payload = json.loads(row.payload)
        assert row.kind == "note_components"
        assert payload["remove_collection_id"] == "6a69e9e316fb000000000001"
        assert payload["remove_collection_name"] == "咨询师简介"
        assert payload["collection_id"] is None


def test_service_passes_remove_fields_to_browser_layer(monkeypatch):
    """服务层 → 浏览器层的**参数名**不许漂:``_apply_sync`` 收到什么就得原样传下去。"""
    seen = {}

    def fake_set(page, account_id, note_id, **kwargs):
        seen.update(kwargs)
        return {"status": "done", "applied": {}}

    class _Client:
        def __init__(self, *_a, **_kw):
            self.page = object()

        def start(self):
            return {"success": True}

        def stop(self):
            pass

    monkeypatch.setattr(note_components, "set_note_components", fake_set)
    monkeypatch.setattr(note_components, "SyncClient", _Client)
    note_components._apply_sync(
        1, _COOKIES, "n1",
        {"collection_id": None, "remove_collection_id": "c1",
         "quoted_note_id": None, "activity_id": None},
        None, None, "咨询师简介",
    )
    assert seen["remove_collection_id"] == "c1"
    assert seen["remove_collection_name"] == "咨询师简介"


# ---------------- 补录原创声明(2026-08-08)----------------


async def test_set_original_declaration_alone_is_a_valid_request(tmp_path, monkeypatch):
    """只给 set_original_declaration=true 就构成一次合法请求(不必再凑一个组件)。

    运营的 49 篇补录就是这个形态:除了补声明什么都不改。
    """
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("补声明号", "uNcOrigOnly", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-components", json={
            "note_id": "6a707e9f0000000008012876", "set_original_declaration": True,
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

        async with db_module.async_session() as s:
            from sqlalchemy import select

            row = await s.scalar(select(BrowserJob))
        payload = json.loads(row.payload)
        assert row.kind == "note_components"
        assert payload["set_original_declaration"] is True
        # 只补声明就不该顺带挂上任何组件
        assert payload["collection_id"] is None and payload["quoted_note_id"] is None


async def test_set_original_declaration_false_is_rejected(tmp_path, monkeypatch):
    """传 false → 422:本期只支持**开启**,静默忽略等于假成功。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("关声明号", "uNcOrigOff", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-components", json={
            "note_id": "n1", "set_original_declaration": False,
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert "只支持 true" in r.text

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            assert await s.scalar(select(func.count()).select_from(BrowserJob)) == 0


async def test_set_original_declaration_false_rejected_even_with_other_components(
    tmp_path, monkeypatch
):
    """搭着别的组件传 false 也照样 422 —— 不许"反正还有别的事做"就把它咽下去。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("关声明混号", "uNcOrigOffMix", _COOKIES)
        r = await c.post(f"/api/accounts/{acc}/note-components", json={
            "note_id": "n1", "collection_id": "c1",
            "set_original_declaration": False,
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text


def test_service_passes_original_declaration_to_browser_layer(monkeypatch):
    """服务层 → 浏览器层的参数名不许漂(与移出字段同款兜底)。"""
    seen = {}

    def fake_set(page, account_id, note_id, **kwargs):
        seen.update(kwargs)
        return {"status": "done", "applied": {}}

    class _Client:
        def __init__(self, *_a, **_kw):
            self.page = object()

        def start(self):
            return {"success": True}

        def stop(self):
            pass

    monkeypatch.setattr(note_components, "set_note_components", fake_set)
    monkeypatch.setattr(note_components, "SyncClient", _Client)
    note_components._apply_sync(
        1, _COOKIES, "n1",
        {"collection_id": None, "remove_collection_id": None,
         "quoted_note_id": None, "activity_id": None},
        None, None, None, True,
    )
    assert seen["set_original_declaration"] is True


async def test_execute_treats_declaration_only_payload_as_a_real_request(monkeypatch):
    """execute 的"至少给一个"判定要认这个布尔字段,否则纯补录 payload 会被当成空请求。"""
    seen = {}

    async def fake_load(_account_id):
        return _COOKIES

    def fake_apply(*args):
        seen["set_original_declaration"] = args[7]
        return {"status": "done", "applied": {"original_declaration": True}}

    monkeypatch.setattr(note_components, "load_account_cookies", fake_load)
    monkeypatch.setattr(note_components, "_apply_sync", fake_apply)

    async def fake_sync(_account_id, _note_id, result):
        return result

    monkeypatch.setattr(note_components, "_sync_ledger", fake_sync)
    out = await note_components.execute(
        1, {"note_id": "n1", "set_original_declaration": True}
    )
    assert "error" not in out, out
    assert seen["set_original_declaration"] is True


def test_manifest_params_cover_every_request_field():
    """**字段级防漂移**:请求体每一个字段都必须在 manifest 的 params 里有一条说明。

    端点级防漂移(tests/test_manifest.py)只管"端点在不在",加了字段忘了写 manifest
    它一声不吭 —— 而 manifest 就是调用方(skill / 运营)唯一的接口说明书,字段不在
    上面 = 这个能力对外不存在。所以这道门在这里补上。
    """
    from app.http.note_components_rest import MANIFEST_ENTRIES, NoteComponentsRequest

    entry = next(
        e for e in MANIFEST_ENTRIES
        if e["method"] == "POST" and e["path"].endswith("/note-components")
    )
    missing = set(NoteComponentsRequest.model_fields) - set(entry["params"])
    assert not missing, f"这些请求字段没写进 manifest params:{sorted(missing)}"


def test_manifest_states_the_original_declaration_contract():
    """manifest 必须写清补声明的三条语义:只支持开启 / 幂等 skipped / 零改动不提交。

    运营是照 manifest 用的:少写一条"幂等",他们就不敢批量重跑;少写一条"零改动不提交",
    他们会以为每次重跑都在真发布。
    """
    from app.http.note_components_rest import MANIFEST_ENTRIES

    entry = next(
        e for e in MANIFEST_ENTRIES
        if e["method"] == "POST" and e["path"].endswith("/note-components")
    )
    text = entry["params"]["set_original_declaration"] + entry["notes"] + entry["errors"]
    assert "只支持开启" in text
    assert "skipped" in text and "零点击" in text
    assert "一次发布都不点" in text
    assert "422" in entry["errors"] and "set_original_declaration" in entry["errors"]
    # 批量补录的节奏提示(会话帽 + 看到 queued 别重试)
    assert "每小时会话帽" in entry["notes"]
    assert "别重试" in entry["notes"]
    # 与发布链共用同一个函数这件事,必须写在对外说明里(运营点名要的答复)
    assert "apply_original_declaration" in entry["notes"]


def test_polling_manifest_lists_original_declaration_in_applied():
    """轮询端点的 returns 要列出 applied.original_declaration,否则调用方不知道去读它。"""
    from app.http.note_components_rest import MANIFEST_ENTRIES

    entry = next(
        e for e in MANIFEST_ENTRIES
        if e["method"] == "GET" and "{job_id}" in e["path"]
    )
    assert "original_declaration" in entry["returns"]
