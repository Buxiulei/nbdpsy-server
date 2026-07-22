"""remake 全链集成：12s 合成假源 → analyze(mock VL)→rewrite(mock LLM)→storyboard
(mock LLM)→render→dub(mock TTS)→compose→deliver，产物齐全、成片时长正确。

真 ffmpeg 渲染/拼接/混音 + 真 Playwright 卡片截图；VL/LLM/TTS 全部打桩（不花 API 钱、
离线可跑）。标 slow（宿主 CI 跑 not slow，本慢测本地跑）。

平移自 test_remake_integration.py。换 import 面：
- celery job_store 同步族 → app.video.scheduler async 族（await + conftest AsyncSession db）。
- STAGE_HANDLERS/_slim 从 app.video（stages/scheduler）取。
- 产物根 settings.UPLOAD_DIR → settings.DATA_DIR（宿主 paths 以 DATA_DIR/uploads 为根）。
- rewriter LLM 打桩 get_llm().chat().content → 薄 provider llm_chat（返回 JSON 字符串）。
- dubber TTS 打桩 get_tts().synthesize(.duration_seconds) → 薄 provider tts_synthesize（返回 float）。
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.video import paths as vt_paths
from app.video import scheduler, stages
from app.video.pipeline import muxer
from app.video.pipeline.remake import analyzer, composer, style, timeline
from app.video.pipeline.remake import storyboard as sb_mod
from app.video.pipeline.remake.renderers import programmatic
from app.video.pipeline.remake.rewriter import CLOSING_LINE

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def fake_source(tmp_path):
    """ffmpeg 合成 12s 假源：0-4s 亮色文字卡、4-12s 深底摆动白球（无音轨）。"""
    import subprocess
    src = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=white:s=640x360:r=10:d=4",
        "-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=8",
        "-f", "lavfi", "-i", "color=c=white:s=24x24:r=10:d=8",
        "-filter_complex",
        "[1:v][2:v]overlay=x='320+200*sin(2*PI*t/1.6)':y=60[ball];"
        "[0:v][ball]concat=n=2:v=1[v]",
        "-map", "[v]", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    return src


@pytest.mark.asyncio
async def test_full_remake_chain(fake_source, tmp_path, db, monkeypatch):
    # 产物根钳到 tmp（自清理）：_base() 与 to_public_url 都读 settings.DATA_DIR
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))

    job = await scheduler.create_job(db, url="https://youtu.be/fake", options={},
                                     user_id=1, mode="remake")
    job.duration_seconds = 12
    await db.commit()

    # download 阶段直接造 stats（不真下载）
    await scheduler.update_stage(db, job, "download", status="done",
                                 stats={"video_path": str(fake_source),
                                        "info": {"title": "假源"}})
    # transcript/resegment/translate 阶段落假台词：一句在引言卡段(0-4s)、一句落球段(4-12s)。
    segs = [{"start": 0.5, "end": 3.5, "en": "hello", "zh": "这是翻译"},
            {"start": 5.0, "end": 7.0, "en": "notice how your body feels now",
             "zh": "留意此刻身体的感受"}]
    seg_file = vt_paths.raw_dir(job.id) / "translated.json"
    seg_file.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    for st in ("transcript", "resegment", "translate"):
        await scheduler.update_stage(db, job, st, status="done",
                                     stats={"segments_path": str(seg_file),
                                            "segment_count": len(segs)})

    # VL 打桩：前 4s 卡片、之后球段
    async def fake_classify(video, t, deadline):
        return ({"kind": "title_card", "text": "introduction"} if t < 4
                else {"kind": "ball_exercise", "text": ""})
    monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
    # 卡片本地化 LLM 打桩
    monkeypatch.setattr(sb_mod, "_chat_localize",
                        AsyncMock(return_value={"introduction": "引言"}))
    # 台词重写 LLM 打桩（薄 provider llm_chat 直返 JSON 字符串）：额外给 "append" 触发 A6 结语补句。
    monkeypatch.setattr(
        "app.video.pipeline.remake.rewriter.llm_chat",
        AsyncMock(return_value='{"0": "这是我们自己的表达", "append": "补一句收束语"}'))

    # TTS 打桩（薄 provider tts_synthesize 返回 float 时长）：写 0.5s 静音 wav
    async def fake_tts_synth(text, *, voice, rate, out_path):
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=24000:cl=mono:d=0.5", out_path],
                       check=True, capture_output=True)
        return 0.5
    monkeypatch.setattr("app.video.pipeline.dubber.tts_synthesize", fake_tts_synth)

    # 强制 libx264（关 NVENC）：本集成测试验证 remake 链路逻辑，不测硬件编码。生产 GPU 盒
    # 被 vLLM 占满时 h264_nvenc 会 InitializeEncoder OOM（环境资源争用，非管线缺陷），
    # 关掉即环境无关、确定性可跑；NVENC 编码路径由 muxer 单测另行覆盖。
    monkeypatch.setattr("app.video.pipeline.muxer.probe_nvenc",
                        AsyncMock(return_value=False))

    # A5 球心居中：spy 程序化渲染器的 ffmpeg 命令，断言 overlay y 落帧高中线（不抽帧）
    render_argv: list[list[str]] = []
    _real_run = programmatic._run_ffmpeg

    async def spy_render_run(argv, *, timeout):
        render_argv.append(argv)
        return await _real_run(argv, timeout=timeout)
    monkeypatch.setattr(programmatic, "_run_ffmpeg", spy_render_run)

    # A6 末尾淡出：spy compose 阶段的 muxer.mux，断言淡出参数已透传到最终 mux
    mux_kwargs: dict = {}
    _real_mux = muxer.mux

    async def spy_mux(*args, **kwargs):
        mux_kwargs.update(kwargs)
        return await _real_mux(*args, **kwargs)
    monkeypatch.setattr(muxer, "mux", spy_mux)

    # 逐阶段跑 remake 链（wave5 新链序：download 后 analyze→rewrite→dub→storyboard→
    # render→compose→deliver，dub 在 storyboard 之前先测自然时长）
    for stage in ["analyze", "rewrite", "dub", "storyboard", "render", "compose", "deliver"]:
        ctx = {"deadline": None}
        st = await stages.STAGE_HANDLERS[stage](job, db, ctx) or {}
        products = st.pop("products", None)
        await scheduler.update_stage(db, job, stage, status="done",
                                     stats=scheduler._slim(st))
        if products:
            await scheduler.finish_job(db, job, products)

    assert job.status == "completed"
    assert "storyboard_url" in job.products
    # 成片存在且时长 = 弹性重排后新总时长（不再钉死原片 12s）
    new_total = job.stages["storyboard"]["stats"]["new_duration_s"]
    final = vt_paths.out_dir(job.id) / "final.mp4"
    assert final.exists()
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True, text=True)
    assert float(probe.stdout.strip()) == pytest.approx(new_total, abs=1.0)
    # wave5 ②：成片 120fps（球拖影根治）
    fps_probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", str(final)],
        capture_output=True, text=True)
    assert fps_probe.stdout.strip() == "120/1"
    # T1a：成片音轨双声道（C1 回归——EMDR 左右提示音不被 amix 降混塌成 mono）
    ch_probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(final)],
        capture_output=True, text=True)
    assert int(ch_probe.stdout.strip()) == 2
    # I4：deliver 后中间产物已清理（scenes/ 与 remade_silent 半成品不残留）
    out_dir = vt_paths.out_dir(job.id)
    assert not (out_dir / "scenes").exists()
    assert not (out_dir / "remade_silent.mp4").exists()
    # meta 里带 attribution
    meta = json.loads((vt_paths.out_dir(job.id) / "meta.json").read_text(encoding="utf-8"))
    assert meta["attribution"].startswith("练习设计参考")

    # ── Phase A（A1-A7）复合断言：与全链照旧全绿并存，验证新行为互不冲突 ──────────
    sb = json.loads(
        (vt_paths.raw_dir(job.id) / "storyboard.json").read_text(encoding="utf-8"))
    retimed = json.loads(
        (vt_paths.raw_dir(job.id) / "rewritten.json").read_text(encoding="utf-8"))
    ball_scenes = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
    static_ball = [s for s in ball_scenes if s["params"].get("static")]
    motion_ball = [s for s in ball_scenes if not s["params"].get("static")]

    # A1+A2 互斥编排：落在球段的那句台词，其语音窗对应一个静止子场景（球停），且与任何
    # 运动子场景零相交。retimed[1] 即球段句（card=0/ball=1/收束=2）。
    ball_line = retimed[1]
    assert ball_line["orig_start"] == 5.0            # 确认取到的是球段那句
    win0 = ball_line["start"]
    win1 = ball_line["end"] + timeline._SPEECH_WINDOW_TAIL
    eps = 1e-6
    assert any(s["t0"] <= win0 + eps and s["t1"] >= win1 - eps for s in static_ball), \
        "球段台词语音窗未切出对应静止子场景（说话时球未停）"
    assert all(not (s["t0"] < win1 - eps and s["t1"] > win0 + eps) for s in motion_ball), \
        "运动子场景与球段台词语音窗相交（互斥编排未生效）"

    # A4 球色来自 BALL_PALETTE：全部球场景取品牌调色板色，运动相位含首相位色（相位序循环）
    assert all(s["params"]["ball_color"] in style.BALL_PALETTE for s in ball_scenes)
    assert motion_ball, "球段应有运动子场景"
    assert style.BALL_PALETTE[0] in {s["params"]["ball_color"] for s in motion_ball}

    # A5 球心居中：渲染 overlay y = 帧高中线 - 球贴图画布半宽（BALL_Y_RATIO=0.50，非硬编码）
    radius = max(2, round(style.VIDEO_H * style.BALL_RADIUS_RATIO))
    expected_y = round(style.VIDEO_H * style.BALL_Y_RATIO) \
        - programmatic.ball_canvas_px(radius) // 2
    ball_cmds = [" ".join(a) for a in render_argv if any("overlay=" in x for x in a)]
    assert ball_cmds, "未捕获到球渲染 ffmpeg 命令"
    assert all(f"y={expected_y}" in c for c in ball_cmds), "球心未垂直居中"

    # A6 结语收束句逐字进重写产物（rewritten.json 覆写后仍在）
    assert any(s["zh"] == CLOSING_LINE for s in retimed), "结语收束句未出现在重写产物"
    # A6 末尾淡出：compose 把 3s 淡出参数透传给最终 muxer.mux
    assert mux_kwargs.get("fade_out_seconds") == composer._FADE_OUT_SECONDS
    assert mux_kwargs.get("total_seconds") == pytest.approx(new_total, abs=1.0)
