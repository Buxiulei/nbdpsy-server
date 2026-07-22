"""视频管线 REST 端点 + /uploads/video 静态取图路由的真断言测试。

覆盖 6 端点语义（建/查/列/重试/修订/删）+ apikey 中间件下的鉴权行为（401 缺 key、403 越权、
404 不存在）+ 方案 C 入队语义（建 job 落 queued、retry/revise 复位 queued）+ 免鉴权产物直链
的防路径穿越。隔离库跑真实 lifespan（rest_helpers.rest_client），视频 worker 不在 API 进程内
起，故 job 建后停在 queued 便于断言。
"""

from datetime import datetime
from unittest.mock import AsyncMock

import app.core.db as db_module
from app.core import config as config_module
from app.http import video_rest
from app.models.video_job import VideoJob
from app.video import paths
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client

_YT = "https://youtu.be/abc123"


async def _new_op(key: str) -> int:
    """建一个启用中的非 admin operator，返回其 id。"""
    return await make_operator(key)


async def _get_row(job_id: int) -> VideoJob:
    """从隔离库读回 VideoJob 行（rest_client 内 async_session 已指向临时库）。"""
    async with db_module.async_session() as s:
        return await s.get(VideoJob, job_id)


async def _set_row(job_id: int, **fields) -> None:
    """直改 VideoJob 行字段（造 running/failed/completed 等前置态）。"""
    async with db_module.async_session() as s:
        job = await s.get(VideoJob, job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        await s.commit()


# ── 鉴权（apikey 中间件）────────────────────────────────────────────────

async def test_create_requires_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/video/jobs", json={"url": _YT})
        assert r.status_code == 401  # 中间件缺 key


async def test_list_requires_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/video/jobs")
        assert r.status_code == 401


# ── 建任务 ──────────────────────────────────────────────────────────────

async def test_create_202_and_queued(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        op_id = await _new_op("op-vid-create")
        r = await client.post(
            "/api/video/jobs",
            json={"url": _YT, "voice": "S_x", "mode": "transport"},
            headers=bearer("op-vid-create"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        row = await _get_row(job_id)
        assert row.status == "queued"          # 方案 C：落 queued 等 worker 轮询
        assert row.created_by == op_id         # 归属记 caller
        assert row.mode == "transport"
        assert row.options["voice"] == "S_x"
        assert row.options["burn_subtitles"] is True


async def test_create_remake_mode_persisted(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-remake")
        r = await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "remake"},
            headers=bearer("op-vid-remake"))
        assert r.status_code == 202
        assert (await _get_row(r.json()["job_id"])).mode == "remake"


async def test_create_rejects_non_youtube(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-badurl")
        r = await client.post(
            "/api/video/jobs", json={"url": "https://evil.com/x"},
            headers=bearer("op-vid-badurl"))
        assert r.status_code == 422  # pydantic field_validator（SSRF 白名单）


# ── 查 / 归属 ───────────────────────────────────────────────────────────

async def test_get_payload_shape(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-get")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "remake"},
            headers=bearer("op-vid-get"))).json()["job_id"]
        r = await client.get(f"/api/video/jobs/{job_id}", headers=bearer("op-vid-get"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["id"] == job_id
        assert data["mode"] == "remake"
        assert data["status"] == "queued"
        # 阶段按 remake 序展开成 name+status（未跑的补 pending）
        names = [s["name"] for s in data["stages"]]
        assert names[0] == "download" and "storyboard" in names
        assert data["stages"][0]["status"] == "pending"
        assert data["products"] == {}


async def test_get_404_missing(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-404")
        r = await client.get("/api/video/jobs/99999", headers=bearer("op-vid-404"))
        assert r.status_code == 404


async def test_get_403_cross_operator(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-owner")
        await _new_op("op-vid-other")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-owner"))).json()["job_id"]
        # 别的 operator 看不到（非 admin、非本人）
        r = await client.get(f"/api/video/jobs/{job_id}", headers=bearer("op-vid-other"))
        assert r.status_code == 403


async def test_admin_sees_any_job(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-a")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-a"))).json()["job_id"]
        r = await client.get(f"/api/video/jobs/{job_id}", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200  # admin 全见


# ── 列表（归属过滤）─────────────────────────────────────────────────────

async def test_list_scoped_by_owner_and_admin(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-l1")
        await _new_op("op-vid-l2")
        for _ in range(2):
            await client.post("/api/video/jobs", json={"url": _YT},
                              headers=bearer("op-vid-l1"))
        await client.post("/api/video/jobs", json={"url": _YT},
                          headers=bearer("op-vid-l2"))
        # op1 只看到自己的 2 条
        r1 = await client.get("/api/video/jobs", headers=bearer("op-vid-l1"))
        assert r1.status_code == 200
        assert len(r1.json()["items"]) == 2
        # admin 看到全部 3 条
        ra = await client.get("/api/video/jobs", headers=bearer(ADMIN_KEY))
        assert len(ra.json()["items"]) == 3
        # status 过滤
        rq = await client.get("/api/video/jobs?status=completed",
                              headers=bearer(ADMIN_KEY))
        assert rq.json()["items"] == []


# ── 重试 ────────────────────────────────────────────────────────────────

async def test_retry_409_when_running(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-r1")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-r1"))).json()["job_id"]
        await _set_row(job_id, status="running")
        r = await client.post(f"/api/video/jobs/{job_id}/retry",
                              headers=bearer("op-vid-r1"))
        assert r.status_code == 409


async def test_retry_202_from_failed_requeues(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-r2")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-r2"))).json()["job_id"]
        # 造失败态：download 卡 error，其余 pending → first_incomplete=download
        await _set_row(job_id, status="failed", error="boom",
                       stages={"download": {"status": "error"}})
        r = await client.post(f"/api/video/jobs/{job_id}/retry",
                              headers=bearer("op-vid-r2"))
        assert r.status_code == 202, r.text
        assert r.json()["resume_stage"] == "download"
        row = await _get_row(job_id)
        assert row.status == "queued"    # 复位交 worker 续跑
        assert row.error is None
        assert row.heartbeat_at is not None  # _requeue 刷了心跳


async def test_retry_403_cross_operator(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-r3")
        await _new_op("op-vid-r3b")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-r3"))).json()["job_id"]
        await _set_row(job_id, status="failed")
        r = await client.post(f"/api/video/jobs/{job_id}/retry",
                              headers=bearer("op-vid-r3b"))
        assert r.status_code == 403


# ── 修订（revise）───────────────────────────────────────────────────────

async def test_revise_400_non_remake(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-rev1")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "transport"},
            headers=bearer("op-vid-rev1"))).json()["job_id"]
        await _set_row(job_id, status="completed")
        r = await client.post(f"/api/video/jobs/{job_id}/revise",
                              json={"instructions": "改改"},
                              headers=bearer("op-vid-rev1"))
        assert r.status_code == 400  # 仅 remake 可修订（ValueError→400）


async def test_revise_409_not_completed(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-rev2")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "remake"},
            headers=bearer("op-vid-rev2"))).json()["job_id"]
        # remake 但仍 queued（未完成）→ 409
        r = await client.post(f"/api/video/jobs/{job_id}/revise",
                              json={"instructions": "改改"},
                              headers=bearer("op-vid-rev2"))
        assert r.status_code == 409


async def test_revise_happy_path_creates_and_requeues_child(
        tmp_path, monkeypatch):
    """revise 成功链：解析/校验/继承打桩，断言派生子 job + 回显 edit_plan + 子 job 复位 queued。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    edit_plan = [{"type": "script_edit", "index": 0, "new_text": "新台词"}]
    monkeypatch.setattr(video_rest.remake_revision, "parse_instructions",
                        AsyncMock(return_value=edit_plan))
    monkeypatch.setattr(video_rest.remake_revision, "validate_edit_plan",
                        lambda *a, **k: None)
    monkeypatch.setattr(video_rest, "inherit_artifacts", lambda *a, **k: None)

    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-rev3")
        parent_id = (await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "remake"},
            headers=bearer("op-vid-rev3"))).json()["job_id"]
        await _set_row(parent_id, status="completed")
        # 父产物基底：_load_raw_json 读 rewritten/storyboard（内容随意，parse 已打桩）
        raw = paths.raw_dir(parent_id)
        (raw / "rewritten.json").write_text("[]", encoding="utf-8")
        (raw / "storyboard.json").write_text("{}", encoding="utf-8")

        r = await client.post(f"/api/video/jobs/{parent_id}/revise",
                              json={"instructions": "第一句改改"},
                              headers=bearer("op-vid-rev3"))
        assert r.status_code == 202, r.text
        body = r.json()
        child_id = body["job_id"]
        assert body["parent_job_id"] == parent_id
        assert body["edit_plan"] == edit_plan
        child = await _get_row(child_id)
        assert child.mode == "remake"
        assert child.parent_job_id == parent_id
        assert child.status == "queued"          # 解死局：复位交 worker 续跑
        assert child.heartbeat_at is not None
        # 前五阶段继承标 done，管线从 rewrite 续跑
        from app.video.scheduler import first_incomplete_stage
        assert first_incomplete_stage(child) == "rewrite"


async def test_revise_400_on_edit_plan_error(tmp_path, monkeypatch):
    """解析抛 EditPlanError → 400 带 LLM 原始说明，不建子 job。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))

    async def _boom(*a, **k):
        raise video_rest.remake_revision.EditPlanError("看不懂你的意见")

    monkeypatch.setattr(video_rest.remake_revision, "parse_instructions", _boom)
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-rev4")
        parent_id = (await client.post(
            "/api/video/jobs", json={"url": _YT, "mode": "remake"},
            headers=bearer("op-vid-rev4"))).json()["job_id"]
        await _set_row(parent_id, status="completed")
        raw = paths.raw_dir(parent_id)
        (raw / "rewritten.json").write_text("[]", encoding="utf-8")
        (raw / "storyboard.json").write_text("{}", encoding="utf-8")
        r = await client.post(f"/api/video/jobs/{parent_id}/revise",
                              json={"instructions": "??"},
                              headers=bearer("op-vid-rev4"))
        assert r.status_code == 400
        assert "看不懂" in r.json()["error"]


# ── 删除 ────────────────────────────────────────────────────────────────

async def test_delete_409_running(tmp_path, monkeypatch):
    """running + 心跳非 NULL = 被 worker 真占用（mark_running 刷了心跳）→ 409 不可删。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-d1")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-d1"))).json()["job_id"]
        # 造「被 worker 占用」态：running 且心跳非 NULL
        await _set_row(job_id, status="running", heartbeat_at=datetime.utcnow())
        r = await client.delete(f"/api/video/jobs/{job_id}", headers=bearer("op-vid-d1"))
        assert r.status_code == 409


async def test_delete_allows_running_with_null_heartbeat(tmp_path, monkeypatch):
    """C1：running + 心跳 NULL = 从未被 worker 占用的暂存态（revision 继承窗口 API 崩溃残留）→
    放行删除，作为唯一 API 补救（retry 对 running 一律 409）。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-dnull")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-dnull"))).json()["job_id"]
        # 造暂存/崩溃残留态：running 但心跳 NULL（未被 worker 占用）
        await _set_row(job_id, status="running", heartbeat_at=None)
        r = await client.delete(f"/api/video/jobs/{job_id}", headers=bearer("op-vid-dnull"))
        assert r.status_code == 200
        assert r.json()["deleted"] == job_id
        assert await _get_row(job_id) is None


async def test_delete_removes_row(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    async with rest_client(tmp_path, monkeypatch) as client:
        await _new_op("op-vid-d2")
        job_id = (await client.post(
            "/api/video/jobs", json={"url": _YT},
            headers=bearer("op-vid-d2"))).json()["job_id"]
        await _set_row(job_id, status="completed")
        r = await client.delete(f"/api/video/jobs/{job_id}", headers=bearer("op-vid-d2"))
        assert r.status_code == 200
        assert r.json()["deleted"] == job_id
        assert await _get_row(job_id) is None


# ── /uploads/video 免鉴权产物直链（防路径穿越）─────────────────────────

async def test_serve_video_product_ok_no_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    token_dir = "1-0123456789abcdef"
    out = tmp_path / "data" / "uploads" / "video" / token_dir / "out"
    out.mkdir(parents=True)
    (out / "final.mp4").write_bytes(b"MP4BYTES")
    async with rest_client(tmp_path, monkeypatch) as client:
        # 免鉴权（/uploads 前缀白名单）——不带 Authorization
        r = await client.get(f"/uploads/video/{token_dir}/out/final.mp4")
        assert r.status_code == 200, r.text
        assert r.content == b"MP4BYTES"
        assert r.headers["content-type"].startswith("video/mp4")


async def test_serve_video_product_404_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/uploads/video/1-0123456789abcdef/out/final.mp4")
        assert r.status_code == 404  # 目录/文件不存在


async def test_serve_video_product_404_bad_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    async with rest_client(tmp_path, monkeypatch) as client:
        # 非法子目录（非 raw/tts/out）→ 404
        assert (await client.get(
            "/uploads/video/1-0123456789abcdef/etc/passwd")).status_code == 404
        # token_dir 格式非法（非 {digits}-{16hex}）→ 404
        assert (await client.get(
            "/uploads/video/notatoken/out/final.mp4")).status_code == 404
        # 文件名含非法字符（空格）→ 404
        assert (await client.get(
            "/uploads/video/1-0123456789abcdef/out/bad name.mp4")).status_code == 404


async def test_serve_video_product_nested_subpath(tmp_path, monkeypatch):
    """嵌套子目录（raw/asr_gaps/ 等管线多级产物）可直链取回——DashScope ASR 云端按公网 URL 拉音频。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    token_dir = "1-0123456789abcdef"
    gaps = tmp_path / "data" / "uploads" / "video" / token_dir / "raw" / "asr_gaps"
    gaps.mkdir(parents=True)
    (gaps / "gap0_0.wav").write_bytes(b"WAVBYTES")
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get(f"/uploads/video/{token_dir}/raw/asr_gaps/gap0_0.wav")
        assert r.status_code == 200, r.text
        assert r.content == b"WAVBYTES"


async def test_serve_video_product_nested_traversal_404(tmp_path, monkeypatch):
    """嵌套路径的穿越/隐藏段攻击全部 404：..、以点开头的段、空段。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))
    async with rest_client(tmp_path, monkeypatch) as client:
        for evil in (
            "/uploads/video/1-0123456789abcdef/raw/../../../etc/passwd",
            "/uploads/video/1-0123456789abcdef/raw/.hidden/x.wav",
            "/uploads/video/1-0123456789abcdef/raw//gap0_0.wav",
            # 注：裸 ".." 段会先被 Starlette 路径规范化成 307 重定向（到不了本路由），
            # follow_redirects 后落到不存在的资源仍是 404——两层防御殊途同归。
            "/uploads/video/1-0123456789abcdef/raw/asr_gaps/..",
        ):
            assert (await client.get(
                evil, follow_redirects=True)).status_code == 404, evil
