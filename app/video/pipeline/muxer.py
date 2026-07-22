"""ffmpeg 合成：音轨替换（copy 不重编码）/ ASS 字幕烧录（单次重编码）/ 音频分层混音。

平移自 video_transport/muxer.py：无 AI 收口、无 config 依赖；全部 ffmpeg/ffprobe/demucs 子进程
本就走 ``create_subprocess_exec``（非阻塞红线达标）。logo 资产随包分发在 ``resources/nbdpsy_logo.png``。
demucs 按设计不进宿主依赖——天然 import/子进程失败即优雅降级为纯配音替换（build_mixed_audio）。
ASS/logo/淡出/立体声 aformat 等逐字保真。
"""
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# WrapStyle: 1 = 行尾自动换行 + 尊重 \N 硬换行（兜底；主要靠 _wrap_zh 手动断行）
# Notice 样式：片头版权声明专用——Alignment 由 build_ass 的 disclaimer_alignment 参数注入
# （默认 8=顶部居中，与底部正文 Default 不冲突；remake 声明页无其他画面故传 5=正中）。
# Outline 3 加粗描边保证任意背景可读，Shadow 0。
# MarginV=60：Alignment 8(顶部) 时是距顶边距；Alignment 5(正中) 时 libass 忽略其垂直分量、
# 文字始终垂直居中，故此值对居中声明无影响，保持一个合理默认即可。
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,{font},60,&H00FFFFFF,&H00000000,&H7F000000,0,2,1,2,60,60,50
Style: Notice,{font},46,&H00FFFFFF,&H00202020,&H64000000,0,3,0,{alignment},60,60,60

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 品牌水印 logo（NBDpsy 完整 logo，随包分发，不裁剪不抠图）与片头版权声明
_LOGO_PATH = Path(__file__).parent / "resources" / "nbdpsy_logo.png"
_LOGO_HEIGHT_RATIO = 0.11    # logo 高 = 视频高 × 该比例（按分辨率等比缩放）
_LOGO_MARGIN_RATIO = 0.037   # 右下角留边 = 视频高 × 该比例
_LOGO_OPACITY = 0.9          # 水印不透明度（整张 logo 卡片统一半透明叠加）
_DISCLAIMER_TEXT = ("本视频由 NBDpsy 心理咨询工作室以学术研究与学习为目的搬运翻译，\n"
                    "如有侵权请联系我们删除")
_DISCLAIMER_SECONDS = 6.0    # 片头声明显示时长（秒）


class MuxError(Exception):
    pass


def _ass_time(seconds: float) -> str:
    # 整数厘秒运算，避免 float 取模在整分钟边界四舍五入出非法时间戳（如 "0:00:60.00"）
    total_cs = int(round(seconds * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


# 软换行优先断点：中英文标点后断行更自然
_ZH_WRAP_PUNCT = "，。！？、；：,.!?;:"


def _wrap_zh(text: str, max_chars: int = 18) -> str:
    """中文字幕断行：libass 对无空格中文不自动换行，长句会溢出画面左右，故手动断行。

    优先在标点(，。！？、；：)后断行；单行超 max_chars 硬断；多行用 ASS 换行符 \\N 连接。
    尊重文本里已有的 \\N（_escape_ass_text 把原文换行转成的）作为硬分段点，逐段再断行——
    因此本函数须在 _escape_ass_text 之后调用，产生的 \\N 不会被再次转义。
    1080p + 60 号字下每行 ~18 个中文字符不溢出。

    排版行首禁则：任何一行都不以标点开头（"。一句话"这种读着很怪）。硬断切在标点前时，
    把标点带回上一行（连续标点最多外扩 2 防跑飞），循环后再兜底把漏网的行首标点并回上一行。
    """
    out: list[str] = []
    for piece in text.split("\\N"):
        remaining = piece
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            cut = max((window.rfind(p) for p in _ZH_WRAP_PUNCT), default=-1)
            # 标点过于靠前会切出零碎短行 → 退回硬断在 max_chars，优先填满整行
            if cut + 1 < max_chars // 2:
                cut = max_chars - 1
            # 行首禁则：下一行不能以标点开头，把标点带回本行（最多外扩 2 个字符）
            for _ in range(2):
                if cut + 1 < len(remaining) and remaining[cut + 1] in _ZH_WRAP_PUNCT:
                    cut += 1
                else:
                    break
            out.append(remaining[:cut + 1])
            remaining = remaining[cut + 1:]
        out.append(remaining)
    # 兜底：极端情况（>2 连续标点 / \N 后紧跟标点）仍可能行首带标点，逐行并回上一行
    for i in range(1, len(out)):
        while out[i] and out[i][0] in _ZH_WRAP_PUNCT:
            out[i - 1] += out[i][0]
            out[i] = out[i][1:]
    return "\\N".join(out)


def _split_screens(wrapped_lines: list[str], max_lines: int = 2) -> list[list[str]]:
    """把 _wrap_zh 折出的行列表按 max_lines 切成多屏，每屏至多 max_lines 行。

    一句折行 >2 行时字幕会占据太多画面高度；切成多屏后在该句配音时长内轮播显示，
    每屏至多两行。纯分组，不改行内容：1 行→1 屏、2 行→1 屏、3 行→2 屏(2+1)、5 行→3 屏(2+2+1)。
    """
    return [wrapped_lines[i:i + max_lines] for i in range(0, len(wrapped_lines), max_lines)]


def build_ass(segments: list[dict], path: Path, *, font: str = "Noto Sans CJK SC",
              disclaimer: str | None = _DISCLAIMER_TEXT,
              disclaimer_seconds: float = _DISCLAIMER_SECONDS,
              disclaimer_alignment: int = 8) -> Path:
    # disclaimer_alignment 注入 Notice 样式的 Alignment（numpad 制：8=顶部居中默认、5=正中）。
    # transport 默认 8，字节不变；remake 声明单独成屏传 5 让文字落画面中央。
    lines = [_ASS_HEADER.format(font=font, alignment=disclaimer_alignment)]
    # 片头版权声明：顶部 Notice 样式，开头 disclaimer_seconds 秒显示，不占用底部正文轨
    if disclaimer and disclaimer_seconds > 0:
        disc = _escape_ass_text(disclaimer.strip())
        lines.append(
            f"Dialogue: 0,{_ass_time(0.0)},{_ass_time(disclaimer_seconds)},"
            f"Notice,,0,0,0,,{disc}\n")
    for seg in segments:
        wrapped = _wrap_zh(_escape_ass_text((seg.get("zh") or "").strip()))
        rows = [ln for ln in wrapped.split("\\N") if ln]
        if not rows:
            continue
        screens = _split_screens(rows, max_lines=2)
        start, end = float(seg["start"]), float(seg["end"])
        # 该句 [start,end] 按各屏字符数比例分配时长：屏间无缝(前屏 end==后屏 start)、
        # 总和=原句时长；按字符数而非均分，避免短屏一闪而过。
        char_counts = [sum(len(ln) for ln in scr) for scr in screens]
        if sum(char_counts) == 0:  # 退化(全空行，不应发生)→均分兜底，防除零
            char_counts = [1] * len(screens)
        grand = sum(char_counts)
        bounds = [start]
        cum = 0
        for c in char_counts:
            cum += c
            bounds.append(start + (end - start) * cum / grand)
        bounds[-1] = end  # 纠正浮点累积误差，末屏精确落在原句 end
        for i, scr in enumerate(screens):
            text = "\\N".join(scr)
            lines.append(
                f"Dialogue: 0,{_ass_time(bounds[i])},{_ass_time(bounds[i + 1])},"
                f"Default,,0,0,0,,{text}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")
    return Path(path)


async def _run_ffmpeg(argv: list[str], *, timeout: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *argv,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise MuxError(f"ffmpeg 超时({timeout}s)")
    if proc.returncode != 0:
        raise MuxError(f"ffmpeg 失败: {stderr.decode(errors='replace')[-500:]}")


async def probe_nvenc() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=1",
        "-c:v", "h264_nvenc", "-frames:v", "1", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        # 全局约束：ffmpeg 子进程一律 wait_for 超时；探测超时=不可用，回退 libx264 是安全默认
        await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0


async def _run_demucs(audio_path: Path, out_root: Path, *, timeout: float) -> Path | None:
    """demucs 分离人声/伴奏，返回伴奏 no_vocals.wav；不可用/失败/超时返回 None（调用方降级）。

    --two-stems=vocals 只分 vocals / no_vocals（比四轨快），GPU 自动使用。
    以 sys.executable 起子进程（= 当前 venv 解释器，demucs 装在同一 venv 才可用）。
    输出固定在 {out_root}/htdemucs/{audio_stem}/no_vocals.wav。
    """
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
        "-o", str(out_root), str(audio_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning("demucs 分离超时(%ss)，降级为纯配音替换", timeout)
        return None
    if proc.returncode != 0:
        logger.warning("demucs 分离失败(rc=%s)，降级为纯配音替换: %s",
                       proc.returncode, stderr.decode(errors="replace")[-300:])
        return None
    no_vocals = out_root / "htdemucs" / audio_path.stem / "no_vocals.wav"
    if not no_vocals.exists():
        logger.warning("demucs 输出缺 no_vocals.wav（%s），降级为纯配音替换", no_vocals)
        return None
    return no_vocals


async def build_mixed_audio(video_path: Path, dub_audio: Path, out_audio: Path, *,
                            bgm_volume: float = 0.25, deadline: float | None = None) -> Path:
    """三层音频：原视频伴奏(去人声) + 中文配音 → 混音 wav，返回用于 mux 的音频路径。

    1. 从原视频抽音轨(-vn)  2. demucs 去人声取伴奏  3. ffmpeg amix 混音
       （配音为 input0 主导时长、音量 1.0；伴奏 input1 当氛围垫、音量 bgm_volume）。
    任一步不可用/失败都优雅降级：返回原配音 dub_audio（退回整轨替换，不 fail 整个 mux）。
    """
    import time

    def _budget(default: float) -> float:
        # 各子步骤超时按 deadline 剩余预算切；demucs 给足（GPU ~30-90s/4min 音频，兜底 600s）
        if deadline is None:
            return default
        return max(30.0, min(default, deadline - time.monotonic()))

    work = Path(out_audio).parent
    orig_audio = work / "orig_audio.wav"

    # 1. 抽原音轨（原视频无音轨 → ffmpeg 失败 → 无从分离，降级）
    try:
        await _run_ffmpeg(["-i", str(video_path), "-vn", "-ac", "2", "-ar", "44100",
                           str(orig_audio)], timeout=_budget(300.0))
    except MuxError as exc:
        logger.warning("抽原音轨失败，降级为纯配音替换: %s", exc)
        return Path(dub_audio)

    # 2. demucs 去人声取伴奏
    no_vocals = await _run_demucs(orig_audio, work, timeout=_budget(600.0))
    if no_vocals is None:
        return Path(dub_audio)

    # 3. amix：配音(1.0) + 伴奏(bgm_volume) → mixed；duration=first 让配音主导时长。
    #    normalize=0 关掉 amix 默认的 1/N 均衡缩放——否则配音会被压掉 6dB，与"配音音量 1.0"
    #    的意图相悖；伴奏已缩到 bgm_volume 当氛围垫，二者相加基本不触顶。
    try:
        await _run_ffmpeg([
            "-i", str(dub_audio), "-i", str(no_vocals), "-filter_complex",
            f"[1]volume={bgm_volume}[bg];"
            f"[0][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0",
            "-ar", "44100", str(out_audio)], timeout=_budget(300.0))
    except MuxError as exc:
        logger.warning("amix 混音失败，降级为纯配音替换: %s", exc)
        return Path(dub_audio)
    return Path(out_audio)


async def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    """探测视频宽高（用于按分辨率等比缩放 logo）；探测失败/超时回退 1920x1080。"""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
        str(video_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        w, h = out.decode().strip().split("x")
        return int(w), int(h)
    except asyncio.TimeoutError:
        proc.kill()
        return 1920, 1080
    except Exception:
        return 1920, 1080


async def mux(video_path: Path, dub_audio: Path, out_path: Path, *,
              ass_path: Path | None, use_nvenc: bool,
              logo_path: Path | None = _LOGO_PATH,
              fade_out_seconds: float | None = None,
              total_seconds: float | None = None,
              deadline: float | None = None) -> None:
    """替换音轨 + 可选烧录字幕 + 可选右下角 logo 水印 + 可选末尾淡出。

    字幕/logo/淡出都为空时走 `-c:v copy` 快路径（不重编码）；任一存在则组滤镜链重编码：
    字幕(subtitles)先烧，logo 缩放+调透明度后 overlay 到右下角。

    A6 末尾淡出：给出 fade_out_seconds(>0) 与 total_seconds 时，在字幕/logo 之后的最终
    视频流叠加 fade=t=out:st={total-fade}:d={fade}、配音轨叠加同参 afade=t=out——淡出落在
    最终流上。两参任一缺省即不淡出（transport 链默认 None → 行为完全不变）。
    """
    import time
    timeout = 1800.0
    if deadline is not None:
        timeout = max(60.0, min(timeout, deadline - time.monotonic()))

    logo = Path(logo_path) if (logo_path and Path(logo_path).exists()) else None
    if logo is None and logo_path:
        logger.warning("logo 文件缺失(%s)，跳过水印叠加", logo_path)

    fade = float(fade_out_seconds) if (fade_out_seconds and fade_out_seconds > 0
                                       and total_seconds is not None) else None

    argv = ["-i", str(video_path), "-i", str(dub_audio)]
    if ass_path is None and logo is None and fade is None:
        # 无字幕无水印无淡出 → 视频流原样 copy（最快，不重编码）
        argv += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
    else:
        vlabel = "0:v"
        fc: list[str] = []
        if ass_path is not None:
            # subtitles 滤镜的 filename 参数转义：' → '\'' ，: → \:
            esc = str(ass_path).replace("'", r"'\''").replace(":", r"\:")
            fc.append(f"[{vlabel}]subtitles=filename='{esc}'[vsub]")
            vlabel = "vsub"
        if logo is not None:
            argv += ["-i", str(logo)]   # 第 3 路输入（index 2）
            _, h = await _probe_dimensions(Path(video_path))
            lh = max(2, round(h * _LOGO_HEIGHT_RATIO))
            m = max(1, round(h * _LOGO_MARGIN_RATIO))
            fc.append(f"[2:v]scale=-2:{lh},format=rgba,"
                      f"colorchannelmixer=aa={_LOGO_OPACITY}[lg]")
            fc.append(f"[{vlabel}][lg]overlay=W-w-{m}:H-h-{m}[vout]")
            vlabel = "vout"
        alabel = "1:a:0"
        if fade is not None:
            # A6：末尾淡出叠加在字幕/logo 之后的最终视频流 + 配音轨上
            st = max(0.0, float(total_seconds) - fade)
            fc.append(f"[{vlabel}]fade=t=out:st={st:g}:d={fade:g}[vfade]")
            vlabel = "vfade"
            fc.append(f"[1:a]afade=t=out:st={st:g}:d={fade:g}[aout]")
            alabel = "[aout]"
        argv += ["-filter_complex", ";".join(fc),
                 "-map", f"[{vlabel}]", "-map", alabel]
        argv += ["-c:v", "h264_nvenc", "-preset", "p4"] if use_nvenc \
            else ["-c:v", "libx264", "-preset", "veryfast"]
    argv += ["-c:a", "aac", "-b:a", "128k", str(out_path)]
    await _run_ffmpeg(argv, timeout=timeout)
