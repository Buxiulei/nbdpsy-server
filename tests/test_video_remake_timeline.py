"""弹性时间轴 relayout 纯函数：归块 / card 顺排(LEAD/GAP/TAIL) / 功能性留白保留 /
ball 块时长不变+相对锚定 / 无句块保持原时长 / MIN_CARD_S 下限 / 游标量化衔接 /
新总时长 / retimed 保留 orig_* / 全局顺序护栏（防双语音重叠）。
"""
import pytest

from app.video.pipeline.remake.timeline import (
    relayout, _assign_block, _LEAD, _GAP, _TAIL, _FUNCTIONAL_PAUSE_MIN, _MIN_CARD_S,
    _MIN_SPEECH_GAP, carve_motion_for_speech, _MIN_STATIC_S, _SPEECH_WINDOW_TAIL,
    _MIN_CLOSING_CARD_S, _closing_card_idx, _snap_static_window, phase_floor, phase_ceil,
)

pytestmark = pytest.mark.unit

FPS = 120


def _card(t0, t1):
    return {"kind": "text_card", "t0": t0, "t1": t1}


def _ball(t0, t1):
    return {"kind": "ball_exercise", "t0": t0, "t1": t1}


def _seg(start, end, **kw):
    return {"start": start, "end": end, "en": "x", "zh": "句", **kw}


class TestAssignBlock:
    def test_lands_in_containing_scene(self):
        scenes = [_card(0, 10), _ball(10, 20)]
        assert _assign_block(3.0, scenes) == 0
        assert _assign_block(15.0, scenes) == 1

    def test_out_of_range_goes_nearest(self):
        scenes = [_card(0, 10), _ball(10, 20)]
        assert _assign_block(25.0, scenes) == 1     # 越右界 → 最近块（末块）
        assert _assign_block(-3.0, scenes) == 0     # 越左界 → 最近块（首块）


class TestCardBlock:
    def test_lead_gap_tail_ordering(self):
        # 单 card 大块，三句：首句 LEAD、句间 GAP、块尾 TAIL
        scenes = [_card(0, 30)]
        segs = [_seg(1, 3), _seg(5, 7), _seg(9, 11)]
        durs = [1.0, 2.0, 1.5]
        btm, retimed, warns = relayout(scenes, segs, durs, fps=FPS)
        assert [s["start"] for s in retimed] == [0.5, 2.5, 5.5]
        assert [s["end"] for s in retimed] == [1.5, 4.5, 7.0]
        # 首句 = LEAD；句间隔 = 前句 end + GAP
        assert retimed[0]["start"] == _LEAD
        assert retimed[1]["start"] == retimed[0]["end"] + _GAP
        # 块尾 = 末句 end + TAIL
        assert btm[0] == (0.0, 7.0 + _TAIL)
        assert not warns

    def test_functional_pause_preserved(self):
        # 原片句间隔 12s(≥8) → 视为功能性留白，用原间隔而非 GAP
        scenes = [_card(0, 40)]
        segs = [_seg(1, 3), _seg(15, 17)]           # 原间隔 15-3=12 ≥ 8
        durs = [1.0, 1.0]
        _, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        assert retimed[0]["start"] == 0.5 and retimed[0]["end"] == 1.5
        assert retimed[1]["start"] == 1.5 + 12.0    # 保留 12s 留白（非 GAP=1.0）
        assert _FUNCTIONAL_PAUSE_MIN == 8.0

    def test_min_card_floor(self):
        # 短句 → 块时长不足 MIN_CARD_S 时被抬到下限；此处 card 后接球段（引言卡，非结语卡），
        # 走常规 4s 下限而非结语 8s 下限（A6）
        scenes = [_card(0, 100), _ball(100, 110)]
        segs = [_seg(1, 2)]
        durs = [0.3]
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[0] == (0.0, _MIN_CARD_S)         # 0.5+0.3+1.5=2.3 < 4.0 → 抬到 4.0

    def test_empty_block_keeps_orig_duration(self):
        # 无句子的「非结语」card 块保持原时长（此处 card 后接球段，非收尾卡，不套 8s 结语下限）
        scenes = [_card(0, 6), _ball(6, 16)]
        btm, retimed, _ = relayout(scenes, [], [], fps=FPS)
        assert btm[0] == (0.0, 6.0)
        assert retimed == []


class TestBallBlock:
    def test_duration_unchanged_and_relative_anchor(self):
        # ball 块新时长=原时长；块内句子相对位置锚定
        scenes = [_ball(10, 30)]                     # 原时长 20
        segs = [_seg(12, 14)]                        # 块内偏移 12-10=2
        durs = [1.0]
        btm, retimed, warns = relayout(scenes, segs, durs, fps=FPS)
        assert btm[0][1] - btm[0][0] == 20.0        # 练习本体一分不动
        assert retimed[0]["start"] == 2.0           # new_t0(0) + 相对偏移 2
        assert retimed[0]["end"] == 3.0
        assert not warns

    def test_sentence_overflow_spills_with_warning(self):
        # ball 块内句子自然时长越出块尾 → 语音完整优先不截断，越块并告警
        scenes = [_ball(0, 5)]
        segs = [_seg(4, 4.5)]                        # 偏移 4，自然时长 3 → 4+3=7 > 5
        durs = [3.0]
        btm, retimed, warns = relayout(scenes, segs, durs, fps=FPS)
        assert retimed[0]["start"] == 4.0
        assert retimed[0]["end"] == 7.0             # 不截断：越出块尾 5.0，语音完整保留
        assert btm[0][1] == 5.0                     # 块尾仍为原时长（画面不跟着延长）
        assert warns and "越块" in warns[0]


class TestClosingCardFloor:
    """A6 结语卡时长下限：结语块（最后一个 card 块、其后无球段）时长 < 8s 抬到 8s。"""

    def test_closing_card_floored_to_8s(self):
        # 球段后接短结语卡 → 自然时长不足 8s，抬到结语下限
        scenes = [_ball(0, 10), _card(10, 12)]
        segs = [_seg(10.5, 11)]                       # orig_start=10.5 落结语卡块
        durs = [0.3]
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[1][1] - btm[1][0] == _MIN_CLOSING_CARD_S
        assert _MIN_CLOSING_CARD_S == 8.0

    def test_closing_card_uses_natural_when_long(self):
        # 结语卡叙述足够长（自然时长 > 8s）→ 用自然时长，不被下限压
        scenes = [_ball(0, 10), _card(10, 30)]
        segs = [_seg(11, 12)]
        durs = [10.0]                                 # 0.5+10+1.5=12 > 8
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[1][1] - btm[1][0] == pytest.approx(12.0)

    def test_intro_card_before_ball_not_floored(self):
        # 引言卡（card 在前、球段在后）不算结语卡 → 常规 4s 下限
        scenes = [_card(0, 100), _ball(100, 110)]
        segs = [_seg(1, 2)]
        durs = [0.3]
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[0] == (0.0, _MIN_CARD_S)

    def test_closing_card_idx_detection(self):
        assert _closing_card_idx([_card(0, 5), _ball(5, 10)]) is None       # 引言卡后有球
        assert _closing_card_idx([_ball(0, 5), _card(5, 10)]) == 1          # 球后结语卡
        assert _closing_card_idx([_card(0, 5)]) == 0                        # 纯卡片
        assert _closing_card_idx([_ball(0, 5)]) is None                     # 无卡片
        assert _closing_card_idx(
            [_card(0, 5), _ball(5, 10), _card(10, 15)]) == 2                # 末尾结语卡

    def test_empty_closing_card_floored_to_8s(self):
        # F2b：末句全在球段、尾部空收尾卡(无句) → 空块也套 8s 结语下限（避免短卡一闪而过）
        scenes = [_ball(0, 10), _card(10, 12)]           # 收尾卡 orig 仅 2s
        segs = [_seg(5, 6)]                               # 唯一句落球段，收尾卡无句
        durs = [1.0]
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[1][1] - btm[1][0] == _MIN_CLOSING_CARD_S   # 空收尾卡抬到 8s
        assert btm[0][1] == btm[1][0]                         # 块间衔接不断

    def test_closing_line_anchored_into_closing_card(self):
        # F2a+relayout 联动：收束句 orig_* 锚进收尾卡(t0+0.1) → 归入收尾卡块，且块 ≥8s、句非 no_dub
        scenes = [_ball(0, 10), _card(10, 12)]
        segs = [_seg(5, 6),                                        # 球段句
                _seg(9, 10, orig_start=10.1, orig_end=10.1)]       # 收束句锚进收尾卡
        durs = [1.0, 3.0]
        btm, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[1][0] <= retimed[1]["start"] <= btm[1][1]      # 收束句归入收尾卡块
        assert btm[1][1] - btm[1][0] >= _MIN_CLOSING_CARD_S       # 收尾卡块 ≥8s
        assert not retimed[1].get("no_dub")                       # 收束句朗读


class TestQuantizeAndTotal:
    def test_cursor_quantized_blocks_contiguous(self):
        # 非栅格块时长：量化游标后相邻块严格衔接、各端点落栅格
        fps = 30
        scenes = [_card(0, 5.017), _card(5.017, 10.04)]
        btm, _, _ = relayout(scenes, [], [], fps=fps)
        assert btm[0][1] == btm[1][0]               # 块间无缝衔接
        for t0, t1 in btm.values():
            for t in (t0, t1):
                assert abs(t * fps - round(t * fps)) < 1e-9

    def test_new_total_is_last_block_end(self):
        # 新总时长 = 末块 new_t1 = 各块新时长顺序累加
        scenes = [_card(0, 30), _ball(30, 50)]
        segs = [_seg(1, 3)]                          # 落 card 块
        durs = [1.0]
        btm, _, _ = relayout(scenes, segs, durs, fps=FPS)
        card_dur = btm[0][1] - btm[0][0]
        ball_dur = btm[1][1] - btm[1][0]
        assert ball_dur == 20.0                      # ball 保原时长
        assert btm[1][1] == card_dur + ball_dur      # 末块 end = 累加总时长
        assert btm[0][1] == btm[1][0]                # 衔接


class TestRetimedFields:
    def test_orig_preserved(self):
        scenes = [_card(0, 30)]
        segs = [_seg(1, 3), _seg(5, 7)]
        btm, retimed, _ = relayout(scenes, segs, [1.0, 1.0], fps=FPS)
        assert [s["orig_start"] for s in retimed] == [1.0, 5.0]
        assert [s["orig_end"] for s in retimed] == [3.0, 7.0]
        assert retimed[0]["zh"] == "句"              # 其余字段不丢

    def test_idempotent_uses_existing_orig(self):
        # 重入：句已带 orig_*（start/end 是上轮新轴），归块与保留必须以 orig_* 为准
        scenes = [_card(0, 10), _ball(10, 20)]
        segs = [_seg(99, 100, orig_start=15.0, orig_end=17.0)]   # start 已是脏新轴
        btm, retimed, _ = relayout(scenes, segs, [1.0], fps=FPS)
        # orig_start=15 → 归 ball 块(idx1)，不被脏 start=99 带偏
        assert retimed[0]["orig_start"] == 15.0 and retimed[0]["orig_end"] == 17.0
        assert btm[1][0] <= retimed[0]["start"] <= btm[1][1]


class TestOverlapGuard:
    """全局顺序护栏：防 wave5 生产 job10 的球段双语音（原片重叠段 / 自然语速超槽）。
    不变量：对所有 i，retimed[i].start >= retimed[i-1].end + _MIN_SPEECH_GAP - 1e-6。
    """

    def test_orig_overlap_same_anchor_staggered(self):
        # 原片两句同 orig_start 落同一 ball 块 → 输出错开，不再同锚点双语音
        scenes = [_ball(0, 30)]
        segs = [_seg(5, 7), _seg(5, 8)]             # 同 orig_start=5（原片重叠段）
        durs = [2.0, 2.0]
        _, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        assert retimed[0]["start"] == 5.0 and retimed[0]["end"] == 7.0
        # 第二句被顺延到 prev_end + 护栏间隔（原算法两句都锚 5 → 双语音）
        assert retimed[1]["start"] == 7.0 + _MIN_SPEECH_GAP
        assert retimed[1]["start"] >= retimed[0]["end"] + _MIN_SPEECH_GAP - 1e-6
        assert _MIN_SPEECH_GAP == 0.3

    def test_natural_duration_overflow_pushes_next(self):
        # ball 块内前句自然时长超过到下句锚点的间距 → 后句推移到 prev_end + 0.3
        scenes = [_ball(0, 30)]
        segs = [_seg(2, 4), _seg(5, 7)]             # 锚点间距 3
        durs = [5.0, 1.0]                           # 前句 2→7 越过后句锚点 5
        _, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        assert retimed[0]["end"] == 7.0
        assert retimed[1]["start"] == 7.0 + _MIN_SPEECH_GAP
        assert retimed[1]["start"] >= retimed[0]["end"] + _MIN_SPEECH_GAP - 1e-6

    def test_stored_gap_never_below_target_after_rounding(self):
        # F-C job14：护栏顶出的句间隙经双端 round(,3) 各损 0.5ms → stored 间隙偶落 0.299（差 1ms）。
        # ceil 到 ms + 游标跟 stored end 后，stored 间隙恒 ≥ _MIN_SPEECH_GAP。
        # 用例 (o0=29.777,d0=2.1475,d1=2.211) 系旧代码实测复现例（gap=0.299）。
        scenes = [_ball(0.0, 200.0)]
        o0, d0, d1 = 29.777, 2.1475, 2.211
        segs = [_seg(o0, o0 + d0), _seg(o0, o0 + d1)]  # 同 orig_start → 次句必被护栏顶
        _, retimed, _ = relayout(scenes, segs, [d0, d1], fps=FPS)
        gap = retimed[1]["start"] - retimed[0]["end"]
        assert gap >= _MIN_SPEECH_GAP - 1e-9, f"句间隙 {gap} 跌破 {_MIN_SPEECH_GAP}"

    def test_global_invariant_mixed_blocks(self):
        # 混合 card/ball（含原片近重叠 + 球段超槽）→ 全部相邻对满足不重叠不变量。
        # 句按时序排布，故 retimed 下标序 == 播放序，可直接对相邻下标断言。
        scenes = [_card(0, 20), _ball(20, 50), _card(50, 70)]
        segs = [
            _seg(1, 3), _seg(8, 10),                # card0
            _seg(22, 24), _seg(24, 26), _seg(40, 42),  # ball（22/24 近重叠）
            _seg(52, 54),                           # card2
        ]
        durs = [2.0, 2.0, 6.0, 6.0, 12.0, 2.0]      # ball 末句 12s 超槽 → 越块 + 挤压后续
        _, retimed, warns = relayout(scenes, segs, durs, fps=FPS)
        for i in range(1, len(retimed)):
            assert retimed[i]["start"] >= retimed[i - 1]["end"] + _MIN_SPEECH_GAP - 1e-6, \
                f"句{i} 与句{i - 1} 重叠"
        assert any("越块" in w for w in warns)      # 球段超槽产出越块告警（不截断）


class TestCarveMotionForSpeech:
    """A2 说话时球停：运动球 run 区间按落入的语音窗切成 静止/运动 交替子段，铺满 run。"""

    def test_no_windows_single_motion(self):
        # 无语音窗 → 整段运动，不切分
        assert carve_motion_for_speech(0.0, 30.0, [], fps=FPS) == [("motion", 0.0, 30.0)]

    def test_window_out_of_run_ignored(self):
        # 语音窗完全落 run 外 → 不切分
        assert carve_motion_for_speech(
            10.0, 20.0, [(0.0, 5.0), (25.0, 30.0)], fps=FPS) == [("motion", 10.0, 20.0)]

    def test_mid_window_splits_motion_static_motion(self):
        # run 中部语音窗 → motion / static / motion 三段严格衔接铺满
        out = carve_motion_for_speech(0.0, 30.0, [(10.0, 14.0)], fps=FPS)
        assert out == [("motion", 0.0, 10.0), ("static", 10.0, 14.0),
                       ("motion", 14.0, 30.0)]

    def test_short_window_expands_to_min_static(self):
        # 语音窗 0.5s < 2s → 静止子段居中扩到 2s（避免闪切）
        out = carve_motion_for_speech(0.0, 30.0, [(10.0, 10.5)], fps=FPS)
        statics = [s for s in out if s[0] == "static"]
        assert len(statics) == 1
        _, s0, s1 = statics[0]
        assert s1 - s0 == pytest.approx(_MIN_STATIC_S)
        assert (s0 + s1) / 2 == pytest.approx(10.25)     # 居中于原窗
        assert _MIN_STATIC_S == 2.0

    def test_window_at_run_start_no_leading_motion(self):
        # 语音窗贴 run 首 → 无前导运动子段，直接静止起
        out = carve_motion_for_speech(0.0, 30.0, [(0.0, 3.0)], fps=FPS)
        assert out == [("static", 0.0, 3.0), ("motion", 3.0, 30.0)]

    def test_window_clipped_to_run_tail(self):
        # 语音窗越 run 右界 → clip 到 run 尾，无尾随运动
        out = carve_motion_for_speech(0.0, 20.0, [(15.0, 25.0)], fps=FPS)
        assert out == [("motion", 0.0, 15.0), ("static", 15.0, 20.0)]

    def test_close_windows_merged(self):
        # 两相近语音窗各扩 2s 后交叠 → 合并成一个静止子段
        out = carve_motion_for_speech(0.0, 30.0, [(10.0, 11.0), (11.5, 12.5)], fps=FPS)
        statics = [s for s in out if s[0] == "static"]
        assert len(statics) == 1
        _, s0, s1 = statics[0]
        assert s0 <= 10.0 and s1 >= 12.5                 # 覆盖两窗

    def test_tiles_run_contiguously_and_alternates(self):
        # 切分产物无缝铺满 [run_t0,run_t1] 且 静止/运动 交替（无同类相邻）
        out = carve_motion_for_speech(0.0, 30.0, [(5.0, 6.0), (20.0, 24.0)], fps=FPS)
        assert out[0][1] == 0.0                           # 首段 t0 == run_t0
        assert out[-1][2] == 30.0                         # 末段 t1 == run_t1
        for a, b in zip(out, out[1:]):
            assert a[2] == b[1]                           # 相邻严格衔接
        kinds = [k for k, _, _ in out]
        assert all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1))

    def test_boundaries_quantized_to_grid(self):
        # 切分边界量化到 1/fps 栅格（与全局量化同栅格，防拼接漂移）
        out = carve_motion_for_speech(0.0, 30.0, [(10.03, 12.07)], fps=FPS)
        for _, t0, t1 in out:
            for t in (t0, t1):
                assert abs(t * FPS - round(t * FPS)) < 1e-9

    def test_window_covering_whole_run_all_static(self):
        # 语音窗覆盖整 run → 全段静止（说话贯穿全程，球全停）
        out = carve_motion_for_speech(0.0, 3.0, [(-1.0, 5.0)], fps=FPS)
        assert out == [("static", 0.0, 3.0)]

    def test_speech_window_tail_constant(self):
        assert _SPEECH_WINDOW_TAIL == 0.5


class TestPhaseSnap:
    """F3 停球零跳变：静止窗边界吸附到「球过中点」栅格 k*(T/2)（球在中心的时刻）。"""

    @staticmethod
    def _on_grid(t, half_period):
        # 到最近 k*(T/2) 的距离 ≤ 1/fps（渲染逐帧，取离过中点最近的一帧）
        nearest = round(t / half_period) * half_period
        return abs(t - nearest) <= 1.0 / FPS + 1e-9

    def test_snap_covers_window_and_lands_on_grid(self):
        # 任意窗 → 边界向外落半周期栅格且完整覆盖原窗（A2 互斥不变量不破）
        period = 1.5                                  # T/2 = 0.75
        h = period / 2
        for lo, hi in [(10.3, 12.1), (5.0, 5.4), (33.33, 41.7), (7.5, 8.25)]:
            g_lo, g_hi = _snap_static_window(lo, hi, 0.0, 100.0, period=period, fps=FPS)
            assert g_lo <= lo + 1e-9 and g_hi >= hi - 1e-9      # 覆盖原窗（向外扩）
            assert self._on_grid(g_lo, h) and self._on_grid(g_hi, h)
            assert g_hi - g_lo >= _MIN_STATIC_S - 1e-9          # 满足最短 2s

    def test_phase_floor_ceil_bracket_on_grid(self):
        period = 1.6                                  # T/2 = 0.8
        h = period / 2
        for t in (3.1, 7.0, 12.37):
            lo, hi = phase_floor(t, period=period, fps=FPS), phase_ceil(t, period=period, fps=FPS)
            assert lo <= t + 1e-9 and hi >= t - 1e-9            # 夹住 t
            assert self._on_grid(lo, h) and self._on_grid(hi, h)

    def test_carve_with_period_static_boundaries_on_grid(self):
        # carve 传 period → 静止子段边界落栅格、覆盖原窗；铺满/交替不变量保持
        period = 1.5
        h = period / 2
        out = carve_motion_for_speech(0.0, 30.0, [(10.3, 12.1)], fps=FPS, period=period)
        statics = [s for s in out if s[0] == "static"]
        assert statics
        for _, t0, t1 in statics:
            assert self._on_grid(t0, h) and self._on_grid(t1, h)
        _, s0, s1 = statics[0]
        assert s0 <= 10.3 + 1e-9 and s1 >= 12.1 - 1e-9         # 覆盖原窗
        assert out[0][1] == 0.0 and out[-1][2] == 30.0         # 铺满整 run
        for a, b in zip(out, out[1:]):
            assert a[2] == b[1]                                 # 相邻严格衔接


class TestNoDubSkipsTimeline:
    """A3：no_dub 句不占时间轴（relayout 跳过归块），但保留在 retimed 保证下标对齐。"""

    def test_all_no_dub_card_block_keeps_orig_duration(self):
        # 免责卡块两句全 no_dub → 该块无有效句 → 走空块分支保持原时长
        scenes = [_card(0, 10), _card(10, 40)]
        segs = [_seg(1, 3, no_dub=True), _seg(2, 4, no_dub=True), _seg(15, 17)]
        durs = [1.0, 1.0, 2.0]
        btm, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        assert btm[0] == (0.0, 10.0)                       # 空块保持原时长
        assert len(retimed) == 3                           # 下标对齐：no_dub 句仍在 retimed
        assert retimed[2]["start"] == 10.0 + _LEAD         # 非 no_dub 句正常排布

    def test_no_dub_does_not_advance_speech_cursor(self):
        # no_dub 句不推进全局配音游标：后续句排布与「无该句」等价
        scenes = [_card(0, 40)]
        durs = [1.0, 2.0]
        _, r_with, _ = relayout(
            scenes, [_seg(1, 3, no_dub=True), _seg(5, 7)], durs, fps=FPS)
        _, r_without, _ = relayout(scenes, [_seg(5, 7)], [2.0], fps=FPS)
        # 非 no_dub 句排到 LEAD（未被前面的 no_dub 句挤后）
        assert r_with[1]["start"] == _LEAD
        assert r_with[1]["start"] == r_without[0]["start"]

    def test_mixed_block_only_dubbed_positioned(self):
        # 同块内 no_dub 与正常句混排：只有正常句进入顺排，块时长按正常句算
        # （card 后接球段 → 非结语卡，走常规 4s 下限而非结语 8s 下限，不干扰本断言）
        scenes = [_card(0, 40), _ball(40, 50)]
        segs = [_seg(1, 3, no_dub=True), _seg(4, 6), _seg(8, 10)]
        durs = [5.0, 1.0, 1.0]                              # no_dub 时长很大，若占轴会撑爆
        btm, retimed, _ = relayout(scenes, segs, durs, fps=FPS)
        # 两正常句顺排：首句 LEAD，次句 = 前句 end + GAP
        assert retimed[1]["start"] == _LEAD
        assert retimed[2]["start"] == retimed[1]["end"] + _GAP
        # 块尾按末正常句算，不含 no_dub 的 5s
        assert btm[0][1] == pytest.approx(retimed[2]["end"] + _TAIL, abs=1e-6)
