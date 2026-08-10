"""代管账号计划的 REST 面:4 个管理端点 + POST /api/publish-jobs 的广播语义。

两条最要紧的不变量在这里钉死:
1. **POST /api/retention-runs 的 dry_run 默认 true,且预演绝不建删除任务** —— 它是控制面
   唯一挡在"不可逆删除"前面的东西,默认值反了就是前端点一下删一批;
2. **传了 account_id 的发布行为一字不变** —— account_id 转可选是纯增,老调用不许受影响。

隔离手法与 test_publish_rest.py 一致(rest_client 跑真实 lifespan;发布用例装假调度器)。
"""

import json
from datetime import datetime

import app.core.db as db_module
from app.core.config import settings
from app.models import BrowserJob, PublishJob, XhsAccount
from app.publish import runtime as runtime_mod
from app.services import operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeScheduler:
    """只记录 submit 的假调度器(与 test_publish_rest.py 一致)。"""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


def _install_fake_scheduler() -> _FakeScheduler:
    fake = _FakeScheduler()
    runtime_mod.set_active_scheduler(fake)
    return fake


async def _set_managed(account_id: int, managed: bool = True, note_cap: int = 100) -> None:
    async with db_module.async_session() as s:
        account = await s.get(XhsAccount, account_id)
        account.managed = managed
        account.note_cap = note_cap
        await s.commit()


async def _grant(op_id: int, *account_ids: int) -> None:
    async with db_module.async_session() as s:
        for acc in account_ids:
            await operator_service.grant_access(s, op_id, acc, op_id)


async def _delete_jobs() -> list[BrowserJob]:
    from sqlalchemy import select

    async with db_module.async_session() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "note_delete")
        )).scalars().all())


def _image_payload(**extra) -> dict:
    body = {"title": "标题", "content": "正文", "images": ["https://cdn/a.png"],
            "topics": []}
    body.update(extra)
    return body


# ---------------- GET /api/managed-accounts ----------------


async def test_managed_accounts_lists_flags_and_counts(tmp_path, monkeypatch):
    """列表给出 managed / note_cap / 台账笔记数;默认值是 false + 100。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        r = await c.get("/api/managed-accounts", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        row = next(a for a in data["accounts"] if a["account_id"] == acc)
        assert row["managed"] is False and row["note_cap"] == 100
        assert row["note_count"] == 0 and row["last_retention_run"] is None
        # 淘汰参数回显:前端要能解释"为什么这篇没被选"
        assert set(data["retention"]) == {
            "enabled", "grace_days", "daily_delete_max", "weights", "check_interval"
        }


async def test_managed_accounts_narrowed_to_visible(tmp_path, monkeypatch):
    """非 admin 只看得到被授权的号(与 GET /api/accounts 同门)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        await seed_account("号二", "u-2", _COOKIES)
        op_key = "op-managed-list"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc1)

        r = await c.get("/api/managed-accounts", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        assert [a["account_id"] for a in r.json()["accounts"]] == [acc1]


# ---------------- PUT /api/accounts/{id}/managed ----------------


async def test_put_managed_sets_flag_and_cap(tmp_path, monkeypatch):
    """改 managed 与 note_cap;空请求体是 no-op 返当前状态。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)

        r = await c.put(f"/api/accounts/{acc}/managed",
                        json={"managed": True, "note_cap": 30},
                        headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json() == {"account_id": acc, "name": "号A",
                            "managed": True, "note_cap": 30}

        # 只改一个字段,另一个保持不变
        r2 = await c.put(f"/api/accounts/{acc}/managed", json={"note_cap": 7},
                         headers=bearer(ADMIN_KEY))
        assert r2.json()["managed"] is True and r2.json()["note_cap"] == 7
        # 空体 no-op
        r3 = await c.put(f"/api/accounts/{acc}/managed", json={},
                         headers=bearer(ADMIN_KEY))
        assert r3.json()["note_cap"] == 7


async def test_put_managed_rejects_bad_cap_and_unknown_fields(tmp_path, monkeypatch):
    """note_cap 越界 → 422;传了白名单外的字段 → 422(不静默吞)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        for body in ({"note_cap": 0}, {"note_cap": 5000}, {"managed": True, "x": 1}):
            r = await c.put(f"/api/accounts/{acc}/managed", json=body,
                            headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, (body, r.text)


async def test_put_managed_requires_access(tmp_path, monkeypatch):
    """无该号授权 → 403;账号不存在 → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        op_key = "op-managed-denied"
        await make_operator(op_key)  # 不授权任何号

        r = await c.put(f"/api/accounts/{acc}/managed", json={"managed": True},
                        headers=bearer(op_key))
        assert r.status_code == 403
        r2 = await c.put("/api/accounts/99999/managed", json={"managed": True},
                         headers=bearer(ADMIN_KEY))
        assert r2.status_code == 404


# ---------------- PUT /api/accounts/{id}/notes/{note_id}/protected ----------------


async def test_put_protected_flips_and_is_idempotent(tmp_path, monkeypatch):
    """标 / 撤保护位都返回**当前态**;重复标同一个值是幂等的 no-op。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _seed_two_notes(acc)

        url = f"/api/accounts/{acc}/notes/nid-差生/protected"
        r = await c.put(url, json={"protected": True}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json() == {"account_id": acc, "note_id": "nid-差生",
                            "title": "差生", "protected": True}

        # 幂等:再标一次还是 true,不报错也不翻转
        assert (await c.put(url, json={"protected": True},
                            headers=bearer(ADMIN_KEY))).json()["protected"] is True
        # 撤回
        assert (await c.put(url, json={"protected": False},
                            headers=bearer(ADMIN_KEY))).json()["protected"] is False


async def test_put_protected_404_when_note_not_owned_by_account(tmp_path, monkeypatch):
    """笔记不属于该号 → 404(哪怕这个 note_id 在别的号名下真实存在)。

    按 (account_id, note_id) 定位而不是只按 note_id:错号也能改的话,一次传错参数就会
    把别人的功能位保护摘掉,而摘掉之后它随时会被下一轮淘汰删走。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        await _seed_notes(acc1, ("号一的笔记", 10))

        r = await c.put(f"/api/accounts/{acc2}/notes/nid-号一的笔记/protected",
                        json={"protected": True}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 404, r.text
        # 断言错误体形状:路由压根没注册时 FastAPI 也回 404,但给的是 {"detail": ...},
        # 那会让这条用例在功能没实现时假绿。本仓的业务 404 一律 {"error": ...}
        assert "error" in r.json(), r.text
        # 台账里压根没有的 note_id 同样 404
        r2 = await c.put(f"/api/accounts/{acc1}/notes/nid-不存在/protected",
                         json={"protected": True}, headers=bearer(ADMIN_KEY))
        assert r2.status_code == 404 and "error" in r2.json()


async def test_put_protected_guards_access_and_body(tmp_path, monkeypatch):
    """无该号授权 → 403;protected 缺失或多传字段 → 422。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _seed_notes(acc, ("一篇", 10))
        url = f"/api/accounts/{acc}/notes/nid-一篇/protected"

        op_key = "op-protected-denied"
        await make_operator(op_key)  # 不授权任何号
        assert (await c.put(url, json={"protected": True},
                            headers=bearer(op_key))).status_code == 403

        for body in ({}, {"protected": True, "x": 1}):
            r = await c.put(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, (body, r.text)


async def test_managed_accounts_reports_protected_count(tmp_path, monkeypatch):
    """列表给出每号的保护位篇数,且保护位**仍计入 note_count**(它占着上限的名额)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _seed_two_notes(acc)

        before = (await c.get("/api/managed-accounts", headers=bearer(ADMIN_KEY))).json()
        row = next(a for a in before["accounts"] if a["account_id"] == acc)
        assert row["protected_count"] == 0 and row["note_count"] == 2

        await c.put(f"/api/accounts/{acc}/notes/nid-差生/protected",
                    json={"protected": True}, headers=bearer(ADMIN_KEY))

        after = (await c.get("/api/managed-accounts", headers=bearer(ADMIN_KEY))).json()
        row = next(a for a in after["accounts"] if a["account_id"] == acc)
        assert row["protected_count"] == 1
        assert row["note_count"] == 2, "保护位被从库存里去掉了 —— 那等于把 note_cap 悄悄放大"


async def test_protected_note_survives_a_real_retention_run(tmp_path, monkeypatch):
    """控制面闭环:标了保护位的那篇,真删轮次一条删除任务都不为它建。

    这是运营真正会走的路径 —— 在控制面点保护、次日淘汰照跑。前面的服务层用例锁选篇口径,
    这条锁的是"从 REST 标进去的那一位真的传到了淘汰"。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)  # 差生(全零)会被选中,优等生不会

        await c.put(f"/api/accounts/{acc}/notes/nid-差生/protected",
                    json={"protected": True}, headers=bearer(ADMIN_KEY))

        r = await c.post("/api/retention-runs",
                         json={"account_id": acc, "dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        run = r.json()["runs"][0]
        # 保护位挡下差生后,超出的那一篇只能由优等生顶上(库存 2 / 上限 1 仍然超着)
        assert [j.account_id for j in await _delete_jobs()] == [acc]
        assert [json.loads(j.payload)["title"] for j in await _delete_jobs()] == ["优等生"]
        guarded = next(d for d in run["details"] if d["title"] == "差生")
        assert guarded["selected"] is False and "保护位" in guarded["skip_reason"]


# ---------------- POST /api/retention-runs ----------------


async def test_retention_run_defaults_to_dry_run_and_creates_no_delete_job(
    tmp_path, monkeypatch
):
    """**默认 dry_run=true:返回将删名单与得分,一条 note_delete 任务都不建。**

    这是控制面唯一挡在不可逆删除前面的东西。默认值一旦反了,前端点一下就是删一批。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)

        r = await c.post("/api/retention-runs", json={"account_id": acc},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["dry_run"] is True
        run = data["runs"][0]
        assert run["note_count"] == 2 and run["cap"] == 1
        assert run["selected_count"] == 1 and run["deleted_count"] == 0
        assert [d["title"] for d in run["details"] if d["selected"]] == ["差生"]

        assert await _delete_jobs() == [], "dry_run 竟然建了删除任务"
        # 预演不落审计行,否则当天的自动轮次会以为"跑过了"而跳过
        assert (await c.get("/api/retention-runs", headers=bearer(ADMIN_KEY))).json()["runs"] == []


async def test_retention_run_real_deletes_and_records(tmp_path, monkeypatch):
    """dry_run=false:建删除任务 + 落审计行,审计流水能读回全量明细。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)

        r = await c.post("/api/retention-runs",
                         json={"account_id": acc, "dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json()["runs"][0]["deleted_count"] == 1

        jobs = await _delete_jobs()
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload)["title"] == "差生"

        listed = (await c.get("/api/retention-runs", headers=bearer(ADMIN_KEY))).json()
        assert len(listed["runs"]) == 1
        run = listed["runs"][0]
        assert run["deleted_count"] == 1 and run["dry_run"] is False
        picked = next(d for d in run["details"] if d["selected"])
        assert picked["job_id"] == jobs[0].id and picked["score"] is not None


async def test_retention_run_rejects_non_managed_account(tmp_path, monkeypatch):
    """指定的号不是代管号 → 422(不静默跑出空结果被误读成"没有超限")。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        r = await c.post("/api/retention-runs", json={"account_id": acc},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 400  # 裸 ValueError → 400 契约
        assert "不是代管账号" in r.json()["error"]


async def test_retention_run_without_account_needs_managed_accounts(tmp_path, monkeypatch):
    """一个代管号都没有时不静默返回空,明说 —— 否则调用方以为"跑过了没得删"。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        await seed_account("号A", "uA", _COOKIES)
        r = await c.post("/api/retention-runs", json={}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 400
        assert "代管账号" in r.json()["error"]


# ---------------- POST /api/retention-runs 的三道真删闸 ----------------
#
# 自动轮次身上那三道闸,手动触发原本一道都不过:kill switch 关着照样能删、当天自动轮次
# 删过了还能再叠一轮、单日封顶按"每次运行"算所以连点三次就是 15 篇。两条路径跑的是同一套
# 选篇代码,却受不同的限速 —— 那不是"手动更灵活",那是绕闸。


async def test_real_run_rejected_while_kill_switch_is_off(tmp_path, monkeypatch):
    """RETENTION_ENABLED=0 时 dry_run=false → 409,一条删除任务都不建。

    kill switch 的语义是"现在谁也别删",它对自动轮次生效却对手动触发不生效的话,这个开关
    等于没有 —— 出事时关掉它,前端点一下照样删。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(settings, "RETENTION_ENABLED", False)
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)

        r = await c.post("/api/retention-runs",
                         json={"account_id": acc, "dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert "RETENTION_ENABLED" in r.json()["detail"]
        assert await _delete_jobs() == []
        # 预演不受 kill switch 影响:它本来就没有副作用
        r2 = await c.post("/api/retention-runs", json={"account_id": acc},
                          headers=bearer(ADMIN_KEY))
        assert r2.status_code == 200, r2.text
        assert r2.json()["runs"][0]["selected_count"] == 1


async def test_real_run_rejected_twice_in_the_same_day(tmp_path, monkeypatch):
    """当天已经真删过一轮 → 再点一次 409(每号每天至多一轮,防重复叠删)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_notes(acc, ("差生", 0), ("中游", 100), ("优等生", 500))

        first = await c.post("/api/retention-runs",
                             json={"account_id": acc, "dry_run": False},
                             headers=bearer(ADMIN_KEY))
        assert first.status_code == 200, first.text
        before = len(await _delete_jobs())

        second = await c.post("/api/retention-runs",
                              json={"account_id": acc, "dry_run": False},
                              headers=bearer(ADMIN_KEY))
        assert second.status_code == 409, second.text
        assert "已经真删过一轮" in second.json()["detail"]
        assert len(await _delete_jobs()) == before, "第二轮又叠了删除任务"


async def test_real_run_quota_counts_todays_deletions(tmp_path, monkeypatch):
    """单日封顶按**当天累计已建的删除数**算,不是按每次运行算。

    按"每次运行"算的话连点三次就是 3×封顶;这里当天已建 2 条、封顶 3,所以本次只剩 1 条额度,
    哪怕超出上限 2 篇、够格的有 3 篇,也只许再删 1 篇。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(settings, "RETENTION_DAILY_DELETE_MAX", 3)
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_notes(acc, ("差生", 0), ("中游", 100), ("优等生", 500))
        await _seed_delete_jobs(acc, 2)  # 当天已经删过 2 篇(别的路径)

        r = await c.post("/api/retention-runs",
                         json={"account_id": acc, "dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        run = r.json()["runs"][0]
        assert run["over_cap"] == 2 and run["eligible_count"] == 3
        assert run["deleted_count"] == 1, "剩余额度只有 1,却删了不止一篇"
        assert len(await _delete_jobs()) == 3  # 预置 2 + 本次 1


async def test_real_run_rejected_when_daily_quota_exhausted(tmp_path, monkeypatch):
    """当天已到单日封顶 → 409,不建任何删除任务。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(settings, "RETENTION_DAILY_DELETE_MAX", 2)
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)
        await _seed_delete_jobs(acc, 2)

        r = await c.post("/api/retention-runs",
                         json={"account_id": acc, "dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert "单日封顶" in r.json()["detail"]
        assert len(await _delete_jobs()) == 2  # 只有预置的那两条


async def test_broadcast_real_run_rejects_whole_batch_if_any_account_blocked(
    tmp_path, monkeypatch
):
    """广播真删只要有一个号不过闸就整批拒,**不部分执行**。

    跑到一半 409 会留下"前几个号删了、后几个没删"的半批状态,事后谁也说不清删到哪了。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(settings, "RETENTION_DAILY_DELETE_MAX", 1)
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        await _set_managed(acc1, True, note_cap=1)
        await _set_managed(acc2, True, note_cap=1)
        await _seed_two_notes(acc1)
        await _seed_two_notes(acc2)
        await _seed_delete_jobs(acc2, 1)  # 只有 2 号到量

        r = await c.post("/api/retention-runs", json={"dry_run": False},
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert str(acc2) in r.json()["detail"]
        assert len(await _delete_jobs()) == 1, "1 号被放行删了,留下半批状态"


# ---------------- 响应契约与 manifest 字段级对齐 ----------------

# manifest 声明的 runs[] 字段集合(POST /api/retention-runs 的 returns)
_RUN_FIELDS = {
    "account_id", "run_date", "note_count", "cap", "over_cap", "eligible_count",
    "selected_count", "deleted_count", "dry_run", "details",
}


async def test_retention_run_response_matches_manifest_fields(tmp_path, monkeypatch):
    """预演与真删两条路径的 runs[] 字段集合相同,且逐个在 manifest 的 returns 里声明过。

    端点级防漂移测试(test_manifest.py)只比对"端点有没有写进 manifest",**字段级漂移它一条
    都发现不了**:真删路径曾多返回一个 manifest 里没有的 recorded,预演路径曾少返回一个
    manifest 里写了的 run_date —— 照 manifest 解析的 agent 消费方会在预演路径上 KeyError。
    """
    from app.http.managed_accounts_rest import MANIFEST_ENTRIES

    entry = next(e for e in MANIFEST_ENTRIES
                 if (e["method"], e["path"]) == ("POST", "/api/retention-runs"))
    for field in _RUN_FIELDS:
        assert field in entry["returns"], f"manifest 的 returns 没声明 {field}"
    assert "recorded" not in entry["returns"]

    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _set_managed(acc, True, note_cap=1)
        await _seed_two_notes(acc)

        preview = await c.post("/api/retention-runs", json={"account_id": acc},
                               headers=bearer(ADMIN_KEY))
        assert preview.status_code == 200, preview.text
        assert set(preview.json()["runs"][0]) == _RUN_FIELDS

        real = await c.post("/api/retention-runs",
                            json={"account_id": acc, "dry_run": False},
                            headers=bearer(ADMIN_KEY))
        assert real.status_code == 200, real.text
        assert set(real.json()["runs"][0]) == _RUN_FIELDS


async def test_managed_accounts_note_count_excludes_deleted(tmp_path, monkeypatch):
    """note_count 不数已被淘汰删掉的笔记(deleted_at 非空的行退出库存)。"""
    from sqlalchemy import select

    from app.models import PublishedNote

    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        await _seed_two_notes(acc)

        before = await c.get("/api/managed-accounts", headers=bearer(ADMIN_KEY))
        assert next(a for a in before.json()["accounts"]
                    if a["account_id"] == acc)["note_count"] == 2

        async with db_module.async_session() as s:
            row = (await s.execute(
                select(PublishedNote).where(PublishedNote.title == "差生")
            )).scalars().first()
            row.deleted_at = datetime.utcnow()
            await s.commit()

        after = await c.get("/api/managed-accounts", headers=bearer(ADMIN_KEY))
        assert next(a for a in after.json()["accounts"]
                    if a["account_id"] == acc)["note_count"] == 1


async def test_retention_runs_list_requires_access(tmp_path, monkeypatch):
    """指定 account_id 越权 → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        op_key = "op-runs-denied"
        await make_operator(op_key)
        r = await c.get(f"/api/retention-runs?account_id={acc}", headers=bearer(op_key))
        assert r.status_code == 403


async def _seed_notes(account_id: int, *specs: tuple[str, int]) -> None:
    """按 (标题, 浏览量) 建台账笔记 + 能 join 上的指标行;都过了宽限期。"""
    from datetime import datetime, timedelta

    from app.models import NoteMetric, PublishedNote

    published_at = datetime.utcnow() - timedelta(days=60)
    beijing = published_at + timedelta(hours=8)
    async with db_module.async_session() as s:
        for title, views in specs:
            s.add(PublishedNote(
                account_id=account_id, note_id=f"nid-{title}", title=title,
                published_at=published_at, platform_published_at=published_at,
                first_seen_at=published_at, permission_code=0, sync_status="orphan",
            ))
            s.add(NoteMetric(
                account_id=account_id, title=title,
                publish_time=beijing.strftime("%Y年%m月%d日%H时%M分%S秒"),
                views=views, likes=views // 10,
            ))
        await s.commit()


async def _seed_two_notes(account_id: int) -> None:
    """一篇全零的「差生」+ 一篇有量的「优等生」,都过了宽限期且能 join 上指标。"""
    await _seed_notes(account_id, ("差生", 0), ("优等生", 500))


async def _seed_delete_jobs(account_id: int, count: int) -> None:
    """当天已经建过的 note_delete 任务(模拟别的路径删过笔记),用来吃掉单日额度。"""
    import uuid

    async with db_module.async_session() as s:
        for _ in range(count):
            s.add(BrowserJob(
                id=uuid.uuid4().hex, kind="note_delete", account_id=account_id,
                operator_id=0, payload=json.dumps({"title": "旧的", "count": 1}),
                status="done",
            ))
        await s.commit()


# ---------------- POST /api/publish-jobs 广播 ----------------


async def test_publish_broadcasts_to_all_managed_accounts(tmp_path, monkeypatch):
    """不传 account_id → 每个代管号各一条独立任务,响应给逐号 job_id。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        acc3 = await seed_account("水军号", "u-3", _COOKIES)
        await _set_managed(acc1)
        await _set_managed(acc2)

        r = await c.post("/api/publish-jobs", json=_image_payload(),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        data = r.json()
        assert data["broadcast"] is True
        assert [j["account_id"] for j in data["jobs"]] == [acc1, acc2]
        assert acc3 not in [j["account_id"] for j in data["jobs"]]
        # 逐条 nudge:每条都是独立任务,各自入队
        assert fake.submitted == [j["job_id"] for j in data["jobs"]]

        async with db_module.async_session() as s:
            for j in data["jobs"]:
                job = await s.get(PublishJob, j["job_id"])
                assert job.status == "pending" and job.account_id == j["account_id"]
                assert json.loads(job.images_json) == ["https://cdn/a.png"]


async def test_publish_with_account_id_unchanged(tmp_path, monkeypatch):
    """传了 account_id → 老响应形状一字不变({job_id, status}),不带 broadcast 键。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        await _set_managed(acc1)
        await _set_managed(acc2)

        r = await c.post("/api/publish-jobs", json=_image_payload(account_id=acc2),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert set(r.json()) == {"job_id", "status"}
        assert r.json()["status"] == "pending"

        from sqlalchemy import select

        async with db_module.async_session() as s:
            rows = list((await s.execute(select(PublishJob))).scalars().all())
        assert [row.account_id for row in rows] == [acc2], "特指账号竟然也广播了"


async def test_publish_broadcast_without_managed_accounts_is_422(tmp_path, monkeypatch):
    """一个代管号都没有 → 422 明说,**绝不静默发 0 条**(那会让调用方以为发出去了)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        await seed_account("水军号", "u-1", _COOKIES)

        r = await c.post("/api/publish-jobs", json=_image_payload(),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert "代管账号" in r.text

        from sqlalchemy import select

        async with db_module.async_session() as s:
            assert (await s.execute(select(PublishJob))).scalars().all() == []


async def test_publish_broadcast_narrowed_to_authorized_accounts(tmp_path, monkeypatch):
    """广播只发给 caller 有权的代管号;一个都没授权 → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        await _set_managed(acc1)
        await _set_managed(acc2)

        op_key = "op-broadcast-partial"
        op_id = await make_operator(op_key)
        await _grant(op_id, acc2)
        r = await c.post("/api/publish-jobs", json=_image_payload(),
                         headers=bearer(op_key))
        assert r.status_code == 202, r.text
        assert [j["account_id"] for j in r.json()["jobs"]] == [acc2]

        blind_key = "op-broadcast-none"
        await make_operator(blind_key)
        r2 = await c.post("/api/publish-jobs", json=_image_payload(),
                          headers=bearer(blind_key))
        assert r2.status_code == 403, r2.text


async def test_publish_broadcast_rejects_explicit_quoted_note_id(tmp_path, monkeypatch):
    """广播 + 显式 quoted_note_id → 422,一条发布任务都不建。

    **笔记 id 归属单个账号**:同一个 quoted_note_id 广播给 N 个号,最多只有它的主人引得上,
    其余 N-1 个号是「引用悄悄没生效、笔记照常发出去」的静默失败 —— 而引用失败只告警不阻断
    发布,事后没人会发现。跨号引用只能靠 related_counselor 让每号各自推导本号的笔记。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        acc2 = await seed_account("号二", "u-2", _COOKIES)
        await _set_managed(acc1)
        await _set_managed(acc2)

        r = await c.post("/api/publish-jobs",
                         json=_image_payload(quoted_note_id="abc123"),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert "广播不支持显式 quoted_note_id" in r.text
        assert "related_counselor" in r.text

        from sqlalchemy import select

        async with db_module.async_session() as s:
            assert (await s.execute(select(PublishJob))).scalars().all() == []


async def test_publish_with_account_id_still_accepts_quoted_note_id(tmp_path, monkeypatch):
    """特指账号 + quoted_note_id 一字不变:拒的只是广播那条路,不是这个字段。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await seed_account("号一", "u-1", _COOKIES)

        r = await c.post("/api/publish-jobs",
                         json=_image_payload(account_id=acc, quoted_note_id="abc123"),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "abc123"


async def test_publish_broadcast_still_validates_images_first(tmp_path, monkeypatch):
    """形状校验在展开广播之后、建任务之前:图片越界 → 400,一条任务都不建。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc1 = await seed_account("号一", "u-1", _COOKIES)
        await _set_managed(acc1)

        r = await c.post("/api/publish-jobs",
                         json=_image_payload(images=[f"u{i}" for i in range(19)]),
                         headers=bearer(ADMIN_KEY))
        assert r.status_code == 400, r.text

        from sqlalchemy import select

        async with db_module.async_session() as s:
            assert (await s.execute(select(PublishJob))).scalars().all() == []
