"""transport 七阶 handler 层单测：注册覆盖 + 各 handler 接线（重建输入/落盘 stats/落库字段）。

平移自 test_stage_tasks.py 的 transport handler 部分（调度器机制已由 test_video_scheduler.py 覆盖，
本文件只测 stages.py 的真 handler 与 pipeline 模块的接线）。db 用 conftest 的 AsyncSession fixture；
pipeline 模块以 monkeypatch 打桩（不打真网/真模型/真 ffmpeg）。
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock

from app.models import VideoJob
from app.video import stages


def _set_stage(job, stage, stats):
    """在内存里给 job 塞一条上游阶段 stats（handler 经 _stage_stats 重建输入的唯一来源）。"""
    st = dict(job.stages or {})
    st[stage] = {"stats": stats}
    job.stages = st


async def _make_job(db, **kw):
    job = VideoJob(url=kw.pop("url", "https://youtu.be/x"), options=kw.pop("options", {}), **kw)
    db.add(job)
    await db.commit()
    return job


def test_transport_stages_registered():
    from app.video.scheduler import STAGE_HANDLERS, STAGE_ORDER
    assert set(STAGE_ORDER) <= set(STAGE_HANDLERS)          # transport 七阶全注册
    for stage in STAGE_ORDER:
        assert callable(STAGE_HANDLERS[stage])


async def test_handle_download_sets_meta(db, tmp_path, monkeypatch):
    job = await _make_job(db)

    async def fake_download(url, workdir, *, max_resolution=1080, deadline=None):
        return {"video_path": str(tmp_path / "v.mp4"), "subtitle_path": None,
                "subtitle_source": None,
                "info": {"id": "vid", "title": "T", "duration": 120}}
    monkeypatch.setattr("app.video.pipeline.downloader.download", fake_download)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    stats = await stages._handle_download(job, db, {"deadline": None})
    assert stats["info"]["duration"] == 120
    assert job.video_id == "vid" and job.title == "T" and job.duration_seconds == 120


async def test_handle_transcript_transport_gates_off_asr(db, tmp_path, monkeypatch):
    # transport job → fill_gaps=False，纯字幕零 ASR；stats 只留标量计数
    job = await _make_job(db)
    assert getattr(job, "mode", None) != "remake"
    captured = {}

    async def fake_ensure(job_id, dl, *, deadline=None, fill_gaps=True):
        captured["fill_gaps"] = fill_gaps
        return {"segments": [{"start": 0, "end": 1, "text": "a"}], "source": "manual"}
    monkeypatch.setattr("app.video.pipeline.transcript.ensure_transcript", fake_ensure)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    stats = await stages._handle_transcript(job, db, {"deadline": None})
    assert captured["fill_gaps"] is False
    assert stats["gap_asr_warning_count"] == 0 and stats["gap_asr_segments"] == 0
    assert stats["segment_count"] == 1 and stats["source"] == "manual"


async def test_handle_transcript_remake_fills_gaps_scalar_stats(db, tmp_path, monkeypatch):
    # remake job → fill_gaps=True；warnings 仅以标量计数入 stats（列表不返回，会被 _slim 丢）
    job = await _make_job(db, mode="remake")

    async def fake_ensure(job_id, dl, *, deadline=None, fill_gaps=True):
        assert fill_gaps is True
        return {"segments": [{"start": 0, "end": 1, "text": "a"}], "source": "manual",
                "gap_asr_segments": 1, "warnings": ["空窗 X 失败", "空窗 Y 失败"]}
    monkeypatch.setattr("app.video.pipeline.transcript.ensure_transcript", fake_ensure)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    stats = await stages._handle_transcript(job, db, {"deadline": None})
    assert stats["gap_asr_warning_count"] == 2 and stats["gap_asr_segments"] == 1
    assert "warnings" not in stats


async def test_handle_resegment_dumps_segments(db, tmp_path, monkeypatch):
    job = await _make_job(db)
    seg_file = tmp_path / "transcript_segments.json"
    seg_file.write_text(json.dumps([{"start": 0, "end": 1, "text": "a"}]), encoding="utf-8")
    _set_stage(job, "transcript", {"segments_path": str(seg_file)})
    monkeypatch.setattr("app.video.pipeline.resegment.needs_resegment", lambda s: False)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    stats = await stages._handle_resegment(job, db, {"deadline": None})
    assert stats["segment_count"] == 1
    assert stats["segments_path"] == str(tmp_path / "segments.json")
    assert (tmp_path / "segments.json").exists()


async def test_handle_translate_sets_term_sheet(db, tmp_path, monkeypatch):
    job = await _make_job(db)
    seg_file = tmp_path / "segments.json"
    seg_file.write_text(json.dumps([{"start": 0, "end": 2, "text": "a"}]), encoding="utf-8")
    _set_stage(job, "resegment", {"segments_path": str(seg_file)})
    _set_stage(job, "download", {"info": {"title": "T"}})
    term_sheet = [{"en": "CBT", "zh": "认知行为疗法", "source": "auto"}]
    monkeypatch.setattr("app.video.pipeline.translator.extract_terms",
                        AsyncMock(return_value=["CBT"]))
    monkeypatch.setattr("app.video.pipeline.translator.resolve_terms",
                        AsyncMock(return_value=term_sheet))
    monkeypatch.setattr("app.video.pipeline.translator.translate_batches",
                        AsyncMock(return_value=[{"start": 0, "end": 2, "en": "a", "zh": "甲"}]))
    monkeypatch.setattr("app.video.pipeline.translator.reflect_and_fit",
                        AsyncMock(side_effect=lambda translated, term_sheet, **k: translated))
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    stats = await stages._handle_translate(job, db, {"deadline": None})
    assert stats["term_count"] == 1 and stats["segment_count"] == 1
    assert job.term_sheet == term_sheet


async def test_handle_dub_transport_overwrites_adjusted_and_builds_track(db, tmp_path, monkeypatch):
    # transport dub：synthesize_all → 实际轴覆写 translated.json（带 orig_*）+ build_track（to_thread）
    job = await _make_job(db)
    job.duration_seconds = 10
    trans_file = tmp_path / "translated.json"
    trans_file.write_text(json.dumps([{"start": 0, "end": 2, "en": "a", "zh": "甲"}]),
                          encoding="utf-8")
    _set_stage(job, "translate", {"segments_path": str(trans_file)})

    async def fake_synth_all(job_id, translated, *, voice, max_rate, video_duration, deadline=None):
        clips = [{"index": 0, "path": "/x/0.wav", "duration": 1.0,
                  "start": 0.0, "rate": 1.0, "warning": None}]
        adjusted = [{**translated[0], "orig_start": 0, "orig_end": 2, "start": 0.0, "end": 1.0}]
        return clips, adjusted
    monkeypatch.setattr("app.video.pipeline.dubber.synthesize_all", fake_synth_all)
    monkeypatch.setattr("app.video.pipeline.dubber.build_track",
                        lambda clips, total, out: out)   # 同步桩，handler 经 to_thread 调
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)
    monkeypatch.setattr(stages.paths, "tts_dir", lambda jid: tmp_path)

    stats = await stages._handle_dub(job, db, {"deadline": None})
    assert stats["clip_count"] == 1 and stats["global_rate"] == 1.0
    assert stats["dub_audio_path"] == str(tmp_path / "dub.wav")
    adj = json.loads(trans_file.read_text(encoding="utf-8"))
    assert adj[0]["orig_end"] == 2 and adj[0]["end"] == 1.0   # 实际轴覆写 + 原轴留存


async def test_handle_mux_wires_ass_and_mux(db, tmp_path, monkeypatch):
    job = await _make_job(db)
    trans_file = tmp_path / "translated.json"
    trans_file.write_text(json.dumps([{"start": 0, "end": 2, "en": "a", "zh": "甲"}]),
                          encoding="utf-8")
    _set_stage(job, "translate", {"segments_path": str(trans_file)})
    _set_stage(job, "download", {"video_path": str(tmp_path / "v.mp4")})
    _set_stage(job, "dub", {"dub_audio_path": str(tmp_path / "dub.wav")})

    monkeypatch.setattr("app.video.pipeline.muxer.probe_nvenc", AsyncMock(return_value=False))
    monkeypatch.setattr("app.video.pipeline.muxer.build_ass",
                        lambda segs, path, **k: path)
    monkeypatch.setattr("app.video.pipeline.muxer.build_mixed_audio",
                        AsyncMock(return_value=Path(tmp_path / "dub.wav")))

    async def fake_mux(video, audio, out, **k):
        Path(out).write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.video.pipeline.muxer.mux", fake_mux)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)
    monkeypatch.setattr(stages.paths, "tts_dir", lambda jid: tmp_path)
    monkeypatch.setattr(stages.paths, "out_dir", lambda jid: tmp_path)

    stats = await stages._handle_mux(job, db, {"deadline": None})
    assert stats["use_nvenc"] is False and stats["burned"] is True
    assert stats["audio_layered"] is False   # mixed == dub_audio（demucs 降级路径）
    assert stats["muxed_path"] == str(tmp_path / "muxed.mp4")


async def test_handle_deliver_transport_assembles_products(db, tmp_path, monkeypatch):
    job = await _make_job(db)
    job.term_sheet = []
    trans_file = tmp_path / "translated.json"
    trans_file.write_text(json.dumps([{"start": 0, "end": 2, "en": "a", "zh": "甲"}]),
                          encoding="utf-8")
    _set_stage(job, "translate", {"segments_path": str(trans_file)})
    _set_stage(job, "download", {"info": {"title": "T"}})
    _set_stage(job, "mux", {"muxed_path": str(tmp_path / "muxed.mp4")})
    captured = {}

    def fake_assemble(job_id, *, final_video, translated, **kw):
        captured["translated"] = translated
        return {"video_url": "/uploads/video/x/out/final.mp4"}
    monkeypatch.setattr("app.video.pipeline.deliver.assemble_products", fake_assemble)

    stats = await stages._handle_deliver(job, db, {"deadline": None})
    assert stats["products"]["video_url"] == "/uploads/video/x/out/final.mp4"
    assert [s["zh"] for s in captured["translated"]] == ["甲"]   # transport 不过滤 no_dub
