"""视频笔记发布的串接:sync_client 分支路由 + 两条执行路径(scheduler / account_worker)。

本仓有**两条**发布执行路径(生产是账号子进程 account_worker,all 模式是 publish/scheduler),
两边都得按 video_path 路由,漏一边就是"接口收了 video、发出去的却是图文任务"。
"""

import json
from pathlib import Path

import pytest


# ---------------- sync_client.publish_note 分支路由 ----------------


class _FakeAtomicBase:
    """记录被调到的步骤名(验分支走向);默认每步都成功。"""

    def __init__(self, page, job_tag=None):
        self.page = page
        self.job_tag = job_tag
        self.calls = []

    def step1_open_publish_page(self):
        self.calls.append("step1")
        return {"success": True, "url": "https://creator.xiaohongshu.com/publish"}

    def step2_upload_images(self, image_paths):
        self.calls.append("step2")
        return {"success": True, "uploaded_count": len(image_paths)}

    def step3_wait_for_upload_processing(self, max_wait=30):
        self.calls.append("step3")
        return {"success": True, "edit_page_loaded": True}

    def step2v_upload_video(self, video_path):
        self.calls.append(f"step2v:{video_path}")
        return {"success": True, "video_path": video_path}

    def step3v_wait_for_video_processing(self, max_wait=None):
        self.calls.append("step3v")
        return {"success": True, "state": "ready", "edit_page_loaded": True}

    def ensure_editor_interactable(self, tries=5):
        self.calls.append("interactable")
        return True

    def wait_for_submit_enabled(self, timeout=120):
        self.calls.append("submit_gate")
        return {"ready": True, "observed": {"submit_disabled": "false"}}

    def step5_fill_content(self, title, content):
        self.calls.append("step5")
        return {"success": True}

    def step6_set_publish_options(self, tags=None):
        self.calls.append("step6")
        return {"success": True, "topics_applied": list(tags or []), "topics_failed": []}

    def step7_click_publish_and_wait(self, max_wait=30):
        self.calls.append("step7")
        return {"success": True, "note_url": "https://xhs/n/1", "note_id": "1"}


@pytest.fixture()
def patched_sync_client(monkeypatch):
    """把 atomic 类与原创声明换成替身,返回 (模块, 记录用的 holder)。"""
    from app.browser import sync_client as sc

    holder = {}

    class _Fake(_FakeAtomicBase):
        def __init__(self, page, job_tag=None):
            super().__init__(page, job_tag)
            holder["atomic"] = self

    monkeypatch.setattr(sc, "XHSPublishAtomicTasks", _Fake)
    monkeypatch.setattr(
        sc, "apply_original_declaration",
        lambda page, human: {"status": "done", "observed": "checked_on"},
    )
    monkeypatch.setattr(sc, "SyncHumanActions", lambda page, **k: object())
    return sc, holder


def test_publish_note_video_branch_skips_image_steps(patched_sync_client):
    """给了 video_path:走 step2v/step3v,**一次都不碰** step2/step3(图文那套)。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", [], ["#心理"], video_path="/data/a.mp4")
    assert r["success"] is True
    calls = holder["atomic"].calls
    assert "step2v:/data/a.mp4" in calls and "step3v" in calls
    assert "step2" not in calls and "step3" not in calls
    # 视频分支独有的两道防御:编辑区可交互校验 + 发布按钮就绪门
    assert calls.index("interactable") < calls.index("step5")
    assert calls.index("submit_gate") < calls.index("step7")


def test_publish_note_image_branch_untouched(patched_sync_client):
    """没给 video_path:老路径逐字节不变,不碰任何 v 分支、也不加就绪门。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", ["/tmp/a.png"], ["#心理"])
    assert r["success"] is True
    calls = holder["atomic"].calls
    assert "step2" in calls and "step3" in calls
    assert not any(c.startswith("step2v") for c in calls)
    assert "step3v" not in calls
    assert "interactable" not in calls and "submit_gate" not in calls


def test_publish_note_video_upload_failure_stops_before_fill(patched_sync_client):
    """视频没传上去就不该继续填内容(否则会往空编辑器打字后点发布)。"""
    sc, holder = patched_sync_client

    class _Fail(_FakeAtomicBase):
        def __init__(self, page, job_tag=None):
            super().__init__(page, job_tag)
            holder["atomic"] = self

        def step3v_wait_for_video_processing(self, max_wait=None):
            self.calls.append("step3v")
            return {"success": False, "error": "视频上传/转码超时(600s),最后判定 uploading",
                    "state": "uploading"}

    import app.browser.sync_client as sc_mod
    sc_mod.XHSPublishAtomicTasks = _Fail
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", [], video_path="/data/a.mp4")
    assert r["success"] is False and "转码" in r["error"]
    assert "step5" not in holder["atomic"].calls
    assert "step7" not in holder["atomic"].calls


def test_publish_note_submit_gate_blocks_publish(patched_sync_client):
    """发布按钮一直禁用 → 不点发布(点禁用按钮永远发不出去,只会换来一句超时)。"""
    sc, holder = patched_sync_client

    class _Stuck(_FakeAtomicBase):
        def __init__(self, page, job_tag=None):
            super().__init__(page, job_tag)
            holder["atomic"] = self

        def wait_for_submit_enabled(self, timeout=120):
            self.calls.append("submit_gate")
            return {"ready": False, "observed": {"found": True, "submit_disabled": "true"}}

    import app.browser.sync_client as sc_mod
    sc_mod.XHSPublishAtomicTasks = _Stuck
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", [], video_path="/data/a.mp4")
    assert r["success"] is False
    assert "submit-disabled" in r["error"]
    assert "step7" not in holder["atomic"].calls


def test_publish_once_forwards_video_path(monkeypatch):
    """publish_once 把 video_path 透传给 publish_note(别在这一层丢参数)。"""
    from app.browser import sync_client as sc

    seen = {}
    monkeypatch.setattr(sc.SyncClient, "start", lambda self: {"success": True})
    monkeypatch.setattr(sc.SyncClient, "stop", lambda self: None)

    def _fake_publish_note(self, title, content, image_paths, topics=None,
                           components=None, job_tag=None, video_path=None):
        seen["video_path"] = video_path
        seen["image_paths"] = image_paths
        return {"success": True, "note_url": "u", "note_id": "i"}

    monkeypatch.setattr(sc.SyncClient, "publish_note", _fake_publish_note)
    r = sc.publish_once(1, [], "T", "C", [], None, None, video_path="/data/a.mp4")
    assert r.success is True
    assert seen["video_path"] == "/data/a.mp4"
    assert seen["image_paths"] == []


# ---------------- 两条执行路径都按 video_path 路由 ----------------


def test_account_worker_video_job_skips_image_pipeline(monkeypatch, tmp_path):
    """account_worker:视频任务不跑图片物料化/去水印,直接把路径交给发布层。"""
    import app.account_worker as aw

    monkeypatch.setattr(aw, "_load_account_cookies", lambda db, aid: [])

    def _boom(*a, **k):
        raise AssertionError("视频任务不该走图片物料化")

    monkeypatch.setattr(aw, "materialize_images", _boom)

    seen = {}

    def _fake_publish_once(account_id, cookies, title, content, image_paths,
                           topics=None, components=None, job_tag=None, video_path=None):
        seen.update(image_paths=image_paths, video_path=video_path)
        return aw.sync_client.PublishResult(success=True, note_url="u")

    monkeypatch.setattr(aw.sync_client, "publish_once", _fake_publish_once)

    job = {
        "id": 7, "title": "T", "content": "C",
        "images_json": "[]", "topics_json": '["#心理"]',
        "video_path": "/data/a.mp4",
    }
    result = aw._execute_publish("db.sqlite", 1, job)
    assert result.success is True
    assert seen["video_path"] == "/data/a.mp4"
    assert seen["image_paths"] == []


def test_account_worker_image_job_still_materializes(monkeypatch, tmp_path):
    """account_worker:图文任务照旧走物料化 + 去水印(video_path 缺列也不炸)。"""
    import app.account_worker as aw

    monkeypatch.setattr(aw, "_load_account_cookies", lambda db, aid: [])
    monkeypatch.setattr(aw, "materialize_images", lambda raw, wd: [Path("/tmp/a.png")])

    async def _fake_dewatermark(paths):
        return paths

    monkeypatch.setattr(aw, "dewatermark_all", _fake_dewatermark)

    seen = {}

    def _fake_publish_once(account_id, cookies, title, content, image_paths,
                           topics=None, components=None, job_tag=None, video_path=None):
        seen.update(image_paths=image_paths, video_path=video_path)
        return aw.sync_client.PublishResult(success=True, note_url="u")

    monkeypatch.setattr(aw.sync_client, "publish_once", _fake_publish_once)

    # 故意不给 video_path 键:老库没跑迁移时 SELECT * 出来就是没有这一列
    job = {"id": 8, "title": "T", "content": "C",
           "images_json": '["https://cdn/1.png"]', "topics_json": "[]"}
    result = aw._execute_publish("db.sqlite", 1, job)
    assert result.success is True
    assert seen["video_path"] is None
    assert seen["image_paths"] == ["/tmp/a.png"]


async def test_scheduler_runner_routes_video(monkeypatch, tmp_path):
    """publish/scheduler 的 runner:视频任务同样跳过图片管线并透传 video_path。"""
    import app.core.db as db_module
    from app.models import PublishJob
    from app.publish.scheduler import PublishScheduler, make_publish_runner
    from app.publish.queue import AccountLocks
    from app.browser import sync_client as sc
    from tests.rest_helpers import rest_client, seed_account

    async with rest_client(tmp_path, monkeypatch):
        acc = await seed_account("号SV", "uSV", [{"name": "a", "value": "b",
                                                  "domain": ".xiaohongshu.com"}])
        async with db_module.async_session() as s:
            job = PublishJob(account_id=acc, title="T", content="C",
                             images_json="[]", topics_json="[]", status="pending",
                             video_path="/data/a.mp4")
            s.add(job)
            await s.commit()
            job_id = job.id

        def _boom(*a, **k):
            raise AssertionError("视频任务不该走图片物料化")

        monkeypatch.setattr("app.publish.scheduler.materialize_images", _boom)

        seen = {}

        def _fake_publish_once(account_id, cookies, title, content, image_paths,
                               topics=None, components=None, job_tag=None,
                               video_path=None):
            seen.update(image_paths=image_paths, video_path=video_path)
            return sc.PublishResult(success=True, note_url="u", note_id="i")

        monkeypatch.setattr(sc, "publish_once", _fake_publish_once)

        scheduler = PublishScheduler(db_module.async_session)
        runner = make_publish_runner(db_module.async_session, scheduler, AccountLocks())
        await runner(job_id)

        assert seen["video_path"] == "/data/a.mp4"
        assert seen["image_paths"] == []
        async with db_module.async_session() as s:
            done = await s.get(PublishJob, job_id)
            assert done.status == "published"
            assert json.loads(done.images_json) == []
