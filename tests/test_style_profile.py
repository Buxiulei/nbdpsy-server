"""每用户风格档案测试:exists 语义 / 版本自增留档 / 乐观锁 409 / 回退造新版 / 越权隔离。

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
from tests.rest_helpers import bearer, make_operator, rest_client

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

        # 另一会话抢先落了 v2 的快照,但当前档案行还停在 v1(正是竞态中间态)
        async with db_module.async_session() as s:
            s.add(StyleProfileVersion(
                operator_id=op_id, version=2, profile=_profile("其他会话"),
                source="manual", note=None, created_by=op_id))
            await s.commit()

        resp = await c.put("/api/style-profile", headers=bearer(key), json={
            "base_version": 1, "profile": _profile("我的"), "source": "manual"})

        assert resp.status_code == 409, f"撞唯一键必须转 409,实得 {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        assert "current_version" in detail and "updated_at" in detail, detail
