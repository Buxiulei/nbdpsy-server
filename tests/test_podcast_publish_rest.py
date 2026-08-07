"""播客音频发布的 REST 入参契约测试(POST/PATCH /api/publish-jobs 的 audio 分支)。

隔离手法与 tests/test_video_publish_rest.py 完全一致(rest_client 真 lifespan + 假调度器)。

覆盖:
- **三选一互斥矩阵**:images/video/audio 的 8 种给法,7 拒 1 过(×3 各一条通路);
- audio 四条准入(存在性/扩展名/体积/时长)在 REST 层就 422,不造注定失败的 pending job;
- cover 准入从"仅 video"放宽为"video 或 audio",且两者体积上限不同;
- ``podcast_collection`` 仅 audio 任务可传,落到复用的 collection_id 列;
- PATCH:播客任务改 images 硬拒 422(与视频任务同款理由)。

时长夹具用 ffmpeg 现造静音 wav,并把时长下限 monkeypatch 到 1 秒 —— 真造 600 秒
夹具只为过门没有信息量,**边界本身**由 tests/test_podcast_policy.py 按真实常量测。
"""

import json
import subprocess

import app.core.db as db_module
from app.models import PublishJob
from app.publish import runtime as runtime_mod
from app.services import operator_service
from tests.rest_helpers import bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeScheduler:
    """只记录 submit 的假调度器(与 tests/test_video_publish_rest.py 同款)。"""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, job_id: int) -> None:
        self.submitted.append(job_id)


def _install_fake_scheduler() -> _FakeScheduler:
    fake = _FakeScheduler()
    runtime_mod.set_active_scheduler(fake)
    return fake


async def _account_with_operator(name: str, uid: str, key: str) -> int:
    acc = await seed_account(name, uid, _COOKIES)
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        await operator_service.grant_access(s, op_id, acc, op_id)
    return acc


def _make_audio(tmp_path, name: str = "ep.mp3", seconds: float = 2) -> str:
    """造一段真实可被 ffprobe 读出时长的音频(低采样率单声道,秒级生成)。"""
    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds), str(path)],
        check=True,
    )
    return str(path)


def _make_video(tmp_path, name: str = "note.mp4") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return str(path)


def _make_cover(tmp_path, name: str = "cover.png", size: int = 64) -> str:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


def _relax_duration(monkeypatch) -> None:
    """时长门下限压到 1 秒,让 REST 测试专注于**路由与互斥**而不是造大夹具。"""
    monkeypatch.setattr("app.publish.policy.AUDIO_MIN_DURATION_S", 1)


# ---------------- 三选一互斥矩阵(8 种给法) ----------------


async def test_media_exclusive_matrix(tmp_path, monkeypatch):
    """images / video / audio 的 8 种组合:恰好给一个才过,其余 5 种全 422。

    为什么整表跑而不是抽查:三选一是 2^3 的判定,改成三分支时最容易漏的正是
    "两两同给"那三格 —— 漏掉的表现是**静默**建了一条媒体类型不明的 job。
    """
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P1", "uP1", "op-pod-matrix")
        images = ["https://cdn/1.png"]
        video = _make_video(tmp_path)
        audio = _make_audio(tmp_path)

        cases = [
            # (images, video, audio, 期望状态码)
            (None, None, None, 422),      # 都不给
            (images, None, None, 202),    # 只给图文
            (None, video, None, 202),     # 只给视频
            (None, None, audio, 202),     # 只给音频
            (images, video, None, 422),
            (images, None, audio, 422),
            (None, video, audio, 422),
            (images, video, audio, 422),
        ]
        for idx, (imgs, vid, aud, expected) in enumerate(cases):
            body = {"account_id": acc, "title": "T", "content": "C"}
            if imgs is not None:
                body["images"] = imgs
            if vid is not None:
                body["video"] = vid
            if aud is not None:
                body["audio"] = aud
            r = await c.post(
                "/api/publish-jobs", json=body, headers=bearer("op-pod-matrix")
            )
            assert r.status_code == expected, f"第 {idx} 格({imgs and 'I'}/" \
                f"{vid and 'V'}/{aud and 'A'})期望 {expected},实得 {r.status_code}: {r.text}"
            if expected == 422:
                assert "三选一" in r.text, f"第 {idx} 格的报错要说清三选一: {r.text}"


async def test_audio_only_creates_job_with_audio_path(tmp_path, monkeypatch):
    """只给 audio → 202;audio_path 落库,images_json 空、video_path 为 None。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await _account_with_operator("号P2", "uP2", "op-pod-ok")
        audio = _make_audio(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C", "audio": audio},
            headers=bearer("op-pod-ok"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert fake.submitted == [job_id]
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.audio_path == audio
            assert job.video_path is None
            assert json.loads(job.images_json) == []


# ---------------- audio 四条准入 ----------------


async def test_audio_bad_extension_422(tmp_path, monkeypatch):
    """扩展名不在播客白名单(.ogg)→ 422,且**不**被三选一那条报错吃掉。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P3", "uP3", "op-pod-ext")
        bad = tmp_path / "ep.ogg"
        bad.write_bytes(b"x")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C", "audio": str(bad)},
            headers=bearer("op-pod-ext"),
        )
        assert r.status_code == 422, r.text
        assert "格式不支持" in r.text


async def test_audio_missing_file_422(tmp_path, monkeypatch):
    """路径不存在 → 422(不建 job)。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P4", "uP4", "op-pod-404")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": str(tmp_path / "没有这个文件.mp3")},
            headers=bearer("op-pod-404"),
        )
        assert r.status_code == 422, r.text
        assert "不存在" in r.text


async def test_audio_too_short_422(tmp_path, monkeypatch):
    """时长不足 10 分钟 → 422(这里不放宽下限,用真实常量判)。

    这条也是"REST 层真的调了 ffprobe"的证据:不读时长的话 2 秒的夹具会照样过。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P5", "uP5", "op-pod-short")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path, "short.mp3", 2)},
            headers=bearer("op-pod-short"),
        )
        assert r.status_code == 422, r.text
        assert "时长" in r.text


async def test_audio_oversize_422(tmp_path, monkeypatch):
    """超 1GB → 422(压常量验判据,不真造 1GB 文件)。"""
    _relax_duration(monkeypatch)
    monkeypatch.setattr("app.publish.policy.XHS_AUDIO_MAX_BYTES", 16)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P6", "uP6", "op-pod-big")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path, "big.mp3")},
            headers=bearer("op-pod-big"),
        )
        assert r.status_code == 422, r.text
        assert "大小" in r.text


# ---------------- cover:准入放宽到 audio,两档上限独立 ----------------


async def test_audio_with_cover_accepted(tmp_path, monkeypatch):
    """audio + cover → 202,cover_path 落库(复用视频那一列)。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P7", "uP7", "op-pod-cover")
        cover = _make_cover(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path), "cover": cover},
            headers=bearer("op-pod-cover"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.cover_path == cover


async def test_audio_cover_size_cap_is_32mb_not_video_rule(tmp_path, monkeypatch):
    """音频封面走 32MB 那档:压到 16 字节后同一张图被拒,报错点名体积。"""
    _relax_duration(monkeypatch)
    monkeypatch.setattr("app.publish.policy.AUDIO_COVER_MAX_BYTES", 16)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P8", "uP8", "op-pod-cover-big")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path), "cover": _make_cover(tmp_path)},
            headers=bearer("op-pod-cover-big"),
        )
        assert r.status_code == 422, r.text
        assert "大小" in r.text


async def test_images_with_cover_still_422(tmp_path, monkeypatch):
    """回归:图文任务传 cover 仍是 422(放宽只放给 audio,没顺手放给图文)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号P9", "uP9", "op-pod-img-cover")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/1.png"], "cover": _make_cover(tmp_path)},
            headers=bearer("op-pod-img-cover"),
        )
        assert r.status_code == 422, r.text


# ---------------- podcast_collection ----------------


async def test_podcast_collection_lands_in_collection_id(tmp_path, monkeypatch):
    """audio + podcast_collection → 202,名称落进复用的 collection_id 列。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号PA", "uPA", "op-pod-coll")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path), "podcast_collection": "心理急救包"},
            headers=bearer("op-pod-coll"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.collection_id == "心理急救包"


async def test_podcast_collection_on_image_job_422(tmp_path, monkeypatch):
    """图文任务传 podcast_collection → 422(它只在播客发布表单里存在)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号PB", "uPB", "op-pod-coll-img")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/1.png"], "podcast_collection": "X"},
            headers=bearer("op-pod-coll-img"),
        )
        assert r.status_code == 422, r.text
        assert "播客" in r.text


async def test_podcast_collection_and_collection_id_conflict_422(tmp_path, monkeypatch):
    """同时给 podcast_collection 与 collection_id → 422:两者共用同一列,后写会静默盖掉前者。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号PC", "uPC", "op-pod-coll-both")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path),
                  "podcast_collection": "心理急救包", "collection_id": "deadbeef"},
            headers=bearer("op-pod-coll-both"),
        )
        assert r.status_code == 422, r.text


# ---------------- PATCH:播客任务改 images 硬拒 ----------------


async def test_patch_images_on_audio_job_422(tmp_path, monkeypatch):
    """给播客任务 PATCH images → 422,且 images_json / audio_path 一个字节不动。

    理由与视频任务同款:runner 按 audio_path 路由,images 写进去也永不生效,
    调用方却拿到 ok:true —— 静默态比报错危险得多。
    """
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号PD", "uPD", "op-pod-patch")
        audio = _make_audio(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C", "audio": audio,
                  "schedule_time": "2099-01-01T00:00:00+00:00"},
            headers=bearer("op-pod-patch"),
        )
        job_id = r.json()["job_id"]
        r2 = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"images": ["https://cdn/1.png"]},
            headers=bearer("op-pod-patch"),
        )
        assert r2.status_code == 422, r2.text
        assert "播客" in r2.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id, populate_existing=True)
            assert json.loads(job.images_json) == []
            assert job.audio_path == audio


async def test_patch_text_fields_on_audio_job_ok(tmp_path, monkeypatch):
    """回归:播客任务改标题/正文/话题/定时**照常**(只有 images 是硬拒)。"""
    _relax_duration(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号PE", "uPE", "op-pod-patch2")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "audio": _make_audio(tmp_path),
                  "schedule_time": "2099-01-01T00:00:00+00:00"},
            headers=bearer("op-pod-patch2"),
        )
        job_id = r.json()["job_id"]
        r2 = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"title": "新标题", "topics": ["心理"]},
            headers=bearer("op-pod-patch2"),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["ok"] is True
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id, populate_existing=True)
            assert job.title == "新标题"
            assert json.loads(job.topics_json) == ["心理"]
