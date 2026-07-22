"""语音优先的弹性时间轴重排引擎（wave5 核心质量模块，纯函数）。

用户裁决：remake 是完全重制（画面全自产），**不必保持原片时长**。现状两个病：
台词被钉回原片时间点 → 句间空等 2-5s；全局压语速塞原时长 → 语速观感不一。
本模块把时间轴交给「自然语音」主导——每句按 rate=1.0 的自然时长排布，句间用固定
自然停顿，功能性留白（原片刻意的长静默）保留，练习本体（球段）时长一分不动。

归块：每句按 orig_start 落入某个 facts 场景区间。按 facts 场景顺序推进游标：
  - ball 块：练习本体，新时长=原时长；块内句子保持相对锚定；
  - card 块：句子顺排（首句提前量 LEAD，句间 GAP，功能性留白按原间隔保留），
             块尾留白 TAIL，块最短 MIN_CARD_S。
块边界量化到 1/fps 栅格（量化游标而非各块独立量化，保证相邻块严格衔接）。
"""

import math

_LEAD = 0.5                    # card 块首句相对块首的提前量（秒）
_GAP = 1.0                     # card 块内句间自然停顿（秒）
_TAIL = 1.5                    # card 块尾留白（秒）
_FUNCTIONAL_PAUSE_MIN = 8.0    # 原片句间隔 ≥ 此值 → 功能性留白，保留原间隔而非压成 GAP
_MIN_CARD_S = 4.0              # 卡片块最短时长（秒），避免一句话卡一闪而过
_MIN_SPEECH_GAP = 0.3          # 全局顺序护栏：任意相邻两句配音轨的最小间隔（秒）。
                               # 缺陷出处：wave5 生产 job10——ball 块按原片相对位置锚定，
                               # 遇「自然语速时长超原片槽」或「原片本就存在重叠时间戳段」时，
                               # 前句未说完就到后句锚点 → 成片双语音同时播（实测 4 对、最长 5.86s）。
                               # 护栏统一 card/ball：new_start = max(原算法位置, prev_end + 本间隔)。

# A2 说话时球停（意见 6）：球块内句子说话时球必须停在中心（原片本来结构——组间提问
# 全落静止休息时刻），否则边说边晃破坏保真。语音窗 = [start, end + _SPEECH_WINDOW_TAIL]，
# 说完再停 _SPEECH_WINDOW_TAIL 秒才恢复摆动；碰运动 run 就把该窗切成静止子场景。
_SPEECH_WINDOW_TAIL = 0.5      # 语音窗尾延展（秒）：说完话球再停 0.5s 才恢复摆动
_MIN_STATIC_S = 2.0            # 静止子场景最短时长（秒）：窗短于此也切 2s，避免闪切

# A6 结语卡时长下限（意见 7）：结语块（收尾卡）时长 < 8s 抬到 8s，给足读卡 + 收束语 + 淡出的时间。
_MIN_CLOSING_CARD_S = 8.0
_CARD_KINDS = ("title_card", "text_card")


def _closing_card_idx(facts_scenes: list[dict]) -> int | None:
    """结语块下标：最后一个 card 块，且其后不再有球段（即位于全片末尾的收尾卡）。

    引言卡这类「card 在前、球段在后」结构不算结语块（其后仍有练习），返回 None——避免把开场
    卡误当收尾卡抬时长。无任何 card 块时返回 None。
    """
    last_card = None
    for i, sc in enumerate(facts_scenes):
        if sc.get("kind") in _CARD_KINDS:
            last_card = i
    if last_card is None:
        return None
    if any(sc.get("kind") == "ball_exercise" for sc in facts_scenes[last_card + 1:]):
        return None
    return last_card


def _quantize(t: float, fps: int) -> float:
    """时间戳量化到 1/fps 帧栅格（与 storyboard._quantize_t 同式，避免循环 import 自带一份）。"""
    return round(float(t) * fps) / fps


def _ceil_ms(t: float) -> float:
    """向上取整到毫秒（F-C）：句 start 存储用 ceil 而非 round，保证与上一句 stored end 的间隙
    不因双端 round(,3) 各损 0.5ms 而落到 0.299（job14 实测 2 对句间隙 0.2965/0.299）。
    -1e-6 容差令本就落 ms 栅格的值不被误抬一档（如 7.3 保持 7.3）。"""
    return math.ceil(t * 1000 - 1e-6) / 1000


def _assign_block(orig_start: float, scenes: list[dict]) -> int:
    """句 orig_start 归入哪个 facts 场景下标；越界（不落任何区间）归最近块。"""
    for idx, sc in enumerate(scenes):
        if float(sc["t0"]) <= orig_start < float(sc["t1"]):
            return idx

    def _dist(sc: dict) -> float:
        t0, t1 = float(sc["t0"]), float(sc["t1"])
        if orig_start < t0:
            return t0 - orig_start
        if orig_start >= t1:
            return orig_start - t1
        return 0.0

    return min(range(len(scenes)), key=lambda i: _dist(scenes[i]))


def relayout(facts_scenes: list[dict], segments: list[dict], durations: list[float],
             *, fps: int, gap: float | None = None) -> tuple[dict, list[dict], list[str]]:
    """语音优先重排。

    Args:
        facts_scenes: analyzer 产出的 facts 场景（含 kind/t0/t1，ball_exercise 为练习块）
        segments:     台词句（start/end 为原片轴；orig_* 存在时优先取，保证重入幂等）
        durations:    各句 rate=1.0 下测得的自然配音时长（与 segments 同序等长）
        fps:          帧率（块边界量化栅格）

    全局顺序护栏（防双语音）：句子按播放顺序（块序 × 块内 orig_start 序）过一条跨块
    游标 prev_end，任意句最终 new_start = max(原算法位置, prev_end + _MIN_SPEECH_GAP)。
    由此保证不变量：对所有 i，retimed[i].start >= retimed[i-1].end + _MIN_SPEECH_GAP - 1e-6
    （i 按播放顺序；segments 按时序输入时即等于下标序）。card 块原算法已含 prev_end + _GAP
    (_GAP > 本间隔)，护栏对其恒等，行为不变；ball 块相对锚定才会被护栏顺延。

    Returns:
        (block_time_map, retimed_segments, warnings)
        block_time_map:   {facts 场景下标: (new_t0, new_t1)}（已量化，块间严格衔接）
        retimed_segments: segments 副本，start/end 为新轴（end=start+自然时长），
                          orig_start/orig_end 保留原片轴
        warnings:         ball 块内句子被护栏顺延后越出块尾的告警（语音完整优先不截断，
                          允许成片压到下一场景画面上）
    """
    warnings: list[str] = []
    # global_param.sentence_gap 覆盖 card 块句间自然停顿（revision B4）；未给沿用模块默认 _GAP
    card_gap = _GAP if gap is None else float(gap)
    n = len(facts_scenes)
    orig_starts = [seg.get("orig_start", seg["start"]) for seg in segments]
    orig_ends = [seg.get("orig_end", seg["end"]) for seg in segments]

    # 归块：每句 -> facts 场景下标；块内句子按 orig_start 排序
    block_segs: dict[int, list[int]] = {i: [] for i in range(n)}
    if n:
        for sid, os in enumerate(orig_starts):
            if segments[sid].get("no_dub"):          # A3：免责/须知句不占时间轴，不归块
                continue                             # （仍留在 retimed 保证下标对齐，卡片显示全文）
            block_segs[_assign_block(os, facts_scenes)].append(sid)
        for ids in block_segs.values():
            ids.sort(key=lambda i: orig_starts[i])

    retimed = [dict(seg) for seg in segments]
    for i, seg in enumerate(retimed):
        seg["orig_start"] = orig_starts[i]
        seg["orig_end"] = orig_ends[i]

    closing_idx = _closing_card_idx(facts_scenes)        # A6：结语卡块（收尾卡）下标，供抬时长下限
    block_time_map: dict[int, tuple[float, float]] = {}
    cur = 0.0
    speech_end: float | None = None                      # 全局配音游标：上一句（跨块，按
                                                         # 播放顺序）自然时长结束点（未量化原值）
    for blk, sc in enumerate(facts_scenes):
        new_t0 = _quantize(cur, fps)
        blk_t0, blk_t1 = float(sc["t0"]), float(sc["t1"])
        orig_dur = blk_t1 - blk_t0
        seg_ids = block_segs[blk]

        if sc.get("kind") == "ball_exercise":
            # 练习本体：新时长=原时长；块内句子相对位置锚定，再过全局护栏防重叠
            new_t1 = _quantize(new_t0 + orig_dur, fps)
            for sid in seg_ids:
                anchor = new_t0 + (orig_starts[sid] - blk_t0)   # 原算法：相对锚定
                s_start = anchor if speech_end is None \
                    else max(anchor, speech_end + _MIN_SPEECH_GAP)
                s_start = _ceil_ms(s_start)              # F-C：ceil 到 ms，间隙不因 round 损失跌破 0.3
                s_end = s_start + durations[sid]
                if s_end > new_t1:                       # 顺延后越出块尾：语音完整优先，不截断
                    warnings.append(
                        f"ball块[{blk_t0:.0f}s] 句{sid} 台词后移越块"
                        f"（语音完整优先不截断，压下一场景画面）")
                retimed[sid]["start"] = s_start
                retimed[sid]["end"] = round(s_end, 3)
                speech_end = retimed[sid]["end"]         # 游标跟 stored end，令间隙不变量在成片 ms 轴成立
        elif not seg_ids:
            # 无句子块保持原时长；但空收尾卡（末句全在球段、收尾卡无句）同样套结语 8s
            # 下限（A6/F2），给足读卡 + 淡出——否则短空收尾卡会一闪而过。
            floor = _MIN_CLOSING_CARD_S if blk == closing_idx else 0.0
            new_t1 = _quantize(new_t0 + max(floor, orig_dur), fps)
        else:
            # card 块：句子顺排，功能性留白按原间隔保留（护栏对块内句恒等）
            prev_orig_end = None
            for pos, sid in enumerate(seg_ids):
                if pos == 0:
                    algo_start = new_t0 + _LEAD
                else:
                    orig_gap = orig_starts[sid] - prev_orig_end
                    seg_gap = orig_gap if orig_gap >= _FUNCTIONAL_PAUSE_MIN else card_gap
                    algo_start = speech_end + seg_gap
                s_start = algo_start if speech_end is None \
                    else max(algo_start, speech_end + _MIN_SPEECH_GAP)
                s_start = _ceil_ms(s_start)              # F-C：ceil 到 ms，间隙不因 round 损失跌破 0.3
                s_end = s_start + durations[sid]
                retimed[sid]["start"] = s_start
                retimed[sid]["end"] = round(s_end, 3)
                speech_end, prev_orig_end = retimed[sid]["end"], orig_ends[sid]
            # A6：结语卡块用 8s 下限（给足读卡 + 收束语 + 淡出），其余 card 块用常规 4s 下限
            floor = _MIN_CLOSING_CARD_S if blk == closing_idx else _MIN_CARD_S
            new_dur = max(floor, speech_end + _TAIL - new_t0)
            new_t1 = _quantize(new_t0 + new_dur, fps)

        block_time_map[blk] = (new_t0, new_t1)
        cur = new_t1                                     # 量化游标推进（块间严格衔接）

    return block_time_map, retimed, warnings


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """按起点排序后合并交叠/相邻区间（容差 1e-9）。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out: list[list[float]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _expand_to_min(lo: float, hi: float, run_t0: float, run_t1: float) -> tuple[float, float]:
    """把静止窗居中扩到 _MIN_STATIC_S 并夹到 [run_t0, run_t1]；run 短于下限则整段静止。"""
    if hi - lo >= _MIN_STATIC_S - 1e-9:
        return (lo, hi)
    mid = (lo + hi) / 2
    nlo, nhi = mid - _MIN_STATIC_S / 2, mid + _MIN_STATIC_S / 2
    if nlo < run_t0:                                     # 顶到 run 首
        nlo, nhi = run_t0, run_t0 + _MIN_STATIC_S
    if nhi > run_t1:                                     # 顶到 run 尾
        nlo, nhi = run_t1 - _MIN_STATIC_S, run_t1
    return (max(nlo, run_t0), min(nhi, run_t1))


# F3 停球零跳变：球心 x = 中线 + 振幅*sin(2π*t_global/T)，故 sin=0 即球在正中的时刻为
# t = k*(T/2)（半周期整数倍，「球过中点」时刻）。静止球恒在中心；只要「运动↔静止」的切换
# 恰好发生在球过中点那一帧，停/起球就与静止中心球严丝合缝，不再跳帧瞬移。以下把边界吸附到
# 该半周期栅格；栅格点先量化到 1/fps（渲染是逐帧的，取离过中点最近的一帧，误差 ≤ 1/(2fps)）。
def _phase_grid_point(k: int, half_period: float, fps: int) -> float:
    """第 k 个「球过中点」时刻 t=k*(T/2)，量化到 1/fps 帧栅格。"""
    return _quantize(k * half_period, fps)


def _phase_k_floor(t: float, h: float, fps: int) -> int:
    """最大的 k 使 _phase_grid_point(k) ≤ t。"""
    k = math.floor(t / h + 1e-9)
    while _phase_grid_point(k, h, fps) > t + 1e-9:
        k -= 1
    return k


def _phase_k_ceil(t: float, h: float, fps: int) -> int:
    """最小的 k 使 _phase_grid_point(k) ≥ t。"""
    k = math.ceil(t / h - 1e-9)
    while _phase_grid_point(k, h, fps) < t - 1e-9:
        k += 1
    return k


def phase_floor(t: float, *, period: float, fps: int) -> float:
    """≤ t 的最近「球过中点」时刻（storyboard 组装边界向静止侧生长时用）。"""
    h = period / 2.0
    return _phase_grid_point(_phase_k_floor(t, h, fps), h, fps)


def phase_ceil(t: float, *, period: float, fps: int) -> float:
    """≥ t 的最近「球过中点」时刻。"""
    h = period / 2.0
    return _phase_grid_point(_phase_k_ceil(t, h, fps), h, fps)


def _snap_static_window(lo: float, hi: float, run_t0: float, run_t1: float,
                        *, period: float, fps: int) -> tuple[float, float]:
    """静止窗 [lo,hi] 边界向外吸附到最近的「球过中点」栅格 k*(T/2)，再按半周期粒度向外
    补足到 _MIN_STATIC_S，最后夹回 [run_t0, run_t1]。

    只向外扩（起点向前、终点向后）：既完整覆盖原语音窗（A2 互斥不变量不破），又让停/起球
    恰落在球滑到中心那一帧（F3 零跳变）。补足最短时两端交替补半周期，保持栅格对齐与大致居中。
    """
    h = period / 2.0
    k_lo = _phase_k_floor(lo, h, fps)
    k_hi = _phase_k_ceil(hi, h, fps)
    extend_low = True
    while (_phase_grid_point(k_hi, h, fps)
           - _phase_grid_point(k_lo, h, fps)) < _MIN_STATIC_S - 1e-9:
        if extend_low:
            k_lo -= 1
        else:
            k_hi += 1
        extend_low = not extend_low
    return (max(run_t0, _phase_grid_point(k_lo, h, fps)),
            min(run_t1, _phase_grid_point(k_hi, h, fps)))


def carve_motion_for_speech(run_t0: float, run_t1: float,
                            speech_windows: list[tuple[float, float]],
                            *, fps: int, period: float | None = None) -> list[tuple[str, float, float]]:
    """把运动球 run 区间 [run_t0, run_t1] 按落入其中的语音窗切成 静止/运动 交替子段。

    A2 说话时球停：球块内句子说话时球必须停在中心（原片本来结构）。语音窗与运动 run
    相交即把该窗切出一个静止子场景（球停），窗前后仍是运动子场景（同一 run，参数不变——
    统一周期/同色，拼接处球位置连续由渲染侧全局 t 公式天然保证，无需相位补偿）。

    Args:
        run_t0/run_t1:   运动 run 的新轴区间（已量化）
        speech_windows:  句语音窗 [w0, w1] 列表（新轴，已含尾延展；可越界，内部自 clip 到 run）
        fps:             帧率（子段边界量化栅格）
        period:          全局球周期 T（秒）。给出时静止窗边界吸附到「球过中点」栅格 k*(T/2)，
                         停/起球恰在中心 → 零跳变（F3）；None 时退回原「居中扩 2s + 1/fps 量化」。

    Returns:
        按时序的 [(kind, t0, t1), ...]（kind ∈ {"motion","static"}），无缝铺满整个 run。
        无相交语音窗 → 单个 ("motion", run_t0, run_t1)（现状不变）。静止子段最短 _MIN_STATIC_S。
    """
    # clip 到 run，仅保留真相交的窗
    clipped: list[tuple[float, float]] = []
    for w0, w1 in speech_windows:
        lo, hi = max(float(w0), run_t0), min(float(w1), run_t1)
        if hi - lo > 1e-9:
            clipped.append((lo, hi))
    if not clipped:
        return [("motion", run_t0, run_t1)]

    # 合并 → 逐窗扩（吸附过中点栅格 / 居中扩最短）→ 扩后可能再交叠，循环合并至稳定
    windows = _merge_intervals(clipped)
    while True:
        if period is not None:
            expanded = _merge_intervals(
                [_snap_static_window(lo, hi, run_t0, run_t1, period=period, fps=fps)
                 for lo, hi in windows])
        else:
            expanded = _merge_intervals(
                [_expand_to_min(lo, hi, run_t0, run_t1) for lo, hi in windows])
        if len(expanded) == len(windows):
            windows = expanded
            break
        windows = expanded

    if period is None:
        # 无相位信息：量化子段边界到帧栅格（防拼接漂移），量化后再夹回 run 并合并。
        # period 给出时 _snap_static_window 已落栅格（内含 1/fps 量化），无需再量化。
        windows = _merge_intervals(
            [(max(run_t0, _quantize(lo, fps)), min(run_t1, _quantize(hi, fps)))
             for lo, hi in windows])

    # 交替铺满：静止窗之间/前后补运动子段（零长运动子段丢弃，衔接不断）
    out: list[tuple[str, float, float]] = []
    cursor = run_t0
    for lo, hi in windows:
        if lo - cursor > 1e-9:
            out.append(("motion", cursor, lo))
        out.append(("static", lo, hi))
        cursor = hi
    if run_t1 - cursor > 1e-9:
        out.append(("motion", cursor, run_t1))
    return out
