"""LLM 重断句：启发式跳过 + 批校验 + 降级保原（平移自 test_resegment.py）。

mock 面随 import 面更换：源 ``get_llm(...).chat -> LLMResponse`` → 薄 provider ``llm_chat -> str``。
"""
import json
from unittest.mock import AsyncMock, patch

from app.video.pipeline.resegment import (
    _batch_iter,
    needs_resegment,
    resegment,
    validate_merge,
)

FRAGMENTS = [{"start": float(i), "end": float(i + 1), "text": f"word{i} word{i}b"}
             for i in range(120)]
SENTENCES = [{"start": 0.0, "end": 8.0,
              "text": "This is a complete sentence with proper punctuation."},
             {"start": 8.0, "end": 15.0,
              "text": "Another well-formed sentence follows here naturally."}]


class TestResegment:
    def test_needs_resegment_heuristic(self):
        assert needs_resegment(FRAGMENTS) is True       # 碎片流：短、无句末标点
        assert needs_resegment(SENTENCES) is False      # 整句：平均>40字符且有标点

    def test_validate_merge_coverage_and_monotonic(self):
        orig = FRAGMENTS[:4]
        good = [{"start": 0.0, "end": 2.0, "text": "word0 word0b word1 word1b"},
                {"start": 2.0, "end": 4.0, "text": "word2 word2b word3 word3b"}]
        assert validate_merge(orig, good) is True
        overlap = [{"start": 0.0, "end": 3.0, "text": "word0 word0b word1 word1b"},
                   {"start": 2.0, "end": 4.0, "text": "word2 word2b word3 word3b"}]
        assert validate_merge(orig, overlap) is False   # 时间重叠
        lossy = [{"start": 0.0, "end": 4.0, "text": "word0"}]
        assert validate_merge(orig, lossy) is False     # 字符覆盖率<95%

    def test_batch_iter_overlap(self):
        batches = list(_batch_iter(FRAGMENTS, size=50, overlap=5))
        assert len(batches[0][1]) == 50
        # 第二批带前一批末尾 5 段重叠上下文
        assert batches[1][1][0] is FRAGMENTS[45]

    async def test_resegment_bad_batch_falls_back(self):
        # llm_chat 直接返回正文字符串（薄 provider），覆盖率不足 → 该批降级保原
        fake = AsyncMock(return_value=json.dumps(
            [{"start": 0.0, "end": 4.0, "text": "x"}]))
        with patch("app.video.pipeline.resegment.llm_chat", fake):
            out = await resegment(FRAGMENTS[:50])
        assert out == FRAGMENTS[:50]  # 降级保原
