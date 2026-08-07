"""视频笔记发布的 REST 入参契约测试(POST /api/publish-jobs 的 video 分支)。

隔离手法与 tests/test_publish_rest.py 完全一致(rest_client 真 lifespan + 假调度器)。

覆盖 brief 必测:
- video 与 images 同给 → 422;两者都不给 → 422(二选一必填);
- 只给 video → 202 且 video_path 落库、images_json 为空列表;
- video 扩展名不在平台白名单 → 422;video 文件不存在 → 422;
- 回归:只给 images 照旧 202;显式 images=[] 且无 video 仍是既有的 400(不是 422)。
"""

import json

import app.core.db as db_module
from app.models import PublishJob
from app.publish import runtime as runtime_mod
from app.services import operator_service
from tests.rest_helpers import bearer, make_operator, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]


class _FakeScheduler:
    """只记录 submit 的假调度器(与 tests/test_publish_rest.py 同款)。"""

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


def _make_video(tmp_path, name: str = "note.mp4") -> str:
    """造一个真实存在的视频文件(内容无所谓,校验只看存在性与扩展名)。"""
    p = tmp_path / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return str(p)


# ---------------- 互斥 / 必填 ----------------


async def test_video_and_images_both_given_422(tmp_path, monkeypatch):
    """video 与 images 同给 → 422,文案说清二选一。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V1", "uV1", "op-video-both")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "images": ["https://cdn/1.png"],
                "video": _make_video(tmp_path),
            },
            headers=bearer("op-video-both"),
        )
        assert r.status_code == 422, r.text
        assert "二选一" in r.text


async def test_neither_video_nor_images_422(tmp_path, monkeypatch):
    """video 与 images 都不给 → 422(而非落一条注定失败的 job)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V2", "uV2", "op-video-none")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C"},
            headers=bearer("op-video-none"),
        )
        assert r.status_code == 422, r.text
        assert "二选一" in r.text


async def test_video_only_creates_job_with_video_path(tmp_path, monkeypatch):
    """只给 video → 202,video_path 落库、images_json 是空列表、立即入队。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await _account_with_operator("号V3", "uV3", "op-video-ok")
        video = _make_video(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C", "video": video},
            headers=bearer("op-video-ok"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert fake.submitted == [job_id]
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.video_path == video
            assert json.loads(job.images_json) == []


async def test_video_bad_extension_422(tmp_path, monkeypatch):
    """扩展名不在平台 accept 白名单(如 .webm)→ 422。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V4", "uV4", "op-video-ext")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "video": _make_video(tmp_path, "note.webm"),
            },
            headers=bearer("op-video-ext"),
        )
        assert r.status_code == 422, r.text
        assert "格式" in r.text


async def test_video_missing_file_422(tmp_path, monkeypatch):
    """路径不存在 → 422(不建 job,免得发布时才炸)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V5", "uV5", "op-video-404")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "video": str(tmp_path / "根本没有这个文件.mp4"),
            },
            headers=bearer("op-video-404"),
        )
        assert r.status_code == 422, r.text
        assert "不存在" in r.text


# ---------------- 回归:图文分支一字不改 ----------------


async def test_images_only_still_accepted(tmp_path, monkeypatch):
    """只给 images 照旧 202,video_path 落 None。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V6", "uV6", "op-video-img")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "images": ["https://cdn/1.png"],
            },
            headers=bearer("op-video-img"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.video_path is None


async def test_explicit_empty_images_without_video_still_400(tmp_path, monkeypatch):
    """显式 images=[] 且无 video:仍是既有的 400「至少 1 张图片」,不被新的 422 抢走。

    显式传空列表 = 调用方已经选了图文这条路(只是没给图),与"两个都没传"语义不同;
    保持 400 让上线前的契约(及 tests/test_publish_rest.py 的断言)逐字节不变。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V7", "uV7", "op-video-empty")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C", "images": []},
            headers=bearer("op-video-empty"),
        )
        assert r.status_code == 400, r.text
        assert "图片" in r.json()["error"]
