"""每用户风格档案测试:exists 语义 / 版本自增留档 / 乐观锁 409 / 回退造新版 / 越权隔离 /
丢字段告知 / 管理员默认档案维护 / 历史分页 / base_version 真源。

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。REST 用 rest_helpers
的隔离库 client(零网络);服务层级联用 conftest 的 db fixture。

钉死三条不可动摇的语义:
- exists:false 时必须是管理员默认档案(skill 侧据此换一句话说)。
- 回退产生**新版本**,中间版本仍在历史里可取(指针式回退会让它们无处可寻)。
- density 五个中文 key 原样往返,一个字符不变(防有人加 key 规范化)。
"""

import copy
import importlib.util
import pathlib

import app.core.db as db_module
from sqlalchemy import func, select

from app.models.style_profile import StyleProfile, StyleProfileVersion
from app.services import style_profile as svc
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# density 的五个中文 key:skill 侧 v1.37.0 定死的跨端接口,逐字写死在此当断言基准
_DENSITY_KEYS = ["信息密度档位", "每页文字量", "每页信息点", "版式档", "运营原话"]


def _profile(tag: str = "A") -> dict:
    """造一份最小但含中文 density 五键的档案(标签用于区分版本内容)。"""
    return {
        "visual": {"palette": [{"name": f"色{tag}", "hex": "#111111"}], "text_color": "#222"},
        "density": {
            "信息密度档位": f"档位{tag}",
            "每页文字量": "100–200 字",
            "每页信息点": "3–5 个",
            "版式档": "留白",
            "运营原话": "别写太满",
        },
        "tone": {"person": "第一人称"},
        "structure": {"ending": f"小动作{tag}"},
    }


# ---------------- ① 无档案:回落管理员默认 + exists:false ----------------


async def test_get_without_profile_returns_admin_default(tmp_path, monkeypatch):
    """无个人档案 → exists:false + source:admin_default + 管理员那套内容(含中文 density 键)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-1"
        await make_operator(key)
        r = await c.get("/api/style-profile", headers=bearer(key))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["exists"] is False
        assert data["source"] == "admin_default"
        assert data["profile"] == svc.ADMIN_DEFAULT_PROFILE
        # 管理员那套的标志物:莫兰迪三色 + 人物卡
        assert data["profile"]["visual"]["palette"][0]["hex"] == "#A8B5C4"
        assert "燕麦色针织衫" in data["profile"]["visual"]["character_card"]
        assert list(data["profile"]["density"].keys()) == _DENSITY_KEYS


async def test_admin_default_row_wins_over_constant(tmp_path, monkeypatch):
    """库里有 operator_id IS NULL 的 seed 行时,回落读它(迁移 seed 的生效路径)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-seedrow"
        await make_operator(key)
        async with db_module.async_session() as s:
            s.add(StyleProfile(
                operator_id=None, version=0, profile=_profile("SEED"),
                source="admin_default", note="seed",
            ))
            await s.commit()
        r = await c.get("/api/style-profile", headers=bearer(key))
        assert r.json()["profile"] == _profile("SEED")
        assert r.json()["exists"] is False  # 仍是"别人的档案",不能说成他自己的


# ---------------- ② ③ 首次 PUT 建 v1;再 PUT 递增且留快照 ----------------


async def test_first_put_creates_version_1_and_flips_exists(tmp_path, monkeypatch):
    """首次 PUT(base_version=0)建 version 1;GET 随即 exists:true 且内容是他自己的。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-2"
        await make_operator(key)
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"),
            "source": "inherited_admin", "note": "初始沿用管理员再微调",
        })
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 1
        assert r.json()["exists"] is True

        got = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert got["exists"] is True
        assert got["version"] == 1
        assert got["source"] == "inherited_admin"
        assert got["note"] == "初始沿用管理员再微调"
        assert got["profile"] == _profile("A")
        assert got["updated_at"]


async def test_second_put_increments_and_appends_snapshot(tmp_path, monkeypatch):
    """再次 PUT → version 递增,且版本表多一条完整快照(每次改动都留档)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-3"
        op_id = await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("B"),
            "source": "reference_sample", "note": "按参考样本 8 张实测更新"})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 2

        async with db_module.async_session() as s:
            count = (await s.execute(
                select(func.count()).select_from(StyleProfileVersion)
                .where(StyleProfileVersion.operator_id == op_id)
            )).scalar_one()
            assert count == 2  # v1 v2 各一条快照
            cur = (await s.execute(
                select(func.count()).select_from(StyleProfile)
                .where(StyleProfile.operator_id == op_id)
            )).scalar_one()
            assert cur == 1  # 当前档案永远只有一行


# ---------------- ④ 乐观锁:base_version 不符 → 409 且带当前 version ----------------


async def test_put_stale_base_version_conflicts(tmp_path, monkeypatch):
    """模拟两个会话:都读到 v1,A 写成功 v2,B 拿旧 base_version 写 → 409 + current_version=2。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-4"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        # 会话 A 与会话 B 都读到 version 1
        base = (await c.get("/api/style-profile", headers=bearer(key))).json()["version"]
        assert base == 1
        ok = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": base, "profile": _profile("B"), "source": "manual"})
        assert ok.json()["version"] == 2

        stale = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": base, "profile": _profile("C"), "source": "manual"})
        assert stale.status_code == 409, stale.text
        detail = stale.json()["detail"]
        assert detail["current_version"] == 2  # skill 侧据此提示"v1 → v2,请重新读取"
        assert detail["updated_at"]
        # 冲突的写没有落地:当前内容仍是 B
        assert (await c.get("/api/style-profile", headers=bearer(key))).json()[
            "profile"] == _profile("B")


# ---------------- ⑤ ⑥ 历史列表(轻)与取某版(全) ----------------


async def test_versions_list_is_desc_and_lightweight(tmp_path, monkeypatch):
    """历史列表倒序,且**不含 profile 全文**(列表要轻)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-5"
        op_id = await make_operator(key)
        for i, tag in enumerate("ABC"):
            await c.put("/api/style-profile", headers=bearer(key), json={
                "base_version": i, "profile": _profile(tag),
                "source": "manual", "note": f"第{i + 1}次"})
        r = await c.get("/api/style-profile/versions", headers=bearer(key))
        assert r.status_code == 200, r.text
        versions = r.json()["versions"]
        assert [v["version"] for v in versions] == [3, 2, 1]
        for v in versions:
            assert "profile" not in v
            assert set(v) == {"version", "source", "note", "created_at", "created_by"}
            assert v["created_by"] == op_id


async def test_get_single_version_returns_full_profile(tmp_path, monkeypatch):
    """取某版含完整 profile(回退前预览);不存在的版本 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-6"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("B"), "source": "manual"})

        r = await c.get("/api/style-profile/versions/1", headers=bearer(key))
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 1
        assert r.json()["profile"] == _profile("A")

        assert (await c.get("/api/style-profile/versions/9",
                            headers=bearer(key))).status_code == 404


# ---------------- ⑦ ⑧ 回退 = 造新版本(要害) ----------------


async def test_rollback_creates_new_version_and_keeps_middle_versions(tmp_path, monkeypatch):
    """v1→v2→v3 后回退到 v1:产生 v4(内容==v1),且 v2/v3 仍在历史里可取。

    这是需求方点名的要害:指针式回退会让中间版本无处可寻,"回退后又后悔"就无解。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-7"
        await make_operator(key)
        for i, tag in enumerate("ABC"):
            await c.put("/api/style-profile", headers=bearer(key), json={
                "base_version": i, "profile": _profile(tag), "source": "manual"})

        r = await c.post("/api/style-profile/rollback", headers=bearer(key),
                         json={"to_version": 1, "base_version": 3})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 4
        assert r.json()["source"] == "rollback"
        assert "v1" in r.json()["note"]

        cur = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert cur["version"] == 4
        assert cur["profile"] == _profile("A")  # 内容等于被回退到的那一版

        # 中间版本没被抹掉:列表里在,内容取得到("回退后又后悔"还能回去)
        versions = (await c.get("/api/style-profile/versions",
                                headers=bearer(key))).json()["versions"]
        assert [v["version"] for v in versions] == [4, 3, 2, 1]
        for ver, tag in ((2, "B"), (3, "C")):
            got = await c.get(f"/api/style-profile/versions/{ver}", headers=bearer(key))
            assert got.status_code == 200
            assert got.json()["profile"] == _profile(tag)

        # 再回退到 v3(后悔了):产生 v5,内容回到 C
        again = await c.post("/api/style-profile/rollback", headers=bearer(key),
                             json={"to_version": 3, "base_version": 4})
        assert again.json()["version"] == 5
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["profile"] == _profile("C")


async def test_rollback_respects_optimistic_lock(tmp_path, monkeypatch):
    """回退同样受 base_version 乐观锁保护:旧 base_version → 409,不产生新版本。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-8"
        await make_operator(key)
        for i, tag in enumerate("AB"):
            await c.put("/api/style-profile", headers=bearer(key), json={
                "base_version": i, "profile": _profile(tag), "source": "manual"})

        r = await c.post("/api/style-profile/rollback", headers=bearer(key),
                         json={"to_version": 1, "base_version": 1})  # 当前已是 2
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["current_version"] == 2
        versions = (await c.get("/api/style-profile/versions",
                                headers=bearer(key))).json()["versions"]
        assert [v["version"] for v in versions] == [2, 1]  # 没造出新版本

        # to_version 不存在 → 404
        assert (await c.post("/api/style-profile/rollback", headers=bearer(key),
                             json={"to_version": 9, "base_version": 2})).status_code == 404


# ---------------- ⑨ density 五个中文 key 原样往返 ----------------


async def test_density_chinese_keys_roundtrip_verbatim(tmp_path, monkeypatch):
    """存进去什么读出来什么:中文 key 一个字符不变(防 key 规范化/英文化)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-9"
        await make_operator(key)
        payload = copy.deepcopy(_profile("A"))
        payload["density"] = {
            "信息密度档位": "高密",
            "每页文字量": "400–600 字",
            "每页信息点": "10–14 个",
            "版式档": "满版",
            "运营原话": "我要塞得满满的,别留白",
        }
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": payload, "source": "manual"})

        for body in (
            (await c.get("/api/style-profile", headers=bearer(key))).json()["profile"],
            (await c.get("/api/style-profile/versions/1",
                         headers=bearer(key))).json()["profile"],
        ):
            assert list(body["density"].keys()) == _DENSITY_KEYS
            assert body["density"] == payload["density"]
            assert body == payload  # 整份档案逐字往返


# ---------------- ⑩ 越权:档案按 apikey 认人 ----------------


async def test_operators_cannot_see_each_other(tmp_path, monkeypatch):
    """A 的档案对 B 不可见:B 读到的是 exists:false + 管理员默认,历史列表也是空。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key_a, key_b = "sp-key-a", "sp-key-b"
        await make_operator(key_a)
        await make_operator(key_b)
        await c.put("/api/style-profile", headers=bearer(key_a), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})

        got_b = (await c.get("/api/style-profile", headers=bearer(key_b))).json()
        assert got_b["exists"] is False
        assert got_b["profile"] == svc.ADMIN_DEFAULT_PROFILE
        assert (await c.get("/api/style-profile/versions",
                            headers=bearer(key_b))).json()["versions"] == []
        # A 的 v1 对 B 是 404(不是"看得到别人的快照")
        assert (await c.get("/api/style-profile/versions/1",
                            headers=bearer(key_b))).status_code == 404
        # B 写自己的档案从 v1 起,不受 A 影响
        assert (await c.put("/api/style-profile", headers=bearer(key_b), json={
            "base_version": 0, "profile": _profile("B"), "source": "manual"})
        ).json()["version"] == 1
        assert (await c.get("/api/style-profile", headers=bearer(key_a))
                ).json()["profile"] == _profile("A")


# ---------------- ⑪ 大小上限:超 64KB 明确 400 ----------------


async def test_oversized_profile_rejected_not_truncated(tmp_path, monkeypatch):
    """profile 超 64KB → 400 且不落库(绝不静默截断)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-10"
        await make_operator(key)
        big = _profile("A")
        big["blob"] = "字" * 30000  # UTF-8 3 字节/字 ≈ 90KB
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": big, "source": "manual"})
        assert r.status_code == 400, r.text
        assert "64" in r.json()["error"] or "上限" in r.json()["error"]
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["exists"] is False  # 没落库


# ---------------- 级联清空 + 迁移 seed 防漂移 ----------------


async def test_delete_operator_cascades_style_profile(db):
    """删运营账号 → 其当前档案与全部历史版本级联清空(应用层级联,不靠 SQLite 外键)。"""
    from app.services import operator_service

    # 背景管理员:delete_operator 的"最后一个管理员"硬保护删到行就判定,无管理员的库会 409
    await operator_service.create_operator(db, "在岗boss", role="admin")
    op, _key = await operator_service.create_operator(db, "苏澜")
    other, _ = await operator_service.create_operator(db, "旁人")
    await svc.save_profile(db, op.id, base_version=0, profile=_profile("A"),
                           source="manual", note=None)
    await svc.save_profile(db, op.id, base_version=1, profile=_profile("B"),
                           source="manual", note=None)
    await svc.save_profile(db, other.id, base_version=0, profile=_profile("X"),
                           source="manual", note=None)

    await operator_service.delete_operator(db, op.id)

    assert (await svc.get_profile(db, op.id))["exists"] is False
    assert await svc.list_versions(db, op.id) == []
    # 旁人的档案不受牵连
    assert (await svc.get_profile(db, other.id))["exists"] is True
    assert len(await svc.list_versions(db, other.id)) == 1


def test_migration_seed_matches_service_constant():
    """防漂移:迁移里 seed 的管理员默认档案与服务层常量逐字一致(两处字面量不许分叉)。"""
    path = _REPO_ROOT / "alembic/versions/c7e9a4b21d38_style_profiles_每用户风格档案.py"
    spec = importlib.util.spec_from_file_location("_mig_style_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ADMIN_DEFAULT_PROFILE == svc.ADMIN_DEFAULT_PROFILE
    assert list(module.ADMIN_DEFAULT_PROFILE["density"].keys()) == _DENSITY_KEYS


async def test_toctou_race_surfaces_409_not_500(tmp_path, monkeypatch):
    """并发竞态:两会话都通过前置校验后,撞唯一约束的那个必须是 409 而非 500。

    评审用真 uvicorn 实测过:前置校验(读 current_version)与写入不是原子操作,
    两个会话同时带同一 base_version 时**双方都能通过校验**,最终只有唯一约束
    uq_style_profile_versions_op_ver 拦得住后落地的那个——但它抛 IntegrityError,
    不是 VersionConflict,会冒到全局兜底变成 500。skill 侧拿到 500 无法区分
    "真故障"与"版本冲突",也拿不到 current_version 做重试提示。

    这里确定性复现那个窗口:先偷偷插一条 v2 快照(模拟另一会话已抢先落地),
    再用 base_version=1 走正常 PUT ——前置校验必然通过(当前档案仍是 v1),
    写入必然撞唯一键。
    """
    import app.core.db as db_module
    from app.models.style_profile import StyleProfileVersion

    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-race"
        op_id = await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("v1"), "source": "manual"})

        # 另一会话抢先落了 v2 的快照(挂在该套 set_id 上),但当前套行还停在 v1(正是竞态中间态)
        async with db_module.async_session() as s:
            set_id = (await s.execute(
                select(StyleProfile.id).where(StyleProfile.operator_id == op_id)
            )).scalar_one()
            s.add(StyleProfileVersion(
                set_id=set_id, operator_id=op_id, version=2, profile=_profile("其他会话"),
                source="manual", note=None, created_by=op_id))
            await s.commit()

        resp = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("我的"), "source": "manual"})

        assert resp.status_code == 409, f"撞唯一键必须转 409,实得 {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        assert "current_version" in detail and "updated_at" in detail, detail


# ---------------- dropped_keys:整份覆盖丢了什么,只有 server 说得出 ----------------


async def test_put_dropped_keys_lists_top_level_and_dotted_paths(tmp_path, monkeypatch):
    """只想改配色却漏带其他字段 → 响应如实列出丢掉的顶层与二级键(不拦截、仍 200)。

    这是需求方点名的要害:整份覆盖是既定语义,但 agent 漏字段时静默清空,运营要到下次
    出图才发现人物卡没了,那时已查不出是哪次 PUT 弄丢的。顶层整段消失只报顶层名——
    再展开成 tone.person 之类只是噪音。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-drop-1"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})

        # agent 只改配色:visual 里只留 palette,tone 整段没带,density 倒是齐的
        partial = copy.deepcopy(_profile("A"))
        partial["visual"] = {"palette": [{"name": "暖橘", "hex": "#E8A87C"}]}
        del partial["tone"]
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": partial, "source": "manual"})

        assert r.status_code == 200, r.text  # 只告知,不报错
        assert r.json()["dropped_keys"] == ["tone", "visual.text_color"]
        # tone 整段消失只报一条,不向下展开
        assert "tone.person" not in r.json()["dropped_keys"]
        # 语义没变:整份覆盖照旧生效,server 不替他补回来
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["profile"] == partial


async def test_dropped_keys_empty_list_when_nothing_lost(tmp_path, monkeypatch):
    """首次建档、以及字段齐全的覆盖 → dropped_keys 是空列表(不是省略这个键)。

    skill 侧要能无条件 resp["dropped_keys"] 读它,省略会让它踩 KeyError。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-drop-2"
        await make_operator(key)
        first = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        assert first.json()["dropped_keys"] == []  # 此前无档案,无从比对

        # 字段一个不少地整体回传(只换内容)→ 无丢弃
        second = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("B"), "source": "manual"})
        assert "dropped_keys" in second.json()
        assert second.json()["dropped_keys"] == []


async def test_rollback_also_reports_dropped_keys(tmp_path, monkeypatch):
    """回退同样给 dropped_keys:回退前有、回退后没有的键在这里现形。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-drop-3"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        richer = copy.deepcopy(_profile("B"))
        richer["extra"] = {"x": 1}  # v2 新增了一个顶层段
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": richer, "source": "manual"})

        r = await c.post("/api/style-profile/rollback", headers=bearer(key),
                         json={"to_version": 1, "base_version": 2})
        assert r.status_code == 200, r.text
        assert r.json()["dropped_keys"] == ["extra"]  # 回退等于丢掉 v2 新增的那段


# ---------------- 管理员默认档案维护入口 ----------------


async def test_admin_default_rejects_non_admin(tmp_path, monkeypatch):
    """普通运营改不了管理员默认档案(它影响所有还没建档的人)→ 403,且内容没动。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-admin-1"
        await make_operator(key)
        r = await c.put("/api/style-profile/admin-default", headers=bearer(key),
                        json={"profile": _profile("坏"), "note": "越权"})
        assert r.status_code == 403, r.text
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["profile"] == svc.ADMIN_DEFAULT_PROFILE


async def test_admin_default_write_is_live_for_operators_without_profile(tmp_path, monkeypatch):
    """管理员改默认档案 → 还没建档的运营下一次 GET 立刻读到新内容(实时读,不是建档快照);
    已建个人档案的运营完全不受影响。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        plain, owner = "sp-key-admin-2", "sp-key-admin-3"
        await make_operator(plain)
        await make_operator(owner)
        # owner 已有自己的档案
        await c.put("/api/style-profile", headers=bearer(owner), json={
            "base_version": 0, "profile": _profile("自己的"), "source": "manual"})

        before = (await c.get("/api/style-profile", headers=bearer(plain))).json()
        assert before["profile"] == svc.ADMIN_DEFAULT_PROFILE

        r = await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY),
                        json={"profile": _profile("新默认"), "note": "换全局调性"})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == before["admin_default_version"] + 1
        assert r.json()["source"] == "admin_default"
        assert r.json()["note"] == "换全局调性"

        after = (await c.get("/api/style-profile", headers=bearer(plain))).json()
        assert after["exists"] is False  # 仍是"别人的档案",不能说成他自己的
        assert after["profile"] == _profile("新默认")  # 实时跟着变
        assert after["admin_default_version"] == r.json()["version"]
        assert after["updated_at"] == r.json()["updated_at"]

        # 已建档的运营不受牵连
        assert (await c.get("/api/style-profile", headers=bearer(owner))
                ).json()["profile"] == _profile("自己的")

        # 再改一次 → 版本继续自增,但不进版本历史表(管理员改动不可回退,是既定取舍)
        again = await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY),
                            json={"profile": _profile("再改")})
        assert again.json()["version"] == r.json()["version"] + 1
        async with db_module.async_session() as s:
            snapshots = (await s.execute(
                select(func.count()).select_from(StyleProfileVersion)
                .where(StyleProfileVersion.operator_id.is_(None))
            )).scalar_one()
            assert snapshots == 0
            rows = (await s.execute(
                select(func.count()).select_from(StyleProfile)
                .where(StyleProfile.operator_id.is_(None))
            )).scalar_one()
            assert rows == 1  # 默认档案永远只有一行


# ---------------- 读回管理员默认档案(改它之前的留底手段) ----------------


async def test_get_admin_default_reads_seed_row(tmp_path, monkeypatch):
    """库里有 operator_id IS NULL 那一行时,读到它的内容与版本(而非内置常量)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-admindefault-seed"
        await make_operator(key)
        async with db_module.async_session() as s:
            s.add(StyleProfile(
                operator_id=None, version=7, profile=_profile("SEED"),
                source="admin_default", note="seed",
            ))
            await s.commit()

        r = await c.get("/api/style-profile/admin-default", headers=bearer(key))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["profile"] == _profile("SEED")
        assert data["admin_default_version"] == 7
        assert data["updated_at"]


async def test_get_admin_default_reflects_admin_write(tmp_path, monkeypatch):
    """没那一行时回落常量(版本 0 / updated_at 空);管理员改过之后读到新内容与递增后的版本。

    这正是"留底"要的:改之前拉一份存起来,改之后能验证拉到的确实是新的那份。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-admindefault-write"
        await make_operator(key)

        before = (await c.get("/api/style-profile/admin-default",
                              headers=bearer(key))).json()
        assert before["profile"] == svc.ADMIN_DEFAULT_PROFILE
        assert before["admin_default_version"] == 0
        assert before["updated_at"] is None

        wrote = await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY),
                            json={"profile": _profile("新默认"), "note": "换全局调性"})
        assert wrote.status_code == 200, wrote.text

        after = (await c.get("/api/style-profile/admin-default",
                             headers=bearer(key))).json()
        assert after["profile"] == _profile("新默认")
        assert after["admin_default_version"] == wrote.json()["version"] == 1
        assert after["updated_at"] == wrote.json()["updated_at"]


async def test_get_admin_default_is_not_admin_only(tmp_path, monkeypatch):
    """非管理员(role=operator)也能读:不是 403,且内容与他未建档时 GET 到的完全相同。

    末尾同时钉死这个端点存在的理由:他一旦建了个人档案,GET /api/style-profile 就悄悄
    变成读他自己那份(照样 200 不报错),拿它当留底手段会存下一份错的底;
    admin-default 读到的始终是默认档案本身。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-admindefault-operator"
        await make_operator(key)

        r = await c.get("/api/style-profile/admin-default", headers=bearer(key))
        assert r.status_code == 200, r.text
        own = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert own["exists"] is False
        assert r.json()["profile"] == own["profile"]

        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("自己的"), "source": "manual"})
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["profile"] == _profile("自己的")  # 同一条命令,内容悄悄换了人
        assert (await c.get("/api/style-profile/admin-default", headers=bearer(key))
                ).json()["profile"] == svc.ADMIN_DEFAULT_PROFILE  # 这条始终是默认档案


# ---------------- 历史列表分页(历史长期保存,一年可能几百版) ----------------


async def test_versions_pagination_and_limit_cap(tmp_path, monkeypatch):
    """limit/offset 翻页 + total/has_more;limit 超 200 直接钳到 200 而不是报错。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-page"
        await make_operator(key)
        for i in range(5):
            await c.put("/api/style-profile", headers=bearer(key), json={
                "base_version": i, "profile": _profile(str(i)), "source": "manual"})

        first = (await c.get("/api/style-profile/versions?limit=2",
                             headers=bearer(key))).json()
        assert [v["version"] for v in first["versions"]] == [5, 4]  # 仍是倒序
        assert first["total"] == 5
        assert first["limit"] == 2
        assert first["offset"] == 0
        assert first["has_more"] is True

        last = (await c.get("/api/style-profile/versions?limit=2&offset=4",
                            headers=bearer(key))).json()
        assert [v["version"] for v in last["versions"]] == [1]
        assert last["offset"] == 4
        assert last["has_more"] is False

        # 超上限不报错,钳到 200;默认(不传参)一次给 50 条以内
        capped = await c.get("/api/style-profile/versions?limit=9999", headers=bearer(key))
        assert capped.status_code == 200, capped.text
        assert capped.json()["limit"] == 200
        assert capped.json()["has_more"] is False
        default = (await c.get("/api/style-profile/versions", headers=bearer(key))).json()
        assert default["limit"] == 50
        assert [v["version"] for v in default["versions"]] == [5, 4, 3, 2, 1]


# ---------------- base_version:下一次 PUT 传什么,server 说了算 ----------------


async def test_base_version_is_single_source_of_truth(tmp_path, monkeypatch):
    """无档案给 0、有档案给当前 version,skill 侧直接透传不必自己推(两端各推一次会分歧)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-key-base"
        await make_operator(key)
        empty = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert empty["exists"] is False
        assert empty["base_version"] == 0

        # 拿它原样回传即可建档
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": empty["base_version"], "profile": _profile("A"),
            "source": "manual"})
        got = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert got["base_version"] == got["version"] == 1  # version 键保留不动

        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": got["base_version"], "profile": _profile("B"),
            "source": "manual"})
        assert (await c.get("/api/style-profile", headers=bearer(key))
                ).json()["base_version"] == 2


# ==================== 多套 + 每套独立版本链(profile_set)====================
# 设计 docs/design-2026-07-28-profile-sets.md(§7 测试要点 + §9 五 blocker)。


async def _new_set(c, key, *, name, kind="carousel", profile=None, frm=None, scope=None):
    """POST /sets 建套的测试小助手;返回响应。"""
    body = {"name": name, "kind": kind}
    if profile is not None:
        body["profile"] = profile
    if frm is not None:
        body["from"] = frm
    url = "/api/style-profile/sets"
    if scope:
        url += f"?scope={scope}"
    return await c.post(url, headers=bearer(key), json=body)


# ---------------- 兼容性铁律:响应字段只增不减(新增 set/kind)----------------


async def test_responses_carry_set_and_kind_fields(tmp_path, monkeypatch):
    """不带 set 的 GET/PUT/versions/{v} 响应新增 set/kind 字段(增字段,老客户端无感)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-set-fields"
        await make_operator(key)
        put = (await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})).json()
        assert put["set"] == "图文" and put["kind"] == "carousel"
        got = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert got["set"] == "图文" and got["kind"] == "carousel"
        one = (await c.get("/api/style-profile/versions/1", headers=bearer(key))).json()
        assert one["set"] == "图文" and one["kind"] == "carousel"


# ---------------- B3:0 套运营首写自动建 图文/carousel/is_active 套 ----------------


async def test_b3_zero_set_first_put_autocreates_default_set(tmp_path, monkeypatch):
    """0 套运营不带 set 的 PUT → 自动建 图文/carousel/is_active 套,version 1;GET /sets 可见。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b3"
        await make_operator(key)
        assert (await c.get("/api/style-profile", headers=bearer(key))).json()["exists"] is False
        assert (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"] == []
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        assert r.status_code == 200 and r.json()["version"] == 1
        sets = (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"]
        assert len(sets) == 1
        assert sets[0]["name"] == "图文"
        assert sets[0]["kind"] == "carousel"
        assert sets[0]["is_active"] is True
        assert sets[0]["version"] == 1


async def test_b3_exists_false_returns_admin_default_active_set(tmp_path, monkeypatch):
    """exists:false 的 GET 返回管理员默认 **is_active** 套(有多套默认时也是 active 那套)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b3-fallback"
        await make_operator(key)
        # 管理员建两套默认(图文 active + 文字版)
        await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY), json={
            "profile": _profile("默认图文"), "note": None})
        await _new_set(c, ADMIN_KEY, name="文字版", kind="typeset",
                       profile=_profile("默认文字"), scope="admin-default")
        got = (await c.get("/api/style-profile", headers=bearer(key))).json()
        assert got["exists"] is False
        assert got["set"] == "图文"  # is_active 那套
        assert got["profile"] == _profile("默认图文")


# ---------------- 两套独立版本链:改 A 不动 B,回退 A 不串 B(需求 §3.1 核心)----------------


async def test_two_sets_independent_version_chains(tmp_path, monkeypatch):
    """建两套 → 改 A 套 → B 套 version/内容不变;回退 A 套不影响 B 套。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-two-chains"
        await make_operator(key)
        # 图文 套(auto) v1
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("图A"), "source": "manual"})
        # 文字版 套 v1
        assert (await _new_set(c, key, name="文字版", kind="typeset",
                               profile=_profile("字A"))).status_code == 200

        # 改 文字版:v1→v2→v3
        await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("字B"), "source": "manual"})
        await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 2, "profile": _profile("字C"), "source": "manual"})

        # 图文 完全不受影响
        gt = (await c.get("/api/style-profile?set=图文", headers=bearer(key))).json()
        assert gt["version"] == 1 and gt["profile"] == _profile("图A")
        assert [v["version"] for v in (await c.get(
            "/api/style-profile/versions?set=图文", headers=bearer(key))).json()["versions"]] == [1]
        # 文字版 自己的链
        assert [v["version"] for v in (await c.get(
            "/api/style-profile/versions?set=文字版", headers=bearer(key))).json()["versions"]] == [3, 2, 1]

        # 回退 文字版 到 v1 → 文字版 v4=字A;图文 仍 v1 字面不动
        rb = await c.post("/api/style-profile/rollback?set=文字版", headers=bearer(key),
                          json={"to_version": 1, "base_version": 3})
        assert rb.status_code == 200 and rb.json()["version"] == 4
        assert (await c.get("/api/style-profile?set=文字版", headers=bearer(key))
                ).json()["profile"] == _profile("字A")
        gt2 = (await c.get("/api/style-profile?set=图文", headers=bearer(key))).json()
        assert gt2["version"] == 1 and gt2["profile"] == _profile("图A")

        # 不带 set 恒指 is_active(图文)那套
        assert (await c.get("/api/style-profile", headers=bearer(key))).json()["set"] == "图文"


async def test_concurrent_puts_two_sets_no_false_conflict(tmp_path, monkeypatch):
    """两套各自 PUT,base_version 互不干扰,都成功(需求 §3.2:语义上不冲突不应被迫串行)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-no-false-conflict"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("图A"), "source": "manual"})
        await _new_set(c, key, name="文字版", kind="typeset", profile=_profile("字A"))
        # 两套当前都 v1;各自带 base_version=1 写,互不 409
        a = await c.put("/api/style-profile?set=图文", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("图B"), "source": "manual"})
        b = await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("字B"), "source": "manual"})
        assert a.status_code == 200 and a.json()["version"] == 2
        assert b.status_code == 200 and b.json()["version"] == 2


# ---------------- is_active 唯一:建 / 设默认 / 删 active 后恒有且仅一个 ----------------


def _active_names(sets):
    return [s["name"] for s in sets if s["is_active"]]


async def test_is_active_unique_across_create_activate_delete(tmp_path, monkeypatch):
    """建套 / PATCH 设默认 / 删 active 套后,is_active 恒有且仅一个。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-active-unique"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        await _new_set(c, key, name="文字版", kind="typeset", profile=_profile("B"))
        await _new_set(c, key, name="水墨", kind="typeset", profile=_profile("C"))

        sets = (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"]
        assert _active_names(sets) == ["图文"]  # 首套是 active,后建的不是

        # 设 水墨 为默认
        p = await c.patch("/api/style-profile/sets/水墨", headers=bearer(key),
                          json={"is_active": True})
        assert p.status_code == 200 and p.json()["is_active"] is True
        sets = (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"]
        assert _active_names(sets) == ["水墨"]
        # 不带 set 现在读 水墨
        assert (await c.get("/api/style-profile", headers=bearer(key))).json()["set"] == "水墨"

        # 删 active(水墨)→ 另一套顶上,仍恰好一个 active
        d = await c.delete("/api/style-profile/sets/水墨", headers=bearer(key))
        assert d.status_code == 200
        sets = (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"]
        assert len(_active_names(sets)) == 1


async def test_patch_is_active_false_rejected_400(tmp_path, monkeypatch):
    """PATCH is_active=false 单发拒绝(否则 0 active)→ 400。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-active-false"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        r = await c.patch("/api/style-profile/sets/图文", headers=bearer(key),
                          json={"is_active": False})
        assert r.status_code == 400, r.text


# ---------------- 删到剩一套拒绝 409 ----------------


async def test_delete_last_set_rejected_409(tmp_path, monkeypatch):
    """只剩一套时再删 → 409(否则运营把自己清空)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-del-last"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        r = await c.delete("/api/style-profile/sets/图文", headers=bearer(key))
        assert r.status_code == 409, r.text


# ---------------- B4:删套→重建同名→写 v1 成功且历史空(数据损坏级回归)----------------


async def test_b4_delete_recreate_same_name_clean_history(tmp_path, monkeypatch):
    """删套须同事务删该套全部版本;重建同名套写 v1 成功、历史只含新版本(无孤儿撞唯一键 500)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b4"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("图"), "source": "manual"})
        # 文字版:攒 v1 v2 v3
        await _new_set(c, key, name="文字版", kind="typeset", profile=_profile("字1"))
        await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("字2"), "source": "manual"})
        await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 2, "profile": _profile("字3"), "source": "manual"})
        # 删 文字版(此时 2 套,允许)
        assert (await c.delete("/api/style-profile/sets/文字版", headers=bearer(key))).status_code == 200
        # 重建同名套 → v1,历史只有 v1(无孤儿 v2/v3)
        again = await _new_set(c, key, name="文字版", kind="typeset", profile=_profile("新字1"))
        assert again.status_code == 200 and again.json()["version"] == 1
        vers = (await c.get("/api/style-profile/versions?set=文字版", headers=bearer(key))).json()["versions"]
        assert [v["version"] for v in vers] == [1]
        # 继续写 v2 不撞唯一键(孤儿历史已清)
        w = await c.put("/api/style-profile?set=文字版", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("新字2"), "source": "manual"})
        assert w.status_code == 200 and w.json()["version"] == 2


# ---------------- B5:profiles-v1 容器哨兵 → 400 ----------------


async def test_b5_container_sentinel_rejected_400(tmp_path, monkeypatch):
    """PUT / PUT admin-default 收到 profiles-v1 多套容器 → 400(工具包过旧),不落库。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b5"
        await make_operator(key)
        container = {"schema": "profiles-v1", "active": "图文",
                     "profiles": {"图文": {"kind": "carousel"}, "文字版": {"kind": "typeset"}}}
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": container, "source": "manual"})
        assert r.status_code == 400, r.text
        assert (await c.get("/api/style-profile", headers=bearer(key))).json()["exists"] is False

        ra = await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY), json={
            "profile": container, "note": None})
        assert ra.status_code == 400, ra.text

        # 仅含 schema 键但**无 profiles** 的普通档案不被误伤(极窄哨兵)
        ok = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": {"schema": "别的", "visual": {}}, "source": "manual"})
        assert ok.status_code == 200, ok.text


# ---------------- B1:管理员默认(NULL)套名靠部分唯一索引拦重名 ----------------


async def test_b1_admin_default_duplicate_name_blocked(tmp_path, monkeypatch):
    """管理员默认多套:重名被部分唯一索引 uq_admin_set_name 拦成 409(NULL 联合约束不生效)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b1"
        await make_operator(key)
        # 建默认 图文(经 PUT admin-default 首建)
        await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY), json={
            "profile": _profile("默认"), "note": None})
        # 再 POST 同名 图文 → 409(部分索引拦 NULL 重名)
        dup = await _new_set(c, ADMIN_KEY, name="图文", kind="carousel",
                             profile=_profile("撞名"), scope="admin-default")
        assert dup.status_code == 409, dup.text
        # 不同名 文字版 → 成功;再撞 文字版 → 409
        assert (await _new_set(c, ADMIN_KEY, name="文字版", kind="typeset",
                               profile=_profile("字"), scope="admin-default")).status_code == 200
        assert (await _new_set(c, ADMIN_KEY, name="文字版", kind="typeset",
                               profile=_profile("再撞"), scope="admin-default")).status_code == 409


async def test_operator_duplicate_set_name_409(tmp_path, monkeypatch):
    """实运营重名套 → 409(联合唯一约束)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-dup-op"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        assert (await _new_set(c, key, name="文字版", kind="typeset")).status_code == 200
        assert (await _new_set(c, key, name="文字版", kind="typeset")).status_code == 409
        # 改名撞已有名 → 409
        await _new_set(c, key, name="水墨", kind="typeset")
        r = await c.patch("/api/style-profile/sets/水墨", headers=bearer(key),
                          json={"new_name": "文字版"})
        assert r.status_code == 409, r.text
        # 改名撞名 + 同时设默认(原子)→ 仍 409 而非 500,且没改成默认
        r2 = await c.patch("/api/style-profile/sets/水墨", headers=bearer(key),
                           json={"new_name": "文字版", "is_active": True})
        assert r2.status_code == 409, r2.text
        sets = (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"]
        assert [s["name"] for s in sets if s["is_active"]] == ["图文"]  # 默认没被改动


# ---------------- 套名边界 + from 复制 ----------------


async def test_set_name_validation_and_from_copy(tmp_path, monkeypatch):
    """name 规则(空/保留字符/超长)→ 400;首尾空白 trim;from 复制当前 profile;from 不存在 → 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-name"
        await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("原"), "source": "manual"})

        assert (await _new_set(c, key, name="")).status_code == 400
        assert (await _new_set(c, key, name="   ")).status_code == 400
        assert (await _new_set(c, key, name="a/b")).status_code == 400
        assert (await _new_set(c, key, name="q?x")).status_code == 400
        assert (await _new_set(c, key, name="字" * 21)).status_code == 400  # 42 显示宽度 > 20

        # 首尾空白被 trim:建成 文字版
        assert (await _new_set(c, key, name="  文字版  ", kind="typeset")).status_code == 200
        assert (await c.get("/api/style-profile?set=文字版", headers=bearer(key))).status_code == 200

        # from 复制 图文 当前 profile
        cp = await _new_set(c, key, name="副本", frm="图文")
        assert cp.status_code == 200
        assert (await c.get("/api/style-profile?set=副本", headers=bearer(key))
                ).json()["profile"] == _profile("原")
        # 改 图文 后 副本 不跟着变(独立副本)
        await c.put("/api/style-profile?set=图文", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("改"), "source": "manual"})
        assert (await c.get("/api/style-profile?set=副本", headers=bearer(key))
                ).json()["profile"] == _profile("原")

        # from 不存在 → 404
        assert (await _new_set(c, key, name="X", frm="没这套")).status_code == 404


# ---------------- set 不存在 → 404(有档案但无此套);0 套 → 回落不 404 ----------------


async def test_set_not_found_semantics(tmp_path, monkeypatch):
    """有档案但无此套:GET/PUT/versions/rollback → 404;运营 0 套 GET ?set= → 回落默认不 404。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-404"
        await make_operator(key)
        # 0 套时 GET ?set=X → 回落默认(exists:false),不 404
        z = await c.get("/api/style-profile?set=不存在", headers=bearer(key))
        assert z.status_code == 200 and z.json()["exists"] is False

        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("A"), "source": "manual"})
        # 有档案但无此套 → 404
        assert (await c.get("/api/style-profile?set=没这套", headers=bearer(key))).status_code == 404
        assert (await c.put("/api/style-profile?set=没这套", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("B"), "source": "manual"})).status_code == 404
        assert (await c.get("/api/style-profile/versions?set=没这套",
                            headers=bearer(key))).status_code == 404
        assert (await c.get("/api/style-profile/versions/1?set=没这套",
                            headers=bearer(key))).status_code == 404
        assert (await c.post("/api/style-profile/rollback?set=没这套", headers=bearer(key),
                             json={"to_version": 1, "base_version": 1})).status_code == 404


# ---------------- 管理员默认多套 + scope 权限(B2)----------------


async def test_admin_default_sets_scope_and_permissions(tmp_path, monkeypatch):
    """scope=admin-default:读(GET /sets)不设门,写(POST/PATCH/DELETE)需 admin;作用于 NULL 行。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b2"
        await make_operator(key)
        # admin 建两套默认
        await c.put("/api/style-profile/admin-default", headers=bearer(ADMIN_KEY), json={
            "profile": _profile("默认图"), "note": None})
        assert (await _new_set(c, ADMIN_KEY, name="文字版", kind="typeset",
                               profile=_profile("默认字"), scope="admin-default")).status_code == 200
        # 非 admin 可读 admin-default 套(客户端 onboarding 据此继承)
        r = await c.get("/api/style-profile/sets?scope=admin-default", headers=bearer(key))
        assert r.status_code == 200
        names = sorted(s["name"] for s in r.json()["sets"])
        assert names == ["图文", "文字版"]
        # 非 admin 写 admin-default → 403
        assert (await _new_set(c, key, name="偷建", kind="typeset", scope="admin-default")
                ).status_code == 403
        assert (await c.patch("/api/style-profile/sets/图文?scope=admin-default",
                              headers=bearer(key), json={"is_active": True})).status_code == 403
        assert (await c.delete("/api/style-profile/sets/图文?scope=admin-default",
                               headers=bearer(key))).status_code == 403
        # admin 读某套 via ?set= 走 admin-default 端点
        got = (await c.get("/api/style-profile/admin-default?set=文字版",
                           headers=bearer(ADMIN_KEY))).json()
        assert got["set"] == "文字版" and got["profile"] == _profile("默认字")
        # 运营自己的 /sets 与 admin-default 隔离:运营 0 套
        assert (await c.get("/api/style-profile/sets", headers=bearer(key))).json()["sets"] == []


# ---------------- 合并收敛补:B3 建套 flush 撞键 → 409、rollback 容器 → 400 ----------------


async def test_b3_autocreate_name_collision_conflicts_409_not_500(tmp_path, monkeypatch):
    """B3 自动建套的 flush 撞 (operator_id,name) 唯一 → 409 非裸 500(修前 flush 在兜底 try 外)。

    确定性触发:先插一条同运营的**非活跃**「图文」套(使 _active_set 返 None 走 B3 建套路径,
    但 name 已被占用),再不带 set 首写 → 建套 flush 撞 name 唯一。fable 指出既有 TOCTOU 测试
    只覆盖版本行 (set_id,version) 撞键(套已存在),盖不住本路径。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-b3-collide"
        op_id = await make_operator(key)
        async with db_module.async_session() as s:
            s.add(StyleProfile(
                operator_id=op_id, name="图文", kind="carousel", is_active=False,
                version=1, profile=_profile("占名"), source="manual"))
            await s.commit()
        # 不带 set 首写:无活跃套 → 走 B3 建「图文」→ flush 撞 (operator_id,name)
        r = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("我的"), "source": "manual"})
        assert r.status_code == 409, r.text  # 关键:409 而非 500


async def test_rollback_to_container_version_rejected_400(tmp_path, monkeypatch):
    """回退目标是迁移拆容器时挂的整份 profiles-v1 容器快照 → 400,不原样写成套里套。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        key = "sp-rb-container"
        op_id = await make_operator(key)
        await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 0, "profile": _profile("图"), "source": "manual"})
        # 偷偷插一条容器版本(模拟迁移拆容器时挂的「迁移前整份历史」)
        async with db_module.async_session() as s:
            set_row = (await s.execute(select(StyleProfile).where(
                StyleProfile.operator_id == op_id))).scalar_one()
            s.add(StyleProfileVersion(
                set_id=set_row.id, operator_id=op_id, version=99,
                profile={"schema": "profiles-v1", "active": "图文", "profiles": {}},
                source="manual", note="迁移前整份历史;模拟"))
            await s.commit()
        r = await c.post("/api/style-profile/rollback", headers=bearer(key), json={
            "to_version": 99, "base_version": 1})
        assert r.status_code == 400, r.text
        # 当前档案没被容器污染
        assert "profiles" not in (await c.get(
            "/api/style-profile", headers=bearer(key))).json()["profile"]
