"""即梦调度器单测：submit 阶段 / poll 阶段 / 启动自愈。

全离线：``app.services.dreamina._run_cli`` 一律 monkeypatch（**绝不真跑 dreamina**——真跑就是
提交真任务、占即梦队列位、success 即扣公司积分）。

覆盖的都是**与钱直接相关**的语义：
- 原子认领 + 「任何路径绝不二次 submit 同一 clip」（重提 = 双倍扣分且排队中无法取消）；
- 歧义结局（超时 / 无 submit_id）判 error 并写明「未知」，不自动重排；
- 查询瞬时失败只写 last_poll_error 不改 status（查询失败 ≠ 任务失败）；
- 启动自愈把崩在 submit 中途的 submitting 残留判 error。
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.video_clip import VideoClip
from app.services import dreamina
from app.services.dreamina import DreaminaScheduler


class _CliRecorder:
    """按子命令分派的假 CLI：记录每次调用参数，返回预置结果。"""

    def __init__(self, results: dict):
        # results: {"text2video"/"image2video"/"query_result"/...: (rc, stdout, stderr)}
        self._results = results
        self.calls: list[list[str]] = []

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        return self._results.get(args[0], (0, "", ""))

    def count(self, sub: str) -> int:
        return sum(1 for c in self.calls if c and c[0] == sub)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """产物目录隔离到 tmp（clip_dir / clips_root 请求时读 settings.DATA_DIR）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    dreamina.reset_credit_cache()


async def _seed(factory, **kw) -> int:
    async with factory() as s:
        clip = VideoClip(**{**dict(
            clip_id=dreamina.new_clip_id(), operation="text2video", prompt="温暖诊室空镜",
            model="seedance2.0fast", duration=5, ratio="9:16",
            status="queued", created_by=1), **kw})
        s.add(clip)
        await s.commit()
        return clip.id


async def _get(factory, row_id: int) -> VideoClip:
    async with factory() as s:
        return await s.get(VideoClip, row_id)


# ── submit 阶段 ─────────────────────────────────────────────────────────────
async def test_submit_success_records_submit_id(db_factory, monkeypatch):
    cli = _CliRecorder({"text2video": (0, json.dumps({"submit_id": "3d64c2221c0e07da"}), "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    assert await DreaminaScheduler(db_factory).submit_once() == 1

    clip = await _get(db_factory, row_id)
    assert clip.status == "submitted"
    assert clip.submit_id == "3d64c2221c0e07da"
    assert clip.submitted_at is not None and clip.error is None
    assert "--poll=0" in cli.calls[0] and "--video_resolution=720p" in cli.calls[0]


async def test_submit_never_touches_non_queued_rows(db_factory, monkeypatch):
    """原子认领：非 queued 的行一律不碰（submitted 的重提就是双倍扣分）。"""
    cli = _CliRecorder({"text2video": (0, '{"submit_id": "aaaabbbbccccdddd"}', "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    await _seed(db_factory, status="submitted", submit_id="aaaabbbbccccdddd")
    await _seed(db_factory, status="querying", submit_id="1111222233334444")
    await _seed(db_factory, status="error", error="x")

    assert await DreaminaScheduler(db_factory).submit_once() == 0
    assert cli.calls == []


async def test_submit_timeout_is_ambiguous_and_never_resubmits(db_factory, monkeypatch):
    """超时 = 任务是否已入即梦队列**未知** → 判 error 并写明，绝不自动重提（跑两轮 CLI 只被调一次）。"""
    cli = _CliRecorder({"text2video": (124, "", "dreamina text2video 超时(120s)")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)
    scheduler = DreaminaScheduler(db_factory)

    await scheduler.submit_once()
    await scheduler.submit_once()          # 第二轮：绝不能再提交同一 clip

    assert cli.count("text2video") == 1
    clip = await _get(db_factory, row_id)
    assert clip.status == "error"
    assert "未知" in clip.error and "不自动重提" in clip.error
    assert clip.submit_id is None


async def test_submit_without_submit_id_is_ambiguous(db_factory, monkeypatch):
    """rc=0 却拿不到 submit_id 同样是歧义结局（CLI 可能已提交但回执异常）。"""
    cli = _CliRecorder({"text2video": (0, '{"ok": true}', "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error" and "未知" in clip.error


async def test_submit_error_with_hex_in_stderr_is_not_faked_submitted(db_factory, monkeypatch):
    """CLI 明确失败(rc=1)但错误原文含 16/32 位 hex（logid / request_id）：**绝不当 submit_id**。

    误判的后果是把「确定失败」伪装成「正常排队」：假 submit_id 查不到 gen_status，poll 只写
    last_poll_error、永不落终态，运营对着一直涨的 queued_seconds 以为在排队。
    """
    blob = ("Error: upstream rejected the request, "
            "request_id=9f8e7d6c5b4a39281726354453627180 logid=0123456789abcdef")
    cli = _CliRecorder({"text2video": (1, "", blob)})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error", "rc!=0 的明确失败不能被原文里的 hex 顶成 submitted"
    assert clip.submit_id is None
    assert "request_id=9f8e7d6c5b4a39281726354453627180" in clip.error   # 原文透出
    assert clip.finished_at is not None


async def test_submit_compliance_error_with_hex_still_lands_error(db_factory, monkeypatch):
    """合规授权错误里带 hex 也一样：确定没入队，原文 + 人工提示，不落 submitted。"""
    blob = f"{dreamina.COMPLIANCE_MARKER}: model not confirmed (trace 0123456789abcdef)"
    cli = _CliRecorder({"text2video": (1, "", blob)})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error" and clip.submit_id is None
    assert dreamina.COMPLIANCE_MARKER in clip.error and "网页端" in clip.error


async def test_submit_keeps_structured_submit_id_even_when_rc_nonzero(db_factory, monkeypatch):
    """rc!=0 但**结构化字段**给了 submit_id：仍记下来。

    结构化 submit_id 是「CLI 真把任务交出去了」的硬证据，丢掉它就是扣了分却没人去取片。
    被禁的只有「从错误原文里正则捞 hex」这条兜底。"""
    cli = _CliRecorder({"text2video": (
        1, '{"submit_id": "3d64c2221c0e07da"}', "warning: download step failed")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "submitted" and clip.submit_id == "3d64c2221c0e07da"


async def test_submit_lock_busy_requeues_instead_of_error(db_factory, monkeypatch):
    """等跨进程 CLI 锁超时 = CLI 根本没被调起 → 任务确定没入队，复位 queued 下轮再提。

    这是**唯一**可以安全复位的失败路径（其余路径 CLI 已被调起，复位就是赌双扣）。"""
    cli = _CliRecorder({"text2video": (125, "", "另一进程正在跑 dreamina，等锁超时(10.0s)")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    assert await DreaminaScheduler(db_factory).submit_once() == 0

    clip = await _get(db_factory, row_id)
    assert clip.status == "queued" and clip.error is None and clip.submit_id is None


async def test_poll_lock_busy_is_not_recorded_as_query_failure(db_factory, monkeypatch):
    """等锁超时不是「查询接口故障」：不写 last_poll_error，也不占节流位（下一轮立刻重来）。"""
    cli = _CliRecorder({"query_result": (125, "", "等锁超时")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="querying", submit_id="1111222233334444")
    scheduler = DreaminaScheduler(db_factory)

    await scheduler.poll_once()
    await scheduler.poll_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "querying" and clip.last_poll_error is None
    assert cli.count("query_result") == 2, "没被节流表挡住，下一轮该照常重查"


async def test_submit_cli_error_passes_through_compliance_原文(db_factory, monkeypatch):
    """合规授权错误**原文透传** + 附人工提示（服务端重试无意义，需人去网页端点一次）。"""
    blob = "Error: AigcComplianceConfirmationRequired for model seedance2.0fast"
    cli = _CliRecorder({"text2video": (1, "", blob)})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error"
    assert dreamina.COMPLIANCE_MARKER in clip.error
    assert "网页端" in clip.error


async def test_submit_image2video_passes_image_and_no_ratio(db_factory, monkeypatch):
    cli = _CliRecorder({"image2video": (0, '{"submit_id": "0f0f0f0f0f0f0f0f"}', "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    await _seed(db_factory, operation="image2video", ratio=None, image_path="/tmp/ref.png")

    await DreaminaScheduler(db_factory).submit_once()

    args = cli.calls[0]
    assert args[0] == "image2video" and "--image=/tmp/ref.png" in args
    assert not any(a.startswith("--ratio") for a in args)


# ── poll 阶段 ──────────────────────────────────────────────────────────────
async def test_poll_querying_keeps_queue_semantics(db_factory, monkeypatch):
    cli = _CliRecorder({"query_result": (0, '{"gen_status": "querying"}', "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="submitted", submit_id="1111222233334444",
                         submitted_at=datetime.utcnow(), last_poll_error="上轮抖动")

    assert await DreaminaScheduler(db_factory).poll_once() == 1

    clip = await _get(db_factory, row_id)
    assert clip.status == "querying"
    assert clip.last_poll_error is None      # 查通了就清掉上一轮的瞬时错误


async def test_poll_success_lands_product_and_credit(db_factory, monkeypatch, tmp_path):
    """success：视频改名 clip.mp4 落位 + video_url + credit_count + expires_at + done。"""
    row_id = await _seed(db_factory, status="querying", submit_id="1111222233334444",
                         submitted_at=datetime.utcnow())
    clip = await _get(db_factory, row_id)
    workdir = dreamina.clip_dir(clip.clip_id)
    downloaded = workdir / "1111222233334444_video_0.mp4"

    async def _fake(args, timeout):
        downloaded.write_bytes(b"fake-mp4")      # 仿 CLI 把片下到 download_dir
        return (0, json.dumps({
            "gen_status": "success", "credit_count": 25,
            "result_json": {"videos": [{"path": str(downloaded)}]},
        }), "")

    monkeypatch.setattr(dreamina, "_run_cli", _fake)

    await DreaminaScheduler(db_factory).poll_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "done" and clip.credit_count == 25
    assert (workdir / "clip.mp4").is_file() and not downloaded.exists()
    assert clip.video_url == f"/uploads/clips/{dreamina.clip_token_dir(clip.clip_id)}/clip.mp4"
    assert clip.finished_at is not None and clip.expires_at is not None
    assert (clip.expires_at - clip.finished_at).days == settings.CLIP_TTL_DAYS
    assert clip.error is None


async def test_poll_failed_records_fail_reason(db_factory, monkeypatch):
    cli = _CliRecorder({"query_result": (
        0, json.dumps({"gen_status": "failed", "fail_reason": "内容审核未通过"}), "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="querying", submit_id="1111222233334444")

    await DreaminaScheduler(db_factory).poll_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error" and clip.error == "内容审核未通过"
    assert clip.finished_at is not None


async def test_poll_transient_failure_does_not_change_status(db_factory, monkeypatch):
    """查询接口抖动 ≠ 任务失败：只写 last_poll_error，status/submit_id 一动不动。

    把它写成终态会让运营以为任务死了而重发——排队中的任务无法取消，那就是双倍扣分。"""
    cli = _CliRecorder({"query_result": (1, "", "connection reset")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="querying", submit_id="1111222233334444")

    await DreaminaScheduler(db_factory).poll_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "querying" and clip.submit_id == "1111222233334444"
    assert "connection reset" in clip.last_poll_error
    assert clip.error is None


async def test_poll_success_without_path_is_transient_not_fake_done(db_factory, monkeypatch):
    """success 但没拿到视频路径：当查询侧异常处理，绝不落「done 却没有 video_url」的假终态。"""
    cli = _CliRecorder({"query_result": (
        0, json.dumps({"gen_status": "success", "result_json": {"videos": []}}), "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="querying", submit_id="1111222233334444")

    await DreaminaScheduler(db_factory).poll_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "querying" and clip.video_url is None
    assert "未取到视频路径" in clip.last_poll_error


async def test_poll_respects_query_interval(db_factory, monkeypatch):
    """同一条在飞任务两次查询之间要隔 CLIP_QUERY_INTERVAL（免给单账号 CLI 加锁竞争）。"""
    cli = _CliRecorder({"query_result": (0, '{"gen_status": "querying"}', "")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    await _seed(db_factory, status="submitted", submit_id="1111222233334444")
    scheduler = DreaminaScheduler(db_factory, query_interval=3600)

    assert await scheduler.poll_once() == 1
    assert await scheduler.poll_once() == 0        # 间隔未到，不重复查
    assert cli.count("query_result") == 1


# ── 优雅停机 ────────────────────────────────────────────────────────────────
async def test_submit_loop_stops_between_shots_on_stop_signal(db_factory, monkeypatch):
    """停机信号在**逐条循环内**生效：积压 3 条时第一条跑完就收摊。

    不查的话停机耗时 = Σ(每条 CLI 超时)，超过 systemd TimeoutStopSec 就被 SIGKILL，
    恰在 submit 中途的行留下 submitting 残留 → 下次启动自愈判 error，人为制造烧钱裁决。"""
    scheduler = DreaminaScheduler(db_factory)
    scheduler._stop_event = asyncio.Event()

    async def _fake(args, timeout):
        scheduler._stop_event.set()        # 仿「第一条 CLI 还没跑完就收到 SIGTERM」
        return (0, '{"submit_id": "3d64c2221c0e07da"}', "")

    monkeypatch.setattr(dreamina, "_run_cli", _fake)
    for _ in range(3):
        await _seed(db_factory)

    assert await scheduler.submit_once() == 1        # 只处理了第一条

    async with db_factory() as s:
        left = (await s.execute(
            select(VideoClip).where(VideoClip.status == "queued"))).scalars().all()
    assert len(left) == 2, "剩下的该原样留在 queued，下次启动继续（绝不是 error）"


async def test_poll_loop_stops_between_shots_on_stop_signal(db_factory, monkeypatch):
    scheduler = DreaminaScheduler(db_factory)
    scheduler._stop_event = asyncio.Event()

    async def _fake(args, timeout):
        scheduler._stop_event.set()
        return (0, '{"gen_status": "querying"}', "")

    monkeypatch.setattr(dreamina, "_run_cli", _fake)
    for _ in range(3):
        await _seed(db_factory, status="submitted", submit_id="1111222233334444")

    assert await scheduler.poll_once() == 1


async def test_submit_second_stage_is_guarded_by_claim_state(db_factory, monkeypatch):
    """第二段落库带 ``WHERE status='submitting'`` 守卫：认领态被别处改写就**不盲写**。

    CLI 一跑就是几十秒到 120s，这期间启动自愈 / sweep / 人工改库都可能已按自己的证据把这行
    判成别的状态。盲写会把一条已判 error 的行顶回 submitted，抹掉那份「未知是否入队」的
    结论——运营再也看不到需要人裁决的信号。
    """
    row_id = await _seed(db_factory)

    async def _fake(args, timeout):
        # 仿「CLI 还在跑，另一路径已经把这行判了 error」
        async with db_factory() as s:
            clip = await s.get(VideoClip, row_id)
            clip.status = "error"
            clip.error = "别处已按自己的证据判定"
            await s.commit()
        return (0, '{"submit_id": "3d64c2221c0e07da"}', "")

    monkeypatch.setattr(dreamina, "_run_cli", _fake)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error" and clip.error == "别处已按自己的证据判定"
    assert clip.submit_id is None, "认领态已变，本次结论已过期，不该盲写回去"


async def test_submit_claim_stamps_time_so_dead_row_is_not_revivable(db_factory, monkeypatch):
    """认领即写 submitted_at —— 它是「这行被 CLI 碰过」的结构化标记。

    歧义结局的 error 行必须**不可复活**：幂等键重放走的复活分支靠 submitted_at 为空判断
    「CLI 从没跑过」，认领时不打这个戳的话，一条可能已在即梦队列里的任务会被同 ref 重放
    复活成 queued 再提交一次 = 双倍扣分。
    """
    cli = _CliRecorder({"text2video": (124, "", "dreamina text2video 超时(120s)")})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory)

    await DreaminaScheduler(db_factory).submit_once()

    clip = await _get(db_factory, row_id)
    assert clip.status == "error" and clip.submitted_at is not None
    assert not dreamina.is_revivable(clip), "跑过 CLI 的歧义结局行绝不能被重放复活"


# ── submitting 滞留巡检 ─────────────────────────────────────────────────────
async def test_sweep_marks_long_stuck_submitting_as_error(db_factory, monkeypatch):
    """认领后久久不落终态的 submitting 行，运行期就得处置，不能等到下次重启。

    对外 submitting 映射成 queued，而 submit 阶段只捡 status='queued'——不扫的话那一行
    永远卡着：运营看着「在排队」，其实没有任何东西在推进它。
    """
    monkeypatch.setattr(dreamina, "_run_cli", _CliRecorder({}))
    stale_after = settings.CLIP_SUBMIT_TIMEOUT * 3
    fresh_id = await _seed(db_factory, status="submitting",
                           submitted_at=datetime.utcnow())
    stale_id = await _seed(db_factory, status="submitting",
                           submitted_at=datetime.utcnow() - timedelta(seconds=stale_after + 60))

    assert await DreaminaScheduler(db_factory).sweep_stale_submitting() == 1

    stale = await _get(db_factory, stale_id)
    assert stale.status == "error" and "未知" in stale.error
    assert stale.finished_at is not None
    # 正在提交中的行绝不误伤（阈值是单次 CLI 超时的 3 倍，留足等锁 + 落库余量）
    assert (await _get(db_factory, fresh_id)).status == "submitting"


async def test_sweep_never_requeues(db_factory, monkeypatch):
    """滞留处置是 error 而**不是复位 queued**——理由与启动自愈逐字相同：CLI 已被调起，
    任务是否已入即梦队列未知，复位就是赌双扣。"""
    monkeypatch.setattr(dreamina, "_run_cli", _CliRecorder({}))
    stale_id = await _seed(
        db_factory, status="submitting",
        submitted_at=datetime.utcnow() - timedelta(seconds=settings.CLIP_SUBMIT_TIMEOUT * 9))

    await DreaminaScheduler(db_factory).sweep_stale_submitting()

    clip = await _get(db_factory, stale_id)
    assert clip.status == "error"
    assert not dreamina.is_revivable(clip), "认领过的行不该被重放复活"


# ── 启动自愈 ────────────────────────────────────────────────────────────────
async def test_heal_submitting_marks_error_not_requeue(db_factory, monkeypatch):
    """崩在 submit 中途的 submitting 残留：判 error 写明「未知」，**绝不复位回 queued**。

    复位 = 赌它没进即梦队列，赌输就是双倍扣分且排队中无法取消。"""
    cli = _CliRecorder({})
    monkeypatch.setattr(dreamina, "_run_cli", cli)
    row_id = await _seed(db_factory, status="submitting")
    ok_id = await _seed(db_factory, status="queued")

    assert await DreaminaScheduler(db_factory).heal_submitting() == 1

    stale = await _get(db_factory, row_id)
    assert stale.status == "error" and "未知" in stale.error
    assert cli.calls == []                          # 自愈期间一次 CLI 都不该跑
    assert (await _get(db_factory, ok_id)).status == "queued"   # 正常 queued 不受影响
