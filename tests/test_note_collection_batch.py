"""合集批量清理单测(不起真浏览器):节流闸 + 撞墙即停 + 逐篇三态 + REST 契约。

锁的是**风控约束与如实上报**,不是功能便利:

- ``limit`` 只能往小压不能放大;扫描与移出各有各的帽子(只读 vs 一次真提交);
- **一轮一会话**:N 篇共用一个 SyncClient,绝不一篇起一次(会话频次才是被弹墙的直接原因);
- **撞墙即停**:剩余篇目一篇不碰、撞墙那篇不记账,已完成的部分照常留在 notes 里;
- **移出只认 applied is True**:False / None 一律 error(这条产品线的失败是静默的);
- 单轮预算用尽时没轮到的进 ``remaining``,**不许当成失败**;
- ``note_collection_batch`` 非幂等,不得进 ``_IDEMPOTENT_KINDS``。

patch 纪律:打在**被测模块的命名空间**(``svc.SyncClient`` / ``svc.set_note_components`` /
``svc.load_account_cookies``),不是源模块。
"""

import json

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.services import browser_jobs_repo as repo
from app.services import note_collection_batch as svc
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]
_CID = "6a69e9e316fb000000000001"
_CNAME = "咨询师简介"


class _Page:
    """假 page:只提供撞墙判定要用的 url,以及 evaluate 的空实现。"""

    def __init__(self, url="https://creator.xiaohongshu.com/publish/update?id=n1"):
        self.url = url

    def evaluate(self, _js, _arg=None):
        return ""


class _Client:
    """假 SyncClient:记录被建了几次 —— 一轮一会话是硬纪律,建两次就是回归。"""

    built = 0

    def __init__(self, _account_id, _cookies, **_kw):
        type(self).built += 1
        self.page = _Page()

    def start(self):
        return {"success": True}

    def stop(self):
        pass


def _no_gap(monkeypatch):
    """篇间间隔压到 0:测的是编排与判据,不是真等 45-120 秒。"""
    monkeypatch.setattr(svc.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(svc.time, "sleep", lambda _s: None)


def _wire(monkeypatch, *, cookies=None):
    _Client.built = 0
    monkeypatch.setattr(svc, "SyncClient", _Client)

    async def fake_cookies(_account_id):
        return _COOKIES if cookies is None else cookies

    monkeypatch.setattr(svc, "load_account_cookies", fake_cookies)


def _payload(note_ids, **kw):
    return {"collection_id": _CID, "collection_name": _CNAME,
            "note_ids": list(note_ids), **kw}


# ---------------- 非幂等纪律 ----------------


def test_batch_kind_is_not_idempotent():
    """绝不能进 _IDEMPOTENT_KINDS:移出路每篇都是一次全量覆盖提交,重跑就是再覆盖一遍。"""
    assert svc.JOB_KIND not in repo._IDEMPOTENT_KINDS


# ---------------- 单轮上限:只能往小压 ----------------


def test_limit_can_only_shrink_never_grow():
    """调用方的 limit 只能往小压 —— 单轮上限是风控闸,不是默认值。"""
    cap_remove = svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT
    cap_scan = svc.settings.NOTE_COLLECTION_SCAN_ROUND_LIMIT
    assert svc.round_limit_of(999, dry_run=False) == cap_remove
    assert svc.round_limit_of(999, dry_run=True) == cap_scan
    assert svc.round_limit_of(2, dry_run=False) == 2
    assert svc.round_limit_of(None, dry_run=False) == cap_remove
    assert svc.round_limit_of(0, dry_run=False) == 1        # 非法值不放大也不归零
    assert svc.round_limit_of("x", dry_run=False) == cap_remove


def test_remove_cap_is_stricter_than_scan_cap():
    """移出每篇一次真提交,帽子必须比只读扫描严 —— 两条路代价差一个数量级。"""
    assert (svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT
            < svc.settings.NOTE_COLLECTION_SCAN_ROUND_LIMIT)


# ---------------- execute:入参与前置 ----------------


async def test_execute_requires_collection_name(monkeypatch):
    """缺 collection_name 在入口就拦:少了它每篇都会在浏览器层被拒,整轮白开一次会话。"""
    _wire(monkeypatch)
    out = await svc.execute(1, {"collection_id": _CID, "note_ids": ["n1"]})
    assert "collection_name" in out["error"]
    assert _Client.built == 0


async def test_execute_without_cookies_opens_no_browser(monkeypatch):
    _wire(monkeypatch, cookies=[])
    out = await svc.execute(1, _payload(["n1"]))
    assert "cookie" in out["error"]
    assert _Client.built == 0


# ---------------- 扫描路(P1 名单):零点击零提交 ----------------


async def test_scan_reports_membership_and_never_submits(monkeypatch):
    """扫描只读合集区:含目标名 = 在合集里;**一次 set_note_components 都不许调**。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    labels = {"n1": _CNAME, "n2": "别的合集", "n3": None}
    current = {"note_id": None}

    def track(_page, _account_id, note_id):
        current["note_id"] = note_id

    monkeypatch.setattr(svc, "open_update_page", track)
    monkeypatch.setattr(svc, "read_collection_label", lambda _p: labels[current["note_id"]])
    monkeypatch.setattr(svc, "set_note_components", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("扫描路调了提交路径")))

    out = await svc.execute(1, _payload(["n1", "n2", "n3"], dry_run=True))
    assert out["dry_run"] is True
    assert out["handled"] == 3 and out["in_collection"] == 1
    assert [n["in_collection"] for n in out["notes"]] == [True, False, False]
    assert _Client.built == 1, "一轮一会话:N 篇必须共用同一个 camoufox"


async def test_scan_single_note_failure_does_not_stop_the_round(monkeypatch):
    """单篇进不去页面 → 这一篇 error,其余照常做完(单篇异常不阻断整轮)。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)

    def open_page(_page, _account_id, note_id):
        if note_id == "n1":
            raise svc.NoteComponentsError("editor_not_ready: 进不去")

    monkeypatch.setattr(svc, "open_update_page", open_page)
    monkeypatch.setattr(svc, "read_collection_label", lambda _p: _CNAME)

    out = await svc.execute(1, _payload(["n1", "n2"], dry_run=True))
    assert out["failed"] == 1 and out["in_collection"] == 1
    assert out["notes"][0]["reason"].startswith("editor_not_ready:")


async def test_scan_membership_is_exact_match_not_substring(monkeypatch):
    """同族合集名(「科普」/「科普合集」):名单判据必须全等,含包含判据就是假阳性。

    这份名单正是 P2 批量移出的输入 —— 「科普合集」的笔记混进「科普」的名单,下一步就会
    被从**正确的**合集里摘出去。扫描路只读,但它的错会一路喂到破坏性操作。
    """
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    labels = {"n1": "科普合集", "n2": "科普", "n3": "科"}
    current = {"note_id": None}

    def track(_page, _account_id, note_id):
        current["note_id"] = note_id

    monkeypatch.setattr(svc, "open_update_page", track)
    monkeypatch.setattr(svc, "read_collection_label", lambda _p: labels[current["note_id"]])
    monkeypatch.setattr(svc, "set_note_components", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("扫描路调了提交路径")))

    out = await svc.execute(
        1, _payload(["n1", "n2", "n3"], dry_run=True, collection_name="科普")
    )
    assert [n["in_collection"] for n in out["notes"]] == [False, True, False]
    assert out["in_collection"] == 1
    # 判不进名单的那两篇要留下实读文案,人工才能看出是同族名撞的
    assert out["notes"][0]["label"] == "科普合集"


# ---------------- 移出路(P2):三态 + 只认 applied is True ----------------


def _remove_result(applied, *, step_status="done", reason=None):
    return {
        "applied": {"collection_remove": applied},
        "components": {"collection_remove": {"status": step_status, "reason": reason}},
        "submitted": step_status == "done", "permission_preserved": True,
    }


async def test_remove_three_statuses(monkeypatch):
    """removed / skipped / error 三态,与单篇端点一致。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    per_note = {
        "n1": _remove_result(True),
        "n2": _remove_result(True, step_status="skipped", reason="本就不在"),
        "n3": _remove_result(False, step_status="error", reason="collection_not_removed: …"),
    }
    monkeypatch.setattr(
        svc, "set_note_components",
        lambda _p, _a, note_id, **_kw: per_note[note_id],
    )
    out = await svc.execute(1, _payload(["n1", "n2", "n3"], limit=3))
    assert [n["status"] for n in out["notes"]] == ["removed", "skipped", "error"]
    assert (out["removed"], out["skipped"], out["failed"]) == (1, 1, 1)


async def test_remove_unconfirmed_readback_is_error_not_success(monkeypatch):
    """applied=None(没能回读)**不是成功**:这条产品线的失败是静默的,只有 True 算数。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    monkeypatch.setattr(svc, "set_note_components",
                        lambda *_a, **_kw: _remove_result(None))
    out = await svc.execute(1, _payload(["n1"]))
    assert out["notes"][0]["status"] == "error"
    assert out["failed"] == 1


async def test_remove_passes_both_fields_to_single_note_path(monkeypatch):
    """批量必须把 id **和** 名字一起传下去:名字缺了浏览器层就拒绝动手。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    seen = {}

    def fake_set(_page, _account_id, note_id, **kwargs):
        seen.update(kwargs)
        return _remove_result(True)

    monkeypatch.setattr(svc, "set_note_components", fake_set)
    await svc.execute(1, _payload(["n1"]))
    assert seen == {"remove_collection_id": _CID, "remove_collection_name": _CNAME}


async def test_remove_hard_failure_of_one_note_does_not_touch_others(monkeypatch):
    """某篇抛硬错(如未取证的确认弹窗)→ 那一篇 error,其余照常(整轮不因它中止)。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)

    def fake_set(_page, _account_id, note_id, **_kw):
        if note_id == "n1":
            raise svc.NoteComponentsError("collection_remove_unknown_modal: 弹窗原文")
        return _remove_result(True)

    monkeypatch.setattr(svc, "set_note_components", fake_set)
    out = await svc.execute(1, _payload(["n1", "n2"]))
    assert out["notes"][0]["status"] == "error"
    assert "collection_remove_unknown_modal" in out["notes"][0]["reason"]
    assert out["notes"][1]["status"] == "removed"


# ---------------- 单轮上限 / 预算 / remaining ----------------


async def test_round_limit_leaves_the_rest_in_remaining(monkeypatch):
    """超过单轮上限的篇目**不做也不算失败**,原样进 remaining 等下一轮。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    monkeypatch.setattr(svc, "set_note_components",
                        lambda *_a, **_kw: _remove_result(True))
    ids = [f"n{i}" for i in range(svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT + 3)]
    out = await svc.execute(1, _payload(ids))
    assert out["picked"] == svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT
    assert out["remaining"] == ids[svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT:]
    assert "error" not in out, "没轮到 ≠ 失败"


async def test_budget_exhaustion_stops_early_and_reports_remaining(monkeypatch):
    """单轮时间预算用尽 → 就地停手,没做的进 remaining(账号子进程硬超时会强杀整轮)。"""
    _wire(monkeypatch)
    monkeypatch.setattr(svc, "ROUND_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(svc.random, "uniform", lambda _a, _b: 1.0)
    monkeypatch.setattr(svc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(svc, "set_note_components",
                        lambda *_a, **_kw: _remove_result(True))
    out = await svc.execute(1, _payload(["n1", "n2", "n3"], limit=3))
    assert out["handled"] == 1                       # 第一篇不收间隔,做完就没预算了
    assert out["remaining"] == ["n2", "n3"]


# ---------------- 撞墙即停 ----------------


async def test_wall_stops_the_round_and_keeps_finished_work(monkeypatch):
    """撞墙:立刻中止、剩余一篇不碰、**撞墙那篇不记账**、已完成的部分照常留在 notes。"""
    _wire(monkeypatch)
    _no_gap(monkeypatch)
    handled = []

    def fake_set(page, _account_id, note_id, **_kw):
        handled.append(note_id)
        if note_id == "n2":
            page.url = "https://www.xiaohongshu.com/website-login/captcha?redirect=x"
        return _remove_result(True)

    walls = []

    async def fake_handle_wall(account_id, wall):
        walls.append((account_id, wall))

    monkeypatch.setattr(svc, "set_note_components", fake_set)
    monkeypatch.setattr(svc, "_handle_wall", fake_handle_wall)
    out = await svc.execute(7, _payload(["n1", "n2", "n3"], limit=3))

    assert handled == ["n1", "n2"], "撞墙后剩余篇目一篇都不许碰"
    assert [n["note_id"] for n in out["notes"]] == ["n1"], "撞墙那篇不记账"
    assert out["removed"] == 1, "已完成的部分不回滚"
    assert "撞风控墙" in out["error"]
    assert walls and walls[0][0] == 7


# ---------------- REST ----------------


async def test_rest_registers_job_and_reports_planned(tmp_path, monkeypatch):
    """202 回 job_id + planned(本轮打算做几篇);payload 五件套按同名落库,不许漂。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("批量清理号", "uNcb", _COOKIES)
        ids = [f"n{i}" for i in range(9)]
        r = await c.post(
            f"/api/accounts/{acc}/collection-batches",
            json={"collection_id": _CID, "collection_name": _CNAME, "note_ids": ids},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued"
        assert body["planned"] == svc.settings.NOTE_COLLECTION_REMOVE_ROUND_LIMIT

        async with db_module.async_session() as s:
            row = await s.get(BrowserJob, body["job_id"])
        assert row.kind == svc.JOB_KIND
        payload = json.loads(row.payload)
        assert payload == {"collection_id": _CID, "collection_name": _CNAME,
                           "note_ids": ids, "dry_run": False, "limit": None}


async def test_rest_validation_and_authz(tmp_path, monkeypatch):
    """必填项缺失 → 422;越权 → 403;未知账号 → 404;三者都不建任务。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("批量校验号", "uNcbV", _COOKIES)
        url = f"/api/accounts/{acc}/collection-batches"
        ok = {"collection_id": _CID, "collection_name": _CNAME, "note_ids": ["n1"]}
        for bad in (
            {"collection_name": _CNAME, "note_ids": ["n1"]},   # 缺 collection_id
            {"collection_id": _CID, "note_ids": ["n1"]},       # 缺 collection_name
            {"collection_id": _CID, "collection_name": _CNAME, "note_ids": []},
        ):
            assert (await c.post(url, json=bad, headers=bearer(ADMIN_KEY))
                    ).status_code == 422, bad

        other_key = "op-ncb-denied-01"
        await make_operator(other_key)
        assert (await c.post(url, json=ok, headers=bearer(other_key))).status_code == 403
        assert (await c.post("/api/accounts/999999/collection-batches", json=ok,
                             headers=bearer(ADMIN_KEY))).status_code == 404

        async with db_module.async_session() as s:
            from sqlalchemy import func, select

            assert await s.scalar(select(func.count()).select_from(BrowserJob)) == 0


async def test_rest_poll_exposes_details_on_error_too(tmp_path, monkeypatch):
    """**失败时的逐篇原因比成功时更值钱**:error 终态同样下发 notes / remaining。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("批量轮询号", "uNcbP", _COOKIES)
        async with db_module.async_session() as s:
            s.add(BrowserJob(
                id="ncb-1", kind=svc.JOB_KIND, account_id=acc, operator_id=0,
                payload="{}", status="error",
                result=json.dumps({
                    "dry_run": False, "picked": 3, "handled": 1, "removed": 1,
                    "skipped": 0, "failed": 0, "remaining": ["n2", "n3"],
                    "notes": [{"note_id": "n1", "status": "removed"}],
                    "error": "撞风控墙(scan_qr)已中止本轮",
                }, ensure_ascii=False),
            ))
            await s.commit()
        r = await c.get("/api/collection-batches/ncb-1", headers=bearer(ADMIN_KEY))
        body = r.json()
        assert body["status"] == "error"
        assert body["remaining"] == ["n2", "n3"]
        assert body["notes"][0]["note_id"] == "n1"
