"""转录来源链：人工字幕 > 自动字幕 > ASR。

YouTube 自动字幕是滚动式（相邻块重复上一行），解析时按行去重。

平移自 video_transport/transcript.py：ASR 收口从 ``get_asr(...).transcribe(url, language="en")``
换成薄 provider ``asr_transcribe(url)``——后者直接返回 ``[{start,end,text}]``（无 ``.segments`` 包装、
language 由 provider 内部固定 en），故此处消费面去掉 ``.segments`` 与 language 参数。paths 走
``app.video.paths``，``_run_ffmpeg`` 复用同包 muxer。
"""
import asyncio
import logging
import re
import shutil
from pathlib import Path

from app.video import paths
from app.video.pipeline.muxer import _run_ffmpeg
from app.video.providers import asr_transcribe

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]+>")

# 相邻 cue 间隔 > 60.0s 判为字幕空窗（YouTube VTT 常整段漏掉球段口播提问）
_GAP_ASR_THRESHOLD = 60.0
# 空窗切段送 ASR 的约束：单段 ≤300s、段间重叠 2s 防切句
_ASR_SEG_MAX = 300.0
_ASR_SEG_OVERLAP = 2.0
# 字幕优先：ASR 段与既有 cue 时间重叠占 ASR 段时长 >50% 则丢弃（冗余）
_CUE_OVERLAP_DROP = 0.5


def _ts(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(path: Path) -> list[dict]:
    segments: list[dict] = []
    seen_lines: list[str] = []          # 滚动去重窗口（最近 2 行）
    cur: dict | None = None
    for raw_line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        m = _TIME_RE.search(line)
        if m:
            cur = {"start": _ts(*m.groups()[:4]), "end": _ts(*m.groups()[4:]), "text": ""}
            continue
        if cur is None or not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        text = _TAG_RE.sub("", line).replace("&nbsp;", " ").strip()
        text = re.sub(r"\s+", " ", text)
        if not text or text in seen_lines:
            continue
        seen_lines.append(text)
        seen_lines = seen_lines[-2:]
        segments.append({"start": cur["start"], "end": cur["end"], "text": text})
    return segments


async def extract_audio(video_path: Path, out_m4a: Path) -> Path:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "copy", str(out_m4a),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    if proc.returncode != 0:
        # m4a copy 不兼容时回退 aac 转码
        proc2 = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(video_path), "-vn", "-c:a", "aac", str(out_m4a),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=900)
        if proc2.returncode != 0:
            raise RuntimeError(f"音频抽取失败: {stderr2.decode(errors='replace')[-300:]}")
    return out_m4a


def detect_gaps(cues: list[dict], duration: float,
                *, threshold: float = _GAP_ASR_THRESHOLD) -> list[tuple[float, float]]:
    """纯函数：找出字幕的空窗区间（相邻 cue 间隔严格大于阈值）。

    含头尾检测——0→首 cue.start、末 cue.end→duration 同样参与；
    无任何 cue 时整片视为一个空窗（全交给 ASR）。
    """
    if not cues:
        return [(0.0, float(duration))] if duration and duration > 0 else []
    ordered = sorted(cues, key=lambda c: c["start"])
    gaps: list[tuple[float, float]] = []
    # 头部：视频开头到首个 cue
    if ordered[0]["start"] > threshold:
        gaps.append((0.0, ordered[0]["start"]))
    # 相邻 cue 之间
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt["start"] - prev["end"] > threshold:
            gaps.append((prev["end"], nxt["start"]))
    # 尾部：末个 cue 到视频结尾（duration 未知则跳过尾检测）
    if duration and duration - ordered[-1]["end"] > threshold:
        gaps.append((ordered[-1]["end"], float(duration)))
    return gaps


def _split_gap(g_start: float, g_end: float) -> list[tuple[float, float]]:
    """长空窗切成 ≤300s 的段，段间重叠 2s 防止在句子中间切断。"""
    if g_end - g_start <= _ASR_SEG_MAX:
        return [(g_start, g_end)]
    chunks: list[tuple[float, float]] = []
    c_start = g_start
    while c_start < g_end:
        c_end = min(c_start + _ASR_SEG_MAX, g_end)
        chunks.append((c_start, c_end))
        if c_end >= g_end:
            break
        c_start = c_end - _ASR_SEG_OVERLAP
    return chunks


def _overlaps_existing(seg: dict, cues: list[dict]) -> bool:
    """ASR 段与任一既有 cue 的时间重叠是否超过 ASR 段自身时长的 50%。"""
    dur = seg["end"] - seg["start"]
    if dur <= 0:
        return False
    for c in cues:
        overlap = min(seg["end"], c["end"]) - max(seg["start"], c["start"])
        if overlap > 0 and overlap / dur > _CUE_OVERLAP_DROP:
            return True
    return False


async def _retry(fn, *args, attempts: int = 4, **kwargs):
    """指数退避重试：末次失败前依次 sleep 2s → 6s → 18s（三档全生效）。

    复制自 dubber._retry 同款模式（不跨文件 import 私有函数）。attempts=4 = 1 次原调用
    + 3 次重试，专治 DashScope ASR / ffmpeg 抽音频的瞬时抖动。调用体幂等（wav 同名覆盖、
    transcribe 无副作用），重试安全。
    """
    delay = 2.0
    for attempt in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 3                             # 2s → 6s → 18s 指数退避


async def _asr_one_gap(gap_dir: Path, gap_idx: int, video_path: Path,
                       g_start: float, g_end: float) -> list[dict]:
    """单个空窗：切段抽 16kHz mono wav → ASR → 时间戳平移回全片轴。"""
    segs: list[dict] = []
    for ci, (c_start, c_end) in enumerate(_split_gap(g_start, g_end)):
        wav = gap_dir / f"gap{gap_idx}_{ci}.wav"

        async def _extract_and_transcribe():
            # ffmpeg 子进程带超时（复用 muxer._run_ffmpeg）；-ss 前置做快速输入定位
            await _run_ffmpeg(
                ["-ss", f"{c_start:.3f}", "-i", str(video_path),
                 "-t", f"{c_end - c_start:.3f}",
                 "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                timeout=600)
            # paraformer 只接公网 URL：wav 落在 uploads 下天然被 /uploads 服务
            return await asr_transcribe(paths.to_absolute_url(wav))

        # 段级重试：抽音频 + ASR 一并覆盖，瞬时故障指数退避重试而非整段丢弃
        result = await _retry(_extract_and_transcribe)
        # asr_transcribe 直接返回 [{start,end,text}]（薄 provider 无 .segments 包装）
        for s in result:
            segs.append({"start": float(s["start"]) + c_start,
                         "end": float(s["end"]) + c_start,
                         "text": s["text"]})
    return segs


async def fill_gaps_with_asr(job_id: int, cues: list[dict],
                             gaps: list[tuple[float, float]], video_path: Path,
                             *, deadline: float | None = None) -> tuple[list[dict], list[str]]:
    """空窗逐段送 ASR 补漏，与字幕 cue 合并排序去重后返回 (segments, warnings)。

    单个空窗失败仅跳过并记 warning；全部空窗失败则抛错（transcript 阶段整体失败）。
    deadline 仅作接口透传——ffmpeg 与 ASR 调用各自已带超时。
    """
    if not gaps:
        return list(cues), []
    gap_dir = paths.raw_dir(job_id) / "asr_gaps"
    gap_dir.mkdir(parents=True, exist_ok=True)
    recovered: list[dict] = []
    warnings: list[str] = []
    success = 0
    try:
        for gi, (g_start, g_end) in enumerate(gaps):
            try:
                gap_segs = await _asr_one_gap(
                    gap_dir, gi, video_path, g_start, g_end)
                recovered.extend(gap_segs)
                success += 1
                logger.info(                       # 成功恢复留痕：区间 + 恢复段数
                    "job %s 字幕空窗 %.1f-%.1fs 的 ASR 补漏成功恢复 %d 段",
                    job_id, g_start, g_end, len(gap_segs))
            except Exception as exc:  # 段级重试仍失败不致命：跳过并记 warning
                # 诊断：某些异常 str() 为空，用 type+repr 保证信息非空可查根因
                # stats 只入库标量计数（_slim 丢列表），明细靠日志留痕不再依赖 stats
                diag = f"{type(exc).__name__}: {exc!r}"
                logger.warning(
                    "job %s 字幕空窗 %.1f-%.1fs 的 ASR 补漏失败，已跳过：%s",
                    job_id, g_start, g_end, diag)
                warnings.append(
                    f"字幕空窗 {g_start:.1f}-{g_end:.1f}s 的 ASR 补漏失败，已跳过：{diag}")
        if success == 0:
            raise RuntimeError(f"全部 {len(gaps)} 处字幕空窗 ASR 补漏均失败")
    finally:
        shutil.rmtree(gap_dir, ignore_errors=True)
    # 字幕优先：与既有 cue 重叠 >50% 的 ASR 段丢弃，其余并入后按起点排序
    kept = [s for s in recovered if not _overlaps_existing(s, cues)]
    merged = sorted([*cues, *kept], key=lambda s: s["start"])
    return merged, warnings


async def ensure_transcript(job_id: int, download_result: dict,
                            *, deadline: float | None = None,
                            fill_gaps: bool = True) -> dict:
    """转录来源链。fill_gaps=False（transport 带字幕路径）时纯字幕零 ASR，恢复旧行为；
    仅 remake 需要 fill_gaps=True 补漏球段口播空窗（门控见 _handle_transcript）。"""
    sub = download_result.get("subtitle_path")
    if sub and Path(sub).exists():
        segments = parse_vtt(Path(sub))
        if segments:
            if not fill_gaps:                    # transport：采信外部字幕，不做空窗 ASR 补漏
                return {"segments": segments,
                        "source": download_result["subtitle_source"]}
            # remake：采信外部字幕后做空窗补漏（YouTube VTT 常整段漏掉球段口播提问）
            duration = float(download_result.get("info", {}).get("duration") or 0) \
                or segments[-1]["end"]
            gaps = detect_gaps(segments, duration)
            merged, warnings = await fill_gaps_with_asr(
                job_id, segments, gaps, Path(download_result["video_path"]),
                deadline=deadline)
            return {"segments": merged,
                    "source": download_result["subtitle_source"],
                    "gap_asr_segments": len(merged) - len(segments),
                    "warnings": warnings}
    # ASR 兜底：抽音频 → 公网 URL → paraformer
    audio = await extract_audio(Path(download_result["video_path"]),
                                paths.raw_dir(job_id) / "audio.m4a")
    segments = await asr_transcribe(paths.to_absolute_url(audio))
    return {"segments": segments, "source": "asr"}
