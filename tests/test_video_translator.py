"""翻译三步：抽术语→查表/RAG裁决→分批翻译+审校时长压缩（平移自 test_translator.py）。

mock 面随 import 面更换：
- 逐句 qwen-mt → provider ``mt_translate(texts, term_sheet=, domains=, tm_list=)``（translation_options
  的构造沉进 provider，故断言改看 term_sheet/domains/tm_list 直传 + 逐句一元素批调用）。
- 抽术语/直译/批量兜底/审校 → provider ``llm_chat(messages, temperature=) -> str``。
- match_terms/upsert_auto_term 改 async（AsyncMock）。
- 分流开关从 TRANSLATE_MODEL 换成 VIDEO_MT_MODEL。
"""
import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.video.pipeline.translator import (
    _MT_CONCURRENCY,
    _REFLECT_PROMPT,
    _TRANSLATE_PROMPT,
    _merge_translation,
    _mt_domains,
    extract_terms,
    over_slot_indices,
    reflect_and_fit,
    resolve_terms,
    translate_batches,
)

SEGS = [{"start": 0.0, "end": 4.0, "text": "Attachment theory was developed by Bowlby."},
        {"start": 4.0, "end": 7.0, "text": "It explains CBT foundations."}]

# 下文预览标签（与 _TRANSLATE_PROMPT 里的字面锁一致，用来从组装好的 prompt 里切出预览值）
_PREVIEW_ANCHOR = "下文预览【仅供理解语境，不要翻译、不要输出】："

_TR = "app.video.pipeline.translator"

# 强制走批量路径（默认档已切 qwen-mt-plus），锁批量 prompt 相关行为
_FORCE_QWEN3 = patch.object(settings, "VIDEO_MT_MODEL", "qwen3.7-plus")


def _mt_echo():
    """qwen-mt provider mock：N 进 N 出，译文 = "中文::" + 源文（逐句一元素批调用）。"""
    async def _mt(texts, *, term_sheet, domains=None, tm_list=None):
        return [f"中文::{t}" for t in texts]
    return AsyncMock(side_effect=_mt)


def _llm_echo_by_input_count():
    """批量路径 mock：按 prompt「输入：」段里的编号句数返回等量译文（N 进 N 出命门）。"""
    async def _chat(*, messages, **kwargs):
        tail = messages[0]["content"].split("输入：", 1)[-1]
        n = len(re.findall(r"(?m)^\d+\. ", tail))
        return json.dumps([f"译文{i + 1}" for i in range(n)], ensure_ascii=False)
    return AsyncMock(side_effect=_chat)


class TestTranslator:
    async def test_extract_terms(self):
        with patch(f"{_TR}.llm_chat",
                   AsyncMock(return_value=json.dumps(["attachment theory", "CBT"]))):
            terms = await extract_terms(SEGS)
        assert "attachment theory" in terms

    async def test_resolve_terms_hits_glossary_then_rag(self):
        with patch(f"{_TR}.match_terms",
                   AsyncMock(return_value={"attachment theory": {"zh": "依恋理论",
                                                                 "source": "manual",
                                                                 "approved": True}})), \
             patch(f"{_TR}._rag_lookup", AsyncMock(return_value="认知行为疗法")), \
             patch(f"{_TR}.upsert_auto_term", AsyncMock()) as up:
            sheet = await resolve_terms(None, ["attachment theory", "CBT"])
        by_en = {t["en"]: t for t in sheet}
        assert by_en["attachment theory"]["zh"] == "依恋理论"
        assert by_en["CBT"]["zh"] == "认知行为疗法"
        assert by_en["CBT"]["source"] == "auto"
        up.assert_awaited_once()

    def test_translate_prompt_carries_word_order_rule(self):
        assert "中文语序重组" in _TRANSLATE_PROMPT
        assert "被动语态" in _TRANSLATE_PROMPT and "转主动" in _TRANSLATE_PROMPT
        assert "念出来像中国人日常说话" in _TRANSLATE_PROMPT

    def test_reflect_prompt_carries_word_order_rule(self):
        assert "语序" in _REFLECT_PROMPT

    def test_translate_prompt_carries_context_window(self):
        assert "通读本批" in _TRANSLATE_PROMPT
        assert "下文预览" in _TRANSLATE_PROMPT
        assert "不要翻译" in _TRANSLATE_PROMPT and "不要输出" in _TRANSLATE_PROMPT

    async def test_translate_batches_next_preview_only_before_last_batch(self):
        # 33 句跨 2 批（_BATCH=30）：首批 prompt 的下文预览含下一批开头句原文，末批预览为"(无)"
        segs = [{"start": float(i), "end": float(i) + 1.0,
                 "text": f"Preview marker sentence {i} here."} for i in range(33)]
        fake = _llm_echo_by_input_count()
        with _FORCE_QWEN3, patch(f"{_TR}.llm_chat", fake):
            out = await translate_batches(segs, [], {"title": "t"})
        assert len(out) == 33
        prompts = [c.kwargs["messages"][0]["content"] for c in fake.await_args_list]
        assert len(prompts) == 2                     # 恰好两批，各一次调用
        preview0 = prompts[0].split(_PREVIEW_ANCHOR, 1)[1].split("\n", 1)[0]
        preview1 = prompts[1].split(_PREVIEW_ANCHOR, 1)[1].split("\n", 1)[0]
        assert "Preview marker sentence 30 here." in preview0
        assert "Preview marker sentence 30 here." not in prompts[0].split("输入：", 1)[-1]
        assert preview1 == "(无)"

    def test_merge_translation_zips_by_index(self):
        zh = ["依恋理论由鲍尔比提出。", "它解释了认知行为疗法的基础。"]
        merged = _merge_translation(SEGS, zh)
        assert merged[0] == {"start": 0.0, "end": 4.0,
                             "en": SEGS[0]["text"], "zh": zh[0]}

    async def test_translate_batches_carries_context(self):
        fake = AsyncMock(return_value=json.dumps(["译文一。", "译文二。"], ensure_ascii=False))
        with _FORCE_QWEN3, patch(f"{_TR}.llm_chat", fake):
            out = await translate_batches(SEGS, [{"en": "CBT", "zh": "认知行为疗法",
                                                  "source": "seed"}], {"title": "t"})
        assert len(out) == 2 and out[1]["zh"] == "译文二。"
        prompt = fake.await_args_list[0].kwargs["messages"][0]["content"]
        assert "认知行为疗法" in prompt          # 术语约束进了 prompt

    def test_over_slot_indices(self):
        translated = [{"start": 0.0, "end": 1.0, "en": "x", "zh": "这句话长得离谱" * 5},
                      {"start": 1.0, "end": 10.0, "en": "y", "zh": "短"}]
        idx = over_slot_indices(translated, cps=4.2, max_ratio=1.25)
        assert idx == [0]

    async def test_reflect_rewrites_only_over_slot(self):
        translated = [{"start": 0.0, "end": 1.0, "en": "x", "zh": "这句话长得离谱" * 5},
                      {"start": 1.0, "end": 10.0, "en": "y", "zh": "短"}]
        with patch(f"{_TR}.llm_chat",
                   AsyncMock(return_value=json.dumps({"0": "压缩后"}, ensure_ascii=False))):
            out = await reflect_and_fit(translated, [], cps=4.2)
        assert out[0]["zh"] == "压缩后" and out[1]["zh"] == "短"

    async def test_translate_batches_length_mismatch_raises(self):
        # 命门：批量译文数量 ≠ 输入句数（输入 2 句，恒返 1 项）→ 重试一次仍不符 → 抛 ValueError
        fake = AsyncMock(return_value=json.dumps(["只有一句"], ensure_ascii=False))
        with _FORCE_QWEN3, patch(f"{_TR}.llm_chat", fake):
            with pytest.raises(ValueError):
                await translate_batches(SEGS, [], {"title": "t"})
        assert fake.await_count == 2      # 首发 + 重试各一次

    # ── qwen-mt 专用翻译模型路径（默认档） ────────────────────────────

    def test_config_default_mt_model(self):
        # config 默认档切到专用翻译模型 qwen-mt-plus
        assert settings.VIDEO_MT_MODEL == "qwen-mt-plus"

    def test_mt_domains_carries_title(self):
        d = _mt_domains("EMDR 眼动脱敏")
        assert "EMDR 眼动脱敏" in d
        assert "Chinese" in d and "colloquial" in d

    async def test_qwen_mt_sends_context_n_in_n_out(self):
        # qwen-mt 路径：逐句 N 进 N 出，每句一元素批调用 mt_translate，带 term_sheet/domains
        fake = _mt_echo()
        with patch(f"{_TR}.mt_translate", fake):
            out = await translate_batches(
                SEGS, [{"en": "CBT", "zh": "认知行为疗法", "source": "seed"}],
                {"title": "EMDR"})
        assert len(out) == 2
        assert out[0]["zh"] == f"中文::{SEGS[0]['text']}"
        assert out[1]["zh"] == f"中文::{SEGS[1]['text']}"
        assert fake.await_count == 2      # 逐句调用
        for call in fake.await_args_list:
            texts = call.args[0]
            assert len(texts) == 1        # 一元素批（provider 内部并发，此处逐句喂）
            assert call.kwargs["term_sheet"] == [{"en": "CBT", "zh": "认知行为疗法",
                                                  "source": "seed"}]
            assert "EMDR" in call.kwargs["domains"]
        assert fake.await_args_list[0].args[0][0] == SEGS[0]["text"]

    async def test_qwen_mt_tm_list_uses_prev_chunk_tail(self):
        # 跨块（>_MT_CONCURRENCY 句）：首块 tm_list 空，次块 tm_list = 上一块尾窗已定稿译对
        n = _MT_CONCURRENCY + 1                       # 9 句：块1=0..7，块2=8
        segs = [{"start": float(i), "end": float(i) + 1.0, "text": f"Sentence {i}."}
                for i in range(n)]
        fake = _mt_echo()
        with patch(f"{_TR}.mt_translate", fake):
            out = await translate_batches(segs, [], {"title": "t"})
        assert len(out) == n
        calls = {c.args[0][0]: c.kwargs["tm_list"] for c in fake.await_args_list}
        assert calls["Sentence 0."] == []
        assert calls["Sentence 7."] == []
        tm = calls["Sentence 8."]                     # 块2：tm_list = idx 3..7（_TM_WINDOW=5）
        assert [p["source"] for p in tm] == [f"Sentence {i}." for i in range(3, 8)]
        assert tm[0]["target"] == "中文::Sentence 3."

    async def test_qwen_mt_concurrency_bounded_by_semaphore(self):
        # 并发：块内并发但受 Semaphore(_MT_CONCURRENCY) 约束——峰值在 (1, 8]
        segs = [{"start": float(i), "end": float(i) + 1.0, "text": f"S{i}."}
                for i in range(_MT_CONCURRENCY * 2)]
        state = {"cur": 0, "peak": 0}

        async def _mt(texts, *, term_sheet, domains=None, tm_list=None):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.01)                 # 留出并发窗口
            state["cur"] -= 1
            return [f"中文::{t}" for t in texts]

        with patch(f"{_TR}.mt_translate", AsyncMock(side_effect=_mt)):
            out = await translate_batches(segs, [], {"title": "t"})
        assert len(out) == _MT_CONCURRENCY * 2
        assert 1 < state["peak"] <= _MT_CONCURRENCY   # 真并发 + 不越信号量上限

    async def test_qwen_mt_degrades_to_qwen3_on_failure(self):
        # 降级：qwen-mt 调用异常 → 失败句回退批量路径按下标回填，job 不 fail
        async def _llm(*, messages, **kwargs):
            tail = messages[0]["content"].split("输入：", 1)[-1]
            m = len(re.findall(r"(?m)^\d+\. ", tail))
            return json.dumps([f"兜底{i + 1}" for i in range(m)], ensure_ascii=False)

        with patch(f"{_TR}.mt_translate", AsyncMock(side_effect=RuntimeError("qwen-mt down"))), \
             patch(f"{_TR}.llm_chat", AsyncMock(side_effect=_llm)):
            out = await translate_batches(SEGS, [], {"title": "t"})
        assert len(out) == 2
        assert out[0]["zh"] == "兜底1" and out[1]["zh"] == "兜底2"

    async def test_qwen_mt_deadline_placeholder_no_llm(self):
        # deadline 已过：不调 provider，剩余句英文原文占位保句数对齐
        fake = _mt_echo()
        with patch(f"{_TR}.mt_translate", fake):
            out = await translate_batches(SEGS, [], {"title": "t"}, deadline=0.0)
        assert len(out) == 2
        assert out[0]["zh"] == SEGS[0]["text"] and out[1]["zh"] == SEGS[1]["text"]
        fake.assert_not_awaited()
