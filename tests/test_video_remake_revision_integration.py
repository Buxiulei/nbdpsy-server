"""revision 全链集成（spec §B5 / B4）：mini 假源跑完整 remake 父 job → revise 改一句台词 →
跑 revision 子链，断言增量成本控制与产物齐全。标 slow（本地跑）。

断言核心：
  1. 仅被改那句 TTS 重合成（synth 只发生一次，文本是新文本）；
  2. 未改句跨 job 命中继承缓存（同名 clip 存在且与父同 inode）；
  3. tts 聚合文件（remake_mixed.wav 等）不被继承（B4 收窄）；
  4. 成片重出、meta.revision 溯源块齐全（parent/instructions/edit_plan）；
  5. 前五阶段 stats 含 inherited_from + 下游消费路径 stats，first_incomplete_stage=rewrite。

平移自 test_remake_revision_integration.py。换 import 面同 test_video_remake_integration；
另：源依赖 REST 端点的 ``_enrich_inherited_stats`` / ``_INHERITED_STAGES``（M4 API 层，尚未迁移），
此处内联同语义 helper 使本集成测试自足——M4 落地 video_rest 后应改回从端点导入。
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.video import paths as vt_paths
from app.video import scheduler
from app.video import stages as vt_stages
from app.video.pipeline.remake import analyzer
from app.video.pipeline.remake import storyboard as sb_mod
from app.video.pipeline.remake.inherit import inherit_artifacts

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# M4 API 层将定义这两者（继承阶段集合 + 补路径 stats）；此处内联使集成测试自足。
_INHERITED_STAGES = ["download", "analyze", "transcript", "resegment", "translate"]
_CHILD_CHAIN = ["rewrite", "dub", "storyboard", "render", "compose", "deliver"]


async def _enrich_inherited_stats(db, child, parent):
    """给继承阶段补下游消费的真实路径 stats（*_path 重指子 raw 已拷入的同名文件，标量原样保留）。

    内联自源 app/api/endpoints/video_transport._enrich_inherited_stats（M4 迁移后应删本地副本、
    改回端点导入）。修复 mark_stages_inherited 只写 inherited_from 的断链：_handle_storyboard 读
    analyze.facts_path、deliver 读 download.info。"""
    child_raw = vt_paths.raw_dir(child.id)
    for name in _INHERITED_STAGES:
        parent_stats = ((parent.stages or {}).get(name) or {}).get("stats") or {}
        stats = {"inherited_from": parent.id}
        for k, v in parent_stats.items():
            if k == "inherited_from":
                continue
            if k.endswith("_path") and isinstance(v, str):
                cand = child_raw / Path(v).name
                if cand.exists():                # 子目录有拷贝才重指
                    stats[k] = str(cand)
            else:
                stats[k] = v                     # info / 计数 / source 等标量保留
        await scheduler.update_stage(db, child, name, status="done", stats=stats)


@pytest.fixture
def fake_source(tmp_path):
    """ffmpeg 合成 12s 假源：0-4s 文字卡、4-12s 深底摆动白球（无音轨）。"""
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


def _install_mocks(monkeypatch, synth_calls):
    """装 VL/卡片本地化/重写 LLM/TTS 打桩（父子两链共用同一套，薄 provider 面）。"""
    async def fake_classify(video, t, deadline):
        return ({"kind": "title_card", "text": "introduction"} if t < 4
                else {"kind": "ball_exercise", "text": ""})
    monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
    monkeypatch.setattr(sb_mod, "_chat_localize",
                        AsyncMock(return_value={"introduction": "引言"}))
    monkeypatch.setattr(
        "app.video.pipeline.remake.rewriter.llm_chat",
        AsyncMock(return_value='{"0": "这是我们自己的表达", "append": "补一句收束语"}'))

    async def fake_tts_synth(text, *, voice, rate, out_path):
        synth_calls.append(text)                       # 记录真正合成的句子（缓存命中不进这里）
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=24000:cl=mono:d=0.5", out_path],
                       check=True, capture_output=True)
        return 0.5
    monkeypatch.setattr("app.video.pipeline.dubber.tts_synthesize", fake_tts_synth)
    # 强制 libx264（关 NVENC）：验证增量修订链路逻辑，不测硬件编码；生产 GPU 被 vLLM 占满时
    # h264_nvenc 会 OOM（环境资源争用，非管线缺陷）。NVENC 路径由 muxer 单测另行覆盖。
    monkeypatch.setattr("app.video.pipeline.muxer.probe_nvenc",
                        AsyncMock(return_value=False))


async def _run_chain(db, job, stage_names):
    """逐阶段跑指定阶段序，末阶段产物落 finish_job（复刻自链尾部）。"""
    for stage in stage_names:
        st = await vt_stages.STAGE_HANDLERS[stage](job, db, {"deadline": None}) or {}
        products = st.pop("products", None)
        await scheduler.update_stage(db, job, stage, status="done",
                                     stats=scheduler._slim(st))
        if products:
            await scheduler.finish_job(db, job, products)


async def _seed_parent_pre_stages(db, fake_source):
    """造父 remake job 并 seed download/transcript/resegment/translate 的假产物 stats。"""
    import shutil
    job = await scheduler.create_job(db, url="https://youtu.be/fake", options={},
                                     user_id=1, mode="remake")
    job.duration_seconds = 12
    await db.commit()
    # 复刻生产 download：视频落 raw/video.mp4（inherit 硬链接的对象；stats.video_path 指它）
    video = vt_paths.raw_dir(job.id) / "video.mp4"
    shutil.copy(str(fake_source), str(video))
    await scheduler.update_stage(db, job, "download", status="done",
                                 stats={"video_path": str(video),
                                        "info": {"title": "假源"}})
    segs = [{"start": 0.5, "end": 3.5, "en": "hello", "zh": "这是翻译"},
            {"start": 5.0, "end": 7.0, "en": "notice how your body feels now",
             "zh": "留意此刻身体的感受"}]
    seg_file = vt_paths.raw_dir(job.id) / "translated.json"
    seg_file.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    for st in ("transcript", "resegment", "translate"):
        await scheduler.update_stage(db, job, st, status="done",
                                     stats={"segments_path": str(seg_file),
                                            "segment_count": len(segs)})
    return job


@pytest.mark.asyncio
async def test_revision_incremental_chain(fake_source, tmp_path, db, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    synth_calls: list[str] = []
    _install_mocks(monkeypatch, synth_calls)

    # ── 1. 跑父 remake 全链到完成 ────────────────────────────────────
    parent = await _seed_parent_pre_stages(db, fake_source)
    await _run_chain(db, parent, ["analyze", "rewrite", "dub", "storyboard",
                                  "render", "compose", "deliver"])
    assert parent.status == "completed"
    parent_rew = json.loads(
        (vt_paths.raw_dir(parent.id) / "rewritten.json").read_text(encoding="utf-8"))
    assert parent_rew[1]["zh"] == "留意此刻身体的感受"
    assert len(synth_calls) == 3                         # 父链三句都合成
    parent_tts = vt_paths.tts_dir(parent.id)
    p0 = next(p for p in parent_tts.iterdir() if p.name.startswith("00000_"))
    p2 = next(p for p in parent_tts.iterdir() if p.name.startswith("00002_"))
    assert (parent_tts / "remake_mixed.wav").exists()    # 父 compose 建了聚合文件

    # ── 2. 派生 revision 子 job（复刻端点接线）：改第 1 句（球段句）─────
    edit_plan = [{"type": "script_edit", "index": 1,
                  "new_text": "留意此刻身体每一处的细微感受"}]
    child = await scheduler.create_revision_job(
        db, parent, instructions="第二句再细腻一些", edit_plan=edit_plan)
    inherit_artifacts(parent.id, child.id)
    await scheduler.mark_stages_inherited(db, child, _INHERITED_STAGES, parent_id=parent.id)
    await _enrich_inherited_stats(db, child, parent)

    child_tts = vt_paths.tts_dir(child.id)
    # B4 收窄：聚合文件不被继承（只硬链接 hash 逐句缓存）
    assert not (child_tts / "remake_mixed.wav").exists()
    assert not (child_tts / "dub.wav").exists()
    # 前五阶段继承：inherited_from + 下游消费路径 stats（analyze.facts_path 重指子 raw）
    assert scheduler.first_incomplete_stage(child) == "rewrite"
    for st in _INHERITED_STAGES:
        assert child.stages[st]["stats"]["inherited_from"] == parent.id
    assert child.stages["analyze"]["stats"]["facts_path"] == \
        str(vt_paths.raw_dir(child.id) / "scene_facts.json")
    assert child.stages["download"]["stats"]["info"]["title"] == "假源"

    # ── 3. 跑 revision 子链，只统计子链的 TTS 合成 ──────────────────
    synth_calls.clear()
    await _run_chain(db, child, _CHILD_CHAIN)
    assert child.status == "completed"

    # 断言 A：仅被改那句重合成（synth 只发生一次，文本是新文本）
    assert synth_calls == ["留意此刻身体每一处的细微感受"], \
        f"应只重合成被改的一句，实际合成 {synth_calls}"
    child_rew = json.loads(
        (vt_paths.raw_dir(child.id) / "rewritten.json").read_text(encoding="utf-8"))
    assert child_rew[1]["zh"] == "留意此刻身体每一处的细微感受"
    assert child_rew[0]["zh"] == parent_rew[0]["zh"]
    assert child_rew[1]["orig_start"] == parent_rew[1]["orig_start"]   # 锚点不变

    # 断言 B：未改句跨 job 命中继承缓存（同名 clip 存在且与父同 inode）
    for pc in (p0, p2):
        assert (child_tts / pc.name).exists()
        assert os.stat(child_tts / pc.name).st_ino == os.stat(pc).st_ino, \
            f"{pc.name} 未命中继承缓存（inode 不同）"

    # 断言 C：成片重出
    final = vt_paths.out_dir(child.id) / "final.mp4"
    assert final.exists()
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True, text=True)
    assert float(probe.stdout.strip()) > 0

    # 断言 D：meta.revision 溯源块齐全
    meta = json.loads(
        (vt_paths.out_dir(child.id) / "meta.json").read_text(encoding="utf-8"))
    assert meta["revision"]["parent_job_id"] == parent.id
    assert meta["revision"]["instructions"] == "第二句再细腻一些"
    assert meta["revision"]["edit_plan"] == edit_plan
    assert meta["video"]["title"] == "假源"               # 继承的 download.info 进 meta
