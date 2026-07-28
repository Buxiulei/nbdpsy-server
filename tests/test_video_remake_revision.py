"""revision（成片修订 v1 纯逻辑层）测试。

B1：parse_instructions（各 EditOp 类型解析 / 混合多 op / 解析失败带 LLM 说明 /
    prompt 携带台词下标+时间+场景摘要+schema）+ validate_edit_plan（op type / index 越界 /
    scene_id / 未知键 / 缺字段）。
B2：apply_edits（script_* 作用 rewritten 副本 / card_edit|ball_style|global_param 写覆盖结构 /
    多 op 混合不错位 / 非法 index fail-fast）——见文件后半。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.remake import revision
from app.video.pipeline.remake.revision import EditPlanError

pytestmark = pytest.mark.unit


# 样例台词（rewritten 结构：start/end/en/zh/orig_start/orig_end[/no_dub]）
REWRITTEN = [
    {"start": 0.0, "end": 3.0, "en": "Welcome.", "zh": "欢迎来到本次练习。",
     "orig_start": 0.0, "orig_end": 3.0},
    {"start": 3.0, "end": 7.0, "en": "Close your eyes.", "zh": "请闭上眼睛。",
     "orig_start": 3.0, "orig_end": 7.0},
    {"start": 7.0, "end": 12.0, "en": "Notice what comes up.", "zh": "留意此刻浮现的感受。",
     "orig_start": 7.0, "orig_end": 12.0},
]

# 样例分镜（场景 id / 类型 / 时间 / content|params）
STORYBOARD = {
    "version": 1, "style": "nbdpsy_v1",
    "source": {"duration_s": 20.0},
    "scenes": [
        {"id": 1, "t0": 0.0, "t1": 5.0, "type": "title_card",
         "renderer": "still_image", "content": {"title": "使用须知"}},
        {"id": 2, "t0": 5.0, "t1": 15.0, "type": "ball_exercise",
         "renderer": "programmatic",
         "params": {"ball_color": "#7B2D3B", "period_s": 2.5, "static": False}},
        {"id": 3, "t0": 15.0, "t1": 20.0, "type": "text_card",
         "renderer": "still_image", "content": {"title": "", "body": "练习结束"}},
    ],
}


async def _parse(content: str, instructions="随便改改"):
    """用 mock LLM（返回 content）跑 parse_instructions。

    换 import 面：源 get_llm(_LLM_KEY).chat(...).content → 薄 provider llm_chat 直返字符串，
    故打桩 revision.llm_chat 的 AsyncMock 直接返回文本，返回该 fake 供断言查 call_args。
    """
    fake = AsyncMock(return_value=content)
    with patch.object(revision, "llm_chat", fake):
        return await revision.parse_instructions(instructions, REWRITTEN, STORYBOARD), fake


# ---------------- B1: parse_instructions ----------------

class TestParseInstructions:
    @pytest.mark.asyncio
    async def test_parse_script_edit(self):
        arr = [{"type": "script_edit", "index": 0, "new_text": "欢迎你来到这次练习。"}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_script_delete(self):
        arr = [{"type": "script_delete", "index": 1}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_script_insert(self):
        arr = [{"type": "script_insert", "after_index": 0, "text": "先做几次深呼吸。"}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_card_edit(self):
        arr = [{"type": "card_edit", "scene_id": 3, "body": "本次练习到此结束"}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_ball_style(self):
        arr = [{"type": "ball_style", "y_ratio": 0.5, "period_s": 2.2}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_global_param(self):
        arr = [{"type": "global_param", "closing_line": "好，练习结束。"}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_scene_edit(self):
        # 第三轮扩展：把运动球段转静止
        arr = [{"type": "scene_edit", "scene_id": 2, "static": True}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_ball_style_color_cycle_periods(self):
        # 第三轮扩展：每晃一组变色
        arr = [{"type": "ball_style", "color_cycle_periods": 1}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_ball_style_set_rounds_range(self):
        # 第五轮扩展：摆动组临床剂量区间
        arr = [{"type": "ball_style", "set_rounds_range": [24, 40]}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_prompt_mentions_set_rounds_range(self):
        _, fake = await _parse(
            json.dumps([{"type": "ball_style", "set_rounds_range": [24, 40]}]))
        prompt = fake.call_args.kwargs["messages"][0]["content"]
        # schema 提及键名 + 「一左一右算一轮」措辞，LLM 才能命中「每组 24-40 轮」类剂量诉求
        assert "set_rounds_range" in prompt and "一左一右" in prompt

    @pytest.mark.asyncio
    async def test_parse_card_edit_duration_s(self):
        # 反馈②：改卡片页面停留时长
        arr = [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0}]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_prompt_mentions_card_duration(self):
        _, fake = await _parse(
            json.dumps([{"type": "card_edit", "scene_id": 3, "duration_s": 8}]))
        prompt = fake.call_args.kwargs["messages"][0]["content"]
        assert "duration_s" in prompt              # schema 提及卡片时长键，LLM 才能命中「停留太长」

    @pytest.mark.asyncio
    async def test_parse_mixed_multi_op(self):
        arr = [
            {"type": "script_edit", "index": 0, "new_text": "欢迎。"},
            {"type": "script_insert", "after_index": 1, "text": "慢慢来。"},
            {"type": "card_edit", "scene_id": 1, "title": "开始前"},
            {"type": "ball_style", "y_ratio": 0.5},
        ]
        ops, _ = await _parse(json.dumps(arr, ensure_ascii=False))
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_extracts_array_embedded_in_text(self):
        # LLM 前后夹带解释文字/代码围栏，仍能抠出数组
        arr = [{"type": "script_delete", "index": 2}]
        content = "好的，我的理解是：\n```json\n" + json.dumps(arr) + "\n```\n以上。"
        ops, _ = await _parse(content)
        assert ops == arr

    @pytest.mark.asyncio
    async def test_parse_failure_raises_with_llm_detail(self):
        # LLM 无法解析 → 返回自然语言说明（无 JSON 数组）→ EditPlanError.detail 含该说明
        msg = "抱歉，你的意见我没有理解，请具体说明要改哪一句。"
        with pytest.raises(EditPlanError) as ei:
            await _parse(msg)
        assert msg in ei.value.detail

    @pytest.mark.asyncio
    async def test_parse_empty_array_raises(self):
        # 空清单也算解析失败，detail 保留 LLM 原文（含其说明）
        with pytest.raises(EditPlanError) as ei:
            await _parse("我找不到可执行的修改。\n[]")
        assert "找不到" in ei.value.detail

    @pytest.mark.asyncio
    async def test_parse_non_json_garbage_raises(self):
        with pytest.raises(EditPlanError):
            await _parse("[这不是合法 JSON")

    @pytest.mark.asyncio
    async def test_parse_prompt_carries_script_index_time_and_scenes_and_schema(self):
        _, fake = await _parse(json.dumps([{"type": "script_delete", "index": 0}]),
                               instructions="把第一句改自然些")
        prompt = fake.call_args.kwargs["messages"][0]["content"]
        # 用户意见入 prompt
        assert "把第一句改自然些" in prompt
        # 台词下标 + 时间 + 文本
        assert "[0]" in prompt and "0.0-3.0s" in prompt and "欢迎来到本次练习" in prompt
        # 场景摘要（id/类型/时间）
        assert "场景 2" in prompt and "ball_exercise" in prompt and "5.0-15.0s" in prompt
        # 操作 schema（八种 EditOp 类型名齐全，含第三轮扩展 scene_edit + color_cycle_periods）
        for t in ("script_edit", "script_delete", "script_insert",
                  "card_edit", "ball_style", "global_param",
                  "scene_edit", "color_cycle_periods"):
            assert t in prompt

    # 源 test_parse_uses_realtime_urgent（断言 llm.chat 传 urgent=True）不迁：薄 provider
    # llm_chat 无 urgent 概念（实时/batch 分流机制不进本宿主），该断言无对应面。


# ---------------- B1: validate_edit_plan ----------------

class TestValidateEditPlan:
    def test_validate_ok_all_types(self):
        ops = [
            {"type": "script_edit", "index": 0, "new_text": "改"},
            {"type": "script_delete", "index": 2},
            {"type": "script_insert", "after_index": 1, "text": "插"},
            {"type": "card_edit", "scene_id": 1, "title": "标题"},
            {"type": "card_edit", "scene_id": 3, "body": "正文"},
            {"type": "ball_style", "y_ratio": 0.5, "palette": ["#fff"]},
            {"type": "global_param", "sentence_gap": 0.4},
        ]
        assert revision.validate_edit_plan(ops, REWRITTEN, STORYBOARD) is None

    def test_validate_illegal_op_type(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan([{"type": "frobnicate"}], REWRITTEN, STORYBOARD)
        assert "frobnicate" in ei.value.detail

    def test_validate_script_index_out_of_bounds(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "script_edit", "index": 99, "new_text": "x"}],
                REWRITTEN, STORYBOARD)
        assert "越界" in ei.value.detail

    def test_validate_insert_after_index_out_of_bounds(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "script_insert", "after_index": 5, "text": "x"}],
                REWRITTEN, STORYBOARD)

    def test_validate_negative_index(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "script_delete", "index": -1}], REWRITTEN, STORYBOARD)

    def test_validate_scene_id_not_exist(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "card_edit", "scene_id": 99, "title": "x"}],
                REWRITTEN, STORYBOARD)
        assert "不存在" in ei.value.detail

    def test_validate_card_edit_on_non_card_scene(self):
        # scene 2 是球段，card_edit 不适用 → fail-fast
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "card_edit", "scene_id": 2, "title": "x"}],
                REWRITTEN, STORYBOARD)
        assert "卡片" in ei.value.detail

    def test_validate_card_edit_needs_title_or_body(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "card_edit", "scene_id": 1}], REWRITTEN, STORYBOARD)
        assert "duration_s" in ei.value.detail    # title/body/duration_s 三者至少一个

    # ---- 反馈②：card_edit.duration_s（改卡片页面停留时长）校验边界 ----

    def test_validate_card_edit_duration_ok(self):
        # scene 3 [15,20] 无配音句 → 下限=LEAD+TAIL=2.0，给 8.0 合法
        assert revision.validate_edit_plan(
            [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0}],
            REWRITTEN, STORYBOARD) is None

    def test_validate_card_edit_duration_only_no_title_body_ok(self):
        # 只给 duration_s（不改文案）合法
        assert revision.validate_edit_plan(
            [{"type": "card_edit", "scene_id": 3, "duration_s": 5.0}],
            REWRITTEN, STORYBOARD) is None

    def test_validate_card_edit_duration_too_short_reports_min(self):
        # scene 1 [0,5] 含两句配音（3.0/4.0s）→ 紧排下限=0.5+7+1.0+1.5=10.0；给 3.0 → fail 报最小值
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "card_edit", "scene_id": 1, "duration_s": 3.0}],
                REWRITTEN, STORYBOARD)
        assert "至少" in ei.value.detail and "10.0" in ei.value.detail

    def test_validate_card_edit_duration_non_positive(self):
        for bad in (0, -5, "8", True):
            with pytest.raises(EditPlanError) as ei:
                revision.validate_edit_plan(
                    [{"type": "card_edit", "scene_id": 3, "duration_s": bad}],
                    REWRITTEN, STORYBOARD)
            assert "正数" in ei.value.detail

    def test_validate_ball_style_unknown_key(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "ball_style", "wobble": 3}], REWRITTEN, STORYBOARD)
        assert "wobble" in ei.value.detail

    def test_validate_ball_style_empty(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "ball_style"}], REWRITTEN, STORYBOARD)

    def test_validate_ball_style_color_mode_valid(self):
        # M4a：color_mode 仅接受 cycle|single
        for cm in ("cycle", "single"):
            assert revision.validate_edit_plan(
                [{"type": "ball_style", "color_mode": cm}], REWRITTEN, STORYBOARD) is None

    def test_validate_ball_style_color_mode_illegal(self):
        # M4a：非法 color_mode → EditPlanError（否则 storyboard 静默走轮播 no-op）
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "ball_style", "color_mode": "rainbow"}], REWRITTEN, STORYBOARD)
        assert "rainbow" in ei.value.detail

    # ---- 第三轮扩展：scene_edit / color_cycle_periods 校验边界 ----

    def test_validate_scene_edit_ok(self):
        assert revision.validate_edit_plan(
            [{"type": "scene_edit", "scene_id": 2, "static": True}],
            REWRITTEN, STORYBOARD) is None

    def test_validate_scene_edit_scene_not_exist(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "scene_edit", "scene_id": 99, "static": True}],
                REWRITTEN, STORYBOARD)
        assert "不存在" in ei.value.detail

    def test_validate_scene_edit_on_non_ball_scene(self):
        # scene 1 是卡片，scene_edit 不适用 → fail-fast
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "scene_edit", "scene_id": 1, "static": True}],
                REWRITTEN, STORYBOARD)
        assert "球段" in ei.value.detail

    def test_validate_scene_edit_static_must_be_true(self):
        for bad in (False, None, "true", 1):
            with pytest.raises(EditPlanError) as ei:
                revision.validate_edit_plan(
                    [{"type": "scene_edit", "scene_id": 2, "static": bad}],
                    REWRITTEN, STORYBOARD)
            assert "static" in ei.value.detail

    def test_validate_scene_edit_missing_static(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "scene_edit", "scene_id": 2}], REWRITTEN, STORYBOARD)

    def test_validate_scene_edit_unknown_key(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "scene_edit", "scene_id": 2, "static": True, "wobble": 1}],
                REWRITTEN, STORYBOARD)
        assert "wobble" in ei.value.detail

    def test_validate_color_cycle_periods_valid(self):
        for n in (1, 2, 5):
            assert revision.validate_edit_plan(
                [{"type": "ball_style", "color_cycle_periods": n}],
                REWRITTEN, STORYBOARD) is None

    def test_validate_color_cycle_periods_non_positive(self):
        for bad in (0, -1):
            with pytest.raises(EditPlanError) as ei:
                revision.validate_edit_plan(
                    [{"type": "ball_style", "color_cycle_periods": bad}],
                    REWRITTEN, STORYBOARD)
            assert "正整数" in ei.value.detail

    def test_validate_color_cycle_periods_non_int(self):
        # 1.5 / True(bool) 都不是正整数
        for bad in (1.5, True):
            with pytest.raises(EditPlanError):
                revision.validate_edit_plan(
                    [{"type": "ball_style", "color_cycle_periods": bad}],
                    REWRITTEN, STORYBOARD)

    # ---- 第五轮扩展：set_rounds_range（摆动组临床剂量区间）校验边界 ----

    def test_validate_set_rounds_range_ok(self):
        for rng in ([24, 40], [1, 1], [10, 100]):
            assert revision.validate_edit_plan(
                [{"type": "ball_style", "set_rounds_range": rng}],
                REWRITTEN, STORYBOARD) is None

    def test_validate_set_rounds_range_wrong_length(self):
        for bad in ([24], [24, 40, 60], []):
            with pytest.raises(EditPlanError) as ei:
                revision.validate_edit_plan(
                    [{"type": "ball_style", "set_rounds_range": bad}],
                    REWRITTEN, STORYBOARD)
            assert "两个正整数" in ei.value.detail

    def test_validate_set_rounds_range_non_positive_int(self):
        # 非整/非正/bool 都非法
        for bad in ([0, 40], [24, 0], [-1, 40], [24.5, 40], [True, 40], ["24", "40"]):
            with pytest.raises(EditPlanError) as ei:
                revision.validate_edit_plan(
                    [{"type": "ball_style", "set_rounds_range": bad}],
                    REWRITTEN, STORYBOARD)
            assert "正整数" in ei.value.detail

    def test_validate_set_rounds_range_min_gt_max(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "ball_style", "set_rounds_range": [40, 24]}],
                REWRITTEN, STORYBOARD)
        assert "不能大于" in ei.value.detail

    def test_validate_global_param_unknown_key(self):
        with pytest.raises(EditPlanError) as ei:
            revision.validate_edit_plan(
                [{"type": "global_param", "foo": 1}], REWRITTEN, STORYBOARD)
        assert "foo" in ei.value.detail

    def test_validate_missing_required_field(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "script_edit", "index": 0}], REWRITTEN, STORYBOARD)

    def test_validate_index_must_be_int_not_bool(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan(
                [{"type": "script_delete", "index": True}], REWRITTEN, STORYBOARD)

    def test_validate_non_dict_op(self):
        with pytest.raises(EditPlanError):
            revision.validate_edit_plan([42], REWRITTEN, STORYBOARD)


# ---------------- B2: apply_edits ----------------

class TestApplyEdits:
    def test_apply_script_edit(self):
        ops = [{"type": "script_edit", "index": 1, "new_text": "请慢慢闭上眼睛。"}]
        segs, _ = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert segs[1]["zh"] == "请慢慢闭上眼睛。"
        # 其余字段与其它句不动，orig_* 保留
        assert segs[1]["orig_start"] == 3.0 and segs[1]["en"] == "Close your eyes."
        assert segs[0]["zh"] == "欢迎来到本次练习。" and segs[2]["zh"] == "留意此刻浮现的感受。"
        assert len(segs) == 3

    def test_apply_script_delete(self):
        ops = [{"type": "script_delete", "index": 1}]
        segs, _ = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert [s["zh"] for s in segs] == ["欢迎来到本次练习。", "留意此刻浮现的感受。"]

    def test_apply_script_insert(self):
        ops = [{"type": "script_insert", "after_index": 0, "text": "先做几次深呼吸。"}]
        segs, _ = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert len(segs) == 4
        new = segs[1]
        assert new["zh"] == "先做几次深呼吸。"
        assert new["no_dub"] is False and new["en"] == ""
        # orig_* 继承邻句（下标 0）
        assert new["orig_start"] == 0.0 and new["orig_end"] == 3.0

    def test_apply_card_edit_writes_overrides(self):
        ops = [{"type": "card_edit", "scene_id": 3, "title": "收尾", "body": "练习结束"}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        # 键归一化为 str（与 JSON 回读的继承种子键一致）
        assert ov["cards"]["3"] == {"title": "收尾", "body": "练习结束"}

    def test_apply_card_edit_merges_str_seed_key_cross_layer(self):
        # 跨层：继承种子 cards 键是 str（JSON 回读），本层 card_edit 给 int scene_id——
        # 归一化后合并进同键，父层字段不丢（否则 "3" 与 3 键分裂）
        seed = {"cards": {"3": {"title": "父标题"}}, "ball": {}, "global": {}}
        ops = [{"type": "card_edit", "scene_id": 3, "body": "子正文"}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides=seed)
        assert list(ov["cards"].keys()) == ["3"]                    # 单键，不分裂
        assert ov["cards"]["3"] == {"title": "父标题", "body": "子正文"}

    def test_apply_multi_card_edit_same_scene_accumulates(self):
        # 同 scene_id 多 card_edit 在同一 overrides 上累积，不互相覆盖（param_overrides 原地累加）
        ops = [
            {"type": "card_edit", "scene_id": 3, "title": "收尾"},
            {"type": "card_edit", "scene_id": 3, "body": "练习结束"},
        ]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["cards"]["3"] == {"title": "收尾", "body": "练习结束"}

    def test_apply_ball_style_writes_overrides(self):
        ops = [{"type": "ball_style", "y_ratio": 0.5, "period_s": 2.2}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["ball"] == {"y_ratio": 0.5, "period_s": 2.2}

    def test_apply_scene_edit_writes_static_source_spans(self):
        # 端点 resolve 已 baked static_source_spans；apply 累积进 ball.static_source_spans
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True,
                "static_source_spans": [[10.0, 20.0]]}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["ball"]["static_source_spans"] == [[10.0, 20.0]]

    def test_apply_multi_scene_edit_accumulates(self):
        # 多条 scene_edit 各贡献一窗，同一 overrides 上追加不覆盖
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True,
                "static_source_spans": [[10.0, 20.0]]},
               {"type": "scene_edit", "scene_id": 3, "static": True,
                "static_source_spans": [[30.0, 40.0]]}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["ball"]["static_source_spans"] == [[10.0, 20.0], [30.0, 40.0]]

    def test_apply_scene_edit_without_baked_spans_is_noop(self):
        # 未 baked（理论上端点必 baked，防御）→ 不写 static_source_spans
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert "static_source_spans" not in ov["ball"]

    def test_apply_ball_style_color_cycle_periods_writes_override(self):
        ops = [{"type": "ball_style", "color_cycle_periods": 1}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["ball"] == {"color_cycle_periods": 1}

    def test_apply_ball_style_set_rounds_range_writes_override(self):
        # 第五轮扩展：set_rounds_range 落进 ball 覆盖（storyboard 消费做摆动组重编排）
        ops = [{"type": "ball_style", "set_rounds_range": [24, 40]}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["ball"] == {"set_rounds_range": [24, 40]}

    def test_apply_card_edit_duration_writes_override(self):
        # 端点 resolve 已 baked duration_source_span；apply 累积进 card_duration_overrides
        ops = [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0,
                "duration_source_span": [20.0, 24.0]}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["card_duration_overrides"] == [[20.0, 24.0, 8.0]]
        assert "3" not in ov["cards"]              # 纯 duration_s 不留空 content 条目

    def test_apply_card_edit_title_and_duration_both(self):
        ops = [{"type": "card_edit", "scene_id": 3, "title": "收尾", "duration_s": 8.0,
                "duration_source_span": [20.0, 24.0]}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["cards"]["3"] == {"title": "收尾"}
        assert ov["card_duration_overrides"] == [[20.0, 24.0, 8.0]]

    def test_apply_card_edit_duration_without_baked_span_noop(self):
        # 未 baked（理论上端点必 baked，防御）→ 不写 card_duration_overrides
        ops = [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert "card_duration_overrides" not in ov

    def test_apply_no_card_duration_key_when_absent(self):
        # 无 duration_s 覆盖 → overrides 不含该键（结构不变）
        _, ov = revision.apply_edits(
            [{"type": "card_edit", "scene_id": 3, "title": "x"}], REWRITTEN,
            param_overrides={})
        assert "card_duration_overrides" not in ov

    def test_apply_global_param_writes_overrides(self):
        ops = [{"type": "global_param", "closing_line": "好，练习结束。"}]
        _, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert ov["global"] == {"closing_line": "好，练习结束。"}

    def test_apply_overrides_structure_always_present(self):
        # 无 card/ball/global op 时三键仍存在（B4 消费侧结构稳定）
        _, ov = revision.apply_edits([], REWRITTEN, param_overrides={})
        assert ov == {"cards": {}, "ball": {}, "global": {}}

    def test_apply_returns_tuple_list_dict(self):
        out = revision.apply_edits([], REWRITTEN, param_overrides={})
        assert isinstance(out, tuple) and isinstance(out[0], list) and isinstance(out[1], dict)

    def test_apply_multi_op_no_offset(self):
        # 关键：delete/edit/insert 混合，下标全按原始列表定位，互不错位。
        # 原始 [0欢迎, 1闭眼, 2留意]：删 1、改 2、在 2 后插 X → [欢迎, 留意改, X]
        ops = [
            {"type": "script_delete", "index": 1},
            {"type": "script_edit", "index": 2, "new_text": "留意改"},
            {"type": "script_insert", "after_index": 2, "text": "X"},
        ]
        segs, _ = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert [s["zh"] for s in segs] == ["欢迎来到本次练习。", "留意改", "X"]
        # 插入句锚在原下标 2（留意句）后，orig_* 继承它
        assert segs[2]["orig_start"] == 7.0

    def test_apply_insert_after_deleted_index(self):
        # 删下标 0 并在其后插新句：新句仍落原位置，orig_* 继承已删邻句
        ops = [
            {"type": "script_delete", "index": 0},
            {"type": "script_insert", "after_index": 0, "text": "新开场"},
        ]
        segs, _ = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert [s["zh"] for s in segs] == ["新开场", "请闭上眼睛。", "留意此刻浮现的感受。"]
        assert segs[0]["orig_start"] == 0.0

    def test_apply_does_not_mutate_input(self):
        original = [dict(s) for s in REWRITTEN]
        ops = [{"type": "script_edit", "index": 0, "new_text": "变了"},
               {"type": "script_delete", "index": 1}]
        revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert REWRITTEN == original          # 入参未被修改

    def test_apply_illegal_index_fail_fast(self):
        with pytest.raises(EditPlanError) as ei:
            revision.apply_edits(
                [{"type": "script_edit", "index": 99, "new_text": "x"}],
                REWRITTEN, param_overrides={})
        assert "越界" in ei.value.detail

    def test_apply_illegal_after_index_fail_fast(self):
        with pytest.raises(EditPlanError):
            revision.apply_edits(
                [{"type": "script_insert", "after_index": 99, "text": "x"}],
                REWRITTEN, param_overrides={})

    def test_apply_end_to_end_with_validate(self):
        # 解析→校验→应用 全链：混合 op 通过 validate 后应用一致
        ops = [
            {"type": "script_edit", "index": 0, "new_text": "欢迎你。"},
            {"type": "card_edit", "scene_id": 1, "title": "开始前"},
            {"type": "ball_style", "y_ratio": 0.5},
        ]
        revision.validate_edit_plan(ops, REWRITTEN, STORYBOARD)   # 不抛
        segs, ov = revision.apply_edits(ops, REWRITTEN, param_overrides={})
        assert segs[0]["zh"] == "欢迎你。"
        assert ov["cards"]["1"] == {"title": "开始前"}
        assert ov["ball"] == {"y_ratio": 0.5}


# ---------------- 第三轮扩展：resolve_scene_edit_spans（scene_id → facts 源时间窗溯源） ----------------

class TestResolveSceneEditSpans:
    """把 scene_edit 的场景 id 溯源成 facts 源时间窗，抗子 job storyboard 场景 id 漂移。

    映射走卡片锚点 offset：弹性时间轴里卡片块被重排（改时长）、球块保时长，故连续球区内
    source = retimed - offset，offset 由该球区之前最近卡片边界确定。
    """

    def _elastic_case(self):
        # 卡片源 [0,10] 被重排拉长到 [0,25]（offset=15），随后运动球段源 [10,20] → 分镜 [25,35]
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "title_card", "text": "intro"},
            {"t0": 10.0, "t1": 20.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5},
        ]}
        storyboard = {"scenes": [
            {"id": 1, "t0": 0.0, "t1": 25.0, "type": "title_card",
             "renderer": "still_image", "content": {"title": "引言"}},
            {"id": 2, "t0": 25.0, "t1": 35.0, "type": "ball_exercise",
             "renderer": "programmatic",
             "params": {"ball_color": "#7A1F2B", "period_s": 1.5, "static": False}},
        ]}
        return facts, storyboard

    def test_resolve_maps_retimed_scene_to_source_span_via_card_offset(self):
        facts, storyboard = self._elastic_case()
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True}]
        revision.resolve_scene_edit_spans(ops, storyboard, facts)
        # 分镜球段 [25,35] 减去球区 offset=15 → 源窗 [10,20]（= facts 运动球段源区间）
        assert ops[0]["static_source_spans"] == [[10.0, 20.0]]

    def test_resolve_identity_when_no_facts(self):
        # facts 缺失（理论上不发生）→ 退化恒等（retimed==source），非弹性父片精确
        _, storyboard = self._elastic_case()
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True}]
        revision.resolve_scene_edit_spans(ops, storyboard, None)
        assert ops[0]["static_source_spans"] == [[25.0, 35.0]]

    def test_resolve_leaves_non_scene_edit_ops_untouched(self):
        facts, storyboard = self._elastic_case()
        ops = [{"type": "ball_style", "y_ratio": 0.5},
               {"type": "scene_edit", "scene_id": 2, "static": True}]
        revision.resolve_scene_edit_spans(ops, storyboard, facts)
        assert "static_source_spans" not in ops[0]           # ball_style 不动
        assert ops[1]["static_source_spans"] == [[10.0, 20.0]]

    def test_resolve_multi_scene_edit_each_gets_span(self):
        # 两卡片区各夹一球段，各自按所在球区 offset 溯源
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "title_card", "text": "a"},
            {"t0": 10.0, "t1": 20.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5},
            {"t0": 20.0, "t1": 24.0, "kind": "text_card", "text": "b"},
            {"t0": 24.0, "t1": 34.0, "kind": "ball_exercise",
             "ball_color_hex": "#A2C40C", "period_s": 1.5},
        ]}
        storyboard = {"scenes": [
            {"id": 1, "t0": 0.0, "t1": 25.0, "renderer": "still_image",
             "type": "title_card", "content": {}},                       # offset 15
            {"id": 2, "t0": 25.0, "t1": 35.0, "type": "ball_exercise",
             "renderer": "programmatic",
             "params": {"period_s": 1.5, "static": False}},
            {"id": 3, "t0": 35.0, "t1": 41.0, "renderer": "still_image",
             "type": "text_card", "content": {}},                        # 源 [20,24] → offset 17
            {"id": 4, "t0": 41.0, "t1": 51.0, "type": "ball_exercise",
             "renderer": "programmatic",
             "params": {"period_s": 1.5, "static": False}},
        ]}
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True},
               {"type": "scene_edit", "scene_id": 4, "static": True}]
        revision.resolve_scene_edit_spans(ops, storyboard, facts)
        assert ops[0]["static_source_spans"] == [[10.0, 20.0]]           # 41-17=24, 51-17=34
        assert ops[1]["static_source_spans"] == [[24.0, 34.0]]


# ---------- 反馈②：resolve_card_duration_spans（卡片 scene_id → facts 卡片源时间窗溯源） ----------

class TestResolveCardDurationSpans:
    """把带 duration_s 的 card_edit 场景 id 溯源成 facts 卡片源时间窗，抗子 job 卡片 id 漂移。

    卡片场景与 facts 非球场景 1:1 顺序对应（build_storyboard 对每个非球 facts 场景恰出一个
    still_image），据序反查父分镜卡片 id → facts 卡片 [t0,t1]。
    """

    def _case(self):
        # facts：卡片[0,10] / 球段[10,20] / 卡片[20,24]；分镜卡片被重排拉长（源窗才是稳定锚）
        facts = {"scenes": [
            {"t0": 0.0, "t1": 10.0, "kind": "title_card", "text": "intro"},
            {"t0": 10.0, "t1": 20.0, "kind": "ball_exercise",
             "ball_color_hex": "#FFFFFF", "period_s": 1.5},
            {"t0": 20.0, "t1": 24.0, "kind": "text_card", "text": "end"},
        ]}
        storyboard = {"scenes": [
            {"id": 1, "t0": 0.0, "t1": 25.0, "type": "title_card",
             "renderer": "still_image", "content": {}},
            {"id": 2, "t0": 25.0, "t1": 35.0, "type": "ball_exercise",
             "renderer": "programmatic", "params": {"period_s": 1.5, "static": False}},
            {"id": 3, "t0": 35.0, "t1": 41.0, "type": "text_card",
             "renderer": "still_image", "content": {}},
        ]}
        return facts, storyboard

    def test_resolve_maps_card_id_to_facts_window(self):
        facts, storyboard = self._case()
        ops = [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0}]
        revision.resolve_card_duration_spans(ops, storyboard, facts)
        assert ops[0]["duration_source_span"] == [20.0, 24.0]        # 分镜卡片3 ↔ facts 卡片[20,24]

    def test_resolve_first_card(self):
        facts, storyboard = self._case()
        ops = [{"type": "card_edit", "scene_id": 1, "duration_s": 8.0}]
        revision.resolve_card_duration_spans(ops, storyboard, facts)
        assert ops[0]["duration_source_span"] == [0.0, 10.0]

    def test_resolve_skips_card_edit_without_duration(self):
        facts, storyboard = self._case()
        ops = [{"type": "card_edit", "scene_id": 3, "title": "x"}]
        revision.resolve_card_duration_spans(ops, storyboard, facts)
        assert "duration_source_span" not in ops[0]                  # 无 duration_s 不溯源

    def test_resolve_leaves_other_ops_untouched(self):
        facts, storyboard = self._case()
        ops = [{"type": "scene_edit", "scene_id": 2, "static": True},
               {"type": "card_edit", "scene_id": 3, "duration_s": 8.0}]
        revision.resolve_card_duration_spans(ops, storyboard, facts)
        assert "duration_source_span" not in ops[0]                  # scene_edit 不动
        assert ops[1]["duration_source_span"] == [20.0, 24.0]

    def test_resolve_identity_when_no_facts(self):
        # facts 缺失（理论上不发生）→ 退化用分镜卡片自身区间
        _, storyboard = self._case()
        ops = [{"type": "card_edit", "scene_id": 3, "duration_s": 8.0}]
        revision.resolve_card_duration_spans(ops, storyboard, None)
        assert ops[0]["duration_source_span"] == [35.0, 41.0]
