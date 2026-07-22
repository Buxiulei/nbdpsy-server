"""碎片字幕 → 语义整句。

自动字幕 / ASR 常把一句话切成很多短碎片（无句末标点、平均字数很短）。
本模块用 LLM 把碎片按语义重新断成整句，便于后续翻译与配音。

设计要点：
1. needs_resegment 启发式先判——已是整句就直接跳过，不白花 LLM 调用
2. _batch_iter 分批 + 批头带上一批末尾若干段作重叠上下文，帮 LLM 正确断句
3. validate_merge 逐批做时间单调 + 字符覆盖率校验，不达标该批降级保原
4. deadline 软预算耗尽时，剩余段全部原样返回

平移自 video_transport/resegment.py：LLM 收口从 ``get_llm(...).chat(...)`` 换成薄 provider
``llm_chat(messages, temperature=...)``——后者直接返回正文字符串（无 LLMResponse/dict 包装），故
去掉 ``_resp_content`` 兼容层与 ``urgent`` 参数，其余断句/校验逻辑逐字保真。
"""
import json
import re
import time

from app.video.providers import llm_chat

# 分批参数：_batch_iter 与 resegment 共用同一常量，保证纯新段切片对齐
_BATCH_SIZE = 50
_OVERLAP = 5

_PROMPT = """你是字幕断句引擎。把下面的英文碎片字幕合并成语义完整的句子。
规则：
1. 只合并/断句，绝不增删改任何单词
2. 每句输出 {"start": 首碎片start, "end": 末碎片end, "text": "合并后的句子"}
3. start/end 用输入里的原值，保持单调递增不重叠
4. 只输出 JSON 数组，不要任何其他文字

输入碎片：
%s"""


def needs_resegment(segments: list[dict]) -> bool:
    """启发式：平均字数过短或句末标点比例过低 → 判定为碎片流，需要重断句。"""
    if not segments:
        return False
    avg_len = sum(len(s["text"]) for s in segments) / len(segments)
    punct_ratio = sum(1 for s in segments if s["text"].rstrip()[-1:] in ".!?") / len(segments)
    return avg_len < 40 or punct_ratio < 0.5


def _batch_iter(segments: list[dict], *, size: int = _BATCH_SIZE, overlap: int = _OVERLAP):
    """按 size 分批，yield (纯新段起始下标, 含重叠上下文的批)。

    第二批起，批头带上一批末尾 overlap 段作为上下文，帮助 LLM 在批边界正确断句；
    这些重叠段会在 resegment 里按 floor 时间过滤掉，不重复计入结果。
    下标 i 即纯新段起点（segments[i:i+size]），调用方据此切片，避免 list.index 的 O(n²)。
    """
    i = 0
    n = len(segments)
    while i < n:
        ctx_start = max(0, i - overlap)
        yield i, segments[ctx_start:i + size]
        i += size


def validate_merge(original: list[dict], merged: list[dict]) -> bool:
    """校验重断句结果：时间单调不重叠 + 字符覆盖率 ≥ 95%（防 LLM 吞词）。"""
    if not merged:
        return False
    prev_end = -1.0
    for seg in merged:
        if seg["start"] < prev_end:           # 时间重叠
            return False
        prev_end = seg["end"]
    orig_chars = len(re.sub(r"\s", "", "".join(s["text"] for s in original)))
    merged_chars = len(re.sub(r"\s", "", "".join(s["text"] for s in merged)))
    return orig_chars > 0 and merged_chars / orig_chars >= 0.95


def _extract_json_array(content: str) -> list:
    """从 LLM 输出里抠出 JSON 数组（容忍前后多余文字）。解析失败返回空列表。"""
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []


async def resegment(segments: list[dict], *, deadline: float | None = None) -> list[dict]:
    """把碎片字幕重断成整句。逐批调用 LLM，校验不过或异常则该批降级保原。

    Args:
        segments: 碎片字幕段列表，每段 {"start", "end", "text"}
        deadline: time.monotonic() 软预算；超时后剩余段原样返回

    Returns:
        重断句后的字幕段列表（与输入等长或更短，字符覆盖率有保证）
    """
    out: list[dict] = []
    for new_start, batch in _batch_iter(segments):
        if deadline is not None and time.monotonic() > deadline:
            out.extend(segments[new_start:])   # 预算耗尽：剩余全部保原样
            break
        pure_new = segments[new_start:new_start + _BATCH_SIZE]
        try:
            content = await llm_chat(
                messages=[{"role": "user",
                           "content": _PROMPT % json.dumps(batch, ensure_ascii=False)}],
                temperature=0.0)
            merged_all = _extract_json_array(content)
            # 丢掉落在重叠上下文里的输出句（end <= 纯新段起点时间）
            floor = pure_new[0]["start"] if pure_new else 0.0
            merged = [m for m in merged_all if m.get("end", 0) > floor]
            if validate_merge(pure_new, merged):
                out.extend(merged)
            else:
                out.extend(pure_new)           # 校验不过：该批降级保原
        except Exception:
            out.extend(pure_new)               # LLM 异常：该批降级保原
    return out
