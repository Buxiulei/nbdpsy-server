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
        # 该窗跨相位边界，各相位分别切出静止子段；第四轮节奏修正的合并 pass 会把这些紧邻静止
        # 并作一段连续静止（问答块「一停到底」），故断言 ≥1 段静止 + 下方「完整覆盖」不变量。
        assert len(statics) >= 1
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

    # ---- 第三轮扩展：static_source_spans（scene_edit 转静止）/ color_cycle_periods（每晃一组变色） ----

    async def test_static_source_spans_turns_moving_run_static_vs_baseline(self):
        # scene_edit 溯源窗命中的运动球段 → 强制转静止（米白 + static + 无 cue）；与基线对照
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 16.0, "kind": "ball_exercise",       # facts 运动段①
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 16.0, "t1": 26.0, "kind": "ball_exercise",      # facts 运动段②（将被强制静止）
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]  # 只落 card，运动段不 carve

        def _statics(sb):
            return [s for s in sb["scenes"]
                    if s["type"] == "ball_exercise" and s["params"].get("static")]

        # 基线：两段都运动，无任何静止球
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            base = await storyboard.build_storyboard(
                facts, duration=26.0, segments=segs, clip_durations=[2.0])
        assert _statics(base) == []

        # 强制 facts 段②（源 [16,26]）静止
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            forced = await storyboard.build_storyboard(
                facts, duration=26.0, segments=segs, clip_durations=[2.0],
                static_source_spans=[[16.0, 26.0]])
        fstatic = _statics(forced)
        fmotion = [s for s in forced["scenes"]
                   if s["type"] == "ball_exercise" and not s["params"].get("static")]
        assert len(fstatic) == 1 and fmotion                     # 段①仍运动、段②转静止
        # 与既有静止段语义一字不差：米白居中 + static=True + 带周期（渲染忽略）
        st = fstatic[0]
        assert st["params"]["ball_color"] == style.CREAM
        assert st["params"]["static"] is True
        assert st["params"]["period_s"] > 0
        # 段②是末段 ball → 强制静止落在片尾
        assert forced["scenes"][-1]["params"].get("static") is True
        # 无提示音：tones 过滤谓词（period_s and not static）把它排除
        assert not ((st["params"].get("period_s")) and not st["params"].get("static"))
        # 栅格吸附终校照走 + schema 校验全过（含 F-B 栅格不变量）
        storyboard.validate_storyboard(forced)

    async def test_static_source_spans_middle_run_splits_with_snapped_boundaries(self):
        # 强制运动 run 中段某 facts 场景静止 → 运动 run 被劈成 [运动|静止|运动]，边界吸附栅格
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 16.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
            {"t0": 16.0, "t1": 26.0, "kind": "ball_exercise",     # 中段：被强制静止
             "ball_color_hex": "#A2C40C", "period_s": 1.5, "period_estimated": True},
            {"t0": 26.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#E8194B", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0], period_s=1.5,
                static_source_spans=[[16.0, 26.0]])
        balls = [s for s in sb["scenes"] if s["type"] == "ball_exercise"]
        # 中段静止把运动 run 一分为二：至少 2 个运动 + 1 个静止
        assert sum(1 for b in balls if b["params"].get("static")) == 1
        assert sum(1 for b in balls if not b["params"].get("static")) >= 2
        # motion↔static 边界吸附「球过中点」栅格（T/2=0.75）
        h = 1.5 / 2

        def _is_m(s):
            return s["type"] == "ball_exercise" and not s["params"].get("static")

        def _is_s(s):
            return s["type"] == "ball_exercise" and s["params"].get("static")

        checked = 0
        for a, b in zip(sb["scenes"], sb["scenes"][1:]):
            if (_is_m(a) and _is_s(b)) or (_is_s(a) and _is_m(b)):
                drift = abs(a["t1"] - round(a["t1"] / h) * h)
                assert drift <= 1.0 / style.FPS + 1e-6
                checked += 1
        assert checked == 2                                      # 进/出静止两条边界
        storyboard.validate_storyboard(sb)

    async def test_color_cycle_periods_splits_long_run_into_per_period_colors(self):
        # 用户「每晃一组变色」= N=1：长运动段按每周期切子场景逐段轮色，过 validate
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 36.0, "kind": "ball_exercise",      # 单个 30s 长运动段（恒色病灶）
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]  # 只落 card，运动段不 carve
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0],
                period_s=1.5, color_cycle_periods=1)
        motion = [s for s in sb["scenes"]
                  if s["type"] == "ball_exercise" and not s["params"].get("static")]
        # 30s 运动段按每 1.5s(=1 周期) 切 → 远多于逐相位的 1 段
        assert len(motion) >= 15
        # 逐段沿调色板顺序轮色：前 5 段 = [勃艮第红, 淡金, 米白, 深金, 勃艮第红]
        assert [m["params"]["ball_color"] for m in motion[:5]] == [
            style.BURGUNDY, style.GOLD, style.CREAM, style.DARK_GOLD, style.BURGUNDY]
        # 相邻子段颜色必不同（每晃一组都变色）
        for a, b in zip(motion, motion[1:]):
            assert a["params"]["ball_color"] != b["params"]["ball_color"]
        # 每段时长 ≈ 1 个周期（1.5s，末尾余段除外）
        for m in motion[:-1]:
            assert m["t1"] - m["t0"] == pytest.approx(1.5, abs=1e-6)
        # 铺满/衔接不破 + schema 校验全过（切点 motion↔motion，全局相位保球位连续）
        for a, b in zip(sb["scenes"], sb["scenes"][1:]):
            assert a["t1"] == pytest.approx(b["t0"], abs=1e-9)
        storyboard.validate_storyboard(sb)

    async def test_color_cycle_periods_n2_doubles_chunk_length(self):
        # N=2：每两个周期换色（变色慢一点）→ 段时长 ≈ 2 周期
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=36.0, segments=segs, clip_durations=[2.0],
                period_s=1.5, color_cycle_periods=2)
        motion = [s for s in sb["scenes"]
                  if s["type"] == "ball_exercise" and not s["params"].get("static")]
        for m in motion[:-1]:
            assert m["t1"] - m["t0"] == pytest.approx(3.0, abs=1e-6)   # 2 × 1.5
        storyboard.validate_storyboard(sb)

    async def test_new_overrides_absent_output_identical_to_baseline(self):
        # 保真红线：不带 color_cycle_periods / static_source_spans / card_duration_overrides
        # （None/空）时输出与基线逐字节一致
        facts = self._motion_facts()
        segs = [{"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"}]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            base = await storyboard.build_storyboard(
                facts, duration=26.0, segments=segs, clip_durations=[2.0])
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            same = await storyboard.build_storyboard(
                facts, duration=26.0, segments=segs, clip_durations=[2.0],
                color_cycle_periods=None, static_source_spans=[],
                card_duration_overrides=[])
        assert base == same

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


class TestSplitMotionGuardrail:
    """color_cycle_periods 切分的最小子场景护栏（job3 碎片病灶根治）。

    切分产物（真发生切分，len>1）绝不短于半个轮色周期（0.5·span）；整段本就短于一个周期
    （carve 短片）则原样透传单段——不由切分制造 1~14 帧的颜色闪片。
    """
    SPAN = 2.4857142857142853               # job3 实测 global_period

    def test_short_piece_below_span_not_split(self):
        # job3 场景6/7 病灶：0.108s 的 carve 短片跨栅格点 → 旧实现切成 0.083+0.025 两碎片。
        # 整段 < span → 护栏不切，原样透传（避免把 carve 短片再劈成亚帧碎片）。
        res = storyboard._split_motion_by_periods(
            261.0, 261.108, run_ref=6.0, span=self.SPAN)
        assert res == [(261.0, 261.108)]

    def test_tail_remainder_merged_into_previous(self):
        # 整段 ≥ span、尾部余量 < 0.5·span → 并入前一子段（沿用前段颜色），不独立成碎片
        res = storyboard._split_motion_by_periods(0.0, 2.1, run_ref=0.0, span=2.0)
        assert res == [(0.0, 2.1)]          # 旧实现会切 [0,2]+[2,2.1]（尾 0.1s 碎片）

    def test_head_remainder_merged_into_next(self):
        # 头部余量 < 0.5·span → 并入后一子段；中段满周期保留，产物均 ≥ 0.5·span
        res = storyboard._split_motion_by_periods(1.9, 6.5, run_ref=0.0, span=2.0)
        assert res == [(1.9, 4.0), (4.0, 6.5)]        # 头 [1.9,2.0]=0.1 并入首段
        assert all(b - a >= 1.0 - 1e-9 for a, b in res)

    def test_exact_divisor_run_splits_evenly_no_merge(self):
        # 整段恰为周期整数倍（保真基线）：逐周期均切，无头尾余量、无归并，行为不变
        res = storyboard._split_motion_by_periods(4.0, 34.0, run_ref=4.0, span=1.5)
        assert len(res) == 20
        assert all(abs((b - a) - 1.5) < 1e-9 for a, b in res)

    def test_span_non_positive_returns_single(self):
        assert storyboard._split_motion_by_periods(0.0, 5.0, run_ref=0.0, span=0.0) \
            == [(0.0, 5.0)]

    def test_fuzz_split_products_never_below_half(self):
        # 属性护栏：切分产物（len>1）恒 ≥ 0.5·span，且铺满/衔接不破
        import random
        span, half = self.SPAN, 0.5 * self.SPAN
        random.seed(7)
        for _ in range(20000):
            m0 = random.uniform(0.0, 10.0)
            m1 = m0 + random.uniform(0.01, 6 * span)
            res = storyboard._split_motion_by_periods(m0, m1, run_ref=0.37, span=span)
            assert abs(res[0][0] - m0) < 1e-9 and abs(res[-1][1] - m1) < 1e-6   # 铺满
            for k in range(len(res) - 1):
                assert abs(res[k][1] - res[k + 1][0]) < 1e-12                    # 衔接
            if len(res) > 1:
                assert all((b - a) >= half - 1e-9 for a, b in res)              # 无碎片

    @pytest.mark.asyncio
    async def test_color_cycle_periods_no_fragment_scenes(self):
        # job3 碎片病灶等价构造：多个长运动 facts 段（各 ≈3 周期）+ 漂移时长，令段边界脱离 N·T
        # 栅格。旧实现头尾余量残成 <0.5·T 碎片（1~14 帧色闪，实测 2 处 0.1/0.2s）；护栏后所有
        # 运动切分子场景 ≥ 0.5·T。非弹性模式（无 segments）→ 无语音 carve，运动段全是切分产物。
        scenes = [{"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"}]
        t = 6.0
        for dur in (7.3, 7.7, 7.4, 7.6, 7.35):      # 各 ≈3×2.5，漂移使段边界不落 2.5 栅格
            scenes.append({"t0": t, "t1": t + dur, "kind": "ball_exercise",
                           "ball_color_hex": "#FFFFFF", "period_s": 2.5,
                           "period_estimated": True})
            t += dur
        facts = {"scenes": scenes, "warnings": []}
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(facts, duration=t, color_cycle_periods=1)
        period = next(s["params"]["period_s"] for s in sb["scenes"]
                      if s["type"] == "ball_exercise")
        half = period / 2.0
        motion = [s for s in sb["scenes"]
                  if s["type"] == "ball_exercise" and not s["params"].get("static")]
        assert len(motion) > 5                       # 确有切分发生（长段被切多子场景）
        for m in motion:
            assert m["t1"] - m["t0"] >= half - 1e-9, \
                f"运动切分子场景 [{m['t0']},{m['t1']}] 短于 0.5·T={half}（碎片闪片）"
        for a, b in zip(sb["scenes"], sb["scenes"][1:]):     # 铺满/衔接不破
            assert a["t1"] == pytest.approx(b["t0"], abs=1e-9)
        storyboard.validate_storyboard(sb)


class TestCardDurationOverride:
    """card_edit.duration_s → build_storyboard(card_duration_overrides) 缩短卡片页面停留（反馈②）。"""

    @pytest.mark.asyncio
    async def test_shortens_no_speech_card_and_shifts_scenes(self):
        # 复刻「使用须知」病灶：无配音卡时长=facts 源区间(20s)，覆盖 duration_s 缩到 8s，后续前移
        facts = {"scenes": [
            {"t0": 0.0, "t1": 20.0, "kind": "text_card",
             "text": "This is not a substitute for medical advice. liability."},
            {"t0": 20.0, "t1": 40.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 25.0, "end": 27.0, "en": "q", "zh": "现在有什么感觉",
                 "orig_start": 25.0, "orig_end": 27.0}]        # 落 ball 块，卡片无句
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            base = await storyboard.build_storyboard(
                facts, duration=40.0, segments=segs, clip_durations=[2.0])
            short = await storyboard.build_storyboard(
                facts, duration=40.0, segments=segs, clip_durations=[2.0],
                card_duration_overrides=[[0.0, 20.0, 8.0]])
        assert base["scenes"][0]["t1"] == pytest.approx(20.0)          # 基线：须知卡=源区间 20s
        assert short["scenes"][0]["t0"] == 0.0
        assert short["scenes"][0]["t1"] == pytest.approx(8.0)          # 覆盖：缩到 8s
        ball = [s for s in short["scenes"] if s["type"] == "ball_exercise"]
        assert ball[0]["t0"] == pytest.approx(8.0)                     # 球段整体前移 12s
        storyboard.validate_storyboard(short)

    @pytest.mark.asyncio
    async def test_no_override_identical_to_baseline(self):
        facts = {"scenes": [
            {"t0": 0.0, "t1": 20.0, "kind": "text_card", "text": "intro"},
            {"t0": 20.0, "t1": 40.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5, "period_estimated": True},
        ], "warnings": []}
        segs = [{"start": 25.0, "end": 27.0, "en": "q", "zh": "甲",
                 "orig_start": 25.0, "orig_end": 27.0}]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            base = await storyboard.build_storyboard(
                facts, duration=40.0, segments=segs, clip_durations=[2.0])
            same = await storyboard.build_storyboard(
                facts, duration=40.0, segments=segs, clip_durations=[2.0],
                card_duration_overrides=[])
        assert base == same


# 第四轮验收：分镜互斥节奏两处修正（语音窗桥接 + 静止尾巴裁剪）
from app.video.pipeline.remake import timeline               # noqa: E402


def _static(t0, t1, period=2.0):
    return {"t0": t0, "t1": t1, "type": "ball_exercise", "renderer": "programmatic",
            "params": {"ball_color": style.CREAM, "bg_color": style.DARK_BG,
                       "period_s": period, "amplitude_ratio": 0.42, "static": True}}


def _motion(t0, t1, color=None, period=2.0):
    return {"t0": t0, "t1": t1, "type": "ball_exercise", "renderer": "programmatic",
            "params": {"ball_color": color or style.BURGUNDY, "bg_color": style.DARK_BG,
                       "period_s": period, "amplitude_ratio": 0.42,
                       "audio_cue": "alternating_tone"}}


class TestSpeechRhythmConstants:
    def test_constants(self):
        assert storyboard._SPEECH_BRIDGE_GAP_S == 6.0
        assert storyboard._POST_SPEECH_MOTION_DELAY_S == 2.0


class TestBridgeSpeechGaps:
    def test_short_gap_between_two_speech_statics_bridged(self):
        # 两个锚着语音的静止块之间 4s 运动间隙（≤6s）→ 间隙并入静止（问答块一停到底）
        scenes = [_static(0.0, 4.0), _motion(4.0, 8.0), _static(8.0, 12.0)]
        sw = [(1.0, 3.5), (9.0, 11.5)]
        storyboard._bridge_speech_gaps(scenes, sw)
        assert scenes[1]["params"].get("static") is True
        assert scenes[1]["params"]["ball_color"] == style.CREAM

    def test_long_gap_not_bridged(self):
        # 间隙 8s（>6s，下一句在远处）→ 运动照常恢复，不并
        scenes = [_static(0.0, 4.0), _motion(4.0, 12.0), _static(12.0, 16.0)]
        sw = [(1.0, 3.5), (13.0, 15.5)]
        storyboard._bridge_speech_gaps(scenes, sw)
        assert not scenes[1]["params"].get("static")

    def test_multi_scene_gap_all_bridged(self):
        # color_cycle 把间隙切成多段运动子场景 → 整段（总 4s ≤6s）全部并入静止
        scenes = [_static(0.0, 4.0), _motion(4.0, 6.0, style.GOLD),
                  _motion(6.0, 8.0, style.BURGUNDY), _static(8.0, 12.0)]
        sw = [(1.0, 3.5), (9.0, 11.5)]
        storyboard._bridge_speech_gaps(scenes, sw)
        assert all(scenes[i]["params"].get("static") for i in (1, 2))

    def test_gap_not_bridged_when_a_side_has_no_speech(self):
        # 右侧静止无语音锚（纯组间休息 run）→ 不桥接（保「语音窗到远处」运动恢复语义）
        scenes = [_static(0.0, 4.0), _motion(4.0, 8.0), _static(8.0, 12.0)]
        sw = [(1.0, 3.5)]                                     # 只锚左侧
        storyboard._bridge_speech_gaps(scenes, sw)
        assert not scenes[1]["params"].get("static")

    def test_empty_speech_windows_noop(self):
        scenes = [_static(0.0, 4.0), _motion(4.0, 8.0), _static(8.0, 12.0)]
        before = [dict(s["params"]) for s in scenes]
        storyboard._bridge_speech_gaps(scenes, [])
        assert [dict(s["params"]) for s in scenes] == before

    def test_total_duration_unchanged(self):
        scenes = [_static(0.0, 4.0), _motion(4.0, 8.0), _static(8.0, 12.0)]
        storyboard._bridge_speech_gaps(scenes, [(1.0, 3.5), (9.0, 11.5)])
        assert scenes[0]["t0"] == 0.0 and scenes[-1]["t1"] == 12.0    # 只改分类不改边界


class TestMergeAdjacentStatics:
    def test_consecutive_statics_merged(self):
        scenes = [_static(0.0, 4.0), _static(4.0, 8.0), _motion(8.0, 10.0)]
        storyboard._merge_adjacent_statics(scenes)
        assert len(scenes) == 2
        assert (scenes[0]["t0"], scenes[0]["t1"]) == (0.0, 8.0)
        assert scenes[0]["params"].get("static")

    def test_static_motion_static_not_merged(self):
        scenes = [_static(0.0, 4.0), _motion(4.0, 8.0), _static(8.0, 12.0)]
        storyboard._merge_adjacent_statics(scenes)
        assert len(scenes) == 3


class TestTrimStaticSpeechTails:
    def test_long_tail_trimmed_to_speech_plus_delay_on_grid(self):
        # 静止块含语音（末句结束 5.0），块拖到 15.0、其后紧邻运动 → 裁到 phase_floor(5+2)=7.0（落栅格）
        scenes = [_static(0.0, 15.0), _motion(15.0, 25.0)]
        sw = [(3.0, 5.5)]                                     # 末句语音结束 = 5.0
        storyboard._trim_static_speech_tails(scenes, sw, period=2.0, fps=style.FPS)
        expect = timeline.phase_floor(7.0, period=2.0, fps=style.FPS)
        assert scenes[0]["t1"] == expect and scenes[1]["t0"] == expect
        assert expect <= 7.0 + 1e-9                          # 静止尾 ≤ 语音结束+延迟
        h = 2.0 / 2
        assert abs(expect - round(expect / h) * h) <= 1.0 / style.FPS + 1e-9   # 落 k*(T/2) 栅格

    def test_short_tail_not_trimmed(self):
        # 尾巴 ≤2s（说完球很快就该动）→ 不裁
        scenes = [_static(0.0, 6.5), _motion(6.5, 16.0)]
        sw = [(3.0, 5.5)]                                     # 末句结束 5.0，尾巴 1.5 ≤ 2
        storyboard._trim_static_speech_tails(scenes, sw, period=2.0, fps=style.FPS)
        assert scenes[0]["t1"] == 6.5

    def test_speechless_rest_run_not_trimmed(self):
        # 纯组间休息 run（无任何语音锚）→ 绝不裁（原片功能性留白保全）
        scenes = [_static(0.0, 20.0), _motion(20.0, 30.0)]
        storyboard._trim_static_speech_tails(scenes, [(100.0, 102.0)], period=2.0, fps=style.FPS)
        assert scenes[0]["t1"] == 20.0

    def test_static_not_followed_by_motion_not_trimmed(self):
        # 其后是卡片（非运动）→ 不裁（无「早点动起来」的对象）
        card = {"t0": 15.0, "t1": 20.0, "type": "text_card", "renderer": "still_image",
                "content": {"title": "", "body": ""}, "transition": "fade"}
        scenes = [_static(0.0, 15.0), card]
        storyboard._trim_static_speech_tails(scenes, [(3.0, 5.5)], period=2.0, fps=style.FPS)
        assert scenes[0]["t1"] == 15.0

    def test_trim_preserves_total_duration(self):
        scenes = [_static(0.0, 15.0), _motion(15.0, 25.0)]
        storyboard._trim_static_speech_tails(scenes, [(3.0, 5.5)], period=2.0, fps=style.FPS)
        assert scenes[0]["t0"] == 0.0 and scenes[-1]["t1"] == 25.0    # 边界左移，总时长不变


class TestSpeechRhythmEndToEnd:
    @pytest.mark.asyncio
    async def test_close_sentences_bridged_into_one_stop(self):
        # 端到端（桥接）：运动 run 内两句提问间隔很近 → 中间不再有运动（问答块一停到底）
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 60.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.0, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},
            {"start": 20.0, "end": 22.0, "en": "q1", "zh": "现在有什么感觉"},   # 落 ball run
            {"start": 24.0, "end": 26.0, "en": "q2", "zh": "就停留在这里"},     # 紧接上一句
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=60.0, segments=segs, clip_durations=[2.0, 2.0, 2.0])
        retimed = sb["retimed_segments"]
        w0 = min(s["start"] for s in retimed if s["zh"] in ("现在有什么感觉", "就停留在这里"))
        w1 = max(s["end"] for s in retimed if s["zh"] in ("现在有什么感觉", "就停留在这里"))
        motions = [s for s in sb["scenes"]
                   if s["type"] == "ball_exercise" and not s["params"].get("static")]
        # 两句语音跨度内无任何运动场景（间隙已并入静止）
        for m in motions:
            assert min(w1, m["t1"]) - max(w0, m["t0"]) <= 1e-9
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_rest_run_with_speech_tail_trimmed(self):
        # 端到端（尾巴裁剪，复刻 job5 25:50）：静止休息 run 内有一句指令语音，说完后静止拖很久
        # 才接运动 run → 裁到 语音结束+2s，后续运动提前起（总时长不变、validate 过）。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 30.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.0, "period_estimated": True},
            {"t0": 30.0, "t1": 50.0, "kind": "ball_exercise", "static": True},   # 组间休息 run
            {"t0": 50.0, "t1": 70.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 2.0, "period_estimated": True},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "a", "zh": "引言"},
            {"start": 35.0, "end": 37.0, "en": "q", "zh": "看着小球说出它的颜色"},  # 落休息 run
        ]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            sb = await storyboard.build_storyboard(
                facts, duration=70.0, segments=segs, clip_durations=[2.0, 2.0])
        instr = next(s for s in sb["retimed_segments"] if s["zh"] == "看着小球说出它的颜色")
        speech_end = instr["end"]
        scenes = sb["scenes"]
        rest = next(s for s in scenes if s["type"] == "ball_exercise"
                    and s["params"].get("static") and s["t0"] <= speech_end < s["t1"] + 20)
        idx = scenes.index(rest)
        # 休息 run 静止尾 ≤ 语音结束+2s（吸附容差内），其后紧邻运动
        assert rest["t1"] <= speech_end + storyboard._POST_SPEECH_MOTION_DELAY_S + 1e-6
        assert not scenes[idx + 1]["params"].get("static")
        # 总时长不变、校验全过
        assert sb["source"]["duration_s"] == scenes[-1]["t1"]
        storyboard.validate_storyboard(sb)


class TestReorchestrateRounds:
    """摆动组临床剂量重编排（第五轮反馈：EMDR 每组 24-40 轮，一左一右=1 轮=一个周期 T）。

    仅弹性模式 + 给出 set_rounds_range 时启用；缺省 None 时输出与基线逐字节一致（恒等锚）。
    """
    T = 2.5                                             # 测试统一周期（period_s=2.5 → 整轮好算）

    async def _build(self, facts, segs, clip_durations, srr, **kw):
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            return await storyboard.build_storyboard(
                facts, duration=kw.pop("duration"), segments=[dict(s) for s in segs],
                clip_durations=clip_durations, period_s=self.T, set_rounds_range=srr, **kw)

    @staticmethod
    def _is_motion(s):
        return s["type"] == "ball_exercise" and not s["params"].get("static")

    @staticmethod
    def _is_static(s):
        return s["type"] == "ball_exercise" and s["params"].get("static")

    def _motion_group_rounds(self, scenes):
        """连续运动场景聚成一组（组内色块 motion↔motion 不隔断），返回各组四舍五入轮数。"""
        rounds, run = [], None
        for sc in scenes:
            if self._is_motion(sc):
                run = [sc["t0"], sc["t1"]] if run is None else [run[0], sc["t1"]]
            elif run is not None:
                rounds.append(round((run[1] - run[0]) / self.T)); run = None
        if run is not None:
            rounds.append(round((run[1] - run[0]) / self.T))
        return rounds

    # ---- 纯 helper 单测 ----

    def test_clamp_rounds_helper(self):
        assert storyboard._clamp_rounds(5, 10, 20) == 10        # 太少 → 抬到 min
        assert storyboard._clamp_rounds(50, 10, 20) == 20       # 太多 → 压到 max
        assert storyboard._clamp_rounds(14, 10, 20) == 14       # 区间内 → 保持
        assert storyboard._clamp_rounds(14.4, 10, 20) == 14     # 四舍五入到整轮

    def test_is_boundary_speech_question_or_check_keyword(self):
        assert storyboard._is_boundary_speech(["现在有什么感受？"])          # 疑问句
        assert storyboard._is_boundary_speech(["试着给这个画面打个分"])       # 检查语义关键词
        assert storyboard._is_boundary_speech(["就待在这里", "浮现什么想法呢？"])  # 任一句命中即边界

    def test_is_boundary_speech_instruction_is_narration(self):
        # 指令/鼓励类旁白不是边界（无疑问句、无检查语义关键词）→ 应前移
        assert not storyboard._is_boundary_speech(["看着球，大声说出它的颜色。"])
        assert not storyboard._is_boundary_speech(["继续保持专注。"])

    def test_build_groups_folds_narration_and_sums_motion(self):
        # [运动][旁白静止][运动][检查点静止][运动]：旁白折进本组、两段运动求和，检查点闭组
        scenes = [
            {"type": "ball_exercise", "t0": 6.0, "t1": 30.0, "params": {}},
            {"type": "ball_exercise", "t0": 30.0, "t1": 40.0, "params": {"static": True}},
            {"type": "ball_exercise", "t0": 40.0, "t1": 70.0, "params": {}},
            {"type": "ball_exercise", "t0": 70.0, "t1": 80.0, "params": {"static": True}},
            {"type": "ball_exercise", "t0": 80.0, "t1": 110.0, "params": {}},
        ]
        retimed = [{"start": 32.0, "end": 34.0, "zh": "看着球说颜色"},
                   {"start": 72.0, "end": 74.0, "zh": "有什么感受？"}]
        groups = storyboard._reorch_build_groups(
            [0, 1, 2, 3, 4], scenes, retimed, {1: [0], 3: [1]})
        assert len(groups) == 2
        assert groups[0]["narr"] == [0] and groups[0]["motion_dur"] == 54.0    # 24+30 求和
        assert groups[0]["bound"][0] == "speech"
        assert groups[1]["narr"] == [] and groups[1]["motion_dur"] == 30.0
        assert groups[1]["bound"] is None                                      # 末组无边界

    def test_build_groups_keeps_trailing_narration_after_last_motion(self):
        # 第五轮裁决：末组「最后一段摆动之后」的旁白（其后无运动/检查点）不前移，留 trailing；
        # 而两段运动之间的旁白仍前移（保组内不间断）。序列 [运动][旁白N1][运动][旁白N2]（无边界）。
        scenes = [
            {"type": "ball_exercise", "t0": 6.0, "t1": 30.0, "params": {}},
            {"type": "ball_exercise", "t0": 30.0, "t1": 40.0, "params": {"static": True}},
            {"type": "ball_exercise", "t0": 40.0, "t1": 70.0, "params": {}},
            {"type": "ball_exercise", "t0": 70.0, "t1": 80.0, "params": {"static": True}},
        ]
        retimed = [{"start": 32.0, "end": 34.0, "zh": "看着球说颜色"},       # 两运动之间 → 前移
                   {"start": 72.0, "end": 74.0, "zh": "慢慢睁开眼睛。"}]      # 末段摆动之后 → 留 trailing
        groups = storyboard._reorch_build_groups(
            [0, 1, 2, 3], scenes, retimed, {1: [0], 3: [1]})
        assert len(groups) == 1
        assert groups[0]["narr"] == [0]                                       # N1 前移进组开头
        assert groups[0]["trailing"] == [1]                                   # N2 留在摆动之后
        assert groups[0]["motion_dur"] == 54.0 and groups[0]["bound"] is None

    # ---- 集成（build_storyboard）----

    def _big_facts(self):
        # 一条长运动 run（~118 轮），被旁白 + 检查点 + 旁白 carve 成两组
        return {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 306.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.5, "period_estimated": True},
        ], "warnings": []}

    def _big_segs(self):
        return [
            {"start": 1.0, "end": 3.0, "en": "i", "zh": "引言"},               # 落 card
            {"start": 50.0, "end": 53.0, "en": "n1", "zh": "看着球，大声说出颜色。"},  # 旁白（前移）
            {"start": 150.0, "end": 153.0, "en": "q", "zh": "现在有什么感受？"},    # 检查点（边界）
            {"start": 250.0, "end": 253.0, "en": "n2", "zh": "继续保持专注。"},      # 旁白（前移）
        ]

    @pytest.mark.asyncio
    async def test_clamps_each_group_to_range(self):
        sb = await self._build(self._big_facts(), self._big_segs(),
                               [2.0, 3.0, 3.0, 3.0], [10, 20], duration=306.0)
        rounds = self._motion_group_rounds(sb["scenes"])
        assert len(rounds) == 2                                    # 检查点把 run 分成两组
        assert all(10 <= r <= 20 for r in rounds), rounds         # 各组轮数 ∈[min,max]
        assert rounds == [20, 20]                                  # 原 ~55 轮 → 压到 max=20
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_clamps_short_group_up_to_min(self):
        # 单条短运动（~8 轮）无 carve → 一组，clamp 抬到 min=10
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 26.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.5, "period_estimated": True},
        ], "warnings": []}
        sb = await self._build(facts, [{"start": 1.0, "end": 3.0, "en": "i", "zh": "引言"}],
                               [2.0], [10, 20], duration=26.0)
        assert self._motion_group_rounds(sb["scenes"]) == [10]
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_zero_speech_within_motion_groups(self):
        # 组内语音=零：任一台词句语音窗 [start,end+0.5] 不与任何运动场景相交（真正互斥）
        sb = await self._build(self._big_facts(), self._big_segs(),
                               [2.0, 3.0, 3.0, 3.0], [10, 20], duration=306.0)
        motions = [s for s in sb["scenes"] if self._is_motion(s)]
        for seg in sb["retimed_segments"]:
            if seg.get("start") is None or seg.get("no_dub"):
                continue
            w0, w1 = seg["start"], seg["end"] + 0.5
            for m in motions:
                assert min(w1, m["t1"]) - max(w0, m["t0"]) <= 1e-6, \
                    f"句 {seg['zh']} 落在运动组内 [{m['t0']},{m['t1']}]"

    @pytest.mark.asyncio
    async def test_narration_folds_into_opening_static(self):
        # 零散旁白前移：旁白句落在某静止场景内，且该静止在其组运动之前（组开头）
        sb = await self._build(self._big_facts(), self._big_segs(),
                               [2.0, 3.0, 3.0, 3.0], [10, 20], duration=306.0)
        narr = next(s for s in sb["retimed_segments"] if s["zh"] == "看着球，大声说出颜色。")
        scenes = sb["scenes"]
        host = next(s for s in scenes if self._is_static(s)
                    and s["t0"] - 1e-6 <= narr["start"] and narr["end"] <= s["t1"] + 1e-6)
        # 该静止窗之后紧邻的是运动（旁白说完球才起摆）
        assert self._is_motion(scenes[scenes.index(host) + 1])

    @pytest.mark.asyncio
    async def test_trailing_narration_stays_after_last_swing(self):
        # 第五轮裁决：末组尾部旁白（如「慢慢睁开眼睛」，其后不再有运动/检查点）保留在末组摆动之后
        # 的静止窗，不前移；成片顺序 = [...最后一组摆动][睁眼静止窗][结尾卡]。
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 100.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.5, "period_estimated": True},
            {"t0": 100.0, "t1": 116.0, "kind": "ball_exercise",           # 尾部旁白所在静止（原片）
             "ball_color_hex": "#FFFFFF", "static": True},
            {"t0": 116.0, "t1": 130.0, "kind": "text_card", "text": "end"},
        ], "warnings": []}
        segs = [
            {"start": 1.0, "end": 3.0, "en": "i", "zh": "引言"},
            {"start": 103.0, "end": 105.0, "en": "e", "zh": "慢慢睁开眼睛。"},   # 末段摆动之后
            {"start": 120.0, "end": 123.0, "en": "c", "zh": "练习到这里就结束了。"},  # 结尾卡
        ]
        sb = await self._build(facts, segs, [2.0, 2.0, 3.0], [10, 20], duration=130.0)
        scenes = sb["scenes"]
        narr = next(s for s in sb["retimed_segments"] if s["zh"] == "慢慢睁开眼睛。")
        last_motion = max((s for s in scenes if self._is_motion(s)), key=lambda s: s["t1"])
        # 「慢慢睁开眼睛」落在最后一组摆动之后（不前移）
        assert narr["start"] > last_motion["t1"] - 1e-6
        host = next(s for s in scenes if self._is_static(s)
                    and s["t0"] - 1e-6 <= narr["start"] and narr["end"] <= s["t1"] + 1e-6)
        hi = scenes.index(host)
        assert self._is_motion(scenes[hi - 1])                       # 其前紧邻末组摆动
        assert scenes[hi + 1]["renderer"] == "still_image"           # 其后紧邻结尾卡
        assert not any(self._is_motion(s) for s in scenes[hi + 1:])  # 尾部旁白后再无摆动
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_functional_rest_is_group_boundary(self):
        # 功能性留白 rest（≥8s 无语音）= 天然组边界，把两段运动分成两组，且 rest 保留原时长
        facts = {"scenes": [
            {"t0": 0.0, "t1": 6.0, "kind": "title_card", "text": "intro"},
            {"t0": 6.0, "t1": 36.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 2.5, "period_estimated": True},
            {"t0": 36.0, "t1": 50.0, "kind": "ball_exercise", "static": True},   # 14s rest 无语音
            {"t0": 50.0, "t1": 80.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 2.5, "period_estimated": True},
        ], "warnings": []}
        sb = await self._build(facts, [{"start": 1.0, "end": 3.0, "en": "i", "zh": "引言"}],
                               [2.0], [10, 20], duration=80.0)
        assert len(self._motion_group_rounds(sb["scenes"])) == 2      # rest 分成两组
        statics = [s for s in sb["scenes"] if self._is_static(s)]
        assert statics and max(s["t1"] - s["t0"] for s in statics) >= 12.0  # rest 原时长保留（未缩成语音窗）
        storyboard.validate_storyboard(sb)

    @pytest.mark.asyncio
    async def test_identity_when_set_rounds_range_none(self):
        # 恒等锚：srr=None 与不传逐字节一致（重编排 pass 门控在 set_rounds_range，不改 None 路径）
        facts, segs = self._big_facts(), self._big_segs()
        cd = [2.0, 3.0, 3.0, 3.0]
        with patch.object(storyboard, "_chat_localize", AsyncMock(return_value={})):
            omitted = await storyboard.build_storyboard(
                facts, duration=306.0, segments=[dict(s) for s in segs],
                clip_durations=cd, period_s=self.T)
            explicit_none = await storyboard.build_storyboard(
                facts, duration=306.0, segments=[dict(s) for s in segs],
                clip_durations=cd, period_s=self.T, set_rounds_range=None)
        import json as _json
        assert (_json.dumps(omitted, sort_keys=True, ensure_ascii=False)
                == _json.dumps(explicit_none, sort_keys=True, ensure_ascii=False))

    @pytest.mark.asyncio
    async def test_reorch_preserves_fill_and_anchor_invariants(self):
        sb = await self._build(self._big_facts(), self._big_segs(),
                               [2.0, 3.0, 3.0, 3.0], [10, 20], duration=306.0)
        scenes = sb["scenes"]
        for a, b in zip(scenes, scenes[1:]):
            assert a["t1"] == pytest.approx(b["t0"], abs=1e-9)        # 铺满/衔接
            assert b["t1"] > b["t0"]                                  # 无零长残留
        assert scenes[0]["t0"] == 0.0                                 # 首锚
        assert sb["source"]["duration_s"] == scenes[-1]["t1"]         # 末锚
        storyboard.validate_storyboard(sb)
