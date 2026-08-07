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
            assert job.video_path == video, "video_path 不许被动"
            assert json.loads(job.images_json) == [], "images_json 不许被写进去"


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


# ---------------- 视频与图文共用同一个请求模型:全字段生效 ----------------


async def test_video_job_carries_every_component_field(tmp_path, monkeypatch):
    """视频请求沿用 PublishNoteRequest 的**全部**可选字段,一个都不许在路上掉。

    这条锁的是"视频没有另造窄版模型"这件事本身:话题 / 定时 / 三组件 / 咨询师 /
    笔记目的全部与图文同一套语义,漏任何一列都会在这里红。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await _account_with_operator("号V8", "uV8", "op-video-full")
        video = _make_video(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "video": video,
                "topics": ["#心理", "#情绪"],
                "schedule_time": "2099-01-01T09:00:00+08:00",
                "collection_id": "col-1",
                "quoted_note_id": "note-1",
                "activity_id": "act-1",
                "related_counselor": "李宇",
                "note_purpose": "推介咨询师",
            },
            headers=bearer("op-video-full"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        # 带 schedule_time 就不该立即入队(与图文同一套定时语义)
        assert fake.submitted == []
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.video_path == video
            assert json.loads(job.topics_json) == ["#心理", "#情绪"]
            assert job.collection_id == "col-1"
            assert job.quoted_note_id == "note-1"
            assert job.activity_id == "act-1"
            assert job.related_counselor == "李宇"
            assert job.note_purpose == "推介咨询师"
            # +08:00 → naive UTC(与图文 _parse_schedule_time 同一套)
            assert job.schedule_time.hour == 1
            assert job.schedule_time.tzinfo is None


async def test_video_job_derives_quoted_note_from_counselor(tmp_path, monkeypatch):
    """没给 quoted_note_id 时,视频任务同样按 related_counselor 推导引用哪篇。

    推导链是端点体里无条件跑的 counselor_quote.resolve_quoted_note_id —— 这条用例
    钉死它没被"视频跳过图片校验"那段挡在分支外。
    """
    from app.http import publish_rest as pr

    async def _fake_resolve(session, account_id, title, counselor):
        return "derived-note-id" if counselor == "李宇" else None

    monkeypatch.setattr(pr.counselor_quote, "resolve_quoted_note_id", _fake_resolve)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V9", "uV9", "op-video-quote")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "video": _make_video(tmp_path), "related_counselor": "李宇",
            },
            headers=bearer("op-video-quote"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "derived-note-id"


async def test_explicit_quoted_note_id_wins_over_counselor_for_video(tmp_path, monkeypatch):
    """显式 quoted_note_id 优先于 related_counselor 推导(与图文同一套优先级)。"""
    from app.http import publish_rest as pr

    async def _boom(*a, **k):
        raise AssertionError("显式 quoted_note_id 时不该走推导")

    monkeypatch.setattr(pr.counselor_quote, "resolve_quoted_note_id", _boom)
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V10", "uV10", "op-video-explicit")
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": acc, "title": "T", "content": "C",
                "video": _make_video(tmp_path),
                "quoted_note_id": "explicit-note", "related_counselor": "李宇",
            },
            headers=bearer("op-video-explicit"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "explicit-note"


# ---------------- PATCH:视频任务不许被静默改成图文 ----------------


async def _make_video_job(c, tmp_path, key: str, acc: int) -> tuple:
    """建一条 pending 视频任务,返回 (job_id, 视频路径)。"""
    video = _make_video(tmp_path)
    r = await c.post(
        "/api/publish-jobs",
        json={"account_id": acc, "title": "T", "content": "C", "video": video,
              "schedule_time": "2099-01-01T09:00:00+08:00"},
        headers=bearer(key),
    )
    assert r.status_code == 202, r.text
    return r.json()["job_id"], video


async def test_patch_images_onto_video_job_is_hard_rejected(tmp_path, monkeypatch):
    """给视频任务 PATCH images → 422 硬拒,且库里两列都不许动。

    放任 images 落库产生的**不是**"改成了图文任务",而是第三种没人预期的迷惑态:
    images_json 写进去了、video_path 还在,而 runner 按 video_path 路由 —— 图片永远
    不生效,调用方却拿到 ok:true,只能等笔记发出来人工看才发现。比报错危险得多,
    所以必须硬拒,不能靠 manifest 里一句警告兜底。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V11", "uV11", "op-video-patch")
        job_id, video = await _make_video_job(c, tmp_path, "op-video-patch", acc)

        r = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"images": ["https://cdn/1.png"]},
            headers=bearer("op-video-patch"),
        )
        assert r.status_code == 422, r.text
        assert "视频任务不可改图片" in r.text
        # 库里一个字节都不许动
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.video_path == video, "video_path 不许被动"
            assert json.loads(job.images_json) == [], "images_json 不许被写进去"


async def test_patch_empty_images_onto_video_job_also_rejected(tmp_path, monkeypatch):
    """连空 images 也拒:显式传这个字段就是在选图文那条路。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V12", "uV12", "op-video-patch2")
        job_id, _ = await _make_video_job(c, tmp_path, "op-video-patch2", acc)

        r = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"images": []},
            headers=bearer("op-video-patch2"),
        )
        assert r.status_code == 422, r.text


async def test_patch_video_job_other_fields_still_works(tmp_path, monkeypatch):
    """视频任务改标题/正文/话题/时间照常 —— 硬拒只针对 images。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V13", "uV13", "op-video-patch3")
        job_id, video = await _make_video_job(c, tmp_path, "op-video-patch3", acc)

        r = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"title": "新标题", "topics": ["#心理"]},
            headers=bearer("op-video-patch3"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert job.title == "新标题"
            assert job.video_path == video, "改标题不该动 video_path"


async def test_patch_images_on_image_job_unaffected(tmp_path, monkeypatch):
    """回归:图文任务 PATCH images 照旧生效(硬拒只看 video_path 有没有)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号V14", "uV14", "op-video-patch4")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/1.png"],
                  "schedule_time": "2099-01-01T09:00:00+08:00"},
            headers=bearer("op-video-patch4"),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        r = await c.patch(
            f"/api/publish-jobs/{job_id}",
            json={"images": ["https://cdn/2.png", "https://cdn/3.png"]},
            headers=bearer("op-video-patch4"),
        )
        assert r.status_code == 200, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, job_id)
            assert json.loads(job.images_json) == [
                "https://cdn/2.png", "https://cdn/3.png"]


# ---------------- 封面图片(cover):仅视频任务有效 ----------------


def _make_cover(tmp_path, name: str = "cover.jpg") -> str:
    """造一个真实存在的封面图文件(校验只看存在性与扩展名)。"""
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return str(p)


async def test_video_with_cover_creates_job(tmp_path, monkeypatch):
    """视频任务给 cover → 202,cover_path 落库。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号C1", "uC1", "op-cover-ok")
        video, cover = _make_video(tmp_path), _make_cover(tmp_path)
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "video": video, "cover": cover},
            headers=bearer("op-cover-ok"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.cover_path == cover
            assert job.video_path == video


async def test_video_without_cover_defaults_to_platform_auto(tmp_path, monkeypatch):
    """不传 cover → cover_path 落 None(= 用平台自动截取的首帧,现行为不变)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号C2", "uC2", "op-cover-none")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "video": _make_video(tmp_path)},
            headers=bearer("op-cover-none"),
        )
        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            assert (await s.get(PublishJob, r.json()["job_id"])).cover_path is None


async def test_image_job_with_cover_422(tmp_path, monkeypatch):
    """图文任务给 cover → 422:图文的封面就是首图,这个语义压根不存在。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号C3", "uC3", "op-cover-img")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "images": ["https://cdn/1.png"], "cover": _make_cover(tmp_path)},
            headers=bearer("op-cover-img"),
        )
        assert r.status_code == 422, r.text
        assert "图文" in r.text and "cover" in r.text


async def test_cover_bad_extension_422(tmp_path, monkeypatch):
    """封面扩展名不在 jpg/jpeg/png/webp 白名单 → 422。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号C4", "uC4", "op-cover-ext")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "video": _make_video(tmp_path),
                  "cover": _make_cover(tmp_path, "cover.gif")},
            headers=bearer("op-cover-ext"),
        )
        assert r.status_code == 422, r.text
        assert "封面" in r.text or "cover" in r.text


async def test_cover_missing_file_422(tmp_path, monkeypatch):
    """封面路径不存在 → 422(不建注定失败的 job)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _account_with_operator("号C5", "uC5", "op-cover-404")
        r = await c.post(
            "/api/publish-jobs",
            json={"account_id": acc, "title": "T", "content": "C",
                  "video": _make_video(tmp_path),
                  "cover": str(tmp_path / "没有这个封面.png")},
            headers=bearer("op-cover-404"),
        )
        assert r.status_code == 422, r.text
        assert "不存在" in r.text
