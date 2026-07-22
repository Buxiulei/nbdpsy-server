"""remake storyboard：schema 校验 + facts→分镜脚本 生成。"""
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.remake import storyboard, style

pytestmark = pytest.mark.unit


def _ball_scene(**kw):
    base = {"id": 3, "t0": 10.0, "t1": 20.0, "type": "ball_exercise",
            "renderer": "programmatic",
            "params": {"ball_color": style.BURGUNDY, "bg_color": style.DARK_BG,
                       "period_s": 1.6, "amplitude_ratio": 0.42,
                       "audio_cue": "alternating_tone"}}
    base.update(kw)
    return base


def _card_scene(**kw):
    base = {"id": 1, "t0": 0.0, "t1": 10.0, "type": "text_card",
            "renderer": "still_image",
            "content": {"title": "使用须知", "body": "正文"}, "transition": "fade"}
    base.update(kw)
    return base


def _sb(scenes):
    return {"version": 1, "style": "nbdpsy_v1",
            "source": {"url": "x", "duration_s": 20.0}, "scenes": scenes}


class TestValidate:
    def test_valid_passes(self):
        storyboard.validate_storyboard(_sb([_card_scene(), _ball_scene()]))

    def test_unimplemented_renderer_rejected(self):
        sb = _sb([_card_scene(renderer="seedance")])
        with pytest.raises(storyboard.StoryboardError, match="seedance"):
            storyboard.validate_storyboard(sb)

    def test_unknown_renderer_rejected(self):
        sb = _sb([_card_scene(renderer="magic")])
        with pytest.raises(storyboard.StoryboardError):
            storyboard.validate_storyboard(sb)

    def test_timeline_gap_rejected(self):
        # 场景必须铺满时间轴：后一场景 t0 != 前一场景 t1 即报错
        sb = _sb([_card_scene(t1=8.0), _ball_scene()])
        with pytest.raises(storyboard.StoryboardError, match="时间轴"):
            storyboard.validate_storyboard(sb)

    def test_ball_scene_requires_positive_period(self):
        bad = _ball_scene()
        bad["params"]["period_s"] = 0
        with pytest.raises(storyboard.StoryboardError, match="period"):
            storyboard.validate_storyboard(_sb([_card_scene(), bad]))

    def test_empty_scenes_rejected(self):
        with pytest.raises(storyboard.StoryboardError):
            storyboard.validate_storyboard(_sb([]))

    def test_first_scene_must_start_at_zero(self):
        # I3：删首场景致全片错位——首 t0!=0 必须报错（相邻衔接检查看不出来）
        sb = _sb([_card_scene(t0=2.0), _ball_scene()])
        with pytest.raises(storyboard.StoryboardError, match="首场景"):
            storyboard.validate_storyboard(sb)

    def test_last_scene_must_reach_duration(self):
        # I3：末 t1 未达 source.duration_s 必须报错
        sb = _sb([_card_scene(), _ball_scene(t1=18.0)])
        with pytest.raises(storyboard.StoryboardError, match="末场景"):
            storyboard.validate_storyboard(sb)

    def test_grid_invariant_rejects_off_grid_elastic(self):
        # F-B job15 不变量：弹性 sb 里 motion 5.00s 整相位直接贴静止 rest、边界漂 k*(T/2) 栅格
        # 355ms（停/起球不在中心→232px 瞬移）→ 必须 fail-fast，不再靠 lead 外部抽查。
        period = 2.486                                     # T/2=1.243 不整除帧栅格，19.0 漂 355ms
        sb = _sb([
            _ball_scene(id=1, t0=0.0, t1=14.0,
                        params={"period_s": period, "ball_color": style.BURGUNDY}),
            _ball_scene(id=2, t0=14.0, t1=19.0,            # motion 5.00s，边界未吸附
                        params={"period_s": period, "ball_color": style.BURGUNDY}),
            _ball_scene(id=3, t0=19.0, t1=30.0,            # 组间静止 rest
                        params={"period_s": period, "static": True,
                                "ball_color": style.CREAM}),
        ])
        sb["source"]["duration_s"] = 30.0
        sb["retimed_segments"] = []                        # 弹性模式标记（handler pop 前在场）
        with pytest.raises(storyboard.StoryboardError, match="漂离"):
            storyboard.validate_storyboard(sb)

    def test_grid_invariant_skips_non_elastic(self):
        # 非弹性原轴模式（无 retimed_segments 键）：组间静止边界本不吸附，栅格校验豁免（A4 原轴回归）
        period = 2.486
        sb = _sb([
            _ball_scene(id=1, t0=0.0, t1=19.0,             # 边界 19.0 漂栅格但非弹性 → 放行
                        params={"period_s": period, "ball_color": style.BURGUNDY}),
            _ball_scene(id=2, t0=19.0, t1=30.0,
                        params={"period_s": period, "static": True,
                                "ball_color": style.CREAM}),
        ])
        sb["source"]["duration_s"] = 30.0
        storyboard.validate_storyboard(sb)                 # 无 retimed_segments → 不校验栅格

    def test_grid_invariant_accepts_on_grid_elastic(self):
        # 弹性模式 + 边界恰落 k*(T/2) 栅格 → 放行（不误伤已吸附的合法分镜）
        period = 2.0                                       # T/2=1.0，边界 19.0 恰在栅格
        sb = _sb([
            _ball_scene(id=1, t0=0.0, t1=19.0,
                        params={"period_s": period, "ball_color": style.BURGUNDY}),
            _ball_scene(id=2, t0=19.0, t1=30.0,
                        params={"period_s": period, "static": True,
                                "ball_color": style.CREAM}),
        ])
        sb["source"]["duration_s"] = 30.0
        sb["retimed_segments"] = []
        storyboard.validate_storyboard(sb)


class TestBuild:
    @pytest.fixture
    def facts(self):
        return {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "text_card",
             "text": "This video is not a substitute for medical advice. liability ..."},
            {"t0": 10.0, "t1": 20.0, "kind": "title_card", "text": "introduction"},
            {"t0": 20.0, "t1": 50.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 50.0, "t1": 60.0, "kind": "other", "text": ""},
        ], "warnings": []}

    async def _build(self, facts):
        # 本地化 LLM 打桩：introduction → 引言
        with patch.object(storyboard, "_chat_localize",
                          AsyncMock(return_value={"introduction": "引言"})):
            return await storyboard.build_storyboard(facts, duration=60.0)

    @pytest.mark.asyncio
    async def test_disclaimer_card_uses_standard_notice(self, facts):
        sb = await self._build(facts)
        first = sb["scenes"][0]
        assert first["content"]["title"] == storyboard.USAGE_NOTICE_TITLE
        assert first["content"]["body"] == storyboard.USAGE_NOTICE_BODY

    @pytest.mark.asyncio
    async def test_title_card_localized(self, facts):
        sb = await self._build(facts)
        assert sb["scenes"][1]["content"]["title"] == "引言"

    @pytest.mark.asyncio
    async def test_ball_color_mapped_to_brand(self, facts):
        sb = await self._build(facts)
        ball = sb["scenes"][2]
        assert ball["renderer"] == "programmatic"
        # wave2：运动 run 按品牌双色轮换，首个运动 run → 勃艮第红
        assert ball["params"]["ball_color"] == style.BURGUNDY
        assert ball["params"]["bg_color"] == style.DARK_BG
        # 全片统一中位周期：唯一实测 1.5 → global_period=1.5
        assert ball["params"]["period_s"] == 1.5

    @pytest.mark.asyncio
    async def test_other_kind_degrades_to_brand_card(self, facts):
        sb = await self._build(facts)
        other = sb["scenes"][3]
        assert other["renderer"] == "still_image"

    @pytest.mark.asyncio
    async def test_output_passes_validation(self, facts):
        sb = await self._build(facts)
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_motion_phases_per_phase_color_static_aggregated(self):
        # A4：运动球恢复 per 相位粒度——每相位一个循环色（不再每 run 单色）；
        # 静止 run 仍聚合为单个米白休息球；周期仍全片统一中位。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 5.0, "kind": "title_card", "text": "intro"},
            # 运动相位 0 / 1（实测 2.5 / 2.6，同 run 内两相位）
            {"t0": 5.0, "t1": 15.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.5, "period_estimated": True},
            {"t0": 15.0, "t1": 25.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 2.6, "period_estimated": True},
            # 静止 run（组间休息）
            {"t0": 25.0, "t1": 29.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "static": True},
            # 运动相位 2（回退默认周期，period_estimated=False，不进中位数）
            {"t0": 29.0, "t1": 39.0, "kind": "ball_exercise",
             "ball_color_hex": "#E8194B", "period_s": 1.6, "period_estimated": False},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=39.0)
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        # per 相位粒度：运动相位不再聚合 → [5,15]+[15,25]+静止[25,29]+[29,39] = 4 个球场景
        assert len(balls) == 4
        assert [(b["t0"], b["t1"]) for b in balls] == [
            (5.0, 15.0), (15.0, 25.0), (25.0, 29.0), (29.0, 39.0)]
        # 统一中位周期：median([2.5, 2.6]) = 2.55，全部球场景一致
        assert all(b["params"]["period_s"] == pytest.approx(2.55) for b in balls)
        # 运动相位循环色：相位0→勃艮第红、相位1→淡金、相位2 本应米白(idx2)但紧邻静止 → 顺延深金
        motion = [b for b in balls if not b["params"].get("static")]
        assert [m["params"]["ball_color"] for m in motion] == [
            style.BURGUNDY, style.GOLD, style.DARK_GOLD]
        # 静止 run 聚合为单个米白休息球
        rest = [b for b in balls if b["params"].get("static")]
        assert len(rest) == 1
        assert rest[0]["params"]["ball_color"] == style.CREAM
        # 输出过 schema 校验
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_motion_phases_cycle_full_palette(self):
        # A4：单个运动 run 内连续 5 相位 → 循环遍历品牌调色板；相位2 米白居 run 中部
        # （不紧邻静止）保留为米白运动球，不触发顺延。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 5.0, "kind": "title_card", "text": "intro"},
            {"t0": 5.0, "t1": 10.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 10.0, "t1": 15.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
            {"t0": 15.0, "t1": 20.0, "kind": "ball_exercise",
             "ball_color_hex": "#E8194B", "period_s": 1.5, "period_estimated": True},
            {"t0": 20.0, "t1": 25.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 25.0, "t1": 30.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=30.0)
        motion = [s for s in sb["scenes"]
                  if s["type"] == "ball_exercise" and not s["params"].get("static")]
        # 相位序 0..4 → 调色板 [勃艮第红, 淡金, 米白, 深金] 循环
        assert [m["params"]["ball_color"] for m in motion] == [
            style.BURGUNDY, style.GOLD, style.CREAM, style.DARK_GOLD, style.BURGUNDY]
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_static_rest_does_not_consume_phase_index(self):
        # A4：静止休息球固定米白、不参与循环——相位序跨静止连续递推（既不重置也不占位）。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 10.0, "t1": 14.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 14.0, "t1": 24.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=24.0)
        motion = [s for s in sb["scenes"]
                  if s["type"] == "ball_exercise" and not s["params"].get("static")]
        # 相位0→勃艮第红、相位1→淡金（静止不占相位序，否则相位1 会落 idx2 米白）
        assert [m["params"]["ball_color"] for m in motion] == [style.BURGUNDY, style.GOLD]
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_cream_defer_skips_slot_no_adjacent_same_color(self):
        # A4 对抗（顺延撞色根治）：静止球紧邻某 run 且该 run 首相位 idx≡2(mod4) 且 run≥2 相位
        # → 顺延须跳过米白槽位（phase_idx 额外 +1），否则顺延的深金会与次相位天然深金相邻同色。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 5.0, "kind": "ball_exercise",       # 相位0
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 5.0, "t1": 10.0, "kind": "ball_exercise",      # 相位1
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
            {"t0": 10.0, "t1": 14.0, "kind": "ball_exercise",     # 静止（run 间隔断）
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 14.0, "t1": 19.0, "kind": "ball_exercise",     # 相位2：idx2 撞米白 + 紧邻静止 → 顺延
             "ball_color_hex": "#E8194B", "period_s": 1.5, "period_estimated": True},
            {"t0": 19.0, "t1": 24.0, "kind": "ball_exercise",     # 相位3：与相位2 同 run 相邻
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=24.0)
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        # 跳槽后 cadence：相位2 顺延深金、相位3 落勃艮第红（而非天然深金）
        motion = [b for b in balls if not b["params"].get("static")]
        assert [m["params"]["ball_color"] for m in motion] == [
            style.BURGUNDY, style.GOLD, style.DARK_GOLD, style.BURGUNDY]
        # 不变量：任意相邻两运动球场景（中间无静止隔断）颜色必不同
        for a, b in zip(balls, balls[1:]):
            if not a["params"].get("static") and not b["params"].get("static"):
                assert a["params"]["ball_color"] != b["params"]["ball_color"], \
                    f"相邻运动球同色 {a['params']['ball_color']} @[{a['t0']},{b['t1']}]"
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_speech_window_spanning_phase_boundary_fully_carved(self):
        # A4×A2 边界（Issue 2）：一条语音窗跨两运动相位边界 → 各相位分别切分，
        # 断言过切安全（零相交不变量）且不漏切（窗被静止子场景并集完整覆盖）。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 21.0, "kind": "ball_exercise",     # 相位0
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 21.0, "t1": 36.0, "kind": "ball_exercise",    # 相位1
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},   # 落 card 块
            # 落相位0 尾部（orig 20∈[6,21)），自然时长 5s 使语音窗跨过相位0/1 新轴边界
            {"start": 20.0, "end": 20.5, "en": "q", "zh": "现在有什么感觉"},
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0, 5.0])
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        motions = [b for b in balls if not b["params"].get("static")]
        statics = [b for b in balls if b["params"].get("static")]
        # 该窗确实跨相位边界：应在边界两侧各切出静止子段（≥2 个静止子场景）
        assert len(statics) >= 2
        # 过切安全：无运动子场景与任一语音窗 [start,end+0.5] 相交
        for seg in sb["retimed_segments"]:
            w0, w1 = seg["start"], seg["end"] + 0.5
            for m in motions:
                assert min(w1, m["t1"]) - max(w0, m["t0"]) <= 1e-9
        # 不漏切：越块那条句（新轴 start 最大）的语音窗被静止子场景并集完整覆盖
        ball_seg = max(sb["retimed_segments"], key=lambda s: s["start"])
        w0, w1 = ball_seg["start"], ball_seg["end"] + 0.5
        cursor = w0
        for a, b in sorted((s["t0"], s["t1"]) for s in statics):
            if a <= cursor + 1e-9:
                cursor = max(cursor, b)
        assert cursor >= w1 - 1e-9, f"语音窗[{w0},{w1}] 未被静止子场景完整覆盖（漏切）"
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_phase_color_composes_with_speech_carving(self):
        # A4×A2：多相位运动 run + 语音窗切分复合——落入某相位的语音窗切出米白静止子场景，
        # 该相位其余运动子段仍保留本相位循环色，相邻相位颜色各自独立。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 21.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 21.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},            # 落 card 块
            {"start": 27.0, "end": 30.0, "en": "q", "zh": "现在有什么感觉"},  # 落运动相位1
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0, 3.0])
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        motion = [b for b in balls if not b["params"].get("static")]
        static = [b for b in balls if b["params"].get("static")]
        # 相位0 勃艮第红、相位1 淡金 都在（语音窗只切相位1，不吞掉相位色）
        assert {m["params"]["ball_color"] for m in motion} == {style.BURGUNDY, style.GOLD}
        # 语音窗切出的静止子场景固定米白 + static 标记
        assert static and all(s["params"]["ball_color"] == style.CREAM for s in static)
        assert all(s["params"]["static"] for s in static)
        # A2 不变量：无运动子场景与任一语音窗 [start,end+0.5] 相交
        for seg in sb["retimed_segments"]:
            w0, w1 = seg["start"], seg["end"] + 0.5
            for m in motion:
                assert min(w1, m["t1"]) - max(w0, m["t0"]) <= 1e-9
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_median_period_falls_back_when_no_measured(self):
        # 无任何实测周期（全静止 / 全回退）→ global_period = DEFAULT_PERIOD_S
        facts = {"scenes": [
            {"t0": 0.0, "t1": 4.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 4.0, "t1": 14.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.6, "period_estimated": False},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=14.0)
        assert all(s["params"]["period_s"] == style.DEFAULT_PERIOD_S
                   for s in sb["scenes"])

    @pytest.mark.asyncio
    async def test_relayout_mode_uses_new_axis(self):
        # wave5：给 segments+clip_durations → 场景走重排新轴，source.duration_s=新总时长，
        # retimed_segments 随 dict 携出（带 orig_*），且过 schema 校验
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "title_card", "text": "intro"},
            {"t0": 10.0, "t1": 30.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "甲"}]   # 落 card 块
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=30.0, segments=segs, clip_durations=[1.0])
        # card 块：句首 LEAD=0.5、句末 1.5、块尾 TAIL=1.5 → max(4.0, 0.5+1.5+1.5)=4.0
        assert sb["scenes"][0]["t0"] == 0.0 and sb["scenes"][0]["t1"] == 4.0
        # ball 块保原时长 20 → [4, 24]
        assert sb["scenes"][1]["t0"] == 4.0 and sb["scenes"][1]["t1"] == 24.0
        # 新总时长 24（不再是原片 30）
        assert sb["source"]["duration_s"] == 24.0
        # retimed 段：新轴 start=0.5，原轴留存 orig_start=1
        retimed = sb["retimed_segments"]
        assert retimed[0]["start"] == 0.5 and retimed[0]["orig_start"] == 1.0
        # storyboard 校验（末场景 t1==新总时长）——retimed_segments 键被 validate 忽略
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_no_segments_keeps_orig_axis_behavior(self):
        # 不给 segments → 行为不变：原轴量化、无 retimed_segments 键
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "title_card", "text": "intro"},
            {"t0": 10.0, "t1": 30.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=30.0)
        assert "retimed_segments" not in sb
        assert sb["source"]["duration_s"] == 30.0        # 原片时长量化
        assert sb["scenes"][1]["t1"] == 30.0

    @pytest.mark.asyncio
    async def test_ball_speech_window_carved_to_static(self):
        # A2：球块内句子的语音窗落在运动 run → 切出静止子场景（球停/米白），前后运动同参数
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "hi", "zh": "引言"},          # 落 card 块
            {"start": 20.0, "end": 24.0, "en": "q", "zh": "现在有什么感觉"},  # 落 ball 块
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0, 4.0])
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        statics = [b for b in balls if b["params"].get("static")]
        motions = [b for b in balls if not b["params"].get("static")]
        # 球块句 orig_start=20 → 新轴 start=18/end=22，窗 [18,22.5]（>2s 直接切）
        assert len(statics) == 1
        st = statics[0]
        assert st["params"]["ball_color"] == style.CREAM
        assert st["params"]["static"] is True
        assert (st["t0"], st["t1"]) == (18.0, 22.5)
        # 前后运动子场景同色（同一 run）、统一周期，无 static 标记
        assert len(motions) == 2
        assert all(m["params"]["ball_color"] == style.BURGUNDY for m in motions)
        assert all(b["params"]["period_s"] == 1.5 for b in balls)
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_no_motion_scene_intersects_any_speech_window(self):
        # A2 核心不变量：任一句子语音窗 [start,end+0.5] 不与任何运动场景相交
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 40.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        # 球块内两句提问（orig 15 / 28 落 [6,40)），分别锚到不同运动子段
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},
            {"start": 15.0, "end": 18.0, "en": "q1", "zh": "闭上眼睛"},
            {"start": 28.0, "end": 30.0, "en": "q2", "zh": "现在有什么感觉"},
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=40.0, segments=segs, clip_durations=[2.0, 3.0, 2.0])
        retimed = sb["retimed_segments"]
        motions = [s for s in sb["scenes"]
                   if s["type"] == "ball_exercise" and not s["params"].get("static")]
        assert motions                                    # 仍有运动段（未被全切）
        for seg in retimed:
            w0, w1 = seg["start"], seg["end"] + 0.5
            for m in motions:
                overlap = min(w1, m["t1"]) - max(w0, m["t0"])
                assert overlap <= 1e-9, \
                    f"句 {seg['zh']} 窗[{w0},{w1}] 与运动场景[{m['t0']},{m['t1']}]相交"
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_carved_static_excluded_from_tones(self):
        # A2 回归：切出的静止子场景无提示音（tones 按 params.static 过滤）
        from app.video.pipeline.remake import tones
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},
            {"start": 20.0, "end": 24.0, "en": "q", "zh": "现在有什么感觉"},
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0, 4.0])
        ball_scenes = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        # 复刻 tones 过滤谓词：静止子场景被排除，只有运动子场景进提示音轨
        toned = [s for s in ball_scenes
                 if (s.get("params") or {}).get("period_s")
                 and not (s.get("params") or {}).get("static")]
        assert len(toned) == 2
        assert all(not s["params"].get("static") for s in toned)
        # endpoint_times 不在静止窗 [18,22.5] 内产任何提示音时刻
        st = next(s for s in ball_scenes if s["params"].get("static"))
        for m in toned:
            for t, _side in tones.endpoint_times(
                    m["t0"], m["t1"], m["params"]["period_s"]):
                assert not (st["t0"] <= t < st["t1"]), f"静止窗内出现提示音 t={t}"

    @pytest.mark.asyncio
    async def test_motion_static_boundaries_snap_to_phase_grid(self):
        # F3 停球零跳变契约：弹性模式下所有 motion↔static 邻接边界落 k*(T/2) 栅格
        # （既含 carve 切出的静止子段边界，也含组间天然静止休息 run 的边界）。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 26.0, "kind": "ball_exercise",       # 运动 run（含一句提问）
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 26.0, "t1": 32.0, "kind": "ball_exercise",      # 组间静止休息 run
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 32.0, "t1": 52.0, "kind": "ball_exercise",      # 运动 run
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},            # 落 card 块
            {"start": 15.0, "end": 18.0, "en": "q", "zh": "现在有什么感觉"},  # 落运动 run1
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=52.0, segments=segs, clip_durations=[2.0, 3.0])
        h = 1.5 / 2                                                 # 全局中位周期 1.5 → T/2=0.75
        scenes = sb["scenes"]

        def _is_motion(s):
            return s["type"] == "ball_exercise" and not s["params"].get("static")

        def _is_static(s):
            return s["type"] == "ball_exercise" and s["params"].get("static")

        checked = 0
        for a, b in zip(scenes, scenes[1:]):
            if (_is_motion(a) and _is_static(b)) or (_is_static(a) and _is_motion(b)):
                boundary = a["t1"]                                 # == b["t0"]
                nearest = round(boundary / h) * h                  # 最近的球过中点
                assert abs(boundary - nearest) <= 1.0 / style.FPS + 1e-9, \
                    f"边界 {boundary} 不在 k*(T/2) 栅格上（停/起球不在中心 → 跳变）"
                checked += 1
        assert checked >= 2                                        # carve 边界 + 组间静止 run 边界
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_short_motion_sliver_boundaries_snap_to_grid(self):
        # F-B job14：T=2.486（T/2=1.243 不整除帧栅格）+ 运动 run 尾语音窗 → 短 motion 尾巴夹在
        # carve-静止 与 组间静止休息 run 之间。旧 ±1帧 clamp 相向挤压致该边界漂离栅格 500ms+。
        # 契约：最终归一化 pass 后所有 motion↔static 边界 mod T/2 偏离 ≤ 1/fps，铺满/衔接不破。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 40.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.486, "period_estimated": True},
            {"t0": 40.0, "t1": 46.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 46.0, "t1": 80.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 2.486, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},
            {"start": 36.0, "end": 38.6, "en": "q", "zh": "现在有什么感觉"},  # run1 尾 → 短 motion 尾巴
            {"start": 60.0, "end": 62.0, "en": "q2", "zh": "再感受"},
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=80.0, segments=segs, clip_durations=[2.0, 2.6, 2.0],
                period_s=2.486)
        h = 2.486 / 2.0                                            # T/2
        scenes = sb["scenes"]

        def _is_motion(s):
            return s["type"] == "ball_exercise" and not s["params"].get("static")

        def _is_static(s):
            return s["type"] == "ball_exercise" and s["params"].get("static")

        checked = 0
        for a, b in zip(scenes, scenes[1:]):
            if (_is_motion(a) and _is_static(b)) or (_is_static(a) and _is_motion(b)):
                boundary = a["t1"]
                drift = abs(boundary - round(boundary / h) * h)
                assert drift <= 1.0 / style.FPS + 1e-6, \
                    f"边界 {boundary} 漂离 k*(T/2) 栅格 {drift * 1000:.1f}ms（停/起球跳变）"
                checked += 1
        assert checked >= 2                                        # carve 边界 + 组间静止 run 边界
        # 铺满/衔接不变量 + 全部非零长 + 首尾锚定
        for a, b in zip(scenes, scenes[1:]):
            assert a["t1"] == pytest.approx(b["t0"], abs=1e-9)     # 无缝衔接（丢弃塌缩子场景后仍成立）
            assert b["t1"] > b["t0"]                               # 无零长残留
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_short_trailing_phase_collapse_exposes_prior_grid(self):
        # F-B job15 漏网（生产止损前实测 2 处）：运动 run 末尾一个短微段(<T/2)接组间静止 rest。
        # 短末相位被终校 snap 塌缩丢弃后，前一个 5.00s 整相位直接贴静止 rest——其边界原是
        # motion↔motion(相位连续被跳过)从未吸附，单遍终校漏网漂 T/2 栅格数百 ms(job15 实测
        # t=358.058 偏 115ms / t=818.058 偏 258ms → 球瞬移 232/490px，用户投诉的跳变)。
        # 契约：循环到不动点后所有 motion↔static 边界落栅格，铺满/衔接/非零长/首尾锚定不破。
        period = 2.486
        scenes_in = [{"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"}]
        t = 6.0
        for hexc in ("#FFFFFF", "#A2C40C"):                       # 复刻生产 2 处 run→rest 结构
            for dur in (5.0, 5.0, 5.0, 0.8):                      # 三个 5.00s 整相位 + 0.8s 短末相位
                scenes_in.append({"t0": t, "t1": t + dur, "kind": "ball_exercise",
                                  "ball_color_hex": hexc, "period_s": period,
                                  "period_estimated": True})
                t += dur
            scenes_in.append({"t0": t, "t1": t + 7.34, "kind": "ball_exercise",  # 组间静止 rest
                              "ball_color_hex": "#FFFFFF", "static": True})
            t += 7.34
        facts = {"scenes": scenes_in, "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]   # 语音只落 card，运动 run 不 carve
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=t, segments=segs, clip_durations=[2.0], period_s=period)
        h = period / 2.0
        scenes = sb["scenes"]

        def _is_motion(s):
            return s["type"] == "ball_exercise" and not s["params"].get("static")

        def _is_static(s):
            return s["type"] == "ball_exercise" and s["params"].get("static")

        checked = 0
        for a, b in zip(scenes, scenes[1:]):
            if (_is_motion(a) and _is_static(b)) or (_is_static(a) and _is_motion(b)):
                boundary = a["t1"]
                drift = abs(boundary - round(boundary / h) * h)
                assert drift <= 1.0 / style.FPS + 1e-6, \
                    f"边界 {boundary} 漂离 k*(T/2) 栅格 {drift * 1000:.1f}ms（短末相位塌缩暴露的漏网）"
                checked += 1
        assert checked >= 2                                       # 两处 run→rest 边界都覆盖
        for a, b in zip(scenes, scenes[1:]):
            assert a["t1"] == pytest.approx(b["t0"], abs=1e-9)    # 无缝衔接
            assert b["t1"] > b["t0"]                              # 无零长残留
        storyboard.validate_storyboard(sb)                       # 内建栅格不变量自然全绿

    @pytest.mark.asyncio
    async def test_scene_times_quantized_to_frame_grid(self):
        # I1：喂非栅格 t（4.03/8.11），输出全部落在 1/30 栅格且相邻衔接连续
        facts = {"scenes": [
            {"t0": 0.0, "t1": 4.03, "kind": "title_card", "text": "intro"},
            {"t0": 4.03, "t1": 8.11, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5},
        ], "warnings": []}
        with patch.object(storyboard, "_chat_localize",
                          AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=8.11)
        for sc in sb["scenes"]:
            for key in ("t0", "t1"):
                grid = sc[key] * style.FPS
                assert abs(grid - round(grid)) < 1e-9, f"{key}={sc[key]} 未落栅格"
        # 相邻衔接：后段 t0 == 前段 t1（量化后仍相等）
        assert sb["scenes"][1]["t0"] == sb["scenes"][0]["t1"]
        # source.duration_s 同步量化
        dur_grid = sb["source"]["duration_s"] * style.FPS
        assert abs(dur_grid - round(dur_grid)) < 1e-9


class TestRevisionOverrides:
    """B4：build_storyboard 的 revision 覆盖参数（ball_style / global.sentence_gap）。

    覆盖机制是显式传参（非 monkeypatch 全局常量）；均 None 时行为不变（本仓其它生成
    测试即 None 分支回归）。这里只断言给出覆盖后落到分镜/relayout 的参数面。
    """

    def _motion_facts(self):
        # card + 两运动相位（同 static=False 聚合为一 motion run，逐相位循环取色）
        return {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 16.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 16.0, "t1": 26.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}

    @pytest.mark.asyncio
    async def test_period_s_override(self):
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(self._motion_facts(),
                                                   duration=26.0, period_s=3.3)
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        assert balls and all(s["params"]["period_s"] == 3.3 for s in balls)

    @pytest.mark.asyncio
    async def test_palette_override(self):
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(self._motion_facts(), duration=26.0,
                                                   palette=["#111111", "#222222"])
        motion = [s for s in sb["scenes"] if s["type"] == "ball_exercise"
                  and not s["params"].get("static")]
        colors = {m["params"]["ball_color"] for m in motion}
        assert colors <= {"#111111", "#222222"} and style.BURGUNDY not in colors

    @pytest.mark.asyncio
    async def test_color_mode_single_uses_first_palette_color(self):
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(self._motion_facts(), duration=26.0,
                                                   color_mode="single")
        motion = [s for s in sb["scenes"] if s["type"] == "ball_exercise"
                  and not s["params"].get("static")]
        assert len(motion) >= 2                                   # 确有多相位
        assert {m["params"]["ball_color"] for m in motion} == {style.BALL_PALETTE[0]}

    @pytest.mark.asyncio
    async def test_y_ratio_override_written_to_ball_params(self):
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(self._motion_facts(),
                                                   duration=26.0, y_ratio=0.7)
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        assert balls and all(s["params"]["y_ratio"] == 0.7 for s in balls)

    @pytest.mark.asyncio
    async def test_no_y_ratio_key_when_not_overridden(self):
        # 不覆盖时 params 不含 y_ratio（渲染器缺省读 style.BALL_Y_RATIO，非 revision 分镜零改动）
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(self._motion_facts(), duration=26.0)
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        assert balls and all("y_ratio" not in s["params"] for s in balls)

    @pytest.mark.asyncio
    async def test_sentence_gap_threads_into_relayout(self):
        captured = {}
        real = storyboard.timeline.relayout

        def spy(*a, **kw):
            captured["gap"] = kw.get("gap")
            return real(*a, **kw)

        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})), \
             patch.object(storyboard.timeline, "relayout", spy):
            await storyboard.build_storyboard(self._motion_facts(), duration=26.0,
                                              segments=segs, clip_durations=[2.0],
                                              sentence_gap=0.42)
        assert captured["gap"] == 0.42
