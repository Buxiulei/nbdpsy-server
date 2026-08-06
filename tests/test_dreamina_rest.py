"""即梦 REST 端点单测：校验矩阵 / 幂等 / 批量 / 积分守卫 / 登录态 / 归属 / 产物直链。

全离线：dreamina CLI 一律 monkeypatch ``app.services.dreamina._run_cli``——REST 层唯一会碰
CLI 的地方是登录态/积分查询（``user_credit``），提交只落 queued 行由 worker 侧调度器执行，
故这些用例不会产生任何真任务、不烧一分积分。

验收对照（需求第七节）：第 2 条 image2video+ratio→422、第 4 条同 ref 重发回原 clip_id、
第 6 条拔登录态 → dreamina-status.logged_in=false + 提交明确报错、第 7 条批量重放零新增、
第 8 条 clip_id 不是 16 位纯小写 hex。
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

import app.core.db as db_module
from app.core.config import settings
from app.models.video_clip import VideoClip
from app.services import dreamina
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _stub_credit(monkeypatch, credit=5000, *, logged_in=True):
    """把 user_credit 的 CLI 调用换成固定余额；logged_in=False 仿「登录态文件被拔掉」。"""
    dreamina.reset_credit_cache()

    async def _fake(args, timeout):
        if args and args[0] == "user_credit":
            if not logged_in:
                return (1, "", "credential not found: ~/.dreamina_cli/credential.json")
            return (0, json.dumps({"total_credit": credit}), "")
        raise AssertionError(f"REST 层不应调用 dreamina {args}")

    monkeypatch.setattr(dreamina, "_run_cli", _fake)


def _shot(**kw) -> dict:
    base = {"operation": "text2video", "prompt": "温暖诊室空镜，晨光缓缓移过沙发",
            "duration": 5, "ratio": "9:16", "model": "seedance2.0fast"}
    base.update(kw)
    return base


async def _clip_count() -> int:
    async with db_module.async_session() as s:
        return (await s.execute(select(func.count()).select_from(VideoClip))).scalar_one()


# ── 提交与校验矩阵 ──────────────────────────────────────────────────────────
async def test_create_clip_ok_and_id_shape(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips", json=_shot(), headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        clip_id = r.json()["clip_id"]
        # 验收第 8 条：绝不能是 16 位纯小写 hex（会撞本机 CLI submit_id 形态）
        assert clip_id.startswith("vc_")
        assert not re.fullmatch(r"[0-9a-f]{16}", clip_id)
        assert await _clip_count() == 1

        got = await client.get(f"/api/video-clips/{clip_id}", headers=bearer(ADMIN_KEY))
        assert got.status_code == 200
        body = got.json()
        assert body["status"] == "queued" and body["submit_id"] is None
        assert body["queued_seconds"] is None and body["video_url"] is None
        assert body["model"] == "seedance2.0fast"


async def test_validation_matrix(tmp_path, monkeypatch):
    """CLI 收不了的组合一律 422，不静默吞（需求第三节 / 验收第 2 条）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        bad = [
            _shot(operation="image2video", image="/uploads/b/01.png", ratio="9:16"),  # 验收第 2 条
            _shot(operation="image2video", ratio=None),                    # 缺 image
            _shot(operation="text2video", image="https://img/x.png"),      # 多余 image
            _shot(operation="multimodal2video", ratio=None),               # 缺 image
            _shot(duration=3), _shot(duration=16),                          # 时长越界
            _shot(model="seedance2.5", duration=31),                        # 连 2.5 也没有 31s
            _shot(model="seedance3.0"), _shot(operation="text2image"),      # 枚举越界
            _shot(prompt=""), _shot(prompt="x" * 2001),                     # 提示词长度
            _shot(ratio="2:3"),                                             # 画幅越界
            _shot(client_ref="x" * 65),                                     # 幂等键过长
            _shot(operation="image2video", ratio=None,                      # image2video 只收单张
                  images=["/uploads/b/01.png", "/uploads/b/02.png"]),
            _shot(operation="multimodal2video", ratio=None, images=[]),     # images 不许空数组
        ]
        for payload in bad:
            r = await client.post("/api/video-clips", json=payload, headers=h)
            assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"
        assert await _clip_count() == 0


# ── 模型面（2026-08-05 CLI 升级：六档模型、默认 2.5、时长上限按模型分档）──────────
async def test_default_model_is_seedance25(tmp_path, monkeypatch):
    """不传 model 时落 seedance2.5（单镜与批量共用同一个 Pydantic 默认值）。"""
    _stub_credit(monkeypatch)
    payload = _shot()
    payload.pop("model")
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips", json=payload, headers=h)
        assert r.status_code == 202, r.text
        got = await client.get(f"/api/video-clips/{r.json()['clip_id']}", headers=h)
        assert got.json()["model"] == "seedance2.5"

        batch = await client.post("/api/video-clip-batches",
                                  json={"shots": [payload, payload]}, headers=h)
        assert batch.status_code == 202, batch.text
        for clip_id in batch.json()["clip_ids"]:
            one = await client.get(f"/api/video-clips/{clip_id}", headers=h)
            assert one.json()["model"] == "seedance2.5"


async def test_duration_ceiling_is_per_model(tmp_path, monkeypatch):
    """只有 seedance2.5 到 30s；其余模型 >15s 当场 422（不许建行再让 CLI 拒）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        ok = await client.post("/api/video-clips",
                               json=_shot(model="seedance2.5", duration=30), headers=h)
        assert ok.status_code == 202, ok.text

        bad = await client.post("/api/video-clips",
                                json=_shot(model="seedance2.0fast", duration=16), headers=h)
        assert bad.status_code == 422, bad.text
        # 文案要说清「不是所有档都能 30s」，否则运营只会看到一个光秃秃的越界
        assert "仅 seedance2.5" in bad.text
        assert await _clip_count() == 1, "422 的那镜不许留下任务行"


async def test_new_models_are_accepted(tmp_path, monkeypatch):
    """本次新增的两档都进枚举（mini 走家族默认 15s 上限）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        for model in ("seedance2.0mini", "seedance2.5"):
            r = await client.post("/api/video-clips",
                                  json=_shot(model=model, duration=15), headers=h)
            assert r.status_code == 202, f"{model} → {r.text}"
        over = await client.post("/api/video-clips",
                                 json=_shot(model="seedance2.0mini", duration=16), headers=h)
        assert over.status_code == 422, over.text


async def test_bad_reference_image_is_4xx_before_any_row(tmp_path, monkeypatch):
    """坏图当场 4xx，**不建任务**（建完再失败要人回头清理）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="image2video", ratio=None, image="/uploads/gone/01.png"),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 400, r.text
        assert "图床里找不到" in r.json()["error"]
        assert await _clip_count() == 0


async def test_image2video_materializes_independent_copy(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    (tmp_path / "uploads" / "b1").mkdir(parents=True)
    (tmp_path / "uploads" / "b1" / "01.png").write_bytes(_PNG)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="image2video", ratio=None, image="/uploads/b1/01.png"),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            clip = (await s.execute(select(VideoClip))).scalars().one()
        assert clip.image_source == "/uploads/b1/01.png"
        assert clip.image_path.endswith(".png")
        assert dreamina.clip_token_dir(clip.clip_id) in clip.image_path


# ── 幂等（验收第 4 条）──────────────────────────────────────────────────────
async def test_client_ref_replay_returns_same_clip(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        payload = _shot(client_ref="shot-01")
        first = await client.post("/api/video-clips", json=payload, headers=h)
        second = await client.post("/api/video-clips", json=payload, headers=h)
        assert first.status_code == second.status_code == 202
        assert first.json()["clip_id"] == second.json()["clip_id"]
        assert second.json()["status"] == "queued"
        assert await _clip_count() == 1, "同 ref 重发绝不能新建第二条（=双倍扣分）"


async def test_client_ref_is_scoped_per_operator(tmp_path, monkeypatch):
    """幂等键按运营隔离：别人用同 ref 不该拿到我的任务（既抢注又泄漏状态）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-dreamina-1"
        await make_operator(op_key)
        payload = _shot(client_ref="shot-01")
        a = await client.post("/api/video-clips", json=payload, headers=bearer(ADMIN_KEY))
        b = await client.post("/api/video-clips", json=payload, headers=bearer(op_key))
        assert a.status_code == b.status_code == 202
        assert a.json()["clip_id"] != b.json()["clip_id"]
        assert await _clip_count() == 2


# ── 批量 ────────────────────────────────────────────────────────────────────
async def test_batch_ids_align_with_shots_and_no_collateral_damage(tmp_path, monkeypatch):
    """clip_ids 与 shots **等长同序**；一镜物化失败只让那一镜 error，其余照常入队。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        shots = [
            _shot(prompt="镜1"),
            _shot(operation="image2video", ratio=None, image="/uploads/gone/01.png", prompt="镜2"),
            _shot(prompt="镜3"),
        ]
        r = await client.post("/api/video-clip-batches", json={"shots": shots},
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        body = r.json()
        assert len(body["clip_ids"]) == 3
        assert body["batch_id"].startswith("vcb_")

        summary = await client.get(f"/api/video-clip-batches/{body['batch_id']}",
                                   headers=bearer(ADMIN_KEY))
        assert summary.status_code == 200
        clips = summary.json()["clips"]
        assert [c["clip_id"] for c in clips] == body["clip_ids"]     # 按 batch_index 同序
        assert [c["status"] for c in clips] == ["queued", "error", "queued"]
        assert "图床里找不到" in clips[1]["error"]
        assert summary.json()["summary"] == {"total": 3, "done": 0, "error": 1, "in_flight": 2}


async def test_batch_replay_creates_nothing(tmp_path, monkeypatch):
    """验收第 7 条：同一份 shots（逐镜同 client_ref）再 POST → 原 clip_ids、零新增任务。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(prompt=f"镜{i}", client_ref=f"ref-{i}") for i in range(4)]
        first = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert first.status_code == 202, first.text
        assert await _clip_count() == 4

        second = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert second.status_code == 202
        assert second.json()["clip_ids"] == first.json()["clip_ids"]
        assert await _clip_count() == 4, "整批重放必须零新增任务（否则 8 镜 fast_vip 白烧 440 积分）"


async def test_batch_replay_is_not_gated_by_credit_or_login(tmp_path, monkeypatch):
    """整批纯重放（全部 ref 命中）**不过登录闸/积分闸**——验收第 7 条要保护的正是这个场景。

    失败链：首发整批入队但响应丢了 → 排队任务结算把余额扣穿 → 重发拿到 409/503 而不是原
    clip_ids → skill 按 4xx 不再重试、告诉运营提交失败（实际 8 镜已在队烧钱）→ 充值后重跑
    （每次进程新生成 uuid client_ref）→ 整批双倍提交双倍扣分。
    """
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(prompt=f"镜{i}", client_ref=f"ref-{i}") for i in range(3)]
        first = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert first.status_code == 202, first.text
        assert await _clip_count() == 3

        # 余额被排队中的任务扣穿：重放照回原 clip_ids
        _stub_credit(monkeypatch, credit=3)
        broke = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert broke.status_code == 202, broke.text
        assert broke.json()["clip_ids"] == first.json()["clip_ids"]

        # 登录态掉了：重放同样照回原 clip_ids（任务早就在队里，503 会误导成「提交失败」）
        _stub_credit(monkeypatch, logged_in=False)
        offline = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert offline.status_code == 202, offline.text
        assert offline.json()["clip_ids"] == first.json()["clip_ids"]
        assert await _clip_count() == 3, "重放全程零新增任务"


async def test_dead_shot_revives_on_replay_after_image_source_fixed(tmp_path, monkeypatch):
    """图源瞬时故障留下的 error 行**不烧死幂等键**：修好图同 ref 重放 → 同 clip_id 复活回 queued。

    不复活的失败链：批量里某镜的图床恰好 502 → 那一镜落 error 行并**占住 (运营, ref) 唯一键**
    → 运营修好图用同一份 shots 重放 → ref 命中的正是这条 error 行、原样返回 → 那一镜永远
    生不出来，而单镜端点同样入参是 400 不建行、重放就能成功（不对称）。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(operation="image2video", ratio=None, prompt="镜1",
                       image="/uploads/b9/01.png", client_ref="rev-1")]
        first = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        assert first.status_code == 202, first.text
        clip_id = first.json()["clip_ids"][0]
        dead = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert dead.json()["status"] == "error" and "图床里找不到" in dead.json()["error"]

        # 运营把图补进图床 → 同 shots 同 ref 重放
        (tmp_path / "uploads" / "b9").mkdir(parents=True)
        (tmp_path / "uploads" / "b9" / "01.png").write_bytes(_PNG)
        second = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)

        assert second.status_code == 202, second.text
        assert second.json()["clip_ids"] == [clip_id], "复活必须保同一个 clip_id（幂等语义）"
        assert await _clip_count() == 1, "复活是原地重置，绝不新建第二条"
        revived = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert revived.json()["status"] == "queued" and revived.json()["error"] is None
        async with db_module.async_session() as s:
            clip = (await s.execute(select(VideoClip))).scalars().one()
        assert clip.image_path and Path(clip.image_path).is_file(), "参考图要真的重新物化落盘"


async def test_revive_keeps_error_when_image_still_broken(tmp_path, monkeypatch):
    """图还是坏的：维持 error 只更新文案，不重置回 queued（否则调度器会提一条没图的任务）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(operation="image2video", ratio=None, prompt="镜1",
                       image="/uploads/still-gone/01.png", client_ref="rev-2")]
        first = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        clip_id = first.json()["clip_ids"][0]

        second = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)

        assert second.status_code == 202 and second.json()["clip_ids"] == [clip_id]
        again = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert again.json()["status"] == "error"
        assert await _clip_count() == 1


async def test_single_post_revives_row_left_dead_by_batch(tmp_path, monkeypatch):
    """单镜端点同样走复活：ref 命中批量留下的可复活行时，回同 clip_id + queued。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shot = _shot(operation="image2video", ratio=None, prompt="镜1",
                     image="/uploads/b8/01.png", client_ref="rev-3")
        batch = await client.post("/api/video-clip-batches", json={"shots": [shot]}, headers=h)
        clip_id = batch.json()["clip_ids"][0]

        (tmp_path / "uploads" / "b8").mkdir(parents=True)
        (tmp_path / "uploads" / "b8" / "01.png").write_bytes(_PNG)
        solo = await client.post("/api/video-clips", json=shot, headers=h)

        assert solo.status_code == 202, solo.text
        assert solo.json()["clip_id"] == clip_id and solo.json()["status"] == "queued"
        assert await _clip_count() == 1


async def test_replay_never_revives_row_that_reached_dreamina(tmp_path, monkeypatch):
    """已进过即梦队列（有 submit_id / 认领过）的 error 行**原样返回，绝不复活**。

    那些行的资金状态未知或已扣分，复活 = 再提交一次 = 可能双倍扣分且排队中无法取消。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shot = _shot(client_ref="burned-1")
        first = await client.post("/api/video-clips", json=shot, headers=h)
        clip_id = first.json()["clip_id"]
        # 仿「已提交到即梦、随后判失败」：submit_id + 认领戳都在
        async with db_module.async_session() as s:
            clip = (await s.execute(select(VideoClip))).scalars().one()
            clip.status, clip.submit_id = "error", "3d64c2221c0e07da"
            clip.submitted_at = datetime.utcnow()
            clip.error = "内容审核未通过"
            await s.commit()

        again = await client.post("/api/video-clips", json=shot, headers=h)

        assert again.status_code == 202 and again.json()["clip_id"] == clip_id
        assert again.json()["status"] == "error"
        got = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert got.json()["error"] == "内容审核未通过", "错误结论不该被复活抹掉"
        assert got.json()["submit_id"] == "3d64c2221c0e07da"
        assert await _clip_count() == 1


# ── 批次号语义 ──────────────────────────────────────────────────────────────
async def test_pure_replay_batch_returns_null_batch_id_not_a_phantom(tmp_path, monkeypatch):
    """整批纯重放（零新建）**不现编 batch_id**：那会是个 DB 里一行都没有的号，
    调用方拿去 GET batch 必 404，比 null 更难排查。命中镜不同源时回 null。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(prompt=f"镜{i}", client_ref=f"solo-{i}") for i in range(2)]
        for shot in shots:                       # 先各自走单镜端点（batch_id 均为 null）
            assert (await client.post("/api/video-clips", json=shot,
                                      headers=h)).status_code == 202

        replay = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)

        assert replay.status_code == 202, replay.text
        assert replay.json()["batch_id"] is None
        assert len(replay.json()["clip_ids"]) == 2
        assert await _clip_count() == 2


async def test_pure_replay_reuses_original_batch_id_when_shared(tmp_path, monkeypatch):
    """命中镜同属一个原批次时，回那个**真存在**的批次号（GET batch 拿它能查到东西）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(prompt=f"镜{i}", client_ref=f"grp-{i}") for i in range(3)]
        first = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)
        batch_id = first.json()["batch_id"]

        replay = await client.post("/api/video-clip-batches", json={"shots": shots}, headers=h)

        assert replay.json()["batch_id"] == batch_id
        summary = await client.get(f"/api/video-clip-batches/{batch_id}", headers=h)
        assert summary.status_code == 200 and summary.json()["summary"]["total"] == 3


async def test_get_batch_rejects_malformed_batch_id(tmp_path, monkeypatch):
    """批次号形态闸：纯重放批的 batch_id 是 null，把 "null"/"None" 当批次号查是调用方 bug，
    早点 404 比返回一个空批更好排查。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        for bad in ("null", "None", "vcb_XYZ", "vc_0123456789", "undefined"):
            r = await client.get(f"/api/video-clip-batches/{bad}", headers=bearer(ADMIN_KEY))
            assert r.status_code == 404, f"{bad} → {r.status_code}"


# ── 产物过期语义 ────────────────────────────────────────────────────────────
async def test_expired_flag_separates_reaped_product_from_task_failure(tmp_path, monkeypatch):
    """产物过期走 ``expired`` 布尔键，**不写进 error**。

    error 只装任务失败原因；掺进「产物已 TTL 清理」会让 skill 侧判 error 的分支把一条
    生成成功、只是产物过期的片当成失败。status 仍是 done、credit_count 仍在（供对账）。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips", json=_shot(), headers=h)
        clip_id = r.json()["clip_id"]
        async with db_module.async_session() as s:
            clip = (await s.execute(select(VideoClip))).scalars().one()
            clip.status, clip.credit_count = "done", 25
            clip.video_url = dreamina.clip_public_url(clip_id, "clip.mp4")
            clip.expires_at = datetime.utcnow() + timedelta(days=1)
            await s.commit()
        fresh = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert fresh.json()["expired"] is False and fresh.json()["video_url"]

        async with db_module.async_session() as s:      # 仿 reaper 清完产物
            clip = (await s.execute(select(VideoClip))).scalars().one()
            clip.video_url = clip.video_path = None
            clip.expires_at = datetime.utcnow() - timedelta(days=1)
            await s.commit()

        gone = await client.get(f"/api/video-clips/{clip_id}", headers=h)
        assert gone.json()["expired"] is True
        assert gone.json()["status"] == "done" and gone.json()["error"] is None
        assert gone.json()["credit_count"] == 25, "积分对账要用，产物过期也得留着"


async def test_batch_partial_replay_still_gates_new_shots(tmp_path, monkeypatch):
    """只要有**真要新建**的镜，闸照挂（不能借重放绕过登录/积分守卫）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        old = [_shot(prompt="镜0", client_ref="mix-0")]
        assert (await client.post("/api/video-clip-batches", json={"shots": old},
                                  headers=h)).status_code == 202

        _stub_credit(monkeypatch, logged_in=False)
        mixed = old + [_shot(prompt="镜1", client_ref="mix-1")]
        r = await client.post("/api/video-clip-batches", json={"shots": mixed}, headers=h)
        assert r.status_code == 503
        assert await _clip_count() == 1


async def test_single_clip_replay_is_not_gated_by_login(tmp_path, monkeypatch):
    """单镜重放同理：ref 命中是纯读，掉登录时也该回原 clip_id 而不是 503。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        payload = _shot(client_ref="solo-1")
        first = await client.post("/api/video-clips", json=payload, headers=h)
        assert first.status_code == 202

        _stub_credit(monkeypatch, logged_in=False)
        again = await client.post("/api/video-clips", json=payload, headers=h)
        assert again.status_code == 202, again.text
        assert again.json()["clip_id"] == first.json()["clip_id"]

        _stub_credit(monkeypatch, credit=1)
        broke = await client.post("/api/video-clips", json=payload, headers=h)
        assert broke.status_code == 202 and broke.json()["reused"] is True
        assert await _clip_count() == 1


async def test_batch_materializes_remote_images_concurrently(tmp_path, monkeypatch):
    """带远程图的批**并发**下载参考图：串行时 8 镜 × 30s 会顶穿调用方 60s 超时——
    调用方拿不到 clip_ids，而任务照建照跑照扣分，且没有按运营列任务的端点能自助找回。

    做法上不测墙钟（易抖），改测「三镜必须同时在飞」：串行则谁也等不到 gate，5s 后超时失败。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    started = 0
    gate = asyncio.Event()

    async def _fake_materialize(source, workdir, *, stem="ref"):
        nonlocal started
        started += 1
        if started == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=5)
        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / f"{stem}.png"
        target.write_bytes(_PNG)
        return target

    monkeypatch.setattr(dreamina, "materialize_ref_image", _fake_materialize)
    async with rest_client(tmp_path, monkeypatch) as client:
        shots = [_shot(operation="multimodal2video", prompt=f"镜{i}",
                       image="https://img.example/a.png") for i in range(3)]
        r = await client.post("/api/video-clip-batches", json={"shots": shots},
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert len(r.json()["clip_ids"]) == 3
        assert started == 3


async def test_batch_size_limits(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        over = await client.post(
            "/api/video-clip-batches",
            json={"shots": [_shot() for _ in range(settings.CLIP_MAX_BATCH + 1)]}, headers=h)
        assert over.status_code == 422
        empty = await client.post("/api/video-clip-batches", json={"shots": []}, headers=h)
        assert empty.status_code == 422
        assert await _clip_count() == 0


# ── 积分守卫 ────────────────────────────────────────────────────────────────
async def test_credit_exhausted_returns_409(tmp_path, monkeypatch):
    """余额连最便宜一镜都不够才拦（409）。"""
    _stub_credit(monkeypatch, credit=10)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips", json=_shot(), headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        batch = await client.post("/api/video-clip-batches", json={"shots": [_shot()]},
                                  headers=bearer(ADMIN_KEY))
        assert batch.status_code == 409
        assert await _clip_count() == 0


async def test_low_credit_warns_but_does_not_block(tmp_path, monkeypatch):
    """余额低于粗估：warning 但**照常入队**（扣费 success 才结算，排队中还有变数）。"""
    _stub_credit(monkeypatch, credit=30)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips", json=_shot(duration=15),
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert "warning" in r.json()
        batch = await client.post(
            "/api/video-clip-batches", json={"shots": [_shot(), _shot(), _shot()]},
            headers=bearer(ADMIN_KEY))
        assert batch.status_code == 202 and "warning" in batch.json()
        assert await _clip_count() == 4


async def test_credits_endpoint_low_watermark(tmp_path, monkeypatch):
    _stub_credit(monkeypatch, credit=100)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/video-credits", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200
        assert r.json() == {"credit": 100, "low_threshold_hit": True, "logged_in": True}


# ── 登录态（验收第 6 条）────────────────────────────────────────────────────
async def test_logged_out_blocks_submission_and_reports_status(tmp_path, monkeypatch):
    """拔掉登录态文件：dreamina-status.logged_in=false（仍 200），提交返回明确错误而非静默排队。"""
    _stub_credit(monkeypatch, logged_in=False)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        status = await client.get("/api/dreamina-status", headers=h)
        assert status.status_code == 200
        assert status.json()["logged_in"] is False
        assert status.json()["compliance_confirmed_models"] == []

        r = await client.post("/api/video-clips", json=_shot(), headers=h)
        assert r.status_code == 503, r.text
        # 503/409 走 HTTPException → detail 键（宿主既有错误契约：4xx 业务错误 error，
        # HTTPException 系 detail；skill 侧 _api_error 双键兼容）
        assert "登录态失效" in r.json()["detail"] and "未入队" in r.json()["detail"]
        batch = await client.post("/api/video-clip-batches", json={"shots": [_shot()]},
                                  headers=h)
        assert batch.status_code == 503
        assert await _clip_count() == 0, "登录态失效时绝不静默排队"


async def test_dreamina_status_reports_confirmed_models(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        async with db_module.async_session() as s:
            s.add(VideoClip(clip_id=dreamina.new_clip_id(), operation="text2video",
                            prompt="p", model="seedance2.0fast_vip", duration=5,
                            status="done", created_by=1))
            s.add(VideoClip(clip_id=dreamina.new_clip_id(), operation="text2video",
                            prompt="p", model="seedance2.0", duration=5,
                            status="queued", created_by=1))
            await s.commit()
        r = await client.get("/api/dreamina-status", headers=bearer(ADMIN_KEY))
        # 只认「真出过片」的模型（观测近似，未出片的不算已授权）
        assert r.json()["compliance_confirmed_models"] == ["seedance2.0fast_vip"]


# ── 鉴权 / 归属 ─────────────────────────────────────────────────────────────
async def test_endpoints_require_apikey(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        for method, path in (("post", "/api/video-clips"), ("get", "/api/video-clips/vc_x"),
                             ("post", "/api/video-clip-batches"),
                             ("get", "/api/video-clip-batches/vcb_x"),
                             ("get", "/api/video-credits"), ("get", "/api/dreamina-status")):
            r = await client.request(method.upper(), path, json={})
            assert r.status_code == 401, f"{method} {path} → {r.status_code}"


async def test_get_clip_ownership(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        owner_key, other_key = "op-owner", "op-other"
        await make_operator(owner_key)
        await make_operator(other_key)
        created = await client.post("/api/video-clips", json=_shot(),
                                    headers=bearer(owner_key))
        clip_id = created.json()["clip_id"]

        assert (await client.get(f"/api/video-clips/{clip_id}",
                                 headers=bearer(other_key))).status_code == 403
        # admin 全见；本人可见
        assert (await client.get(f"/api/video-clips/{clip_id}",
                                 headers=bearer(ADMIN_KEY))).status_code == 200
        assert (await client.get(f"/api/video-clips/{clip_id}",
                                 headers=bearer(owner_key))).status_code == 200
        missing = await client.get("/api/video-clips/vc_ffffffffff", headers=bearer(owner_key))
        assert missing.status_code == 404
        assert (await client.get("/api/video-clip-batches/vcb_ffffffffff",
                                 headers=bearer(owner_key))).status_code == 404


async def test_batch_ownership(tmp_path, monkeypatch):
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        owner_key, other_key = "op-b-owner", "op-b-other"
        await make_operator(owner_key)
        await make_operator(other_key)
        r = await client.post("/api/video-clip-batches", json={"shots": [_shot()]},
                              headers=bearer(owner_key))
        batch_id = r.json()["batch_id"]
        assert (await client.get(f"/api/video-clip-batches/{batch_id}",
                                 headers=bearer(other_key))).status_code == 403
        assert (await client.get(f"/api/video-clip-batches/{batch_id}",
                                 headers=bearer(ADMIN_KEY))).status_code == 200


# ── 产物直链 ────────────────────────────────────────────────────────────────
async def test_serve_clip_product(tmp_path, monkeypatch):
    """免鉴权直链：token 目录即访问控制；错 token / 穿越一律 404。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    clip_id = dreamina.new_clip_id()
    (dreamina.clip_dir(clip_id) / "clip.mp4").write_bytes(b"mp4-bytes")
    token_dir = dreamina.clip_token_dir(clip_id)
    async with rest_client(tmp_path, monkeypatch) as client:
        ok = await client.get(f"/uploads/clips/{token_dir}/clip.mp4")   # 不带 apikey
        assert ok.status_code == 200 and ok.content == b"mp4-bytes"
        assert ok.headers["content-type"] == "video/mp4"

        # token 猜错（形态合法但 HMAC 不对）→ 404
        wrong = f"{clip_id}-" + "0" * 16
        assert (await client.get(f"/uploads/clips/{wrong}/clip.mp4")).status_code == 404
        # 文件名不在白名单 → 404（原始 {submit_id}_video_0.mp4 也取不到）
        assert (await client.get(
            f"/uploads/clips/{token_dir}/1111222233334444_video_0.mp4")).status_code == 404
        assert (await client.get(f"/uploads/clips/{token_dir}/clip.mp4.bak")).status_code == 404


async def test_serve_clip_product_rejects_traversal(tmp_path, monkeypatch):
    """直接以病态参数调处理函数：正则白名单先挡下，不落到文件系统。"""
    import pytest
    from fastapi import HTTPException

    from app.http import dreamina_rest

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    for token_dir, name in (("..", "clip.mp4"), ("../../etc", "clip.mp4"),
                            ("vc_0123456789-0123456789abcdef", "../../../etc/passwd"),
                            ("vc_0123456789-0123456789abcdef", "clip.mp4\n")):
        with pytest.raises(HTTPException) as exc:
            await dreamina_rest.serve_clip_product(token_dir, name)
        assert exc.value.status_code == 404


# ── 多图参考 / 首尾帧（CLI 能力面开放）──────────────────────────────────────
def _seed_uploads(tmp_path, count: int, folder: str = "refs") -> list[str]:
    """在本服务图床里放 count 张真 PNG，返回它们的 /uploads 路径（顺序即返回序）。"""
    d = tmp_path / "uploads" / folder
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(count):
        (d / f"{i:02d}.png").write_bytes(_PNG + str(i).encode())
        out.append(f"/uploads/{folder}/{i:02d}.png")
    return out


async def _only_clip() -> VideoClip:
    async with db_module.async_session() as s:
        return (await s.execute(select(VideoClip))).scalars().one()


async def test_multi_reference_images_reach_cli_as_repeated_flags(tmp_path, monkeypatch):
    """多图参考：3 张（场景 + 人物定妆 + 道具）各自物化成独立副本，CLI 收到 3 个 --image。

    跨镜人物一致性是这条产线最大的质量风险——只有一个图槽时身份只能靠出图阶段锚，
    视频生成阶段照样漂。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 3)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", images=images),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    assert len(paths) == 3 and len(set(paths)) == 3, "三张要是三个独立副本，不能互相覆盖"
    for p in paths:
        assert Path(p).is_file() and dreamina.clip_token_dir(clip.clip_id) in p
    assert [a for a in dreamina.build_submit_args(clip) if a.startswith("--image=")] == [
        f"--image={p}" for p in paths]


async def test_ref_image_count_ceiling_is_per_model(tmp_path, monkeypatch):
    """张数上限按模型分档，超限**当场 422**（白跑一次提交就是白烧一次积分）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 31)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        over25 = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", model="seedance2.5", images=images),
            headers=h)
        assert over25.status_code == 422, over25.text

        over20 = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", model="seedance2.0fast",
                       images=images[:10]),
            headers=h)
        assert over20.status_code == 422, over20.text
        assert "9" in over20.text, "文案要说清 2.0 家族的档位，不能只报一个光秃秃的越界"

        ok20 = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", model="seedance2.0fast",
                       images=images[:9]),
            headers=h)
        assert ok20.status_code == 202, ok20.text
        ok25 = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", model="seedance2.5", duration=5,
                       images=images[:30]),
            headers=h)
        assert ok25.status_code == 202, ok25.text
    assert await _clip_count() == 2, "两条 422 都不许留下任务行"


async def test_image_and_images_together_is_422(tmp_path, monkeypatch):
    """``image`` 与 ``images`` 同给 → 422：本层的规矩是宁可 422 也不静默丢字段。

    静默取其一时调用方无从发现另一半没生效，而这里每一次提交都在花钱。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 2)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", image=images[0], images=images),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert await _clip_count() == 0


async def test_single_image_call_is_byte_identical(tmp_path, monkeypatch):
    """回归锁：老的单 ``image`` 调用一切照旧——image_path 仍是那一张，CLI 参数仍是单个 --image。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 1)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", image=images[0]),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    assert clip.image_source == images[0]
    assert clip.image_path.endswith("ref.png")
    assert dreamina.ref_paths(clip) == [clip.image_path]
    args = dreamina.build_submit_args(clip)
    assert [a for a in args if a.startswith("--image=")] == [f"--image={clip.image_path}"]


async def test_frames2video_first_and_last(tmp_path, monkeypatch):
    """首尾帧：两张图物化成 [首, 尾] 两元素，CLI 收到 --first/--last。

    这是分镜级运动控制的入口——指定一个镜头从哪一帧开始、到哪一帧结束，中间交给模型。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    first, last = _seed_uploads(tmp_path, 2, folder="frames")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="frames2video", ratio=None, model="seedance2.5",
                       duration=8, first_image=first, last_image=last),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        got = await client.get(f"/api/video-clips/{r.json()['clip_id']}",
                               headers=bearer(ADMIN_KEY))
        assert got.json()["operation"] == "frames2video"

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    assert len(paths) == 2 and paths[0] != paths[1]
    assert Path(paths[0]).read_bytes() == _PNG + b"0"      # [0] 必须是首帧，顺序不许错
    assert Path(paths[1]).read_bytes() == _PNG + b"1"
    args = dreamina.build_submit_args(clip)
    assert args[0] == "frames2video"
    assert f"--first={paths[0]}" in args and f"--last={paths[1]}" in args


async def test_frames2video_validation_matrix(tmp_path, monkeypatch):
    """首尾帧的组合闸：缺一帧 / 传 ratio / 传 image(s) / 时长越界，一律 422 不建行。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    first, last = _seed_uploads(tmp_path, 2, folder="frames")
    frames = dict(operation="frames2video", ratio=None, model="seedance2.5",
                  first_image=first, last_image=last)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        bad = [
            _shot(**{**frames, "last_image": None}),          # 缺尾帧
            _shot(**{**frames, "first_image": None}),         # 缺首帧
            _shot(**{**frames, "ratio": "9:16"}),             # ratio 由首帧推断，传了就 422
            _shot(**{**frames, "image": first}),              # 首尾帧模式不收 image
            _shot(**{**frames, "images": [first, last]}),     # 也不收 images
            _shot(**{**frames, "model": "seedance2.0fast", "duration": 16}),  # 该档只到 15s
            _shot(operation="image2video", ratio=None, image=first,
                  first_image=first),                          # 首尾帧字段只属于 frames2video
            _shot(first_image=first, last_image=last),         # text2video 也不收
        ]
        for payload in bad:
            r = await client.post("/api/video-clips", json=payload, headers=h)
            assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"
        assert await _clip_count() == 0

        ok = await client.post("/api/video-clips",
                               json=_shot(**frames, duration=30), headers=h)
        assert ok.status_code == 202, ok.text


async def test_batch_shots_carry_new_fields(tmp_path, monkeypatch):
    """批量 ``shots[]`` 自动继承新字段：一批里混多图镜与首尾帧镜都能落行。

    电影化的 25-30 镜只能走批量端点，新能力不透到这里等于没开放。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 3)
    async with rest_client(tmp_path, monkeypatch) as client:
        shots = [
            _shot(operation="multimodal2video", images=images),
            _shot(operation="frames2video", ratio=None, model="seedance2.5", duration=8,
                  first_image=images[0], last_image=images[2]),
        ]
        r = await client.post("/api/video-clip-batches", json={"shots": shots},
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        clip_ids = r.json()["clip_ids"]
        assert len(clip_ids) == 2 and all(clip_ids)

    async with db_module.async_session() as s:
        rows = (await s.execute(
            select(VideoClip).order_by(VideoClip.batch_index))).scalars().all()
    assert len(dreamina.ref_paths(rows[0])) == 3
    assert len(dreamina.ref_paths(rows[1])) == 2
    assert rows[1].operation == "frames2video"


# ── 多帧故事 multiframe2video ────────────────────────────────────────────────
def _mf(**kw) -> dict:
    """multiframe2video 入参骨架（该 operation 不收 ratio / model 不可选，故都不给）。"""
    base = {"operation": "multiframe2video"}
    base.update(kw)
    return base


async def test_multiframe_shorthand_two_images(tmp_path, monkeypatch):
    """恰好 2 张走简写：prompt + duration，CLI 参数是 --images 逗号串 + --prompt + --duration。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 2, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips",
                              json=_mf(images=images, prompt="空椅推向窗外", duration=5),
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    assert len(paths) == 2
    args = dreamina.build_submit_args(clip)
    assert args[0] == "multiframe2video"
    assert f"--images={','.join(paths)}" in args
    assert "--prompt=空椅推向窗外" in args and "--duration=5" in args
    assert not any(a.startswith("--transition-") for a in args)


async def test_multiframe_five_images_with_four_transitions(tmp_path, monkeypatch):
    """5 张 + 4 段：逐段提示词与时长各 4 个；台账里 prompt/duration 落派生值。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 5, folder="mf")
    prompts = ["一到二", "二到三", "三到四", "四到五"]
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_mf(images=images, transition_prompts=prompts,
                     transition_durations=[2.0, 3.0, 4.0, 5.0]),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    args = dreamina.build_submit_args(clip)
    assert len([a for a in args if a.startswith("--transition-prompt=")]) == 4
    assert len([a for a in args if a.startswith("--transition-duration=")]) == 4
    assert not any(a.startswith("--prompt=") or a.startswith("--duration=") for a in args)
    # 台账两列是派生值（NOT NULL 列要有意义的内容）：逐段提示词连起来 + 总时长
    assert all(p in clip.prompt for p in prompts)
    assert clip.duration == 14


async def test_multiframe_transitions_default_durations(tmp_path, monkeypatch):
    """不给逐段时长：CLI 侧按每段 3s 默认，台账总时长按同一口径算（3 张 → 6s）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 3, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_mf(images=images, transition_prompts=["一到二", "二到三"]),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    assert clip.duration == 6
    assert not any(a.startswith("--transition-duration")
                   for a in dreamina.build_submit_args(clip))


async def test_multiframe_validation_matrix(tmp_path, monkeypatch):
    """multiframe 的组合闸：段数不匹配 / 张数越界 / 段时长越界 / 总时长不足 / 简写字段错位。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    imgs = _seed_uploads(tmp_path, 21, folder="mf")
    five, two, three = imgs[:5], imgs[:2], imgs[:3]
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        bad = [
            # 5 张只给 3 段（需要 4 段）
            _mf(images=five, transition_prompts=["a", "b", "c"]),
            _mf(images=imgs[:1], prompt="x", duration=5),          # 1 张不成故事
            _mf(images=imgs, transition_prompts=["a"] * 20),        # 21 张越界
            _mf(images=three, transition_prompts=["a", "b"],
                transition_durations=[9.0, 3.0]),                   # 某段 9s 越界
            _mf(images=three, transition_prompts=["a", "b"],
                transition_durations=[0.5, 3.0]),                   # 某段 <1s
            _mf(images=two, transition_prompts=["a"],
                transition_durations=[1.5]),                        # 总时长 <2s
            _mf(images=three, transition_prompts=["a", "b"], prompt="整片描述"),  # 长式不收 prompt
            _mf(images=three, transition_prompts=["a", "b"], duration=6),        # 长式不收 duration
            _mf(images=three, prompt="x", duration=5),              # 3 张必须逐段给
            _mf(images=two, duration=5),                            # 简写缺 prompt
            _mf(images=two, prompt="x", duration=1),                # 简写 1s → 总时长不足
            _mf(images=two, prompt="x", duration=9),                # 简写超 8s
            _mf(images=two, prompt="x", duration=5, ratio="9:16"),  # 画幅由首图推断
            _mf(images=two, prompt="x", duration=5, image=two[0]),  # 不收 image
            _mf(images=two, prompt="x", duration=5, first_image=two[0]),  # 不收首尾帧
            _mf(prompt="x", duration=5),                            # 完全没给 images
            _mf(images=two, prompt="x", duration=5,
                transition_durations=[3.0]),                        # 只给时长不给提示词
            # 非 multiframe 的 operation 不认这两个新字段
            _shot(transition_prompts=["a"]),
            _shot(transition_durations=[3.0]),
        ]
        for payload in bad:
            r = await client.post("/api/video-clips", json=payload, headers=h)
            assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"
        assert await _clip_count() == 0

        # 段数提示要说清"要几段"，不能只报一个光秃秃的越界
        mismatch = await client.post(
            "/api/video-clips",
            json=_mf(images=five, transition_prompts=["a", "b", "c"]), headers=h)
        assert "4" in mismatch.text


async def test_multiframe_rejects_model_because_platform_fixed(tmp_path, monkeypatch):
    """model 在这条 operation 上由平台固定：传了非默认值 **422 而不是静默忽略**。

    静默忽略会让调用方以为自己选上了档（与 frames2video 拒绝 ratio 同一条纪律）。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_mf(images=two, prompt="x", duration=5, model="seedance2.0fast"),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 422, r.text
        assert "不可选" in r.text
        assert await _clip_count() == 0


async def test_multiframe_warns_price_is_unmeasured(tmp_path, monkeypatch):
    """该模式单价未实测 → 提交时如实给出"估不出"的告警，别让运营以为余额够。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips",
                              json=_mf(images=two, prompt="x", duration=5), headers=h)
        assert r.status_code == 202, r.text
        assert "未实测" in r.json().get("warning", "")

        batch = await client.post(
            "/api/video-clip-batches",
            json={"shots": [_mf(images=two, prompt="x", duration=5, client_ref="mf-1")]},
            headers=h)
        assert batch.status_code == 202, batch.text
        assert "未实测" in batch.json().get("warning", "")


async def test_multiframe_credit_guard_still_hard_blocks(tmp_path, monkeypatch):
    """估不出价 ≠ 不设防：余额连最便宜一镜都不够时照旧 409（保守拦截）。"""
    _stub_credit(monkeypatch, credit=10)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips",
                              json=_mf(images=two, prompt="x", duration=5),
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert await _clip_count() == 0


async def test_prompt_and_duration_stay_required_for_other_operations(tmp_path, monkeypatch):
    """回归锁：为了 multiframe 放宽的 prompt / duration，在其余 operation 上仍是必填 + 老边界。

    放宽字段界是为了让 multiframe 长式能不带这两个字段；一旦顺手把别的 operation 也放开，
    text2video 少传 duration 就会一路跑到 CLI 才失败，那正是本层最想避免的结局。
    """
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        for payload in ({"operation": "text2video", "duration": 5},        # 缺 prompt
                        {"operation": "text2video", "prompt": "x"},        # 缺 duration
                        _shot(duration=3),                                  # 老下限 4s 不变
                        _shot(duration=0), _shot(duration=31)):
            r = await client.post("/api/video-clips", json=payload, headers=h)
            assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"
        assert await _clip_count() == 0


async def test_batch_carries_multiframe_shots(tmp_path, monkeypatch):
    """批量 shots[] 同样吃 multiframe（25-30 镜的电影化只能走批量端点）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 3, folder="mf")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={"shots": [
            _mf(images=images[:2], prompt="甲到乙", duration=4),
            _mf(images=images, transition_prompts=["一到二", "二到三"],
                transition_durations=[3.0, 4.0]),
        ]}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert len(r.json()["clip_ids"]) == 2

    async with db_module.async_session() as s:
        rows = (await s.execute(
            select(VideoClip).order_by(VideoClip.batch_index))).scalars().all()
    assert all(r.operation == "multiframe2video" for r in rows)
    assert len(dreamina.ref_paths(rows[1])) == 3
    assert rows[1].duration == 7


# ── 顺序即语义：多图不许去重 / 重排 / 合并 ─────────────────────────────────────
async def test_reference_image_order_is_preserved_exactly(tmp_path, monkeypatch):
    """多图的**数组顺序 = @图片N 编号顺序**，逐位比对，不只是「数量对」。

    prompt 里「@图片1 锁人物、@图片2 定场景」是靠位置绑定语义的。顺序一乱不会报错，
    只会默默出错图——这类 bug 线上最难查，故这里按位钉死到具体内容。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 3, folder="ordered")   # 内容各带序号，可反查是哪一张
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", images=images),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    # 副本内容按位对回请求数组：第 i 张必须是请求里的第 i 张
    for i, path in enumerate(paths):
        assert Path(path).read_bytes() == _PNG + str(i).encode(), f"第 {i + 1} 张错位了"
    flags = [a for a in dreamina.build_submit_args(clip) if a.startswith("--image=")]
    assert flags == [f"--image={p}" for p in paths], "CLI 参数顺序必须与数组顺序逐位一致"


async def test_duplicate_image_urls_are_not_merged(tmp_path, monkeypatch):
    """**同一个 URL 传两次 = 两个槽**，绝不去重合并。

    URL 相同不代表语义相同：prompt 完全可以写「@图片1 锁人物、@图片2 定场景」而两者
    指向同一张图。合并会让**后面所有编号前移**，prompt 的绑定全错位，且不报错。
    这条锁防的是将来有人为省一次下载做「相同 URL 只物化一次」的优化——那时行为变了
    测试却不红，线上默默出错图。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    first, second = _seed_uploads(tmp_path, 2, folder="dup")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_shot(operation="multimodal2video", images=[first, first, second]),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    assert len(paths) == 3, "三个槽就要三条路径，去重就是错位"
    assert len(set(paths)) == 3, "同 URL 的两个槽也要各自独立副本，不能共用一个文件"
    assert all(Path(p).is_file() for p in paths)
    # 顺序 A,A,B：前两张内容相同、第三张不同
    assert Path(paths[0]).read_bytes() == Path(paths[1]).read_bytes() == _PNG + b"0"
    assert Path(paths[2]).read_bytes() == _PNG + b"1"
    flags = [a for a in dreamina.build_submit_args(clip) if a.startswith("--image=")]
    assert flags == [f"--image={p}" for p in paths] and len(flags) == 3


async def test_batch_shot_keeps_image_order(tmp_path, monkeypatch):
    """批量端点与单镜共用同一条物化路径，这里只做顺序冒烟（同 URL 也不合并）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    first, second = _seed_uploads(tmp_path, 2, folder="bdup")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={"shots": [
            _shot(operation="multimodal2video", images=[second, first, first]),
        ]}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    assert len(paths) == 3 and len(set(paths)) == 3
    assert Path(paths[0]).read_bytes() == _PNG + b"1"       # 第一个槽是 second
    assert Path(paths[1]).read_bytes() == Path(paths[2]).read_bytes() == _PNG + b"0"


async def test_multiframe_image_order_is_preserved(tmp_path, monkeypatch):
    """多帧故事的图序就是**故事顺序**，同样逐位钉死（--images 逗号串按位对齐）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 4, folder="story")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/video-clips",
            json=_mf(images=images, transition_prompts=["一到二", "二到三", "三到四"]),
            headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text

    clip = await _only_clip()
    paths = dreamina.ref_paths(clip)
    for i, path in enumerate(paths):
        assert Path(path).read_bytes() == _PNG + str(i).encode(), f"第 {i + 1} 帧错位了"
    assert f"--images={','.join(paths)}" in dreamina.build_submit_args(clip)


# ── multiframe 的 model 占位与逐段原话回显 ───────────────────────────────────
async def test_multiframe_stores_placeholder_not_a_real_model(tmp_path, monkeypatch):
    """multiframe 行**绝不存真实模型名**：那条 operation 的模型由平台固定，存 seedance2.5
    等于在库里记一条我们明知不成立的事实，日后排查/统计/对账的人会被骗且毫无痕迹。

    存一个明显不是档位名的占位符，下游一眼能看出「这列对这条 operation 不适用」。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="ph")
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips",
                              json=_mf(images=two, prompt="甲到乙", duration=5), headers=h)
        assert r.status_code == 202, r.text
        clip = await _only_clip()
        assert clip.model == dreamina.MULTIFRAME_MODEL_PLACEHOLDER
        assert clip.model not in ("seedance2.0", "seedance2.0fast", "seedance2.0_vip",
                                  "seedance2.0fast_vip", "seedance2.0mini", "seedance2.5")
        # 占位符也绝不能混进提交参数（CLI 在这条子命令上不收 --model_version）
        assert not any(a.startswith("--model_version")
                       for a in dreamina.build_submit_args(clip))
        got = await client.get(f"/api/video-clips/{clip.clip_id}", headers=h)
        assert got.json()["model"] == dreamina.MULTIFRAME_MODEL_PLACEHOLDER


async def test_multiframe_rejects_explicit_model_but_not_the_default(tmp_path, monkeypatch):
    """**显式传 model** 才 422；根本没传就走平台固定，不误伤默认值。

    靠 pydantic 的 model_fields_set 区分「请求体里真的出现过这个键」与「用了字段默认值」，
    因此这一闸连「显式传了恰好等于默认档的 seedance2.5」都能挡住。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="ph2")
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        for model in ("seedance2.0fast", dreamina.DEFAULT_MODEL):
            r = await client.post(
                "/api/video-clips",
                json=_mf(images=two, prompt="x", duration=5, model=model), headers=h)
            assert r.status_code == 422, f"{model} → {r.text}"
            assert "不可选" in r.text
        assert await _clip_count() == 0

        ok = await client.post("/api/video-clips",
                               json=_mf(images=two, prompt="x", duration=5), headers=h)
        assert ok.status_code == 202, ok.text


async def test_multiframe_get_returns_raw_segments_not_only_derived(tmp_path, monkeypatch):
    """GET 必须回**逐段原话**：台账里的 prompt 是连起来的派生串，调用方拿它还原不了
    自己传了什么，事后查「我第 3 段写的什么」必须查得到原文。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    images = _seed_uploads(tmp_path, 4, folder="raw")
    prompts = ["空椅缓缓转向窗", "窗光漫过来访者侧脸", "镜头拉远收进整个诊室"]
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post(
            "/api/video-clips",
            json=_mf(images=images, transition_prompts=prompts,
                     transition_durations=[3.0, 4.0, 5.0]),
            headers=h)
        assert r.status_code == 202, r.text
        body = (await client.get(f"/api/video-clips/{r.json()['clip_id']}",
                                 headers=h)).json()

    assert [t["prompt"] for t in body["transitions"]] == prompts, "逐段原话要能原样取回"
    assert [t["duration"] for t in body["transitions"]] == [3.0, 4.0, 5.0]
    # 派生的 prompt / duration 只落库供人看台账，**本来就不在对外视图里**——
    # 加上 transitions 之后，调用方能拿到的这条 operation 的入参就只有原话这一份。
    clip = await _only_clip()
    assert clip.prompt == " → ".join(prompts) and clip.duration == 12
    assert "prompt" not in body and "duration" not in body


async def test_non_multiframe_clip_has_null_transitions(tmp_path, monkeypatch):
    """其余 operation 的 transitions 恒为 null（别让调用方对着空列表写分支）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips", json=_shot(), headers=h)
        body = (await client.get(f"/api/video-clips/{r.json()['clip_id']}",
                                 headers=h)).json()
        assert body["transitions"] is None


# ── 预估费用回显（estimated_credits）─────────────────────────────────────────
async def test_single_clip_returns_estimated_credits(tmp_path, monkeypatch):
    """提交前就能知道这一镜大概烧多少：默认档 2.5 按 130/5s 线性折算。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        r = await client.post("/api/video-clips",
                              json=_shot(model="seedance2.5", duration=10), headers=h)
        assert r.status_code == 202, r.text
        assert r.json()["estimated_credits"] == 260

        cheap = await client.post("/api/video-clips",
                                  json=_shot(model="seedance2.0fast", duration=5), headers=h)
        assert cheap.json()["estimated_credits"] == 25


async def test_estimated_credits_is_null_for_unmeasured_tier(tmp_path, monkeypatch):
    """估不出就**回 null**，不回 0——0 会被读成「这镜不要钱」，正好是最危险的误读。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clips",
                              json=_shot(model="seedance2.0mini", duration=5),
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert r.json()["estimated_credits"] is None


async def test_replay_estimates_zero_new_spend(tmp_path, monkeypatch):
    """口径是「**本次新增**的预估消耗」：纯重放零新建零扣分，故为 0。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        payload = _shot(model="seedance2.5", duration=10, client_ref="est-replay")
        first = await client.post("/api/video-clips", json=payload, headers=h)
        second = await client.post("/api/video-clips", json=payload, headers=h)
        assert first.json()["estimated_credits"] == 260
        assert second.json()["estimated_credits"] == 0
        assert await _clip_count() == 1


async def test_batch_returns_total_and_per_shot_estimates(tmp_path, monkeypatch):
    """整批合计 + 逐镜，逐镜与 shots 等长同序（下标即 shot-NN）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={"shots": [
            _shot(model="seedance2.5", duration=10),      # 260
            _shot(model="seedance2.0fast", duration=5),   # 25
            _shot(model="seedance2.0fast_vip", duration=8),  # ceil(8/5)=2 档 × 55
        ]}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["estimated_credits_per_shot"] == [260, 25, 110]
        assert body["estimated_credits"] == 395
        assert len(body["estimated_credits_per_shot"]) == len(body["clip_ids"])


async def test_batch_total_is_null_when_any_shot_unpriced(tmp_path, monkeypatch):
    """批里有估不出的镜 → **合计回 null**：给一个漏算了几镜的数当总账比没有更危险。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={"shots": [
            _shot(model="seedance2.5", duration=5),
            _shot(model="seedance2.0mini", duration=5),
        ]}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert r.json()["estimated_credits_per_shot"] == [130, None]
        assert r.json()["estimated_credits"] is None


async def test_batch_replayed_shots_count_zero(tmp_path, monkeypatch):
    """重放命中的镜在逐镜里是 0（那笔钱首发时就算过了），合计只含本次新增。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        first = _shot(model="seedance2.5", duration=5, client_ref="mix-1")
        second = _shot(model="seedance2.5", duration=5, client_ref="mix-2")
        await client.post("/api/video-clip-batches", json={"shots": [first]}, headers=h)
        again = await client.post("/api/video-clip-batches",
                                  json={"shots": [first, second]}, headers=h)
        assert again.status_code == 202, again.text
        assert again.json()["estimated_credits_per_shot"] == [0, 130]
        assert again.json()["estimated_credits"] == 130


# ── 预算护栏 max_credits ─────────────────────────────────────────────────────
async def test_max_credits_rejects_whole_batch_with_zero_rows(tmp_path, monkeypatch):
    """超预算 → **整批 409，一镜都不建**（查库确认零行）。

    这道闸的意义就在「零副作用」：拒绝之后留下半批任务，比没有护栏更糟——运营以为没提交，
    队列里却在烧分，且排队中的即梦任务无法取消。
    """
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        shots = [_shot(model="seedance2.5", duration=10)] * 2      # 预估 520
        r = await client.post("/api/video-clip-batches",
                              json={"shots": shots, "max_credits": 519},
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert "520" in r.text and "519" in r.text
        assert await _clip_count() == 0, "预算护栏拒绝后不许留下任何任务行"


async def test_max_credits_exactly_equal_passes(tmp_path, monkeypatch):
    """恰好等于预估 → 放行（上限是「不许超」，不是「必须留余量」）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        shots = [_shot(model="seedance2.5", duration=10)] * 2      # 预估 520
        r = await client.post("/api/video-clip-batches",
                              json={"shots": shots, "max_credits": 520},
                              headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert r.json()["estimated_credits"] == 520
        assert await _clip_count() == 2


async def test_max_credits_refuses_batch_containing_unpriceable_shot(tmp_path, monkeypatch):
    """批内含估不出价的镜 → **409 并说清是哪一镜**，绝不「按能估的部分」放行。

    护栏对运营的承诺是「绝不超支」；含未知项时兑现不了这个承诺，悄悄放行等于卖假保证。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="guard")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={
            "shots": [_shot(model="seedance2.5", duration=5),
                      _mf(images=two, prompt="空椅转向窗", duration=5)],
            "max_credits": 10000,
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert "multiframe2video" in r.text and "第 2 镜" in r.text
        assert "拆分" in r.text and "max_credits" in r.text
        assert await _clip_count() == 0


async def test_without_max_credits_batch_behaves_exactly_as_before(tmp_path, monkeypatch):
    """回归锁：**不传 max_credits 就一步预算判定都不做**。

    同样这批（远超任何合理预算、且含估不出价的 multiframe）在不带上限时照旧全部入队——
    护栏是显式的可选闸，不是偷偷加上的新默认。
    """
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    two = _seed_uploads(tmp_path, 2, folder="noguard")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={
            "shots": [_shot(model="seedance2.5", duration=30)] * 3
                     + [_mf(images=two, prompt="空椅转向窗", duration=5)],
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        assert len(r.json()["clip_ids"]) == 4 and all(r.json()["clip_ids"])
        assert r.json()["batch_id"].startswith("vcb_")
        assert await _clip_count() == 4


async def test_max_credits_guard_runs_before_login_and_materialization(tmp_path, monkeypatch):
    """闸排在**一切副作用之前**：掉登录（503）与坏参考图（400）都轮不到，先 409。

    顺序错了就会先物化图、先跑 CLI 查登录，再来说「超预算」——那时钱和时间都花过了。
    """
    _stub_credit(monkeypatch, logged_in=False)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video-clip-batches", json={
            "shots": [_shot(operation="image2video", ratio=None,
                            image="/uploads/gone/01.png",
                            model="seedance2.5", duration=10)],
            "max_credits": 1,
        }, headers=bearer(ADMIN_KEY))
        assert r.status_code == 409, r.text
        assert await _clip_count() == 0


async def test_max_credits_ignores_pure_replay(tmp_path, monkeypatch):
    """纯重放不花新钱 → 预估 0，**再小的预算也放行**（否则重放会被自己的护栏卡死）。"""
    _stub_credit(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        shots = [_shot(model="seedance2.5", duration=10, client_ref="guard-replay")]
        first = await client.post("/api/video-clip-batches",
                                  json={"shots": shots, "max_credits": 260}, headers=h)
        assert first.status_code == 202, first.text
        again = await client.post("/api/video-clip-batches",
                                  json={"shots": shots, "max_credits": 1}, headers=h)
        assert again.status_code == 202, again.text
        assert again.json()["estimated_credits"] == 0
        assert await _clip_count() == 1


# ── 段尾帧提取 ──────────────────────────────────────────────────────────────
def _make_mp4(path: Path, seconds: int = 2) -> Path:
    """真跑 ffmpeg 造一段素材（与 test_video_muxer 同款）。"""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=160x120:rate=10", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


async def _seed_done_clip(client, headers, *, seconds: int = 2) -> tuple[str, Path]:
    """建一条 clip 并落成 done + 一段真 mp4（调用方须先把 DATA_DIR 指到 tmp_path）。"""
    r = await client.post("/api/video-clips", json=_shot(), headers=headers)
    clip_id = r.json()["clip_id"]
    video = _make_mp4(dreamina.clip_dir(clip_id) / "clip.mp4", seconds=seconds)
    async with db_module.async_session() as s:
        clip = (await s.execute(
            select(VideoClip).where(VideoClip.clip_id == clip_id))).scalars().one()
        clip.status = "done"
        clip.video_path = str(video)
        clip.video_url = dreamina.clip_public_url(clip_id, "clip.mp4")
        clip.expires_at = datetime.utcnow() + timedelta(days=7)
        await s.commit()
    return clip_id, video


async def test_frame_last_returns_direct_link_reusable_as_reference(tmp_path, monkeypatch):
    """回直链而不是图片流：它能**原样当下一镜的首帧参考**传回来，省一个下载+上传来回。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        clip_id, _video = await _seed_done_clip(client, h)
        r = await client.get(f"/api/video-clips/{clip_id}/frame", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["t"] == "last"
        assert body["frame_url"].endswith("/frame_last.png")
        assert (dreamina.clip_dir(clip_id) / "frame_last.png").stat().st_size > 0

        # 免鉴权直链能取回，且 Content-Type 是 PNG（MP4 白名单不能把它当视频回）
        raw = await client.get(body["frame_url"])
        assert raw.status_code == 200 and raw.headers["content-type"] == "image/png"
        assert raw.content.startswith(b"\x89PNG")

        # 真正的目的：这条路径直接作下一段的 first_image
        nxt = await client.post("/api/video-clips", json=_shot(
            operation="frames2video", ratio=None,
            first_image=body["frame_url"], last_image=body["frame_url"]), headers=h)
        assert nxt.status_code == 202, nxt.text


async def test_frame_at_second_and_idempotent(tmp_path, monkeypatch):
    """t=秒数取指定时刻；同 t 重复请求复用已抽的帧（不重跑 ffmpeg）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        clip_id, _video = await _seed_done_clip(client, h)
        r = await client.get(f"/api/video-clips/{clip_id}/frame?t=1", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["frame_url"].endswith("/frame_1.000.png")
        frame = dreamina.clip_dir(clip_id) / "frame_1.000.png"
        stamp = frame.stat().st_mtime_ns

        again = await client.get(f"/api/video-clips/{clip_id}/frame?t=1.0", headers=h)
        assert again.status_code == 200 and again.json()["frame_url"] == r.json()["frame_url"]
        assert frame.stat().st_mtime_ns == stamp, "同 t 不该重抽"


async def test_frame_error_branches(tmp_path, monkeypatch):
    """每种「拿不到帧」各有各的码，**绝不回半张图**：没跑完 409 / 产物没了 410 / t 越界 422。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        queued = await client.post("/api/video-clips", json=_shot(), headers=h)
        pending_id = queued.json()["clip_id"]
        not_done = await client.get(f"/api/video-clips/{pending_id}/frame", headers=h)
        assert not_done.status_code == 409, not_done.text

        clip_id, video = await _seed_done_clip(client, h)
        for bad_t in ("abc", "-1", "", "00:00:01"):
            bad = await client.get(f"/api/video-clips/{clip_id}/frame?t={bad_t}", headers=h)
            assert bad.status_code == 422, f"t={bad_t!r} → {bad.status_code}"
        over = await client.get(f"/api/video-clips/{clip_id}/frame?t=99", headers=h)
        assert over.status_code == 422 and "超出视频时长" in over.text
        assert not (dreamina.clip_dir(clip_id) / "frame_99.000.png").exists()

        missing = await client.get("/api/video-clips/vc_00000000ff/frame", headers=h)
        assert missing.status_code == 404, missing.text

        video.unlink()          # 仿 TTL 清理：行还在、产物没了
        gone = await client.get(f"/api/video-clips/{clip_id}/frame", headers=h)
        assert gone.status_code == 410, gone.text


async def test_frame_respects_ownership(tmp_path, monkeypatch):
    """归属照旧：别人的片段抽不了帧（帧就是内容本身）。"""
    _stub_credit(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        h = bearer(ADMIN_KEY)
        clip_id, _video = await _seed_done_clip(client, h)
        await make_operator("other-key")
        r = await client.get(f"/api/video-clips/{clip_id}/frame", headers=bearer("other-key"))
        assert r.status_code == 403, r.text
