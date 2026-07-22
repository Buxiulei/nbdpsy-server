"""rewriter（wave5 口播本土化重构）：新签名(无 cps) / 结构保持 / 场景定调+两条新硬规则+
few-shot 进 prompt / 反思环被调用且失败兜底 / 不再调 reflect_and_fit。

平移自 test_remake_rewriter.py。mock 面随 import 面更换：源 ``get_llm(_LLM_KEY).chat(...).content``
→ 薄 provider ``llm_chat(messages, temperature=) -> str``（直接返回正文字符串）。故打桩改为
patch ``rewriter.llm_chat`` 的 AsyncMock 直接返回文本，断言从 ``llm.chat.call_args`` 改看
``fake.call_args``。源 ``test_registry_has_video_remake_key``（registry key 概念）随薄 provider
下线，不迁。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.remake import rewriter

pytestmark = pytest.mark.unit

SEGS = [{"start": 0.0, "end": 4.0, "en": "hello", "zh": "你好，这里是原始翻译一"},
        {"start": 4.0, "end": 8.0, "en": "world", "zh": "这是原始翻译二"}]
TERMS = [{"en": "EMDR", "zh": "眼动脱敏与再加工", "source": "glossary"}]


def _fixes(*vals):
    return json.dumps({str(i): v for i, v in enumerate(vals)}, ensure_ascii=False)


class TestRewrite:
    @pytest.mark.asyncio
    async def test_rewrites_and_keeps_structure(self):
        # 初改与反思环都回同一 JSON（反思环幂等）：zh 被改，其余字段不动
        fake = AsyncMock(return_value=_fixes("改写后一", "改写后二"))
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert [s["zh"] for s in out] == ["改写后一", "改写后二"]
        assert out[0]["start"] == 0.0 and out[0]["en"] == "hello"
        # 术语表进了初改 prompt（第一次调用）
        first_prompt = fake.call_args_list[0].kwargs["messages"][0]["content"]
        assert "眼动脱敏与再加工" in first_prompt

    @pytest.mark.asyncio
    async def test_prompt_carries_scene_rules_and_fewshot(self):
        fake = AsyncMock(return_value=_fixes("改"))
        with patch.object(rewriter, "llm_chat", fake):
            await rewriter.rewrite_segments([SEGS[0]], TERMS)
        prompt = fake.call_args_list[0].kwargs["messages"][0]["content"]
        # 场景定调：闭眼跟随
        assert "闭上眼睛" in prompt
        # 保留的人名去除硬规则
        assert "人名" in prompt and "不得保留" in prompt
        # 新硬规则 a：平台机制话术本地化
        assert "描述中的链接" in prompt and "进度条" in prompt
        # 新硬规则 b：口头确认语不直译成质问
        assert "Got it" in prompt and "清楚了吗" in prompt
        # few-shot 关键句
        assert "刚才，你讲述了这段记忆现在的样子" in prompt
        assert "好，我们继续" in prompt

    @pytest.mark.asyncio
    async def test_reflection_loop_invoked(self):
        # 单批：初改 + 反思环 = 两次 LLM 调用；第二次是反思 prompt
        fake = AsyncMock(return_value=_fixes("初改一", "初改二"))
        with patch.object(rewriter, "llm_chat", fake):
            await rewriter.rewrite_segments(SEGS, TERMS)
        assert fake.await_count == 2
        reflect_prompt = fake.call_args_list[1].kwargs["messages"][0]["content"]
        assert "翻译腔" in reflect_prompt

    @pytest.mark.asyncio
    async def test_reflection_failure_keeps_first_pass(self):
        # 初改成功、反思环抛错 → 保留初改文本（优化不是闸门）
        fake = AsyncMock(side_effect=[_fixes("初改一", "初改二"),
                                      RuntimeError("reflect boom")])
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert [s["zh"] for s in out] == ["初改一", "初改二"]

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_original(self):
        fake = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert [s["zh"] for s in out] == [s["zh"] for s in SEGS]

    @pytest.mark.asyncio
    async def test_out_of_range_key_ignored(self):
        # LLM 幻觉越界下标 key 不得抛 IndexError / 越界写入
        fixes = json.dumps({"0": "改写后", "9": "幻觉"}, ensure_ascii=False)
        fake = AsyncMock(return_value=fixes)
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert len(out) == 2
        assert out[0]["zh"] == "改写后"
        assert out[1]["zh"] == SEGS[1]["zh"]

    @pytest.mark.asyncio
    async def test_non_json_response_keeps_original(self):
        fake = AsyncMock(return_value="抱歉，我无法完成。")
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert [s["zh"] for s in out] == [s["zh"] for s in SEGS]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        assert await rewriter.rewrite_segments([], TERMS) == []

    def test_no_longer_uses_reflect_and_fit(self):
        # 弹性时间轴后 slot 压缩失义，rewriter 不再引用 translator.reflect_and_fit
        assert not hasattr(rewriter, "reflect_and_fit")


class TestMarkNoDub:
    """A3 免责/须知台词不配音：orig 时间落入免责须知卡范围内的句子标 no_dub（通用规则）。"""

    def test_marks_sentence_inside_disclaimer_card(self):
        # 免责 text_card [10,20]：落入的句标 no_dub，块外的正常句不标
        facts = [{"kind": "text_card", "t0": 10.0, "t1": 20.0,
                  "text": "Disclaimer: this is not medical advice"}]
        segs = [{"start": 12.0, "end": 15.0, "zh": "免责台词"},
                {"start": 25.0, "end": 28.0, "zh": "正常台词"}]
        out = rewriter.mark_no_dub(segs, facts)
        assert out is segs                         # 原地修改并返回同一列表
        assert segs[0].get("no_dub") is True
        assert "no_dub" not in segs[1]

    def test_uses_orig_start_over_start(self):
        # 重排后 start 已移走，但 orig_start 落在免责卡内 → 仍按 orig 判定（幂等）
        facts = [{"kind": "text_card", "t0": 10.0, "t1": 20.0, "text": "免责声明"}]
        segs = [{"start": 99.0, "end": 100.0, "orig_start": 12.0,
                 "orig_end": 14.0, "zh": "x"}]
        rewriter.mark_no_dub(segs, facts)
        assert segs[0]["no_dub"] is True

    def test_non_disclaimer_card_not_marked(self):
        facts = [{"kind": "text_card", "t0": 0.0, "t1": 10.0, "text": "introduction"}]
        segs = [{"start": 5.0, "end": 8.0, "zh": "引言"}]
        rewriter.mark_no_dub(segs, facts)
        assert "no_dub" not in segs[0]

    def test_ball_scene_with_keyword_not_marked(self):
        # 判定限 title/text card：ball_exercise 即便文本含关键词也不算免责卡
        facts = [{"kind": "ball_exercise", "t0": 0.0, "t1": 10.0, "text": "免责"}]
        segs = [{"start": 5.0, "end": 8.0, "zh": "x"}]
        rewriter.mark_no_dub(segs, facts)
        assert "no_dub" not in segs[0]

    def test_no_disclaimer_scene_is_noop(self):
        facts = [{"kind": "ball_exercise", "t0": 0.0, "t1": 10.0, "text": ""}]
        segs = [{"start": 5.0, "end": 8.0, "zh": "x"}]
        rewriter.mark_no_dub(segs, facts)
        assert "no_dub" not in segs[0]

    def test_range_is_left_closed_right_open(self):
        # [t0,t1)：orig_start == t1 不算落入（与 timeline 归块半开区间一致）
        facts = [{"kind": "text_card", "t0": 10.0, "t1": 20.0, "text": "免责声明"}]
        segs = [{"start": 20.0, "end": 22.0, "zh": "边界"},
                {"start": 10.0, "end": 12.0, "zh": "左端"}]
        rewriter.mark_no_dub(segs, facts)
        assert "no_dub" not in segs[0]             # 右端开
        assert segs[1]["no_dub"] is True           # 左端闭


class TestClosingLine:
    """A6 结语收束句：最后一批 prompt 含补句规则 + 固定文案；LLM 给 append 则追加为新 segment。"""

    def test_closing_line_constant_exact(self):
        # 收束句逐字与 Global Constraints 一致
        assert rewriter.CLOSING_LINE == (
            "现在，慢慢把注意力带回当下的环境。本次练习到这里，感谢你的参与。")

    @pytest.mark.asyncio
    async def test_last_batch_prompt_carries_closing_rule(self):
        # 单批即最后一批：初改 prompt 尾部含补句规则 + 固定收束文案（逐字）
        fake = AsyncMock(return_value=_fixes("改"))
        with patch.object(rewriter, "llm_chat", fake):
            await rewriter.rewrite_segments([SEGS[0]], TERMS)
        prompt = fake.call_args_list[0].kwargs["messages"][0]["content"]
        assert rewriter.CLOSING_LINE in prompt          # 固定收束文案逐字进 prompt
        assert "append" in prompt and "结语" in prompt   # 补句规则标记

    @pytest.mark.asyncio
    async def test_appends_closing_segment_when_llm_returns_append(self):
        # LLM 在最后一批返回 append → 收束句作为新 segment 追加末尾，继承末句时间锚点
        content = json.dumps({"0": "改一", "1": "改二", "append": rewriter.CLOSING_LINE},
                             ensure_ascii=False)
        fake = AsyncMock(return_value=content)
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert len(out) == 3
        assert out[-1]["zh"] == rewriter.CLOSING_LINE
        assert out[-1]["start"] == SEGS[-1]["start"]    # 继承末句锚点，供 relayout 归入结语块
        assert "no_dub" not in out[-1]                  # 收束句必须配音

    @pytest.mark.asyncio
    async def test_no_append_key_no_extra_segment(self):
        # 同义收尾已存在（LLM 不给 append）→ 不追加、不重复收尾
        fake = AsyncMock(return_value=_fixes("改一", "改二"))
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_append_text_is_constant_not_llm_variant(self):
        # 硬化：append 键仅作「是否追加」信号，LLM 回传的变体文本（改标点/漏字）不采用，
        # 最终追加句逐字等于 CLOSING_LINE 常量（逐字保证从 prompt 依赖收紧为代码保证）
        variant = "现在,慢慢把注意力带回当下.本次练习到这里,谢谢参与"   # 半角标点+漏字变体
        content = json.dumps({"0": "改一", "1": "改二", "append": variant},
                             ensure_ascii=False)
        fake = AsyncMock(return_value=content)
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert len(out) == 3
        assert out[-1]["zh"] == rewriter.CLOSING_LINE      # 逐字常量，非变体
        assert out[-1]["zh"] != variant

    @pytest.mark.asyncio
    async def test_appended_closing_en_blanked(self):
        # 收束句无英文源：append 时 en 置空串，避免双语 md 中英错配（继承末句 en 会张冠李戴）
        content = json.dumps({"0": "改一", "1": "改二", "append": rewriter.CLOSING_LINE},
                             ensure_ascii=False)
        fake = AsyncMock(return_value=content)
        with patch.object(rewriter, "llm_chat", fake):
            out = await rewriter.rewrite_segments(SEGS, TERMS)
        assert out[-1]["en"] == ""                         # 非继承末句的 "world"


class TestAnchorClosingLine:
    """A6/F2 收束句锚定收尾卡：orig_* 落卡内使 relayout 归块正确 + 无条件清 no_dub 保证朗读。"""

    def test_anchors_to_closing_card_and_clears_no_dub(self):
        # 末句在球段(继承锚点 90)，尾部收尾卡[100,105] → 收束句 orig_* 锚到 100.1(落卡内)
        facts = [{"kind": "ball_exercise", "t0": 0.0, "t1": 100.0, "text": ""},
                 {"kind": "text_card", "t0": 100.0, "t1": 105.0, "text": "谢谢观看"}]
        segs = [{"start": 90, "end": 92, "orig_start": 90, "orig_end": 92, "zh": "球段末句"},
                {"start": 90, "end": 92, "orig_start": 90, "orig_end": 92,
                 "zh": rewriter.CLOSING_LINE}]
        out = rewriter.anchor_closing_line(segs, facts)
        assert out is segs
        assert segs[-1]["orig_start"] == pytest.approx(100.1)
        assert segs[-1]["orig_end"] == pytest.approx(100.1)
        assert not segs[-1].get("no_dub")                  # 收束句必须朗读
        assert segs[0]["orig_start"] == 90                 # 球段末句锚点不受影响

    def test_closing_line_dubbed_last_when_card_has_original_lines(self):
        # F-A job13/14 真实结构：收尾卡内有原文旁白(2 句) + 收束句。收束句必须锚到卡内台词之后，
        # 使 relayout 后它是全片最后一句配音；旧行为锚 t0+0.1 → 排卡内第一位 → 「感谢参与」先播。
        from app.video.pipeline.remake.timeline import relayout
        facts = [{"kind": "ball_exercise", "t0": 0.0, "t1": 100.0, "text": ""},
                 {"kind": "text_card", "t0": 100.0, "t1": 110.0, "text": "谢谢观看"}]
        segs = [
            {"start": 90, "end": 92, "orig_start": 90, "orig_end": 92, "zh": "球段末句"},
            {"start": 101, "end": 103, "orig_start": 101, "orig_end": 103, "zh": "卡内原文一"},
            {"start": 104, "end": 106, "orig_start": 104, "orig_end": 106, "zh": "卡内原文二"},
            {"start": 90, "end": 92, "orig_start": 90, "orig_end": 92,
             "zh": rewriter.CLOSING_LINE},
        ]
        rewriter.anchor_closing_line(segs, facts)
        # 锚到卡内原文的 orig_end 最大值(106)之后：orig_start=106.2、orig_end=106.7
        assert segs[-1]["orig_start"] == pytest.approx(106.2)
        assert segs[-1]["orig_end"] == pytest.approx(106.7)
        # 契约：relayout 后收束句是全片最后一句配音（start 大于其它所有句 end）
        _, retimed, _ = relayout(facts, segs, [2.0, 2.0, 2.0, 2.0], fps=120)
        closing_start = retimed[-1]["start"]
        assert closing_start > max(r["end"] for r in retimed[:-1])

    def test_narrow_boundary_disclaimer_still_dubbed(self):
        # 窄边界根治：末句锚点落免责区间 → mark_no_dub 会误标收束句 no_dub，anchor 后仍朗读
        facts = [{"kind": "text_card", "t0": 0.0, "t1": 10.0, "text": "disclaimer"}]
        segs = [{"start": 5, "end": 7, "orig_start": 5, "orig_end": 7,
                 "zh": rewriter.CLOSING_LINE, "no_dub": True}]   # 模拟 mark_no_dub 已误标
        rewriter.anchor_closing_line(segs, facts)
        assert not segs[-1].get("no_dub")                  # 关键：收束句仍朗读
        assert segs[-1]["orig_start"] == pytest.approx(0.1)  # 唯一 card 且其后无球 → 结语卡

    def test_no_closing_card_keeps_anchor_clears_no_dub(self):
        # 末尾是球段(无结语卡)：锚点保留继承，但仍无条件清 no_dub
        facts = [{"kind": "ball_exercise", "t0": 0.0, "t1": 10.0, "text": ""}]
        segs = [{"start": 5, "end": 7, "orig_start": 5, "orig_end": 7,
                 "zh": rewriter.CLOSING_LINE, "no_dub": True}]
        rewriter.anchor_closing_line(segs, facts)
        assert segs[-1]["orig_start"] == 5                 # 无结语卡 → 锚点不改
        assert not segs[-1].get("no_dub")

    def test_non_closing_last_segment_is_noop(self):
        # 末段不是收束句(未追加) → 原样不动，不误伤普通末句
        facts = [{"kind": "text_card", "t0": 0.0, "t1": 10.0, "text": "x"}]
        segs = [{"start": 5, "end": 7, "orig_start": 5, "orig_end": 7, "zh": "普通末句"}]
        rewriter.anchor_closing_line(segs, facts)
        assert segs[0]["orig_start"] == 5                  # 未被改写
        assert "no_dub" not in segs[0]

    def test_empty_segments_is_noop(self):
        assert rewriter.anchor_closing_line([], []) == []
