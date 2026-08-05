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
            _shot(model="seedance3.0"), _shot(operation="text2image"),      # 枚举越界
            _shot(prompt=""), _shot(prompt="x" * 2001),                     # 提示词长度
            _shot(ratio="2:3"),                                             # 画幅越界
            _shot(client_ref="x" * 65),                                     # 幂等键过长
        ]
        for payload in bad:
            r = await client.post("/api/video-clips", json=payload, headers=h)
            assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"
        assert await _clip_count() == 0


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

    async def _fake_materialize(source, workdir):
        nonlocal started
        started += 1
        if started == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=5)
        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / "ref.png"
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
