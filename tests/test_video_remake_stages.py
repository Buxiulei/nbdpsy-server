"""remake 五阶 handler 层单测：analyze/rewrite/storyboard/compose/deliver 的接线与保真语义。

平移自 test_stage_tasks.py 的 TestRemakeHandlers（调度器机制 / STAGE_BUDGET / 阶段序已由
test_video_scheduler.py 覆盖，本文件只测 stages.py 的 remake handler 与 pipeline 模块接线）。
换 import 面：源 job_store 同步族 → scheduler async 族（await + AsyncSession）；handler 签名
``(job, db, ctx)`` → ``(job, session, ctx)``；pipeline 模块打桩路径改宿主新路径。db 用 conftest
的 AsyncSession fixture；rewriter LLM 打桩改 provider ``llm_chat``（本文件多以 rewrite_segments
整体打桩，不触真 LLM）。
"""
import json

import pytest

from app.video import scheduler
from app.video import stages
from app.video.pipeline.remake import inherit as remake_inherit
from app.video.pipeline.remake.rewriter import CLOSING_LINE


async def _make_job(db, **kw):
    """建一条 remake job（scheduler.create_job async）。"""
    kw.setdefault("mode", "remake")
    return await scheduler.create_job(
        db, url=kw.pop("url", "https://youtu.be/x"),
        options=kw.pop("options", {}), user_id=kw.pop("user_id", 1),
        mode=kw.pop("mode"))


async def _set_stage(db, job, stage, stats):
    """标某上游阶段 done + 写 stats（handler 经 _stage_stats 重建输入）。"""
    await scheduler.update_stage(db, job, stage, status="done", stats=stats)


def test_remake_stages_registered():
    # remake 五阶 + shared 六阶都在 STAGE_HANDLERS（全量注册自检在 stages.py import 期已 assert）
    from app.video.scheduler import STAGE_HANDLERS, REMAKE_STAGE_ORDER
    assert set(REMAKE_STAGE_ORDER) <= set(STAGE_HANDLERS)
    for stage in ("analyze", "rewrite", "storyboard", "render", "compose"):
        assert callable(STAGE_HANDLERS[stage])


async def test_handle_dub_remake_natural_synth_writes_clips(db, tmp_path, monkeypatch):
    # mode=remake 时 dub 走 synthesize_natural（rate=1.0），落 dub_clips.json，不建配音轨
    job = await _make_job(db)
    seg_file = tmp_path / "rewritten.json"
    seg_file.write_text('[{"start":0,"end":2,"en":"a","zh":"改"}]', encoding="utf-8")
    await _set_stage(db, job, "rewrite", {"segments_path": str(seg_file)})
    captured = {}

    async def fake_natural(job_id, segments, **kw):
        captured["zh"] = segments[0]["zh"]
        return [{"index": 0, "path": "/x/0.wav", "duration": 1.3}]
    monkeypatch.setattr("app.video.pipeline.dubber.synthesize_natural", fake_natural)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    st = await stages._handle_dub(job, db, {"deadline": None})
    assert captured["zh"] == "改"                 # 从 rewrite 台词取
    assert st["clip_count"] == 1
    assert st["clips_path"] == str(tmp_path / "dub_clips.json")
    clips = json.loads((tmp_path / "dub_clips.json").read_text(encoding="utf-8"))
    assert clips[0]["duration"] == 1.3            # 落盘自然时长


async def test_handle_storyboard_overwrites_rewritten_new_axis(db, tmp_path, monkeypatch):
    # storyboard 阶段：喂重写台词+自然时长重排，覆写 rewritten.json 新轴，stats 带 new_duration_s
    job = await _make_job(db)
    job.duration_seconds = 30
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps({"scenes": [], "warnings": []}), encoding="utf-8")
    await _set_stage(db, job, "analyze", {"facts_path": str(facts_file)})
    rew_file = tmp_path / "rewritten.json"
    rew_file.write_text(json.dumps([{"start": 1, "end": 3, "zh": "甲"}]), encoding="utf-8")
    await _set_stage(db, job, "rewrite", {"segments_path": str(rew_file)})
    clips_file = tmp_path / "dub_clips.json"
    clips_file.write_text(json.dumps([{"index": 0, "path": "/x", "duration": 1.0}]),
                          encoding="utf-8")
    await _set_stage(db, job, "dub", {"clips_path": str(clips_file)})

    retimed = [{"start": 0.5, "end": 1.5, "zh": "甲", "orig_start": 1, "orig_end": 3}]
    fake_sb = {"scenes": [{"id": 1}], "source": {"duration_s": 8.5},
               "warnings": [], "retimed_segments": retimed}
    captured = {}

    async def fake_build(facts, *, duration, segments=None, clip_durations=None, **_kw):
        captured["segments"] = segments
        captured["durations"] = clip_durations
        captured["overrides"] = _kw
        return dict(fake_sb)      # 浅拷贝：handler pop 不污染原 dict
    monkeypatch.setattr("app.video.pipeline.remake.storyboard.build_storyboard", fake_build)
    monkeypatch.setattr("app.video.pipeline.remake.storyboard.validate_storyboard",
                        lambda sb: None)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    st = await stages._handle_storyboard(job, db, {"deadline": None})
    assert captured["segments"][0]["zh"] == "甲"   # 重写台词进 relayout
    assert captured["durations"] == [1.0]          # 自然时长进 relayout
    new_rew = json.loads(rew_file.read_text(encoding="utf-8"))
    assert new_rew[0]["start"] == 0.5 and new_rew[0]["orig_start"] == 1   # 覆写成新轴
    sb_written = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
    assert "retimed_segments" not in sb_written    # storyboard.json 不含 retimed
    assert st["new_duration_s"] == 8.5


async def test_handle_analyze_guards_missing_duration(db, tmp_path):
    # I2：duration 缺失时 analyze 入口 fail-fast，不再全片白扫后误导性失败
    job = await _make_job(db)
    job.duration_seconds = None
    await _set_stage(db, job, "download", {"video_path": str(tmp_path / "v.mp4")})
    with pytest.raises(ValueError, match="时长未知"):
        await stages._handle_analyze(job, db, {"deadline": None})


async def test_handle_rewrite_marks_no_dub_from_facts(db, tmp_path, monkeypatch):
    # A3：rewrite 后按 facts 免责卡范围标 no_dub，落 rewritten.json + stats.no_dub_count
    job = await _make_job(db)
    trans_file = tmp_path / "translated.json"
    trans_file.write_text(json.dumps(
        [{"start": 1.0, "end": 3.0, "zh": "免责台词"},
         {"start": 30.0, "end": 33.0, "zh": "正常台词"}]), encoding="utf-8")
    await _set_stage(db, job, "translate", {"segments_path": str(trans_file)})
    facts_file = tmp_path / "scene_facts.json"
    facts_file.write_text(json.dumps({"scenes": [
        {"kind": "text_card", "t0": 0.0, "t1": 10.0,
         "text": "Disclaimer: not medical advice"},
        {"kind": "ball_exercise", "t0": 10.0, "t1": 40.0, "text": ""}]}),
        encoding="utf-8")
    await _set_stage(db, job, "analyze", {"facts_path": str(facts_file)})

    async def fake_rewrite(translated, terms, **kw):        # LLM 打桩：原样返回
        return [dict(t) for t in translated]
    monkeypatch.setattr("app.video.pipeline.remake.rewriter.rewrite_segments", fake_rewrite)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    st = await stages._handle_rewrite(job, db, {"deadline": None})
    rew = json.loads((tmp_path / "rewritten.json").read_text(encoding="utf-8"))
    assert rew[0].get("no_dub") is True         # 落免责卡 [0,10] 内
    assert "no_dub" not in rew[1]               # 球段句正常
    assert st["no_dub_count"] == 1


async def test_handle_rewrite_revision_applies_edit_plan_without_llm(db, tmp_path, monkeypatch):
    # B4：options.revision 存在 → 读继承的 rewritten.json apply edit_plan，不调 LLM 重写
    edit_plan = [{"type": "script_edit", "index": 1, "new_text": "改后台词"}]
    job = await _make_job(
        db, options={"revision": {"instructions": "改第二句", "edit_plan": edit_plan}})
    rew_file = tmp_path / "rewritten.json"          # inherit 拷来的父副本
    rew_file.write_text(json.dumps([
        {"start": 0.0, "end": 2.0, "zh": "第一句", "en": "a",
         "orig_start": 0.0, "orig_end": 2.0},
        {"start": 2.0, "end": 4.0, "zh": "第二句", "en": "b",
         "orig_start": 2.0, "orig_end": 4.0}]), encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("revision 分支不得调 LLM 重写")
    monkeypatch.setattr("app.video.pipeline.remake.rewriter.rewrite_segments", _boom)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    st = await stages._handle_rewrite(job, db, {"deadline": None})
    rew = json.loads(rew_file.read_text(encoding="utf-8"))
    assert rew[1]["zh"] == "改后台词" and rew[0]["zh"] == "第一句"
    assert rew[1]["orig_start"] == 2.0              # orig_* 保留（relayout 重入幂等）
    assert st["revision"] is True and st["edit_op_count"] == 1
    ov = json.loads((tmp_path / "param_overrides.json").read_text(encoding="utf-8"))
    assert ov == {"cards": {}, "ball": {}, "global": {}}   # 三键结构稳定


async def test_handle_rewrite_revision_closing_line_override(db, tmp_path, monkeypatch):
    # B4：global_param.closing_line 覆盖末尾收束句文本（父已锚定 orig_*，仅改 zh）
    edit_plan = [{"type": "global_param", "closing_line": "好，我们到这里。"}]
    job = await _make_job(
        db, options={"revision": {"instructions": "换个结语", "edit_plan": edit_plan}})
    rew_file = tmp_path / "rewritten.json"
    rew_file.write_text(json.dumps([
        {"start": 0.0, "end": 2.0, "zh": "正文", "en": "a"},
        {"start": 2.0, "end": 5.0, "zh": CLOSING_LINE, "en": "",
         "orig_start": 2.0, "orig_end": 2.1}]), encoding="utf-8")
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    await stages._handle_rewrite(job, db, {"deadline": None})
    rew = json.loads(rew_file.read_text(encoding="utf-8"))
    assert rew[-1]["zh"] == "好，我们到这里。"
    assert rew[-1]["orig_start"] == 2.0             # 锚点不动，只换文本
    ov = json.loads((tmp_path / "param_overrides.json").read_text(encoding="utf-8"))
    assert ov["global"]["closing_line"] == "好，我们到这里。"


async def _revision_rewrite_once(db, tmp_path, monkeypatch, base, edit_plan):
    """seed 不可变基底 rewritten_inherited.json → 跑一次 revision rewrite → 返回 rewritten.json。"""
    job = await _make_job(
        db, options={"revision": {"instructions": "i", "edit_plan": edit_plan}})
    (tmp_path / "rewritten_inherited.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)
    await stages._handle_rewrite(job, db, {"deadline": None})
    return json.loads((tmp_path / "rewritten.json").read_text(encoding="utf-8"))


async def test_revision_rewrite_idempotent_on_delete_reentry(db, tmp_path, monkeypatch):
    # I1：崩溃后重入不得二次 apply——恒从不可变基底重算。delete 例：连跑两次产物一致
    base = [{"zh": "甲", "en": "a"}, {"zh": "乙", "en": "b"}, {"zh": "丙", "en": "c"}]
    plan = [{"type": "script_delete", "index": 1}]
    first = await _revision_rewrite_once(db, tmp_path, monkeypatch, base, plan)
    assert [s["zh"] for s in first] == ["甲", "丙"]
    second = await _revision_rewrite_once(db, tmp_path, monkeypatch, base, plan)
    assert second == first                       # 读不可变基底，仍得 [甲,丙]（非 [甲]）


async def test_revision_rewrite_idempotent_on_insert_reentry(db, tmp_path, monkeypatch):
    # I1：insert 例：连跑两次不重复插入
    base = [{"zh": "甲", "en": "a"}, {"zh": "乙", "en": "b"}]
    plan = [{"type": "script_insert", "after_index": 0, "text": "新"}]
    first = await _revision_rewrite_once(db, tmp_path, monkeypatch, base, plan)
    assert [s["zh"] for s in first] == ["甲", "新", "乙"]
    second = await _revision_rewrite_once(db, tmp_path, monkeypatch, base, plan)
    assert second == first                       # 非 [甲,新,新,乙]


async def test_revision_closing_line_ineffective_warns_not_silent(db, tmp_path, monkeypatch):
    # M4b：父末句非标准收束句 → closing_line 无可替换目标 → stats 记 warning（不静默 no-op）
    base = [{"zh": "正文一", "en": "a"}, {"zh": "正文二", "en": "b"}]  # 无 CLOSING_LINE
    plan = [{"type": "global_param", "closing_line": "新结语"}]
    job = await _make_job(
        db, options={"revision": {"instructions": "i", "edit_plan": plan}})
    (tmp_path / "rewritten_inherited.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)
    st = await stages._handle_rewrite(job, db, {"deadline": None})
    assert "无标准收束句" in st["closing_line_warning"]
    rew = json.loads((tmp_path / "rewritten.json").read_text(encoding="utf-8"))
    assert rew[-1]["zh"] == "正文二"             # 末句未被误改


async def test_revision_closing_line_idempotent_on_reentry(db, tmp_path, monkeypatch):
    # I1：seed 恒从不可变继承种子读 → closing_line revision 连跑两遍产物一致
    base = [{"zh": "正文", "en": "a"},
            {"zh": CLOSING_LINE, "en": "", "orig_start": 2.0, "orig_end": 2.1}]
    plan = [{"type": "global_param", "closing_line": "新收束语"}]
    job = await _make_job(
        db, options={"revision": {"instructions": "i", "edit_plan": plan}})
    (tmp_path / "rewritten_inherited.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    s1 = await stages._handle_rewrite(job, db, {"deadline": None})
    r1 = json.loads((tmp_path / "rewritten.json").read_text(encoding="utf-8"))
    assert r1[-1]["zh"] == "新收束语" and "closing_line_warning" not in s1
    # 重入（模拟落盘后被硬杀再跑）：不受上一遍落盘 param_overrides.json 影响
    s2 = await stages._handle_rewrite(job, db, {"deadline": None})
    r2 = json.loads((tmp_path / "rewritten.json").read_text(encoding="utf-8"))
    assert r2 == r1 and "closing_line_warning" not in s2


async def test_revision_chain_inherits_param_overrides(db, tmp_path, monkeypatch):
    # Imp-1：revision-of-revision——一层 ball_style.y_ratio 覆盖在二层（只改一句话）仍生效
    from app.core.config import settings
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    base = [{"zh": "甲", "en": "a"}, {"zh": "乙", "en": "b"}]

    # 一层 revision：ball_style.y_ratio=0.7 覆盖 → 落 param_overrides.json
    j1 = await _make_job(
        db, options={"revision": {"instructions": "调球位",
                                  "edit_plan": [{"type": "ball_style", "y_ratio": 0.7}]}})
    (stages.paths.raw_dir(j1.id) / "rewritten_inherited.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    await stages._handle_rewrite(j1, db, {"deadline": None})
    ov1 = json.loads(
        (stages.paths.raw_dir(j1.id) / "param_overrides.json").read_text(encoding="utf-8"))
    assert ov1["ball"]["y_ratio"] == 0.7
    (stages.paths.raw_dir(j1.id) / "video.mp4").write_bytes(b"V")   # inherit 需父 video.mp4

    # 二层 revision：只改一句话，继承一层 param_overrides（走真 inherit_artifacts）
    j2 = await scheduler.create_revision_job(
        db, j1, instructions="改第一句",
        edit_plan=[{"type": "script_edit", "index": 0, "new_text": "甲改"}])
    remake_inherit.inherit_artifacts(j1.id, j2.id)
    await stages._handle_rewrite(j2, db, {"deadline": None})
    ov2 = json.loads(
        (stages.paths.raw_dir(j2.id) / "param_overrides.json").read_text(encoding="utf-8"))
    assert ov2["ball"]["y_ratio"] == 0.7        # 父层覆盖存活（Imp-1 修复）
    rew2 = json.loads(
        (stages.paths.raw_dir(j2.id) / "rewritten.json").read_text(encoding="utf-8"))
    assert rew2[0]["zh"] == "甲改" and rew2[1]["zh"] == "乙"   # 本层一句改也生效


async def test_handle_storyboard_consumes_param_overrides(db, tmp_path, monkeypatch):
    # B4：storyboard 读 param_overrides → ball/sentence_gap 透传 build_storyboard，
    # cards 覆盖对应卡片场景 content（scene_id 字符串键匹配）
    job = await _make_job(db)
    job.duration_seconds = 30
    for name, payload in (
            ("scene_facts.json", {"scenes": [], "warnings": []}),
            ("rewritten.json", [{"start": 1, "end": 3, "zh": "甲"}]),
            ("dub_clips.json", [{"index": 0, "path": "/x", "duration": 1.0}]),
            ("param_overrides.json", {
                "cards": {"1": {"title": "改标题"}},
                "ball": {"y_ratio": 0.7, "period_s": 2.2,
                         "palette": ["#111"], "color_mode": "single"},
                "global": {"sentence_gap": 0.4}})):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    await _set_stage(db, job, "analyze", {"facts_path": str(tmp_path / "scene_facts.json")})
    await _set_stage(db, job, "rewrite", {"segments_path": str(tmp_path / "rewritten.json")})
    await _set_stage(db, job, "dub", {"clips_path": str(tmp_path / "dub_clips.json")})

    captured = {}

    async def fake_build(facts, *, duration, segments=None, clip_durations=None, **kw):
        captured.update(kw)
        return {"scenes": [{"id": 1, "type": "title_card",
                            "content": {"title": "原标题"}}],
                "source": {"duration_s": 12.0}, "warnings": []}
    monkeypatch.setattr("app.video.pipeline.remake.storyboard.build_storyboard", fake_build)
    monkeypatch.setattr("app.video.pipeline.remake.storyboard.validate_storyboard",
                        lambda sb: None)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    await stages._handle_storyboard(job, db, {"deadline": None})
    assert captured["y_ratio"] == 0.7 and captured["period_s"] == 2.2
    assert captured["palette"] == ["#111"] and captured["color_mode"] == "single"
    assert captured["sentence_gap"] == 0.4
    # card_edit 覆盖写回分镜（scene_id "1" 匹配 id=1 的卡片场景）
    sb = json.loads((tmp_path / "storyboard.json").read_text(encoding="utf-8"))
    assert sb["scenes"][0]["content"]["title"] == "改标题"


async def test_handle_compose_filters_no_dub(db, tmp_path, monkeypatch):
    # A3：no_dub 句不进配音轨(build_track)、不进字幕轨(compose 的 segments)
    job = await _make_job(db)
    job.duration_seconds = 20
    sb_file = tmp_path / "storyboard.json"
    sb_file.write_text(json.dumps({"scenes": [{"id": 1, "type": "text_card"}],
                                   "source": {"duration_s": 20.0}}), encoding="utf-8")
    await _set_stage(db, job, "storyboard", {"storyboard_path": str(sb_file)})
    rendered_file = tmp_path / "rendered.json"
    rendered_file.write_text(json.dumps([str(tmp_path / "s1.mp4")]), encoding="utf-8")
    await _set_stage(db, job, "render", {"scene_paths_path": str(rendered_file)})
    rew_file = tmp_path / "rewritten.json"
    rew_file.write_text(json.dumps([
        {"start": 0.5, "end": 1.5, "zh": "免责", "no_dub": True},
        {"start": 2.0, "end": 3.0, "zh": "正常"}]), encoding="utf-8")
    await _set_stage(db, job, "rewrite", {"segments_path": str(rew_file)})
    clips_file = tmp_path / "dub_clips.json"
    clips_file.write_text(json.dumps([
        {"index": 0, "path": None, "duration": 0.0, "no_dub": True},
        {"index": 1, "path": str(tmp_path / "1.wav"), "duration": 1.0}]),
        encoding="utf-8")
    await _set_stage(db, job, "dub", {"clips_path": str(clips_file)})

    captured = {}

    async def fake_concat(paths_, out, **kw):
        return out

    async def fake_mix(a, b, out, **kw):
        return out

    (tmp_path / "param_overrides.json").write_text(
        json.dumps({"cards": {}, "ball": {},
                    "global": {"disclaimer_text": "自定义声明"}}), encoding="utf-8")

    async def fake_compose(video, audio, segments, out, **kw):
        captured["sub_segments"] = segments
        captured["disclaimer"] = kw.get("disclaimer")
        out.write_text("x", encoding="utf-8")

    def fake_build_track(clips, total, out):
        captured["dub_clips"] = clips
        return out

    def fake_tones(scenes, total, out):
        return out

    async def fake_nvenc():
        return False

    monkeypatch.setattr("app.video.pipeline.remake.composer.concat_scenes", fake_concat)
    monkeypatch.setattr("app.video.pipeline.remake.composer.mix_audio", fake_mix)
    monkeypatch.setattr("app.video.pipeline.remake.composer.compose", fake_compose)
    monkeypatch.setattr("app.video.pipeline.dubber.build_track", fake_build_track)
    monkeypatch.setattr("app.video.pipeline.remake.tones.bilateral_track", fake_tones)
    monkeypatch.setattr("app.video.pipeline.muxer.probe_nvenc", fake_nvenc)
    monkeypatch.setattr(stages.paths, "out_dir", lambda jid: tmp_path)
    monkeypatch.setattr(stages.paths, "tts_dir", lambda jid: tmp_path)
    monkeypatch.setattr(stages.paths, "raw_dir", lambda jid: tmp_path)

    await stages._handle_compose(job, db, {"deadline": None})
    # 配音轨只含非 no_dub 句(index 1)，path=None 的 no_dub 句被过滤（否则 build_track 崩）
    assert [c["index"] for c in captured["dub_clips"]] == [1]
    # 字幕 segments 过滤掉 no_dub 句，只剩正常句
    assert [s["zh"] for s in captured["sub_segments"]] == ["正常"]
    # B4：global_param.disclaimer_text 透传给 composer.compose
    assert captured["disclaimer"] == "自定义声明"


async def test_handle_deliver_excludes_no_dub_from_transcript(db, tmp_path, monkeypatch):
    # A3：no_dub 句（原片轴、未重排）不进交付 SRT/逐字稿，避免错轴污染
    job = await _make_job(db)
    rew_file = tmp_path / "rewritten.json"
    rew_file.write_text(json.dumps([
        {"start": 0.5, "end": 1.5, "zh": "免责", "en": "d", "no_dub": True},
        {"start": 2.0, "end": 3.0, "zh": "正常", "en": "n"}]), encoding="utf-8")
    await _set_stage(db, job, "rewrite", {"segments_path": str(rew_file)})
    await _set_stage(db, job, "download", {"info": {}})
    await _set_stage(db, job, "compose", {"muxed_path": str(tmp_path / "final.mp4")})
    await _set_stage(db, job, "storyboard", {"storyboard_path": str(tmp_path / "sb.json")})
    (tmp_path / "sb.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")
    captured = {}

    def fake_assemble(job_id, *, final_video, translated, **kw):
        captured["translated"] = translated
        return {"video_url": "/uploads/video/x/out/final.mp4"}
    monkeypatch.setattr("app.video.pipeline.deliver.assemble_products", fake_assemble)
    monkeypatch.setattr(stages.paths, "out_dir", lambda jid: tmp_path)

    st = await stages._handle_deliver(job, db, {"deadline": None})
    assert st["products"]["video_url"].endswith("final.mp4")
    # 交付台词过滤掉 no_dub 句（免责句原片轴未重排，排除避免错轴污染）
    assert [s["zh"] for s in captured["translated"]] == ["正常"]
