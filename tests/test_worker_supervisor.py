"""Supervisor 调度中枢测试(app/worker.py,设计 §四 多账号并行调度)。

隔离手法:publish_jobs 走 db_factory 隔离库真 SQL;browser_jobs 台账(P1 产物)与
account_worker 子进程(B 产物)在本分支不存在 —— 按契约签名注入假 repo、monkeypatch
``asyncio.create_subprocess_exec`` 记录派生(不起真进程)。

覆盖(任务书必测):
- 账号公平轮转:A 号 10 单不阻塞 B 号首单(同一轮各拿到子进程);派过的账号移到轮转表尾。
- 全局子进程数封顶 max_procs;同账号同一时刻至多 1 个子进程。
- kind=op_images 不派子进程,supervisor 进程内认领执行并写回终态。
- request_stop(SIGTERM/SIGINT 处理器所调)后停止派发,run() 及时退出。
"""

import asyncio
from datetime import datetime, timedelta

import app.services.op_images as op_images_service
from app.models.publish_job import PublishJob
from app.worker import Supervisor, _REPO_ROOT


# ---------------- 假件:browser_jobs repo / 子进程 ----------------


class _FakeRepo:
    """按 browser_jobs_repo 契约签名的假台账(P1 集成前的替身)。"""

    def __init__(self, rows: list[dict] | None = None, claimable: bool = True):
        self.rows = rows or []
        self.claimable = claimable  # False 模拟"已被他处领走"(claim 返回 None)
        self.recover_calls = 0
        self.claims: list[tuple] = []
        self.finishes: list[tuple] = []

    async def recover_stale(self) -> int:
        self.recover_calls += 1
        return 0

    async def list_dispatchable(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def claim_job_sync(self, db_path, job_id, worker_tag):
        self.claims.append((db_path, job_id, worker_tag))
        if not self.claimable:
            return None
        row = next((r for r in self.rows if r["id"] == job_id), None)
        return dict(row) if row else None

    def finish_job_sync(self, db_path, job_id, status, result):
        self.finishes.append((job_id, status, result))


class _FakeProc:
    """可控退出的假子进程(pid 仅作标识,杀进程路径在测试里被替换掉,绝不触真 killpg)。"""

    _next_pid = 4_000_000

    def __init__(self):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None
        self._exited = asyncio.Event()

    async def wait(self):
        await self._exited.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def exit(self, code: int = 0):
        self.returncode = code
        self._exited.set()

    def kill(self):
        self.exit(-9)


def _install_spawn_recorder(monkeypatch) -> list[dict]:
    """把 asyncio.create_subprocess_exec 换成记录器:登记 cmd/kwargs,返回假进程。"""
    spawned: list[dict] = []

    async def _fake_exec(*cmd, **kwargs):
        proc = _FakeProc()
        spawned.append({"cmd": list(cmd), "kwargs": kwargs, "proc": proc})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    # 杀进程组路径改为直杀假进程:假 pid 不落真 killpg,杜绝误伤真实进程。
    monkeypatch.setattr(
        Supervisor, "_kill_process_group", staticmethod(lambda proc: proc.kill())
    )
    return spawned


def _cmd_account(cmd: list[str]) -> int:
    return int(cmd[cmd.index("--account-id") + 1])

def _cmd_opt(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


async def _seed_pending(db_factory, account_id: int, n: int = 1) -> list[int]:
    """灌 n 条到期 pending 发布任务(created_at 逐条 +1s,oldest-first 可断言)。"""
    ids = []
    base = datetime.utcnow() - timedelta(minutes=30)
    async with db_factory() as s:
        for i in range(n):
            job = PublishJob(
                account_id=account_id,
                title=f"t{account_id}-{i}",
                content="c",
                images_json="[]",
                topics_json="[]",
                status="pending",
                created_at=base + timedelta(seconds=i),
            )
            s.add(job)
            await s.flush()
            ids.append(job.id)
        await s.commit()
    return ids


async def _drain(sup: Supervisor, timeout: float = 2.0) -> None:
    """等 supervisor 内部后台 task(子进程回收/op_images 执行)全部结束。"""
    tasks = list(sup._tasks)
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout
        )


async def _finish_all(sup: Supervisor, spawned: list[dict]) -> None:
    """测试收尾:让全部假子进程退出并回收,不留悬挂 task。"""
    for s in spawned:
        s["proc"].exit()
    await _drain(sup)


async def _wait_live_procs(sup: Supervisor, n: int, timeout: float = 2.0) -> None:
    """轮询等待存活子进程数降到 n(个别子进程退出后回收出表,不等其余在跑任务)。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while len(sup._procs) > n:
        assert asyncio.get_running_loop().time() < deadline, "子进程回收超时"
        await asyncio.sleep(0.01)


# ---------------- 账号公平轮转 ----------------


async def test_fair_dispatch_a10_not_blocking_b(db_factory, monkeypatch):
    """A 号灌 10 单不阻塞 B 号首单:同一轮扫描 A、B 各拿到 1 个子进程;
    A 批封顶 batch_per_account 且 oldest first;命令行按 account_worker CLI 契约拼装。"""
    spawned = _install_spawn_recorder(monkeypatch)
    a_ids = await _seed_pending(db_factory, 1, 10)
    b_ids = await _seed_pending(db_factory, 2, 1)
    sup = Supervisor(
        db_factory, repo=_FakeRepo(), batch_per_account=3, max_procs=6
    )

    await sup.scan_once()

    by_acc = {_cmd_account(s["cmd"]): s["cmd"] for s in spawned}
    assert set(by_acc) == {1, 2}, "B 号首单不得被 A 号大批量饿死"
    # A 批:最多 3 单、oldest first(created_at 升序 = 前 3 个 id)
    assert _cmd_opt(by_acc[1], "--publish-job-ids") == ",".join(
        str(i) for i in a_ids[:3]
    )
    assert _cmd_opt(by_acc[2], "--publish-job-ids") == str(b_ids[0])
    # CLI 契约:.venv python -m app.account_worker,cwd=仓库根,独立进程组
    for s in spawned:
        assert s["cmd"][0].endswith(".venv/bin/python")
        assert s["cmd"][1:3] == ["-m", "app.account_worker"]
        assert s["kwargs"]["cwd"] == str(_REPO_ROOT)
        assert s["kwargs"]["start_new_session"] is True

    await _finish_all(sup, spawned)


async def test_rotation_moves_dispatched_to_tail(db_factory, monkeypatch):
    """上轮派过的账号排队尾:并发上限 1 时,第一轮派 A,第二轮轮到 B(而非 A 连庄)。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_pending(db_factory, 1, 2)
    await _seed_pending(db_factory, 2, 1)
    sup = Supervisor(db_factory, repo=_FakeRepo(), max_procs=1)

    await sup.scan_once()
    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    spawned[0]["proc"].exit()
    await _drain(sup)  # 子进程回收出表,释放全局并发额度

    await sup.scan_once()
    assert [_cmd_account(s["cmd"]) for s in spawned] == [1, 2]

    await _finish_all(sup, spawned)


# ---------------- 并发闸 ----------------


async def test_global_proc_cap(db_factory, monkeypatch):
    """全局子进程数封顶 max_procs:3 个账号各有单,上限 2 → 本轮只派 2 个;
    有子进程退出后,下一轮补派第 3 个账号。"""
    spawned = _install_spawn_recorder(monkeypatch)
    for acc in (1, 2, 3):
        await _seed_pending(db_factory, acc, 1)
    sup = Supervisor(db_factory, repo=_FakeRepo(), max_procs=2)

    await sup.scan_once()
    assert len(spawned) == 2

    spawned[0]["proc"].exit()
    await _wait_live_procs(sup, 1)  # 只等退出的那个回收,另一个仍在跑
    await sup.scan_once()
    assert {_cmd_account(s["cmd"]) for s in spawned} == {1, 2, 3}

    await _finish_all(sup, spawned)


async def test_same_account_at_most_one_proc(db_factory, monkeypatch):
    """同账号严格串行:已有存活子进程的账号,后续扫描不再派第二个进程。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_pending(db_factory, 1, 5)
    sup = Supervisor(db_factory, repo=_FakeRepo())

    await sup.scan_once()
    await sup.scan_once()  # 子进程未退出,第二轮必须跳过该账号
    assert len(spawned) == 1

    await _finish_all(sup, spawned)


async def test_scan_without_repo_still_dispatches_publish(db_factory, monkeypatch):
    """browser_jobs repo 缺席(P1 未集成/容缺导入为 None):publish 派发照常工作。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_pending(db_factory, 1, 1)
    sup = Supervisor(db_factory)
    sup._repo = None  # 显式模拟容缺态(默认即回落到模块级导入结果)

    await sup.scan_once()
    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]

    await _finish_all(sup, spawned)


# ---------------- op_images 进程内执行 ----------------


async def test_op_images_runs_in_process(db_factory, monkeypatch):
    """op_images 不派子进程:supervisor 内乐观认领 → execute → 写回 done 终态。"""
    spawned = _install_spawn_recorder(monkeypatch)
    payload = {"prompts": ["p1"], "count": 1}
    row = {
        "id": "opimg_s1_1",
        "kind": "op_images",
        "account_id": None,
        "operator_id": 7,
        "payload": payload,
        "status": "queued",
        "created_at": datetime.utcnow(),
    }
    repo = _FakeRepo(rows=[row])
    calls: list[dict] = []

    async def _fake_execute(p):
        calls.append(p)
        return {"urls": ["https://cdn/x.png"], "errors": []}

    # 本分支 op_images 服务尚无 execute(P1 提炼),raising=False 直接注入契约函数
    monkeypatch.setattr(op_images_service, "execute", _fake_execute, raising=False)
    sup = Supervisor(db_factory, repo=repo)

    await sup.scan_once()
    await _drain(sup)

    assert spawned == [], "op_images 是 API 调用型任务,不得派账号子进程"
    assert calls == [payload]
    assert repo.claims and repo.claims[0][1] == "opimg_s1_1"
    assert repo.claims[0][2].startswith("supervisor-")
    assert repo.finishes == [
        ("opimg_s1_1", "done", {"urls": ["https://cdn/x.png"], "errors": []})
    ]
    assert sup._op_inflight == set()


async def test_op_images_error_and_unclaimed(db_factory, monkeypatch):
    """execute 抛异常 → 写回 error 终态;认领失败(已被领走)→ 不执行不写回。"""
    _install_spawn_recorder(monkeypatch)
    row = {"id": "opimg_s1_2", "kind": "op_images", "account_id": None,
           "payload": {}, "status": "queued", "created_at": datetime.utcnow()}

    async def _boom(p):
        raise RuntimeError("生图接口挂了")

    monkeypatch.setattr(op_images_service, "execute", _boom, raising=False)
    repo = _FakeRepo(rows=[row])
    sup = Supervisor(db_factory, repo=repo)
    await sup.scan_once()
    await _drain(sup)
    assert repo.finishes == [("opimg_s1_2", "error", {"error": "生图接口挂了"})]

    # 认领失败:claim 返回 None → 静默退,不 finish
    repo2 = _FakeRepo(rows=[row], claimable=False)
    sup2 = Supervisor(db_factory, repo=repo2)
    await sup2.scan_once()
    await _drain(sup2)
    assert repo2.finishes == []


# ---------------- 停机语义 ----------------


async def test_request_stop_halts_dispatch(db_factory, monkeypatch):
    """request_stop(SIGTERM/SIGINT 处理器所调)后:扫描循环及时退出,不再派发。"""
    spawned = _install_spawn_recorder(monkeypatch)
    # 关掉周期后台组件,聚焦调度循环本身(开关语义有独立的 lifespan 测试)
    from app.core.config import settings

    monkeypatch.setattr(settings, "BROWSER_REAP_INTERVAL", 0)
    monkeypatch.setattr(settings, "PLACEHOLDER_REAP_INTERVAL", 0)
    monkeypatch.setattr(settings, "COOKIE_CHECK_INTERVAL", 0)

    repo = _FakeRepo()
    sup = Supervisor(db_factory, repo=repo, scan_interval=0.02)
    run_task = asyncio.create_task(sup.run())

    # 至少扫过 2 轮(证明循环在跑)
    for _ in range(200):
        if repo.recover_calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert repo.recover_calls >= 2

    sup.request_stop()
    await asyncio.wait_for(run_task, timeout=2)  # 无子进程时应立即退出

    frozen = repo.recover_calls
    await _seed_pending(db_factory, 1, 1)
    await asyncio.sleep(0.1)
    assert repo.recover_calls == frozen, "停机后不得再扫描"
    assert spawned == [], "停机后不得再派发"
