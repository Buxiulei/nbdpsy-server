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

M3a 范围：仅 transport 七阶（download..deliver）；shared handler（transcript/dub/deliver）保留其
remake 分支逐字保真，分支内 remake 模块（remake.composer 等）为惰性前向引用，由 M3b 落地。
remake 五阶（analyze/rewrite/storyboard/render/compose）与全量 assert 留 M3b/M3c 补回。
"""
import asyncio
import json
from pathlib import Path

from app.core.config import settings
from app.video import paths
from app.video.scheduler import STAGE_HANDLERS, STAGE_ORDER, stage_order

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


# ── 注册进调度器 STAGE_HANDLERS（原地 mutate，保持与 scheduler 引用同一 dict）───────────
STAGE_HANDLERS.update({
    "download": _handle_download,
    "transcript": _handle_transcript,
    "resegment": _handle_resegment,
    "translate": _handle_translate,
    "dub": _handle_dub,
    "mux": _handle_mux,
    "deliver": _handle_deliver,
})
# M3a 子集自检：transport 七阶必须全部注册（漏一个自链会在该阶段 KeyError 断链）。
# remake 五阶留 M3b；全量 assert（== STAGE_ORDER | REMAKE_STAGE_ORDER）留 M3c 补回。
assert set(STAGE_ORDER) <= set(STAGE_HANDLERS), "transport 七阶 handler 未全部注册"
