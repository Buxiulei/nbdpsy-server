"""账号子进程(app.account_worker)单测:mock 掉浏览器/repo 契约点,纯 DB 状态机验证。

不依赖 Agent A 的 browser_jobs_repo 真实现——桩模块注入 ``app.services.browser_jobs_repo``;
不起浏览器——monkeypatch ``sync_client.publish_once`` 与各 service 的 ``execute``。

覆盖(任务书要求):
- 认领失败跳过(publish 竞态占不到 / browser job claim 返回 None);
- 成功 / 失败重试 / need_manual_login / account_restricted 四种发布终态落库;
- 冷却与日上限不满足 → 顺延 next_retry_at、不递增 retries、不执行;
- browser job 终态写回(done / error / execute 抛异常 / 未知 kind);
- 异常兜底不留 publishing/running 悬挂(含落库路径全挂时 finally 归位);
- 退出码恒 0;先 publish 后 browser 的串行次序;心跳线程 touch。
"""

import json
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import account_worker as aw
from app.browser.sync_client import PublishResult
from app.core.config import settings
from app.models.publish_job import PublishJob
from app.models.xhs_account import XhsAccount

DELAYS = settings.retry_delays  # 默认 "120,600,1800"


# ---------------- fixtures ----------------


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, monkeypatch):
    """物料化临时目录指向 tmp_path,避免测试往仓库 ./data/uploads 写垃圾。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))


@pytest.fixture
def wdb(tmp_path):
    """账号子进程视角的文件级 sqlite:同步引擎建表,暴露 (db_path, engine)。

    worker 走 sqlite3 直连读写同一文件;断言用 SQLAlchemy 同步会话回读。
    """
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    path = str(tmp_path / "worker.db")
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    yield path, engine
    engine.dispose()


class FakeRepo:
    """browser_jobs_repo 契约桩:记录 claim/finish/heartbeat 调用。"""

    def __init__(self, claim_result=None):
        self.claim_result = claim_result
        self.claim_calls: list[tuple] = []
        self.finish_calls: list[tuple] = []
        self.heartbeat_calls: list[str] = []

    def claim_job_sync(self, db_path, job_id, worker_tag):
        self.claim_calls.append((db_path, job_id, worker_tag))
        return self.claim_result

    def finish_job_sync(self, db_path, job_id, status, result, worker_tag=None):
        self.finish_calls.append((job_id, status, result))

    def heartbeat_sync(self, db_path, job_id):
        self.heartbeat_calls.append(job_id)


@pytest.fixture
def fake_repo(monkeypatch):
    """把桩 repo 注入 ``app.services.browser_jobs_repo`` 导入点(A 的产物尚不存在)。"""
    import app.services as services_pkg

    repo = FakeRepo()
    monkeypatch.setitem(sys.modules, "app.services.browser_jobs_repo", repo)
    monkeypatch.setattr(services_pkg, "browser_jobs_repo", repo, raising=False)
    return repo


# ---------------- 建数据辅助 ----------------


def _make_account(engine, name: str = "acc") -> int:
    with Session(engine) as session:
        acc = XhsAccount(name=name)
        session.add(acc)
        session.commit()
        return acc.id


def _make_job(engine, account_id: int, **overrides) -> int:
    defaults = dict(
        account_id=account_id,
        title="标题",
        content="正文",
        images_json="[]",
        topics_json="[]",
    )
    defaults.update(overrides)
    with Session(engine) as session:
        job = PublishJob(**defaults)
        session.add(job)
        session.commit()
        return job.id


def _get_job(engine, job_id: int) -> PublishJob:
    with Session(engine) as session:
        return session.get(PublishJob, job_id)


def _patch_publish_once(monkeypatch, result_or_exc, calls=None):
    """monkeypatch 拟人层入口:记录调用并返回固定结果 / 抛异常。"""

    def fake_publish_once(
        account_id, cookies, title, content, image_paths, topics, components=None,
        job_tag=None, video_path=None, cover_path=None,
    ):
        # job_tag 是发布任务 id,只用于给失败现场截图打标(见 publish_artifacts_rest);
        # 记进 calls 让"确实按 job 打了标"这条能被断言,而不是悄悄没传。
        if calls is not None:
            calls.append(
                (account_id, cookies, title, content, image_paths, topics, job_tag)
            )
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(aw.sync_client, "publish_once", fake_publish_once)


def _patch_dewatermark(monkeypatch, calls=None, fail_on=None):
    """把去水印闸的叶子(真起 chromium 的那层)换成桩,批量编排走真实 dewatermark_all。

    桩按输入路径产出可区分的 ``{原路径}.shot.jpg``,便于断言顺序;``fail_on`` 命中的
    输入返回 None(模拟 reraster 未产出),用于验 fail-closed。
    """
    from app.imagegen import postprocess

    async def fake_dewatermark(path):
        if calls is not None:
            calls.append(path)
        if fail_on is not None and path == fail_on:
            return None
        return f"{path}.shot.jpg"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)


# ---------------- publish:四种终态落库 ----------------


def test_publish_success_terminal(wdb, monkeypatch):
    """成功:pending → published,note 回填、error 清空、started_at 保留(冷却历史依据)。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    calls = []
    _patch_publish_once(
        monkeypatch,
        PublishResult(success=True, note_id="nid", note_url="https://xhs/1"),
        calls,
    )

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "published"
    assert job.note_id == "nid"
    assert job.note_url == "https://xhs/1"
    assert job.error is None
    assert job.started_at is not None
    assert job.retries == 0
    # 发布参数由 job 正确拆出(账号无 cookie → 空列表)
    acc_id, cookies, title, content, image_paths, topics, job_tag = calls[0]
    assert (acc_id, cookies, title, content) == (account_id, [], "标题", "正文")
    # job_tag 必须是这条任务的 id:截图靠它打前缀,不传就等于失败现场又取不回来了
    # (GET /api/publish-jobs/{id}/artifacts 会永远返回空,而且是**静默**的)
    assert job_tag == str(job_id)


def test_publish_failure_schedules_retry(wdb, monkeypatch):
    """普通失败:回 pending + retries 递增 + next_retry_at 按 delays[0]×抖动排期。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, PublishResult(success=False, error="boom"))

    before = datetime.utcnow()
    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "pending"
    assert job.retries == 1
    assert job.error == "boom"
    assert job.started_at is None
    assert job.next_retry_at is not None
    assert job.next_retry_at >= before + timedelta(seconds=DELAYS[0] * 0.8 - 1)
    assert job.next_retry_at <= before + timedelta(seconds=DELAYS[0] * 1.5 + 5)


def test_publish_need_manual_login_fails_immediately(wdb, monkeypatch):
    """I1:need_manual_login → 立即 failed,retries 不递增、无重试排期。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(
        monkeypatch,
        PublishResult(success=False, error="创作中心未登录", need_manual_login=True),
    )

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "failed"
    assert job.retries == 0
    assert job.next_retry_at is None
    assert job.started_at is None
    assert "创作中心未登录" in job.error


def test_publish_account_restricted_fails_immediately(wdb, monkeypatch):
    """F3:account_restricted → 立即 failed,retries 不递增、无重试排期。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(
        monkeypatch,
        PublishResult(success=False, error="账号禁发", account_restricted=True),
    )

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "failed"
    assert job.retries == 0
    assert job.next_retry_at is None
    assert "账号禁发" in job.error


def test_publish_retry_exhausted_to_failed(wdb, monkeypatch):
    """重试耗尽(retries 预置为 len(delays))→ 再失败一次即终态 failed。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id, retries=len(DELAYS))
    _patch_publish_once(monkeypatch, PublishResult(success=False, error="final"))

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "failed"
    assert job.error == "final"
    assert job.retries == len(DELAYS)


# ---------------- publish:认领失败 / 门 ----------------


def test_publish_claim_race_skips(wdb, monkeypatch):
    """竞态:载参时 pending、认领时已被别处占走 → 乐观 UPDATE 落空,不触发发布。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    # DB 真实状态 publishing(别处已占);started_at=None 避免影响冷却门查询
    job_id = _make_job(engine, account_id, status="publishing")
    # 模拟"载参时还是 pending"的竞态窗口
    monkeypatch.setattr(
        aw,
        "_load_publish_job",
        lambda db, jid: dict(
            id=jid, status="pending", retries=0,
            title="t", content="c", images_json="[]", topics_json="[]",
        ),
    )
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    assert calls == []  # 未发布
    assert _get_job(engine, job_id).status == "publishing"  # 占有者的状态未被打扰


def test_publish_not_pending_skips(wdb, monkeypatch):
    """非 pending(已 published)直接跳过,不发布不改行。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id, status="published")
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    assert calls == []
    assert _get_job(engine, job_id).status == "published"


def test_publish_cooldown_postpones_without_retry_increment(wdb, monkeypatch):
    """冷却不满足(兄弟 job 刚发)→ 顺延 next_retry_at ∈ now+[MIN,MAX],retries 不递增、不执行。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    # 兄弟 job 刚刚发布(elapsed≈0 < MIN=1200,冷却必不满足)
    _make_job(engine, account_id, status="published", started_at=datetime.utcnow())
    job_id = _make_job(engine, account_id)
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True), calls)

    before = datetime.utcnow()
    aw.run_publish_job(db_path, account_id, job_id)

    assert calls == []  # 未执行发布
    job = _get_job(engine, job_id)
    assert job.status == "pending"
    assert job.retries == 0  # 不递增
    assert job.started_at is None  # 未被占用
    assert job.next_retry_at is not None
    assert job.next_retry_at >= before + timedelta(
        seconds=settings.PUBLISH_MIN_INTERVAL_MIN - 2
    )
    assert job.next_retry_at <= before + timedelta(
        seconds=settings.PUBLISH_MIN_INTERVAL_MAX + 5
    )


def test_publish_daily_cap_postpones(wdb, monkeypatch):
    """日上限达标 → 顺延不执行(冷却间隔压 0 隔离变量,只验日上限)。"""
    db_path, engine = wdb
    monkeypatch.setattr(settings, "PUBLISH_MIN_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "PUBLISH_MIN_INTERVAL_MAX", 0)
    monkeypatch.setattr(settings, "PUBLISH_DAILY_CAP", 1)
    account_id = _make_account(engine)
    # 当日已有 1 条 published(达上限);冷却间隔为 0 不拦
    _make_job(engine, account_id, status="published", started_at=datetime.utcnow())
    job_id = _make_job(engine, account_id)
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    assert calls == []
    job = _get_job(engine, job_id)
    assert job.status == "pending"
    assert job.retries == 0
    assert job.next_retry_at is not None


# ---------------- publish:异常兜底不留悬挂 ----------------


def test_publish_exception_does_not_stick(wdb, monkeypatch):
    """publish_once 抛异常:兜底按普通失败裁决 → pending + retries 递增,不卡 publishing。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, RuntimeError("浏览器炸了"))

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "pending"  # 不卡 publishing
    assert job.retries == 1
    assert job.next_retry_at is not None
    assert job.started_at is None
    assert "浏览器炸了" in job.error


def test_publish_apply_failure_finally_requeues(wdb, monkeypatch):
    """落库路径全挂(_apply 两次都抛)→ finally 兜底归位 pending + next_retry,绝不留 publishing。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, PublishResult(success=True, note_id="n"))

    def boom_apply(*args, **kwargs):
        raise RuntimeError("落库炸了")

    monkeypatch.setattr(aw, "_apply_publish_decision", boom_apply)

    aw.run_publish_job(db_path, account_id, job_id)

    job = _get_job(engine, job_id)
    assert job.status == "pending"  # finally 归位,不悬挂 publishing
    assert job.started_at is None
    assert job.next_retry_at is not None
    assert job.retries == 0  # 兜底路径不递增


# ---------------- browser job ----------------


def _browser_row(job_id, kind, account_id, payload=None):
    return {
        "id": job_id,
        "kind": kind,
        "account_id": account_id,
        "operator_id": 1,
        "payload": payload or {},
        "status": "running",
    }


def test_browser_job_done_writes_result(wdb, fake_repo, monkeypatch):
    """execute 正常返回 → finish_job_sync(done, result);claim 带 worker_tag。"""
    db_path, _ = wdb
    fake_repo.claim_result = _browser_row("bj1", "note_export", 7, {"count": 5})
    seen = {}

    async def fake_execute(account_id, payload):
        seen["args"] = (account_id, payload)
        return {"note_count": 5}

    monkeypatch.setattr(
        "app.services.note_export.execute", fake_execute, raising=False
    )

    aw.run_browser_job(db_path, 7, "bj1", "account_worker:a7:pid1")

    assert seen["args"] == (7, {"count": 5})
    assert fake_repo.claim_calls == [(db_path, "bj1", "account_worker:a7:pid1")]
    assert fake_repo.finish_calls == [("bj1", "done", {"note_count": 5})]


def test_browser_job_error_result(wdb, fake_repo, monkeypatch):
    """execute 返回 {"error": ...} → 终态 error 写回。"""
    db_path, _ = wdb
    fake_repo.claim_result = _browser_row("bj2", "cookie_check", 7)

    async def fake_execute(account_id, payload):
        return {"error": "cookie 失效"}

    monkeypatch.setattr(
        "app.services.cookie_check.execute", fake_execute, raising=False
    )

    aw.run_browser_job(db_path, 7, "bj2", "tag")

    assert fake_repo.finish_calls == [("bj2", "error", {"error": "cookie 失效"})]


def test_browser_job_claim_none_skips(wdb, fake_repo, monkeypatch):
    """claim 返回 None(非 queued)→ 不执行、不写终态。"""
    db_path, _ = wdb
    fake_repo.claim_result = None
    called = {"n": 0}

    async def fake_execute(account_id, payload):
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "app.services.cookie_check.execute", fake_execute, raising=False
    )

    aw.run_browser_job(db_path, 7, "bj3", "tag")

    assert called["n"] == 0
    assert fake_repo.finish_calls == []


def test_browser_job_execute_exception_writes_error(wdb, fake_repo, monkeypatch):
    """execute 抛异常 → 兜底 finish(error),不留 running 悬挂。"""
    db_path, _ = wdb
    fake_repo.claim_result = _browser_row("bj4", "note_delete", 7)

    async def boom_execute(account_id, payload):
        raise RuntimeError("删除翻车")

    monkeypatch.setattr(
        "app.services.note_delete.execute", boom_execute, raising=False
    )

    aw.run_browser_job(db_path, 7, "bj4", "tag")

    assert len(fake_repo.finish_calls) == 1
    job_id, status, result = fake_repo.finish_calls[0]
    assert (job_id, status) == ("bj4", "error")
    assert "删除翻车" in result["error"]


def test_browser_job_unknown_kind_writes_error(wdb, fake_repo):
    """未知 kind → 兜底 finish(error),同样不悬挂。"""
    db_path, _ = wdb
    fake_repo.claim_result = _browser_row("bj5", "bogus_kind", 7)

    aw.run_browser_job(db_path, 7, "bj5", "tag")

    assert len(fake_repo.finish_calls) == 1
    job_id, status, result = fake_repo.finish_calls[0]
    assert (job_id, status) == ("bj5", "error")
    assert "bogus_kind" in result["error"]


def test_browser_job_op_images_payload_only(wdb, fake_repo, monkeypatch):
    """op_images 无账号绑定:execute 只收 payload(account_id 为 None 也不炸)。"""
    db_path, _ = wdb
    fake_repo.claim_result = _browser_row(
        "opimg_s1_1", "op_images", None, {"prompts": ["p"]}
    )
    seen = {}

    async def fake_execute(payload):
        seen["payload"] = payload
        return {"urls": ["https://img/1"], "errors": []}

    monkeypatch.setattr("app.services.op_images.execute", fake_execute, raising=False)

    aw.run_browser_job(db_path, 7, "opimg_s1_1", "tag")

    assert seen["payload"] == {"prompts": ["p"]}
    assert fake_repo.finish_calls == [
        ("opimg_s1_1", "done", {"urls": ["https://img/1"], "errors": []})
    ]


def test_heartbeat_loop_touches_until_stopped(wdb, fake_repo):
    """心跳线程体:周期 touch heartbeat_sync,stop_event 置位即退。"""
    db_path, _ = wdb
    stop = threading.Event()
    thread = threading.Thread(
        target=aw._heartbeat_loop, args=(db_path, "bj6", stop, 0.01), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while len(fake_repo.heartbeat_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(fake_repo.heartbeat_calls) >= 2
    assert set(fake_repo.heartbeat_calls) == {"bj6"}


# ---------------- CLI:退出码 / 串行次序 ----------------


def test_main_exit_code_zero_despite_failures(wdb, fake_repo, monkeypatch):
    """publish 抛异常 + browser claim 落空:退出码仍恒 0,失败记录在台账行。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, RuntimeError("全都炸"))
    fake_repo.claim_result = None

    rc = aw.main(
        [
            "--account-id", str(account_id),
            "--publish-job-ids", str(job_id),
            "--browser-job-ids", "bj9",
            "--db", db_path,
        ]
    )

    assert rc == 0
    # 发布异常已兜底落台账(pending 排重试),不悬挂
    assert _get_job(engine, job_id).status == "pending"


def test_main_serial_publish_first_ascending(wdb, monkeypatch):
    """串行契约:先 publish(id 升序,乱序入参也归序)后 browser jobs(保持传入顺序)。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    order = []
    monkeypatch.setattr(
        aw, "run_publish_job", lambda db, acc, jid: order.append(("p", jid))
    )
    monkeypatch.setattr(
        aw, "run_browser_job", lambda db, acc, jid, tag: order.append(("b", jid))
    )

    rc = aw.main(
        [
            "--account-id", str(account_id),
            "--publish-job-ids", "5,2,9",
            "--browser-job-ids", "h2,h1",
            "--db", db_path,
        ]
    )

    assert rc == 0
    assert order == [("p", 2), ("p", 5), ("p", 9), ("b", "h2"), ("b", "h1")]


def test_publish_materializes_images_before_publish(wdb, monkeypatch):
    """图片经物料化(URL → 本地路径)后才传给 publish_once,顺序保持。"""
    from pathlib import Path

    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(
        engine,
        account_id,
        images_json=json.dumps(["https://cdn/a.png"]),
        topics_json=json.dumps(["#心理"]),
    )
    captured = {}

    def fake_materialize(images, workdir):
        captured["materialize"] = (list(images), str(workdir))
        return [Path("/local/a.png")]

    monkeypatch.setattr(aw, "materialize_images", fake_materialize)
    _patch_dewatermark(monkeypatch)
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True, note_id="n"), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    assert captured["materialize"][0] == ["https://cdn/a.png"]
    _, _, _, _, image_paths, topics, _ = calls[0]
    # 交给浏览器的是去水印后的产物,不是物料化的原图
    assert image_paths == ["/local/a.png.shot.jpg"]
    assert topics == ["#心理"]
    assert _get_job(engine, job_id).status == "published"


# ---------------- publish:发布口去水印闸(fail-closed) ----------------


def test_publish_dewatermarks_every_image_in_page_order(wdb, monkeypatch):
    """闸放行:每张图都过去水印,publish_once 收到的是清洗后路径且页序与原序严格一致。"""
    from pathlib import Path

    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(
        engine,
        account_id,
        images_json=json.dumps(["https://cdn/a.png", "https://cdn/b.png", "https://cdn/c.png"]),
    )
    monkeypatch.setattr(
        aw,
        "materialize_images",
        lambda images, workdir: [Path(f"/local/img_{i:02d}.png") for i in range(len(images))],
    )
    dw_calls = []
    _patch_dewatermark(monkeypatch, calls=dw_calls)
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True, note_id="n"), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    # 逐张过闸,且按页序
    assert dw_calls == ["/local/img_00.png", "/local/img_01.png", "/local/img_02.png"]
    _, _, _, _, image_paths, _, _ = calls[0]
    assert image_paths == [
        "/local/img_00.png.shot.jpg",
        "/local/img_01.png.shot.jpg",
        "/local/img_02.png.shot.jpg",
    ]
    assert _get_job(engine, job_id).status == "published"


def test_publish_dewatermark_failure_aborts_whole_job(wdb, monkeypatch):
    """闸拦下:任一张去水印失败 → publish_once 根本不被调用,整个发布任务失败排重试。

    这是 job 14/15/16 事故的反向锚:绝不允许"某张失败就拿原图顶上"的静默降级。
    """
    from pathlib import Path

    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(
        engine,
        account_id,
        images_json=json.dumps(["https://cdn/a.png", "https://cdn/b.png"]),
    )
    monkeypatch.setattr(
        aw,
        "materialize_images",
        lambda images, workdir: [Path(f"/local/img_{i:02d}.png") for i in range(len(images))],
    )
    _patch_dewatermark(monkeypatch, fail_on="/local/img_01.png")
    calls = []
    _patch_publish_once(monkeypatch, PublishResult(success=True), calls)

    aw.run_publish_job(db_path, account_id, job_id)

    assert calls == []  # 一张都没发出去
    job = _get_job(engine, job_id)
    assert job.status == "pending"  # 兜底按普通失败裁决 → 排重试
    assert job.retries == 1
    assert job.next_retry_at is not None
    assert "第 2 页" in job.error
    assert "拒绝发布未去水印的图" in job.error


def test_publish_success_triggers_auto_archive(wdb, monkeypatch):
    """自动归档钩子:发布 published 后,account_worker 自动把内容归档进 content_archive
    (source_publish_job_id=该 job),归档函数收到正确 db_path+job_id。绝不阻断发布终态。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, PublishResult(success=True, note_url="https://xhs/9"))

    seen = {}

    def fake_archive(dbp, jid):
        seen["args"] = (dbp, jid)
        return 42

    # account_worker 在 published 分支内 from ... import archive_published_job 后调用,
    # patch 源模块函数即命中(延迟 import 取的是模块属性)。
    monkeypatch.setattr(
        "app.services.content_archive.archive_published_job", fake_archive
    )

    aw.run_publish_job(db_path, account_id, job_id)

    assert _get_job(engine, job_id).status == "published"  # 发布正常终态
    assert seen["args"] == (db_path, job_id)  # 归档钩子被调,参数正确


def test_auto_archive_failure_does_not_break_publish(wdb, monkeypatch):
    """归档抛异常绝不回滚发布:钩子炸了,job 仍稳定 published(终态已先落定)。"""
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, PublishResult(success=True))

    def boom(dbp, jid):
        raise RuntimeError("归档炸了")

    monkeypatch.setattr("app.services.content_archive.archive_published_job", boom)

    aw.run_publish_job(db_path, account_id, job_id)  # 不应抛
    assert _get_job(engine, job_id).status == "published"  # 发布仍成功


def test_publish_success_persists_applied_echo(wdb, monkeypatch):
    """发布成功后 result_json 落库 = 服务端实际应用的话题/组件回显。

    没有它,参数被静默丢弃(2026-08-03 文字版话题全丢)只能等笔记发出去人工读正文
    才察觉 —— 运营为此白删了一篇。落了库,REST 轮询里 topics_applied: [] 当场可见。
    """
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    calls = []
    _patch_dewatermark(monkeypatch)
    _patch_publish_once(
        monkeypatch,
        PublishResult(
            success=True, note_id="nid", note_url="https://xhs/1",
            applied={"topics_requested": ["恋爱脑"], "topics_applied": [],
                     "topics_failed": [{"tag": "恋爱脑", "reason": "content_box_focus_failed"}],
                     "components": {}},
        ),
        calls,
    )

    aw.run_publish_job(db_path, account_id, job_id)

    import json as _json
    import sqlite3 as _sq
    with _sq.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT result_json FROM publish_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    echo = _json.loads(raw)
    assert echo["topics_applied"] == []
    assert echo["topics_failed"][0]["reason"] == "content_box_focus_failed"


def test_publish_success_does_not_schedule_comments(wdb, monkeypatch):
    """发布成功**不再**排自动评论(2026-08-04 运营裁定:自动互动只点赞+收藏)。

    schedule_note_comments 保留为模块能力(执行契约 note_comment_task 也要认历史行),
    但发布钩子不再调它;矩阵互动(点赞+收藏)钩子照常。本测锁住"评论步已从自动管线
    摘除",防止日后有人看到模块还在就把钩子接回去。
    """
    db_path, engine = wdb
    account_id = _make_account(engine)
    job_id = _make_job(engine, account_id)
    _patch_publish_once(monkeypatch, PublishResult(success=True, note_url="https://xhs/10"))

    called = {"comments": False, "interact": False}
    monkeypatch.setattr(
        "app.services.note_comment_task.schedule_note_comments",
        lambda dbp, jid: called.__setitem__("comments", True) or [],
    )
    monkeypatch.setattr(
        "app.services.matrix_interact.schedule_matrix_interact",
        lambda dbp, jid: called.__setitem__("interact", True) or [],
    )

    aw.run_publish_job(db_path, account_id, job_id)

    assert _get_job(engine, job_id).status == "published"
    assert called["interact"] is True    # 点赞+收藏钩子照常
    assert called["comments"] is False   # 评论钩子已摘除
