"""逐句 TTS + 全片统一语速同步 + pydub 拼轨。

同步铁律（不可妥协）：语速全片必须统一。提前说完留空白；没说完整体顺延；
调语速只能全文统一调（绝无逐句变速）；尽可能保音画同步，尾部允许溢出。

算法两遍合成：
  第一遍全部 rate=1.0 测各句实际时长 → 决策全局统一 rate（二分）→
  必要时第二遍全部同 rate 重合成。时间轴由纯函数 plan_timeline 规划：
  说得快落回原轴留白，说不完从 cur 顺延，顺延后遇原轴大间隙自动追回。

平移自 video_transport/dubber.py。换 import 面 + 非阻塞红线适配：
- TTS 收口从 ``get_tts(...).synthesize(...)->TtsResult`` 换成薄 provider
  ``tts_synthesize(text, voice=, out_path=, rate=)->float``（直接返回时长秒，故薄适配去掉
  ``.duration_seconds``）；paths 走 ``app.video.paths``。
- 单进程 asyncio worker 下 pydub 解码为同步阻塞段：``_synth_pass`` 读已存 wav 时长经
  ``asyncio.to_thread`` 下沉线程（不阻塞事件循环）。``build_track`` 保持同步纯函数——由 handler
  层 ``asyncio.to_thread(build_track, ...)`` 调（stages.py）。二分统一语速/plan_timeline/hash 命名逐行保真。
"""
import asyncio
import hashlib
import math
import time
from pathlib import Path

from pydub import AudioSegment

from app.video import paths
from app.video.providers import tts_synthesize

_GAP = 0.12         # 句间最小呼吸间隙（秒）：零间隙紧贴播不自然，也给「句间绝不重叠」留正裕度
_MAX_DRIFT = 1.5    # 单句实际 start 与原轴 start 的最大允许偏移（秒）：顺延漂移上限，超则抬语速压回


def _name_by_rate(i: int, seg: dict, rate: float) -> str:
    """transport 命名：下标 + 语速标记。两遍合成（rate 不同）互不覆盖、同 rate 幂等。"""
    return f"{i:05d}_r{rate:.2f}.wav"


def _name_by_hash(i: int, seg: dict, rate: float) -> str:
    """remake 命名：下标 + zh 文本 md5 短串。

    同文本跨 job 命中继承来的 tts 缓存不重合成；改文/增句因 hash 变化自然失效重合成。
    """
    digest = hashlib.md5(seg["zh"].encode("utf-8")).hexdigest()[:8]
    return f"{i:05d}_{digest}.wav"


def _wav_duration(path: str) -> float:
    """读 wav 时长（秒）。pydub 解码同步阻塞，供 _synth_pass 经 to_thread 下沉调用。"""
    return AudioSegment.from_wav(path).duration_seconds


def _max_drift(plan: list[dict], starts: list[float]) -> float:
    """全片最大单句漂移 = max(实际 start - 原轴 start)。

    plan 与 starts 等长；start_i >= 原轴 start（由 plan_timeline 的 max 保证），故漂移非负。
    """
    return max((p["start"] - s for p, s in zip(plan, starts)), default=0.0)


def plan_timeline(starts: list[float], durations: list[float],
                  rate: float = 1.0, gap: float = _GAP) -> tuple[list[dict], float]:
    """规划每句实际配音时间轴（纯函数，同步算法核心）。

    Args:
        starts:    各句原始字幕起点（秒，原轴）
        durations: 各句在 rate=1.0 下测得的实际配音时长（秒）
        rate:      全片统一语速倍率；实际时长 = durations[i] / rate
                   （二分决策时用 rate 模拟；最终规划传实际 rate 下时长 + rate=1.0）
        gap:       句间最小呼吸间隙（秒）

    规则（顺序扫描，cur 为上一句结束时间水位）：
        start_i = max(原轴 start, cur + gap)（首句下限为 0）：
          - 说得快：上一句在原轴内说完，cur+gap 未越过本句原轴 → start_i 落回原轴，句间留白
          - 说不完：cur+gap 越过本句原轴 → start_i 顺延到 cur+gap，整体后移
          - 顺延后遇原轴大间隙：max 取回原轴，自动追回同步

    **不变量（句间绝不重叠）**：对所有 i>=1，start_i >= end_(i-1) + gap ——
    由 max(原轴, cur+gap) 数学保证。build_track 按 start overlay，故成片零重叠。

    Returns:
        (plan, cur_final)，plan = [{"start": start_i, "end": end_i}, ...]，
        cur_final 为末句结束时间（用于判断是否超总时长）。
    """
    cur = 0.0
    plan: list[dict] = []
    for i, (start, dur) in enumerate(zip(starts, durations)):
        floor = 0.0 if i == 0 else cur + gap    # 首句不被幻影前句推后
        start_i = max(start, floor)
        end_i = start_i + dur / rate
        plan.append({"start": round(start_i, 3), "end": round(end_i, 3)})
        cur = end_i
    return plan, cur


def _decide_rate(starts: list[float], durations: list[float],
                 total_limit: float, max_rate: float,
                 gap: float = _GAP, max_drift: float = _MAX_DRIFT) -> tuple[float, str | None]:
    """全局统一语速决策（纯函数），选出的仍是单一全局 rate，全片同速（铁律不破）。

    可行谓词 = 末句 cur_final <= total_limit **且** 峰值漂移 <= max_drift。
    rate 越高：cur_final 与漂移都单调减小 → 谓词单调，可二分。

    rate=1.0 足够松（总时长含 0.5s 宽容 + 漂移达标）→ 全片 1.0，不重合成；
    否则二分 rate∈[1.0, max_rate]（5 轮，精度~0.01）找同时满足两条件的最小 rate；
    max_rate 仍不满足（总时长溢出或漂移超标）→ 取 max_rate 并记 warning
    （语速绝不超上限，接受残余溢出/漂移）。句间 gap 累积占总时长，一并计入。

    Returns:
        (rate, warning)。warning 仅在 max_rate 也不满足时非 None。
    """
    def feasible(rate: float, *, slack: float = 0.0) -> bool:
        plan, cur_final = plan_timeline(starts, durations, rate=rate, gap=gap)
        return cur_final <= total_limit + slack and _max_drift(plan, starts) <= max_drift

    if feasible(1.0, slack=0.5):
        return 1.0, None

    if not feasible(max_rate):
        plan, cur_final = plan_timeline(starts, durations, rate=max_rate, gap=gap)
        md = _max_drift(plan, starts)
        return max_rate, (f"全片最大语速 {max_rate:.2f} 仍不满足："
                          f"末句 {cur_final:.1f}s/上限 {total_limit:.1f}s、"
                          f"峰值漂移 {md:.1f}s/上限 {max_drift:.1f}s，接受残余")

    lo, hi = 1.0, max_rate                    # lo 不满足、hi 满足，二分收敛 hi 到边界
    for _ in range(5):
        mid = (lo + hi) / 2
        if feasible(mid):
            hi = mid
        else:
            lo = mid
    # 向上取整到 0.01 保证取整后仍满足；再钳回 max_rate 不越界
    return min(math.ceil(hi * 100) / 100, max_rate), None


async def _synth_pass(translated: list[dict], ttsdir: Path, voice: str,
                      rate: float, sem: asyncio.Semaphore,
                      deadline: float | None, namer=_name_by_rate) -> list[float]:
    """一遍全量合成（全文统一 rate），返回各句实际时长列表。

    并发限速 Semaphore(4)；文件名由 namer 决定（transport 带 rate 标记两遍互不覆盖，
    remake 带 zh 文本 hash 跨 job 命中缓存），同名文件已存在则读时长跳过（断点续跑幂等）。
    失败走 _retry 指数退避。
    """
    durations: list[float | None] = [None] * len(translated)

    async def _one(i: int, seg: dict):
        if seg.get("no_dub"):                      # 免责/须知句不配音，时长 0 占位保持下标对齐
            durations[i] = 0.0
            return
        out = ttsdir / namer(i, seg, rate)
        async with sem:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("dub 阶段预算耗尽")
            if out.exists():                       # 幂等：断点续跑跳过（pydub 解码下沉线程）
                durations[i] = await asyncio.to_thread(_wav_duration, str(out))
                return
            # tts_synthesize 直接返回时长秒（薄适配：源 TtsResult.duration_seconds → float）
            durations[i] = await _retry(tts_synthesize, seg["zh"], voice=voice,
                                        rate=rate, out_path=str(out))

    await asyncio.gather(*[_one(i, s) for i, s in enumerate(translated)])
    return [d if d is not None else 0.0 for d in durations]


async def synthesize_all(job_id: int, translated: list[dict], *, voice: str,
                         max_rate: float, video_duration: float,
                         deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    """两遍合成 + 全片统一语速规划，返回 (clips, adjusted_segments)。

    第一遍全部 rate=1.0 测时长 → 决策全局统一 rate → 若需压缩则第二遍全部同 rate 重合成。
    时间轴由 plan_timeline 规划（留白 / 顺延 / 追回）。

    Returns:
        clips: [{"index","path","duration","start"(实际轴),"rate","warning"}]
        adjusted_segments: translated 副本，start/end 改为实际配音轴，
                           原轴存 orig_start/orig_end（幂等：重入用 orig 轴规划不二次顺延）。
    """
    ttsdir = paths.tts_dir(job_id)
    sem = asyncio.Semaphore(4)                     # 并发限速，两遍都用

    # 原轴：优先取 orig_start（重入 adjusted.json 时避免二次顺延），否则原始 start
    starts = [seg.get("orig_start", seg["start"]) for seg in translated]

    durations = await _synth_pass(translated, ttsdir, voice, 1.0, sem, deadline)
    total_limit = float(video_duration or 0) or \
        max((seg.get("orig_end", seg["end"]) for seg in translated), default=0.0)
    rate, warning = _decide_rate(starts, durations, total_limit, max_rate)

    if rate > 1.005:                               # 需压缩：全部同 rate 重合成
        durations = await _synth_pass(translated, ttsdir, voice, rate, sem, deadline)

    # durations 已是实际 rate 下时长，最终规划不再除 rate
    plan, _ = plan_timeline(starts, durations)
    clips: list[dict] = []
    adjusted: list[dict] = []
    for i, seg in enumerate(translated):
        out = ttsdir / _name_by_rate(i, seg, rate)
        clips.append({"index": i, "path": str(out), "duration": durations[i],
                      "start": plan[i]["start"], "rate": rate,
                      "warning": warning if i == 0 else None})
        adjusted.append({**seg,
                         "orig_start": seg.get("orig_start", seg["start"]),
                         "orig_end": seg.get("orig_end", seg["end"]),
                         "start": plan[i]["start"], "end": plan[i]["end"]})
    return clips, adjusted


async def synthesize_natural(job_id: int, segments: list[dict], *, voice: str,
                             deadline: float | None = None) -> list[dict]:
    """自然合成模式（remake wave5）：全部句子 rate=1.0 合成，只测自然时长。

    弹性时间轴接管排布——本函数**不做** _decide_rate / plan_timeline / adjusted 轴回写，
    只复用 _synth_pass 的并发限速 + 重试 + 同名幂等（断点续跑跳过已合成句）。
    时间轴重排交给 remake.timeline.relayout（storyboard 阶段），配音轨在 compose 阶段建。

    Returns:
        clips: [{"index","path","duration"}]（自然时长；无 rate/start/warning 字段）
    """
    ttsdir = paths.tts_dir(job_id)
    sem = asyncio.Semaphore(4)                          # 并发限速
    # remake 走 zh 文本 hash 命名：未改句跨 job 命中继承缓存跳过，改文/增句自然失效重合成
    durations = await _synth_pass(segments, ttsdir, voice, 1.0, sem, deadline,
                                  namer=_name_by_hash)
    # no_dub 句不合成 → 占位 clip（duration=0/path=None，带 no_dub 标记供下游过滤），
    # 保持 clips 与 segments 逐句下标对齐（storyboard clip_durations / compose 按 index 对位）。
    clips: list[dict] = []
    for i, seg in enumerate(segments):
        if seg.get("no_dub"):
            clips.append({"index": i, "path": None, "duration": 0.0, "no_dub": True})
        else:
            clips.append({"index": i, "path": str(ttsdir / _name_by_hash(i, seg, 1.0)),
                          "duration": durations[i]})
    return clips


async def _retry(fn, *args, attempts: int = 4, **kwargs):
    """指数退避重试：末次失败前依次 sleep 2s → 6s → 18s（三档全生效）。

    attempts=4：TTS 偶发失败多一档重试更 robust；合成幂等（同 out_path 覆盖），重试无副作用。
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


def build_track(clips: list[dict], total_duration: float, out_wav: Path) -> Path:
    """pydub 拼轨：24kHz mono 静音底 + 按 start 偏移 overlay 每句配音。

    底长 = max(total_duration, 末句实际 end + 0.3)——顺延溢出时不截语音。
    同步纯函数：handler 层经 ``asyncio.to_thread`` 调用（pydub 解码/导出阻塞，不占事件循环）。
    """
    last_end = max((c["start"] + c["duration"] for c in clips), default=0.0)
    track_len = max(total_duration, last_end + 0.3)
    track = AudioSegment.silent(duration=int(track_len * 1000), frame_rate=24000)
    track = track.set_channels(1).set_sample_width(2)
    for clip in sorted(clips, key=lambda c: c["start"]):
        seg = AudioSegment.from_wav(clip["path"])
        track = track.overlay(seg, position=int(clip["start"] * 1000))
    track.export(str(out_wav), format="wav")
    return Path(out_wav)
