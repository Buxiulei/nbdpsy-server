"""发布 runner 层测试(与工具/REST 无关,只测 make_publish_runner 的图片物料化行为)。

从 tests/test_publish_tools.py::test_runner_materializes_images_and_cleans_workdir
原样整体搬入 —— runner 是 app/publish/scheduler.py 里的纯 DB + 浏览器调用层,不依赖
MCP 工具还是 REST 端点入队,故独立成文件,方便 Task 6 删除 test_publish_tools.py 时
不误删这条用例。

注:本文件测的 runner 当前是**单进程回滚位**(NBDPSY_ROLE=all,生产已由 api/worker
双单元取代),去水印闸与活路径 account_worker 同源,回滚后同样不发未去水印的图。
"""

import json
from pathlib import Path

from app.models import PublishJob, XhsAccount
from app.publish.queue import AccountLocks
from app.publish.scheduler import PublishScheduler, make_publish_runner


def _patch_dewatermark(monkeypatch, fail_on=None):
    """把去水印闸的叶子(真起 chromium 的那层)换成桩,批量编排走真实 dewatermark_all。"""
    from app.imagegen import postprocess

    async def fake_dewatermark(path):
        if fail_on is not None and path == fail_on:
            return None
        return f"{path}.shot.jpg"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)


# ---------------- runner 图片物料化(接线洞修复) ----------------


async def test_runner_materializes_images_and_cleans_workdir(
    db_factory, monkeypatch, tmp_path
):
    """runner:URL/base64 图片先 materialize_images 落本地 → 用本地路径调 publish_once → 发布后清理 workdir。"""
    from app.publish import scheduler as scheduler_mod

    # workdir 落到隔离临时目录(而非真实 ./data/uploads)
    monkeypatch.setattr(scheduler_mod.settings, "UPLOAD_DIR", str(tmp_path))

    async with db_factory() as s:
        acc = XhsAccount(name="acc")
        s.add(acc)
        await s.commit()
        account_id = acc.id
    async with db_factory() as s:
        job = PublishJob(
            account_id=account_id, title="T", content="C",
            images_json=json.dumps(["https://cdn/a.png", "https://cdn/b.png"]),
            topics_json="[]", status="pending",
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    captured = {}

    def fake_materialize(images, workdir):
        # 真建 workdir + 落一个文件,供之后断言清理
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        f = workdir / "img_00.png"
        f.write_bytes(b"fake")
        captured["workdir"] = workdir
        captured["images"] = list(images)
        return [f]

    monkeypatch.setattr(scheduler_mod, "materialize_images", fake_materialize)
    _patch_dewatermark(monkeypatch)

    def fake_publish_once(
        acc_id, cookies, title, content, image_paths, topics, components=None,
        video_path=None, cover_path=None, audio_path=None, podcast_collection=None,
    ):
        # video_path:图文任务恒为 None(视频分支的路由另有用例覆盖)
        captured["image_paths"] = image_paths
        captured["video_path"] = video_path
        # 发布时 workdir 及物料文件仍在
        captured["exists_during"] = captured["workdir"].exists()
        from app.browser.sync_client import PublishResult
        return PublishResult(success=True, note_url="https://xhs/ok")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    # 物料化收到原始 URL 列表;publish_once 用的是物料化 + 去水印后的本地路径
    assert captured["images"] == ["https://cdn/a.png", "https://cdn/b.png"]
    assert captured["image_paths"] == [str(captured["workdir"] / "img_00.png") + ".shot.jpg"]
    assert captured["exists_during"] is True
    # 发布后 workdir 被清理
    assert not captured["workdir"].exists()

    async with db_factory() as s:
        assert (await s.get(PublishJob, job_id)).status == "published"


async def test_runner_dewatermark_failure_aborts_publish(db_factory, monkeypatch, tmp_path):
    """闸拦下(回滚位同款 fail-closed):任一张去水印失败 → publish_once 不被调用,job 排重试。"""
    from app.publish import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod.settings, "UPLOAD_DIR", str(tmp_path))

    async with db_factory() as s:
        acc = XhsAccount(name="acc")
        s.add(acc)
        await s.commit()
        account_id = acc.id
    async with db_factory() as s:
        job = PublishJob(
            account_id=account_id, title="T", content="C",
            images_json=json.dumps(["https://cdn/a.png", "https://cdn/b.png"]),
            topics_json="[]", status="pending",
        )
        s.add(job)
        await s.commit()
        job_id = job.id

    monkeypatch.setattr(
        scheduler_mod,
        "materialize_images",
        lambda images, workdir: [Path(f"/local/img_{i:02d}.png") for i in range(len(images))],
    )
    _patch_dewatermark(monkeypatch, fail_on="/local/img_01.png")

    called = {"n": 0}

    def fake_publish_once(*args, **kwargs):
        called["n"] += 1
        from app.browser.sync_client import PublishResult
        return PublishResult(success=True)

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    assert called["n"] == 0  # 一张都没发出去
    async with db_factory() as s:
        job = await s.get(PublishJob, job_id)
        assert job.status == "pending"  # 兜底 finish(fail) → 排重试,不卡 publishing
        assert job.retries == 1
        assert "第 2 页" in job.error
        assert "拒绝发布未去水印的图" in job.error
