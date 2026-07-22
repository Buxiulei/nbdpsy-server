"""视频管线阶段 handler 层：把 transport 七阶体接线进调度器 STAGE_HANDLERS。

签名 ``async (job, session, ctx) -> stats``（源 celery ``(job, db, ctx)`` 平移，db→AsyncSession）。
ctx={"deadline": monotonic秒}。跨阶段大数据不靠内存传递（阶段跑在不同会话/自链步里），统一落盘
raw_dir/*.json，stats 只回路径 + 计数；每个 handler 开头从上一阶段 stats 读文件重建输入。
_slim（剔大列表）与 products 弹出由调度器 ``_run_stages`` 统一做，handler 返回原始 stats 即可。

【非阻塞红线】本层跑在单进程 asyncio worker 的事件循环上（见 scheduler.py STAGE_HANDLERS 契约）：
- ffmpeg/ffprobe/demucs/yt-dlp 子进程：pipeline 内部本就 ``create_subprocess_exec``（非阻塞）；
- pydub 拼轨 ``dubber.build_track``（解码/导出阻塞）：本层经 ``asyncio.to_thread`` 下沉线程；
- ASS/SRT/meta 等小文本写：非「大读写」，直接调（build_ass / assemble_products）。

各 handler 内**延迟 import** 具体阶段模块（源同款）：worker 启动仅 import 本模块完成注册，pydub/
httpx/openai 等重依赖推迟到首个 job 触及该阶段时才加载。

M3b 补齐：remake 五阶（analyze/rewrite/storyboard/render/compose）落地 + 全量注册。shared
handler（transcript/dub/deliver）的 remake 分支 M3a 已逐字保真，其前向引用的 remake 模块
（remake.composer 等）本 track 一并落地。全量 assert（== STAGE_ORDER | REMAKE_STAGE_ORDER）补回。

【非阻塞红线·remake 段补充】compose 阶段的 tones.bilateral_track（numpy 合成立体声轨 + wave 落盘）
与 dubber.build_track（pydub 拼轨）都是同步 CPU 段——经 asyncio.to_thread 下沉线程，不占事件循环
（与 dub 阶段 build_track 同款处理）。render 阶段的 ball_png/screenshot 由 renderers 内部自持非阻塞。
"""
import asyncio
import json
import logging
from pathlib import Path

from app.core.config import settings
from app.video import paths
from app.video.scheduler import (
    REMAKE_STAGE_ORDER,
    STAGE_HANDLERS,
    STAGE_ORDER,
    stage_order,
)

logger = logging.getLogger(__name__)

_CPS = 4.2   # 中文配音朗读速度估算（字/秒）；审校压缩闸门用


# ── 阶段间数据落盘 / 重建小工具 ────────────────────────────────────
def _stage_stats(job, stage: str) -> dict:
    """读上一阶段落库的 stats（重建本阶段输入的唯一来源）。"""
    return ((job.stages or {}).get(stage) or {}).get("stats") or {}


def _dump_json(path: Path, data) -> Path:
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Path(path)


def _load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_param_overrides(job) -> dict:
    """读 revision rewrite 阶段落盘的参数覆盖（cards/ball/global）；非 revision job 返空结构。

    storyboard 消费 cards（场景 content 覆盖）/ ball（球段参数）/ global.sentence_gap；
    compose 消费 global.disclaimer_text。文件缺失（普通 remake）时给稳定空结构，消费侧零分支。
    """
    p = paths.raw_dir(job.id) / "param_overrides.json"
    ov = _load_json(p) if p.exists() else {}
    return {"cards": ov.get("cards") or {}, "ball": ov.get("ball") or {},
            "global": ov.get("global") or {}}


def _apply_card_overrides(sb: dict, cards: dict) -> None:
    """card_edit 覆盖：把 cards[scene_id]={title?,body?} 写回分镜对应卡片场景 content。

    scene_id 经 param_overrides.json 落盘/回读后是字符串键（JSON object 键），故按 str 匹配
    storyboard 场景 id（build_storyboard 对同一继承 facts 确定性重排，卡片场景 id 与父稳定一致）。
    """
    if not cards:
        return
    for sc in sb.get("scenes") or []:
        # 只改卡片场景（与 validate_edit_plan 一致）：script 增删改会改变球段子场景切分，令球段之后
        # 卡片的 sequential id 相对父漂移——漂到球段则本守卫跳过（退化 no-op）；极窄情况下若恰漂到
        # 另一卡片会误改邻卡（v1 已知限制，可再 revise 修正）。纯 card_edit（无 script 增删）恒安全。
        if sc.get("type") not in ("title_card", "text_card"):
            continue
        edit = cards.get(str(sc.get("id")))
        if not edit:
            continue
        content = sc.setdefault("content", {})
        if edit.get("title") is not None:
            content["title"] = edit["title"]
        if edit.get("body") is not None:
            content["body"] = edit["body"]


# ── 阶段 handler：签名 async (job, session, ctx) -> stats dict ────────
async def _handle_download(job, session, ctx):
    from app.video.pipeline.downloader import download
    result = await download(job.url, paths.raw_dir(job.id),
                            max_resolution=int((job.options or {}).get("max_resolution", 1080)),
                            deadline=ctx["deadline"])
    job.video_id = result["info"].get("id")
    job.title = result["info"].get("title")
    job.duration_seconds = result["info"].get("duration")
    await session.commit()
    return result  # video_path/subtitle_path/subtitle_source/info 直接进 stats


async def _handle_transcript(job, session, ctx):
    from app.video.pipeline import transcript
    # download stats 即 ensure_transcript 需要的 download_result（含 video_path/subtitle_path）
    # 空窗 ASR 补漏仅 remake 走：transport 带字幕路径纯字幕零 ASR（恢复旧行为）
    fill_gaps = getattr(job, "mode", None) == "remake"
    result = await transcript.ensure_transcript(
        job.id, _stage_stats(job, "download"), deadline=ctx["deadline"],
        fill_gaps=fill_gaps)
    segments = result["segments"]
    out = _dump_json(paths.raw_dir(job.id) / "transcript_segments.json", segments)
    # warnings 明细走日志（_slim 丢列表），stats 只留标量计数以便入库/前端可见
    return {"segments_path": str(out), "source": result["source"],
            "segment_count": len(segments),
            "gap_asr_segments": result.get("gap_asr_segments", 0),
            "gap_asr_warning_count": len(result.get("warnings", []))}


async def _handle_resegment(job, session, ctx):
    from app.video.pipeline import resegment as reseg
    segments = _load_json(_stage_stats(job, "transcript")["segments_path"])
    if reseg.needs_resegment(segments):
        segments = await reseg.resegment(segments, deadline=ctx["deadline"])
    out = _dump_json(paths.raw_dir(job.id) / "segments.json", segments)
    return {"segments_path": str(out), "segment_count": len(segments)}


async def _handle_translate(job, session, ctx):
    from app.video.pipeline import translator
    segments = _load_json(_stage_stats(job, "resegment")["segments_path"])
    video_meta = _stage_stats(job, "download").get("info", {})
    terms = await translator.extract_terms(segments)
    term_sheet = await translator.resolve_terms(session, terms)
    translated = await translator.translate_batches(
        segments, term_sheet, video_meta, deadline=ctx["deadline"])
    translated = await translator.reflect_and_fit(
        translated, term_sheet, cps=_CPS, deadline=ctx["deadline"])
    job.term_sheet = term_sheet   # 落库列，deliver / API 直接取
    await session.commit()
    out = _dump_json(paths.raw_dir(job.id) / "translated.json", translated)
    return {"segments_path": str(out), "term_count": len(term_sheet),
            "segment_count": len(translated)}


async def _handle_dub(job, session, ctx):
    from app.video.pipeline import dubber
    voice = (job.options or {}).get("voice") or settings.DOUBAO_TTS_VOICE
    if getattr(job, "mode", None) == "remake":
        # wave5 弹性时间轴：只 rate=1.0 测每句自然时长落 dub_clips.json，
        # 不改写台词文件、不建配音轨（时间轴重排在 storyboard，配音轨在 compose）。
        translated = _load_json(_stage_stats(job, "rewrite")["segments_path"])
        clips = await dubber.synthesize_natural(
            job.id, translated, voice=voice, deadline=ctx["deadline"])
        out = _dump_json(paths.raw_dir(job.id) / "dub_clips.json", clips)
        return {"clips_path": str(out), "clip_count": len(clips)}
    # transport 分支：两遍合成 + 全片统一语速 + 实际轴回写 translate.json + build_track
    translated_path = _stage_stats(job, "translate")["segments_path"]
    translated = _load_json(translated_path)
    total_duration = float(job.duration_seconds or 0) or \
        max((t["end"] for t in translated), default=0.0)
    clips, adjusted = await dubber.synthesize_all(
        job.id, translated, voice=voice, max_rate=settings.TTS_MAX_RATE,
        video_duration=total_duration, deadline=ctx["deadline"])
    # 时间轴传导：实际配音轴覆盖落盘 translated.json（原轴存 orig_*）——
    # 下游 mux(build_ass 字幕) / deliver(SRT+双语md) 读同一文件，自动音字同步。
    _dump_json(Path(translated_path), adjusted)
    # pydub 拼轨阻塞：下沉线程不占事件循环（非阻塞红线）
    dub_wav = await asyncio.to_thread(
        dubber.build_track, clips, total_duration, paths.tts_dir(job.id) / "dub.wav")
    return {"dub_audio_path": str(dub_wav), "clip_count": len(clips),
            "global_rate": clips[0]["rate"] if clips else 1.0,
            "warning_count": sum(1 for c in clips if c.get("warning"))}


async def _handle_mux(job, session, ctx):
    from app.video.pipeline import muxer
    translated = _load_json(_stage_stats(job, "translate")["segments_path"])
    video_path = _stage_stats(job, "download")["video_path"]
    dub_audio = _stage_stats(job, "dub")["dub_audio_path"]
    burn = (job.options or {}).get("burn_subtitles", True)
    use_nvenc = await muxer.probe_nvenc()
    ass_path = muxer.build_ass(translated, paths.raw_dir(job.id) / "subtitle.ass") if burn else None
    # 音频分层：保留原视频 BGM/音效(demucs 去人声) 叠中文配音；demucs 不可用则降级纯配音替换
    mixed_audio = await muxer.build_mixed_audio(
        Path(video_path), Path(dub_audio), paths.tts_dir(job.id) / "mixed.wav",
        deadline=ctx["deadline"])
    out = paths.out_dir(job.id) / "muxed.mp4"
    await muxer.mux(Path(video_path), mixed_audio, out,
                    ass_path=ass_path, use_nvenc=use_nvenc, deadline=ctx["deadline"])
    return {"muxed_path": str(out), "use_nvenc": use_nvenc, "burned": burn,
            "audio_layered": mixed_audio != Path(dub_audio)}


async def _handle_deliver(job, session, ctx):
    from app.video.pipeline import deliver
    remake = getattr(job, "mode", None) == "remake"
    src_stage = "rewrite" if remake else "translate"
    final_stage = "compose" if remake else "mux"
    translated = _load_json(_stage_stats(job, src_stage)["segments_path"])
    if remake:
        # no_dub 句（免责/须知）不朗读且时间轴未重排（仍为原片轴），排除出交付
        # 字幕(transcript_*.srt)/逐字稿，避免错轴条目污染成片同源的字幕文件。
        translated = [s for s in translated if not s.get("no_dub")]
    video_meta = _stage_stats(job, "download").get("info", {})
    muxed = _stage_stats(job, final_stage)["muxed_path"]
    stats = {name: _stage_stats(job, name) for name in stage_order(job)}
    storyboard_path = None
    attribution = None
    if remake:
        # M3b 前向引用：remake.composer 由 remake 链子任务落地；transport job 不进本分支。
        from app.video.pipeline.remake.composer import ATTRIBUTION
        storyboard_path = Path(_stage_stats(job, "storyboard")["storyboard_path"])
        attribution = ATTRIBUTION
    # revision 溯源块：parent/instructions/edit_plan 入 meta，随产物公开
    revision_meta = None
    rev_opts = (job.options or {}).get("revision")
    if rev_opts:
        revision_meta = {"parent_job_id": job.parent_job_id,
                         "instructions": rev_opts.get("instructions"),
                         "edit_plan": rev_opts.get("edit_plan") or []}
    products = deliver.assemble_products(
        job.id, final_video=Path(muxed), translated=translated,
        term_sheet=job.term_sheet or [], video_meta=video_meta, stats=stats,
        storyboard=storyboard_path, attribution=attribution, revision=revision_meta)
    if remake:
        # 中间产物清理：成片已 move 成 final.mp4，scenes/ 与 remade_silent 半成品无保留价值
        import shutil
        out_dir = paths.out_dir(job.id)
        shutil.rmtree(out_dir / "scenes", ignore_errors=True)
        (out_dir / "remade_silent.mp4").unlink(missing_ok=True)
        (out_dir / "remade_silent.concat.txt").unlink(missing_ok=True)
    return {"products": products}


# ── remake 专属 handler：analyze/rewrite/storyboard/render/compose ──
async def _handle_analyze(job, session, ctx):
    from app.video.pipeline.remake import analyzer
    video_path = _stage_stats(job, "download")["video_path"]
    duration = float(job.duration_seconds or 0)
    # fail-fast 在正确阶段：duration<=0 会让 analyzer 全片白扫 4 阶段后才以误导性
    # 错误失败——入口直接拦，错误定位到"时长未知"这个真因。
    if duration <= 0:
        raise ValueError("analyze: 原片时长未知(duration_seconds 缺失)，无法分镜")
    facts = await analyzer.analyze(Path(video_path), duration,
                                   deadline=ctx["deadline"])
    out = _dump_json(paths.raw_dir(job.id) / "scene_facts.json", facts)
    return {"facts_path": str(out), "scene_count": len(facts["scenes"]),
            "warning_count": len(facts.get("warnings", []))}


async def _handle_rewrite(job, session, ctx):
    from app.video.pipeline.remake import rewriter
    # revision 分支（spec §B3）：读父继承的 rewritten.json 为基底 apply edit_plan，不调 LLM。
    rev_opts = (job.options or {}).get("revision")
    if rev_opts:
        from app.video.pipeline.remake import revision as rev
        raw = paths.raw_dir(job.id)
        # I1 幂等：恒从不可变基底 rewritten_inherited.json 读、apply 后只写 rewritten.json——
        # 崩溃后重入在同一基底上重算，绝不二次 apply（缺基底时防御式退回 rewritten.json）。
        base_path = raw / "rewritten_inherited.json"
        if not base_path.exists():
            base_path = raw / "rewritten.json"
        base = _load_json(base_path)
        ops = rev_opts.get("edit_plan") or []
        # Imp-1 链式修订：以继承的 param_overrides_inherited.json（**不可变种子**）为起点累积
        # （父层 ball/global/card 覆盖不丢），原始 remake 父无此文件时 {} 起步。种子恒从只读文件读、
        # 绝不读本 handler 落盘的 param_overrides.json（后者含本层已合并覆盖，重入会破坏 I1 幂等）。
        # 父层 closing_line 已 baked 进继承 base rewritten，记为「替换前预期末句」——本层若再改 closing
        # 才在此基础上替换（delta 语义），链式再改可续。
        seed_path = raw / "param_overrides_inherited.json"
        seed = _load_json(seed_path) if seed_path.exists() else {}
        prev_closing = (seed.get("global") or {}).get("closing_line") or rewriter.CLOSING_LINE
        new_rewritten, overrides = rev.apply_edits(ops, base, param_overrides=seed)
        # global_param.closing_line：只应用本层 delta 改末尾收束句 zh（父层已 baked 进 base）。
        stats_extra = {}
        current_closing = next(
            (op.get("closing_line") for op in ops
             if op.get("type") == "global_param" and op.get("closing_line") is not None), None)
        if current_closing is not None:
            if new_rewritten and new_rewritten[-1].get("zh") == prev_closing:
                new_rewritten[-1] = {**new_rewritten[-1], "zh": current_closing}
            else:                                # M4b：无标准收束句可替换 → 不静默 no-op
                msg = "closing_line 覆盖未生效(父片无标准收束句)"
                logger.warning("job %s revision: %s", job.id, msg)
                stats_extra["closing_line_warning"] = msg
        out = _dump_json(raw / "rewritten.json", new_rewritten)
        _dump_json(raw / "param_overrides.json", overrides)
        return {"segments_path": str(out), "segment_count": len(new_rewritten),
                "no_dub_count": sum(1 for s in new_rewritten if s.get("no_dub")),
                "revision": True, "edit_op_count": len(ops), **stats_extra}
    translated = _load_json(_stage_stats(job, "translate")["segments_path"])
    rewritten = await rewriter.rewrite_segments(
        translated, job.term_sheet or [], deadline=ctx["deadline"])
    # A3 免责/须知台词不配音：analyze 在 rewrite 之前，facts 已就绪——按免责须知卡时间范围
    # 给落入其中的句子标 no_dub（下游 dub/字幕/时间轴据此跳过，使用须知卡仍显示完整文案）。
    facts = _load_json(_stage_stats(job, "analyze")["facts_path"])
    scenes = facts.get("scenes") or []
    rewriter.mark_no_dub(rewritten, scenes)
    # A6/F2 收束句锚定收尾卡（须在 mark_no_dub 之后）：存在结语卡时把追加的收束句 orig_* 落到
    # 收尾卡内(t0+0.1)使 relayout 归块正确；并清 no_dub 保证朗读（根治窄边界再标记）。
    rewriter.anchor_closing_line(rewritten, scenes)
    out = _dump_json(paths.raw_dir(job.id) / "rewritten.json", rewritten)
    return {"segments_path": str(out), "segment_count": len(rewritten),
            "no_dub_count": sum(1 for s in rewritten if s.get("no_dub"))}


async def _handle_storyboard(job, session, ctx):
    from app.video.pipeline.remake import storyboard
    facts = _load_json(_stage_stats(job, "analyze")["facts_path"])
    # 弹性时间轴：喂重写台词 + 各句自然时长（dub 阶段 rate=1.0 实测），按语音优先重排
    rewritten_path = _stage_stats(job, "rewrite")["segments_path"]
    segments = _load_json(rewritten_path)
    clips = _load_json(_stage_stats(job, "dub")["clips_path"])
    clip_durations = [c["duration"] for c in sorted(clips, key=lambda c: c["index"])]
    # revision 参数覆盖（B4）：ball→球段参数、global.sentence_gap→句间停顿、cards→场景 content
    ov = _load_param_overrides(job)
    ball, glob = ov["ball"], ov["global"]
    sb = await storyboard.build_storyboard(
        facts, duration=float(job.duration_seconds or 0),
        segments=segments, clip_durations=clip_durations,
        y_ratio=ball.get("y_ratio"), palette=ball.get("palette"),
        period_s=ball.get("period_s"), color_mode=ball.get("color_mode"),
        sentence_gap=glob.get("sentence_gap"))
    _apply_card_overrides(sb, ov["cards"])      # card_edit：覆盖对应卡片场景 content
    # 校验须在 pop 前跑：F-B 栅格不变量靠 retimed_segments 键判弹性模式（见 validate_storyboard），
    # pop 后键消失会漏掉该校验。校验失败即 fail-fast（spec §10）。
    storyboard.validate_storyboard(sb)
    # 重排后台词覆写回 rewritten.json（compose 字幕 / deliver SRT 同源新轴）；
    # retimed_segments 不落进 storyboard.json，pop 掉。
    retimed = sb.pop("retimed_segments", None)
    if retimed is not None:
        _dump_json(Path(rewritten_path), retimed)
    out = _dump_json(paths.raw_dir(job.id) / "storyboard.json", sb)
    return {"storyboard_path": str(out), "scene_count": len(sb["scenes"]),
            "warning_count": len(sb.get("warnings", [])),
            "new_duration_s": sb["source"]["duration_s"]}


async def _handle_render(job, session, ctx):
    from app.video.pipeline.remake import renderers
    sb = _load_json(_stage_stats(job, "storyboard")["storyboard_path"])
    scene_dir = paths.out_dir(job.id) / "scenes"
    scene_dir.mkdir(exist_ok=True)
    rendered: list[str] = []
    for scene in sb["scenes"]:
        render = renderers.get_renderer(scene["renderer"])
        p = await render(scene, scene_dir / f"scene_{scene['id']:03d}.mp4",
                         deadline=ctx["deadline"])
        rendered.append(str(p))
    out = _dump_json(paths.raw_dir(job.id) / "rendered_scenes.json", rendered)
    return {"scene_paths_path": str(out), "scene_count": len(rendered)}


async def _handle_compose(job, session, ctx):
    from app.video.pipeline import dubber, muxer
    from app.video.pipeline.remake import composer, tones
    sb = _load_json(_stage_stats(job, "storyboard")["storyboard_path"])
    scene_paths = [Path(p) for p in
                   _load_json(_stage_stats(job, "render")["scene_paths_path"])]
    # rewritten.json 已被 storyboard 覆写成重排后新轴；dub_clips 提供每句自然时长音频
    segments = _load_json(_stage_stats(job, "rewrite")["segments_path"])
    clips_meta = _load_json(_stage_stats(job, "dub")["clips_path"])
    total = float(sb["source"]["duration_s"] or job.duration_seconds or 0)
    silent = await composer.concat_scenes(
        scene_paths, paths.out_dir(job.id) / "remade_silent.mp4",
        deadline=ctx["deadline"])
    # 配音轨：每句音频按重排后 start 落位（弹性时间轴的 start = 新轴）；
    # A3：no_dub 句无音频（path=None）不进配音轨——免责/须知靠卡片画面显示全文。
    dub_clips = [{"index": c["index"], "path": c["path"], "duration": c["duration"],
                  "start": segments[c["index"]]["start"]}
                 for c in clips_meta if not c.get("no_dub")]
    # 【非阻塞红线】pydub 拼配音轨阻塞：下沉线程不占事件循环（与 dub 阶段 build_track 同款）。
    dub_audio = await asyncio.to_thread(
        dubber.build_track, dub_clips, total, paths.tts_dir(job.id) / "dub.wav")
    ball_scenes = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
    # 【非阻塞红线】bilateral_track 是 numpy 合成立体声 + wave 落盘的同步 CPU 段，下沉线程。
    tones_wav = await asyncio.to_thread(
        tones.bilateral_track, ball_scenes, total, paths.tts_dir(job.id) / "tones.wav")
    mixed = await composer.mix_audio(
        dub_audio, tones_wav, paths.tts_dir(job.id) / "remake_mixed.wav",
        deadline=ctx["deadline"])
    out = paths.out_dir(job.id) / "muxed.mp4"
    # A3：no_dub 句不进字幕轨（build_ass 前过滤）——使用须知只在卡片画面显示，不朗读不打轴
    sub_segments = [s for s in segments if not s.get("no_dub")]
    # revision global_param.disclaimer_text：覆盖片头声明文案（未给沿用 REMAKE_DISCLAIMER）
    disclaimer = _load_param_overrides(job)["global"].get("disclaimer_text")
    await composer.compose(silent, mixed, sub_segments, out,
                           use_nvenc=await muxer.probe_nvenc(),
                           total_duration=total, disclaimer=disclaimer,
                           deadline=ctx["deadline"])
    return {"muxed_path": str(out), "tones": tones_wav is not None,
            "scene_count": len(scene_paths)}


# ── 注册进调度器 STAGE_HANDLERS（原地 mutate，保持与 scheduler 引用同一 dict）───────────
STAGE_HANDLERS.update({
    "download": _handle_download,
    "transcript": _handle_transcript,
    "resegment": _handle_resegment,
    "translate": _handle_translate,
    "dub": _handle_dub,
    "mux": _handle_mux,
    "deliver": _handle_deliver,
    "analyze": _handle_analyze,
    "rewrite": _handle_rewrite,
    "storyboard": _handle_storyboard,
    "render": _handle_render,
    "compose": _handle_compose,
})
# 全量注册自检（源 tasks.py:413）：覆盖两种 mode 的全部阶段，漏一个自链会在该阶段 KeyError 断链。
assert set(STAGE_HANDLERS) == set(STAGE_ORDER) | set(REMAKE_STAGE_ORDER), \
    "STAGE_HANDLERS 与两种 mode 的阶段集合不一致"
