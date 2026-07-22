"""成片合成（spec §8）：场景 concat + 人声/提示音混音 + 字幕/logo/片头声明。

字幕与 logo 复用 muxer（build_ass 的 disclaimer 参数注入 remake 专用文案，muxer 零改动）。
"""
import time
from pathlib import Path

from app.video.pipeline import muxer
from app.video.pipeline.muxer import _run_ffmpeg

# 片头声明（烧进视频，Global Constraints 逐字文案）
REMAKE_DISCLAIMER = ("本视频由 NBDpsy 心理咨询工作室制作，练习设计参考国际公开 EMDR 自助资料，\n"
                     "不构成医疗建议，如有需要请咨询专业人员")
# 发布简介 attribution（进 meta.json，不烧视频）
ATTRIBUTION = "练习设计参考国际公开的 EMDR 双侧刺激自助方法"
# A6 末尾淡出时长（秒）：全片最后 3s 画面 fade=out + 音频 afade=out
_FADE_OUT_SECONDS = 3.0


def _budget(deadline: float | None, default: float) -> float:
    if deadline is None:
        return default
    return max(60.0, min(default, deadline - time.monotonic()))


async def concat_scenes(scene_paths: list[Path], out_path: Path, *,
                        deadline: float | None = None) -> Path:
    """concat demuxer 无损拼接（各段同规格，Task 3/4 输出契约保证）。"""
    out_path = Path(out_path)
    listfile = out_path.with_suffix(".concat.txt")
    lines = []
    for p in scene_paths:
        esc = str(Path(p).resolve()).replace("'", r"'\''")
        lines.append(f"file '{esc}'\n")
    listfile.write_text("".join(lines), encoding="utf-8")
    await _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile),
                       "-c", "copy", str(out_path)],
                      timeout=_budget(deadline, 600.0))
    return out_path


async def mix_audio(dub_wav: Path, tones_wav: Path | None, out_wav: Path, *,
                    deadline: float | None = None) -> Path:
    """人声 + 双侧提示音混音；无提示音直接用配音轨。

    normalize=0 关掉 amix 默认均衡缩放（否则配音被压 6dB，搬运链已踩过）。
    duration=first 让配音轨主导总时长。
    dub 轨是 24kHz 单声道（dubber.build_track set_channels(1)），若直接与立体声提示音
    amix，声道协商会按第一路 mono 布局把提示音降混——EMDR 左右交替声道会塌成单声道。
    故先把配音轨 aformat 升成 stereo 再混，输出侧再补 -ac 2 锚定，保住左右分声道。
    """
    if tones_wav is None:
        return Path(dub_wav)
    await _run_ffmpeg([
        "-i", str(dub_wav), "-i", str(tones_wav), "-filter_complex",
        "[0]aformat=channel_layouts=stereo[d];"
        "[d][1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0",
        "-ar", "44100", "-ac", "2", str(out_wav)],
        timeout=_budget(deadline, 300.0))
    return Path(out_wav)


async def compose(video: Path, audio: Path, segments: list[dict],
                  out_path: Path, *, use_nvenc: bool,
                  total_duration: float | None = None,
                  disclaimer: str | None = None,
                  deadline: float | None = None) -> None:
    """concat 后的无声成片 + 混音轨 → 烧字幕 + logo + remake 片头声明 + 末尾淡出。

    A6：给出 total_duration(>0) 时，muxer 在字幕/logo 之后的最终视频/音频流末尾叠加 3s
    淡出（fade=out / afade=out，st=总时长-3）；不给时不淡出（行为不变）。
    revision global_param.disclaimer_text 覆盖片头声明文案，未给沿用 REMAKE_DISCLAIMER。
    """
    # F1：声明页无其他画面，文字必须落画面正中——disclaimer_alignment=5（numpad 正中），
    # 而非默认 8（顶部居中，用户实拍反馈顶端显得空）。
    ass = muxer.build_ass(segments, Path(out_path).with_suffix(".ass"),
                          disclaimer=disclaimer or REMAKE_DISCLAIMER,
                          disclaimer_alignment=5)
    fade = _FADE_OUT_SECONDS if (total_duration and total_duration > 0) else None
    await muxer.mux(Path(video), Path(audio), Path(out_path),
                    ass_path=ass, use_nvenc=use_nvenc,
                    fade_out_seconds=fade, total_seconds=total_duration,
                    deadline=deadline)
