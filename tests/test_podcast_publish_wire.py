"""播客发布的串接:sync_client 分支路由 + 两条执行路径(scheduler / account_worker)。

本仓有**两条**发布执行路径(生产是账号子进程 account_worker,all 模式是 publish/scheduler),
两边都得按 audio_path 路由,漏一边就是"接口收了 audio、发出去的却是图文任务"。

另有一条播客独有的坑要钉死:``collection_id`` 那一列在播客任务上存的是**播客合集名称**
(列级多态)。它必须走 ``podcast_collection`` 参数,**绝不能**混进按 hex id 找笔记合集的
三组件链路 —— 那会拿名字去匹配 id,静默设不上。
"""

import json

import pytest


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

    def step3v_wait_for_video_processing(self, max_wait=None, video_path=None):
        self.calls.append("step3v")
        return {"success": True, "state": "ready", "edit_page_loaded": True}

    def step2a_upload_audio(self, audio_path, cover_path=None):
        self.calls.append(f"step2a:{audio_path}:{cover_path}")
        return {"success": True, "audio_path": audio_path,
                "audio_cover": ({"status": "done", "cover_path": cover_path}
                                if cover_path else {"status": "skipped"})}

    def step3a_wait_for_audio_upload(self, max_wait=None, audio_path=None):
        self.calls.append("step3a")
        return {"success": True, "wait_time": 1.0, "edit_page_loaded": True}

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
    """把 atomic 类 / 原创声明 / 合集选择换成替身,返回 (模块, 记录用的 holder)。"""
    from app.browser import sync_client as sc

    holder = {"collection_calls": []}

    class _Fake(_FakeAtomicBase):
        def __init__(self, page, job_tag=None):
            super().__init__(page, job_tag)
            holder["atomic"] = self

    monkeypatch.setattr(sc, "XHSPublishAtomicTasks", _Fake)
    monkeypatch.setattr(
        sc, "apply_original_declaration",
        # **kw 必须留:合并层裁决后调用点会带 handle_consent_modal=True(三路径统一),
        # 假件签名锁死的话那个 kwarg 会 TypeError——被 sync_client 的 try/except 吞成
        # status=error,测试照绿但这条链再也没被真正走到(静默失效,比红更糟)。
        lambda page, human, **kw: {"status": "done", "observed": "checked_on"},
    )
    monkeypatch.setattr(sc, "SyncHumanActions", lambda page, **k: object())

    def _select(page, human, name):
        holder["collection_calls"].append(name)
        return {"status": "done", "name": name}

    monkeypatch.setattr(sc, "select_podcast_collection", _select)
    monkeypatch.setattr(
        sc.SyncClient, "_apply_components",
        lambda self, atomic, responses, components: {"__components_ran__": components},
    )
    return sc, holder


# ---------------- sync_client.publish_note 分支路由 ----------------


def test_audio_branch_skips_image_and_video_steps(patched_sync_client):
    """给了 audio_path:走 step2a/step3a,图文的 step2/3 与视频的 step2v/3v 一次都不碰。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", [], ["#心理"], audio_path="/data/ep.mp3")
    assert r["success"] is True
    calls = holder["atomic"].calls
    assert "step2a:/data/ep.mp3:None" in calls and "step3a" in calls
    assert "step2" not in calls and "step3" not in calls
    assert not any(c.startswith("step2v") for c in calls) and "step3v" not in calls
    # 与视频同源的两道防御都要在
    assert calls.index("interactable") < calls.index("step5")
    assert calls.index("submit_gate") < calls.index("step7")


def test_audio_wins_over_video_when_both_given(patched_sync_client):
    """判型优先级 audio → video → 图文:两个都给(不该发生)时走播客,不走两遍媒体步。

    REST 层已把三选一钉死,这里锁的是**浏览器层自己也有确定的优先级**,
    不会因为上游哪天漏了校验就变成"两条媒体分支都跑一遍"。
    """
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    client.publish_note("T", "C", [], video_path="/data/a.mp4", audio_path="/data/ep.mp3")
    calls = holder["atomic"].calls
    assert "step3a" in calls and "step3v" not in calls


def test_image_branch_untouched_by_podcast(patched_sync_client):
    """回归:没给 audio_path 时图文老路径一行不变。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("标题", "正文", ["/tmp/a.png"], ["#心理"])
    assert r["success"] is True
    calls = holder["atomic"].calls
    assert "step2" in calls and "step3" in calls
    assert not any(c.startswith("step2a") for c in calls) and "step3a" not in calls


def test_audio_upload_failure_stops_before_fill(patched_sync_client):
    """音频没传上去就不继续填内容(否则往空表单打字后点发布)。"""
    sc, holder = patched_sync_client

    class _Fail(_FakeAtomicBase):
        def __init__(self, page, job_tag=None):
            super().__init__(page, job_tag)
            holder["atomic"] = self

        def step3a_wait_for_audio_upload(self, max_wait=None, audio_path=None):
            self.calls.append("step3a")
            return {"success": False, "error": "音频上传超时(540s):「去发布」按钮始终未翻转"}

    import app.browser.sync_client as sc_mod
    sc_mod.XHSPublishAtomicTasks = _Fail
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("T", "C", [], audio_path="/data/ep.mp3")
    assert r["success"] is False and "去发布" in r["error"]
    assert "step5" not in holder["atomic"].calls
    assert "step7" not in holder["atomic"].calls


def test_podcast_collection_selected_and_echoed(patched_sync_client):
    """传了合集名 → 调 select_podcast_collection,并把结果回显到 components。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("T", "C", [], audio_path="/data/ep.mp3",
                            podcast_collection="心理急救包")
    assert holder["collection_calls"] == ["心理急救包"]
    assert r["components"]["podcast_collection"]["status"] == "done"


def test_podcast_collection_failure_does_not_block_publish(patched_sync_client,
                                                           monkeypatch):
    """合集选不上 → 只告警,笔记照发(与三组件同语义),结果如实回显。"""
    sc, holder = patched_sync_client
    monkeypatch.setattr(
        sc, "select_podcast_collection",
        lambda page, human, name: {"status": "error",
                                   "reason": "podcast_collection_field_not_found"},
    )
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("T", "C", [], audio_path="/data/ep.mp3",
                            podcast_collection="心理急救包")
    assert r["success"] is True
    assert r["components"]["podcast_collection"]["status"] == "error"
    assert "step7" in holder["atomic"].calls


def test_audio_task_never_runs_note_collection_components(patched_sync_client):
    """播客任务**完全不跑**三组件那一段:合集名不能被当成笔记合集 id 去匹配。"""
    sc, holder = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note(
        "T", "C", [], components={"collection_id": "心理急救包"},
        audio_path="/data/ep.mp3", podcast_collection="心理急救包",
    )
    assert "__components_ran__" not in r["components"], "播客任务不该走 _apply_components"


def test_audio_cover_echoed_under_components_cover(patched_sync_client):
    """播客封面在 step2a 弹窗里设,但回显仍归一到 components.cover(不为媒体类型换键名)。"""
    sc, _ = patched_sync_client
    client = sc.SyncClient(account_id=1, cookies=[])
    r = client.publish_note("T", "C", [], audio_path="/data/ep.mp3",
                            cover_path="/data/c.png")
    assert r["components"]["cover"] == {"status": "done", "cover_path": "/data/c.png"}


def test_publish_once_forwards_audio_and_collection(monkeypatch):
    """publish_once 把 audio_path / podcast_collection 透传下去(别在这一层丢参数)。"""
    from app.browser import sync_client as sc

    seen = {}
    monkeypatch.setattr(sc.SyncClient, "start", lambda self: {"success": True})
    monkeypatch.setattr(sc.SyncClient, "stop", lambda self: None)

    def _fake(self, title, content, image_paths, topics=None, components=None,
              job_tag=None, video_path=None, cover_path=None, audio_path=None,
              podcast_collection=None):
        seen.update(audio_path=audio_path, podcast_collection=podcast_collection,
                    image_paths=image_paths, cover_path=cover_path)
        return {"success": True, "note_url": "u", "note_id": "i"}

    monkeypatch.setattr(sc.SyncClient, "publish_note", _fake)
    r = sc.publish_once(1, [], "T", "C", [], None, None, audio_path="/data/ep.mp3",
                        cover_path="/data/c.png", podcast_collection="心理急救包")
    assert r.success is True
    assert seen == {"audio_path": "/data/ep.mp3", "podcast_collection": "心理急救包",
                    "image_paths": [], "cover_path": "/data/c.png"}


# ---------------- 两条执行路径都按 audio_path 路由 ----------------


def test_account_worker_audio_job_skips_image_pipeline(monkeypatch):
    """account_worker:播客任务不跑图片物料化/去水印,且合集名走 podcast_collection。"""
    import app.account_worker as aw

    monkeypatch.setattr(aw, "_load_account_cookies", lambda db, aid: [])

    def _boom(*a, **k):
        raise AssertionError("播客任务不该走图片物料化")

    monkeypatch.setattr(aw, "materialize_images", _boom)

    seen = {}

    def _fake(account_id, cookies, title, content, image_paths, topics=None,
              components=None, job_tag=None, video_path=None, cover_path=None,
              audio_path=None, podcast_collection=None):
        seen.update(image_paths=image_paths, audio_path=audio_path,
                    components=components, podcast_collection=podcast_collection)
        return aw.sync_client.PublishResult(success=True, note_url="u")

    monkeypatch.setattr(aw.sync_client, "publish_once", _fake)

    job = {"id": 9, "title": "T", "content": "C", "images_json": "[]",
           "topics_json": '["#心理"]', "audio_path": "/data/ep.mp3",
           "collection_id": "心理急救包", "quoted_note_id": "n1", "activity_id": None}
    result = aw._execute_publish("db.sqlite", 1, job)
    assert result.success is True
    assert seen["audio_path"] == "/data/ep.mp3" and seen["image_paths"] == []
    assert seen["podcast_collection"] == "心理急救包"
    assert seen["components"]["collection_id"] is None, "合集名绝不能混进三组件"
    assert seen["components"]["quoted_note_id"] == "n1", "其余组件照常透传"


def test_account_worker_video_job_unaffected_by_podcast(monkeypatch):
    """回归:视频任务的 collection_id 照旧当笔记合集 id 走三组件,不被播客改动波及。"""
    import app.account_worker as aw

    monkeypatch.setattr(aw, "_load_account_cookies", lambda db, aid: [])
    seen = {}

    def _fake(account_id, cookies, title, content, image_paths, topics=None,
              components=None, job_tag=None, video_path=None, cover_path=None,
              audio_path=None, podcast_collection=None):
        seen.update(components=components, podcast_collection=podcast_collection,
                    audio_path=audio_path)
        return aw.sync_client.PublishResult(success=True, note_url="u")

    monkeypatch.setattr(aw.sync_client, "publish_once", _fake)
    job = {"id": 10, "title": "T", "content": "C", "images_json": "[]",
           "topics_json": "[]", "video_path": "/data/a.mp4",
           "collection_id": "deadbeefdeadbeefdeadbeef"}
    aw._execute_publish("db.sqlite", 1, job)
    assert seen["components"]["collection_id"] == "deadbeefdeadbeefdeadbeef"
    assert seen["podcast_collection"] is None and seen["audio_path"] is None


async def test_scheduler_runner_routes_audio(monkeypatch, tmp_path):
    """publish/scheduler 的 runner:播客任务同样跳过图片管线并透传 audio_path + 合集名。"""
    import app.core.db as db_module
    from app.browser import sync_client as sc
    from app.models import PublishJob
    from app.publish.queue import AccountLocks
    from app.publish.scheduler import PublishScheduler, make_publish_runner
    from tests.rest_helpers import rest_client, seed_account

    async with rest_client(tmp_path, monkeypatch):
        acc = await seed_account("号SP", "uSP", [{"name": "a", "value": "b",
                                                  "domain": ".xiaohongshu.com"}])
        async with db_module.async_session() as s:
            job = PublishJob(account_id=acc, title="T", content="C", images_json="[]",
                             topics_json="[]", status="pending",
                             audio_path="/data/ep.mp3", collection_id="心理急救包")
            s.add(job)
            await s.commit()
            job_id = job.id

        def _boom(*a, **k):
            raise AssertionError("播客任务不该走图片物料化")

        monkeypatch.setattr("app.publish.scheduler.materialize_images", _boom)

        seen = {}

        def _fake(account_id, cookies, title, content, image_paths, topics=None,
                  components=None, job_tag=None, video_path=None, cover_path=None,
                  audio_path=None, podcast_collection=None):
            seen.update(image_paths=image_paths, audio_path=audio_path,
                        components=components, podcast_collection=podcast_collection)
            return sc.PublishResult(success=True, note_url="u", note_id="i")

        monkeypatch.setattr(sc, "publish_once", _fake)

        scheduler = PublishScheduler(db_module.async_session)
        runner = make_publish_runner(db_module.async_session, scheduler, AccountLocks())
        await runner(job_id)

        assert seen["audio_path"] == "/data/ep.mp3" and seen["image_paths"] == []
        assert seen["podcast_collection"] == "心理急救包"
        assert seen["components"]["collection_id"] is None
        async with db_module.async_session() as s:
            done = await s.get(PublishJob, job_id)
            assert done.status == "published"
            assert json.loads(done.images_json) == []


# ---------------- supervisor 硬超时按音频体积伸缩 ----------------


def test_spawn_timeout_scales_for_audio_payload(tmp_path, monkeypatch):
    """1GB 音频与 GB 级视频同量级:supervisor 的进程硬超时必须一样给它加时。"""
    import app.worker as worker_mod

    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"x" * (300 * 1024 * 1024))
    sizes = [audio.stat().st_size]
    # 300MB → 300 + 3*120 = 660s,叠在基准之上
    assert worker_mod.Supervisor._spawn_timeout_for(1800.0, sizes) == 1800 + 660
    # 没有媒体载荷 → 逐字节等于基准(普通浏览器任务行为不许被改)
    assert worker_mod.Supervisor._spawn_timeout_for(1800.0, [None, None]) == 1800.0


def test_publish_media_sizes_reads_audio_column(tmp_path, monkeypatch):
    """``_publish_video_sizes`` 要同时看 video_path 与 audio_path 两列。"""
    import sqlite3

    import app.worker as worker_mod

    db = tmp_path / "t.db"
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"x" * 4096)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE publish_jobs (id INTEGER PRIMARY KEY,"
        " video_path TEXT, audio_path TEXT)"
    )
    conn.execute("INSERT INTO publish_jobs VALUES (1, NULL, ?)", (str(audio),))
    conn.execute("INSERT INTO publish_jobs VALUES (2, NULL, NULL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(worker_mod, "_sqlite_db_path", lambda: str(db))
    assert worker_mod.Supervisor._publish_video_sizes([1, 2]) == [4096, None]


# ---------------- 合集创建 kind 挂进 worker ----------------


def test_podcast_collection_kind_resolves(monkeypatch):
    """``podcast_collection_create`` 必须挂在 account_worker 的 kind 分发表里。

    漏挂的表现是任务被派下去后一律 error「未知 browser job kind」—— 而 REST 那边
    照样 202,调用方只能等轮询到 error 才知道。
    """
    import app.account_worker as aw
    from app.services import podcast_collection

    seen = {}

    async def _fake(account_id, payload):
        seen.update(account_id=account_id, payload=payload)
        return {"status": "done"}

    monkeypatch.setattr(podcast_collection, "execute", _fake)
    call = aw._resolve_execute("podcast_collection_create")
    import asyncio

    asyncio.run(call(7, {"name": "X"}))
    assert seen == {"account_id": 7, "payload": {"name": "X"}}


def test_unknown_kind_still_raises():
    """回归:没挂过的 kind 照旧抛 ValueError(别被新分支顺手吞掉)。"""
    import app.account_worker as aw

    with pytest.raises(ValueError):
        aw._resolve_execute("podcast_collection_delete")
