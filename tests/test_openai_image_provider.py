"""gpt-image 一致性生图 provider:有界并发 + 429 退避 + usage 日志(全 stub,零上游调用)。

硬约束锚(下游按下标把图落成 P01.png…PNN.png,顺序/空位语义一变就落错图):
- generate_batch 返回列表与 prompts **逐位对应**,失败位是 success=False 占位而非跳过
- 并发峰值 <= OPENAI_IMAGE_CONCURRENCY
- 429 退避重试(只重试限流),moderation 一律不重试
- usage 缺省(None / 无该字段)不得炸掉一次已成功的生图

**绝不打真实 OpenAI API**:client 全部注入假对象(provider._client)。
"""

import asyncio
import base64
from pathlib import Path

import pytest

from app.core.config import settings
from app.imagegen import openai_image
from app.imagegen.openai_image import OpenAIImageProvider, _is_rate_limit_error


class _FakeUsage:
    """仿 OpenAI 图像响应的 usage 对象。"""

    def __init__(self, input_tokens=100, output_tokens=1500, total_tokens=1600):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.input_tokens_details = {"text_tokens": 40, "image_tokens": 60}


class _FakeImageData:
    def __init__(self, b64: str):
        self.b64_json = b64


class _FakeResponse:
    """仿上游响应:data[0].b64_json 承载图片,usage 可为 None(中转不返回)。"""

    def __init__(self, payload: str, usage=None):
        self.data = [_FakeImageData(base64.b64encode(payload.encode()).decode())]
        self.usage = usage


class _FakeImages:
    """假 images 端点:edit/generate 都走同一个 handler(收 prompt 吐响应或抛异常)。"""

    def __init__(self, handler):
        self._handler = handler
        self.edit_calls = []
        self.generate_calls = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        return await self._handler(kwargs["prompt"])

    async def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return await self._handler(kwargs["prompt"])


class _FakeClient:
    def __init__(self, handler):
        self.images = _FakeImages(handler)


def _provider(tmp_path, handler) -> OpenAIImageProvider:
    """建 provider 并注入假 client(绕开真 AsyncOpenAI 初始化)。"""
    p = OpenAIImageProvider(str(tmp_path / "out"))
    p._client = _FakeClient(handler)
    return p


def _anchor(tmp_path) -> str:
    """落一份外部锚点文件,返回路径。"""
    path = tmp_path / "anchor.png"
    path.write_bytes(b"anchor-bytes")
    return str(path)


def _payload_of(result) -> str:
    """读回落盘图片的内容(测试里就是 prompt 原文,用于验证下标未错位)。"""
    return Path(result.path).read_text()


# ---------------- ① 下标严格对齐(含中间失败位) ----------------


async def test_batch_results_align_with_prompts_even_when_one_fails(tmp_path, monkeypatch):
    """外部锚点:第 2 位失败 → 该位 success=False 占位,其余位不移位且内容对号入座。

    故意让完成顺序与提交顺序相反(越靠后越快返回),验证对齐靠 gather 保序而非巧合。
    """
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 5)
    prompts = [f"page-{i}" for i in range(5)]

    async def handler(prompt: str):
        idx = int(prompt.split("-")[1])
        await asyncio.sleep((len(prompts) - idx) * 0.01)  # 倒序完成
        if idx == 2:
            raise RuntimeError("upstream boom")
        return _FakeResponse(prompt, usage=_FakeUsage())

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(prompts, anchor_path=_anchor(tmp_path))

    assert len(results) == len(prompts)
    assert not results[2].success
    assert "boom" in results[2].error
    assert results[2].path is None
    for i in (0, 1, 3, 4):
        assert results[i].success, results[i].error
        assert _payload_of(results[i]) == prompts[i]  # 内容与下标一一对应,未错位


async def test_internal_anchor_keeps_p1_at_index_zero(tmp_path, monkeypatch):
    """内部锚点:results[0] 是 generate 出的 P1,edit 结果填 1..N-1,总长 == prompts。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 3)
    prompts = [f"page-{i}" for i in range(4)]

    async def handler(prompt: str):
        return _FakeResponse(prompt, usage=_FakeUsage())

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(prompts)

    assert len(results) == len(prompts)
    assert all(r.success for r in results)
    assert [_payload_of(r) for r in results] == prompts
    # P1 走 generate,其余 3 张走 edit
    assert len(p._client.images.generate_calls) == 1
    assert len(p._client.images.edit_calls) == 3


# ---------------- ② 信号量确实封顶并发 ----------------


async def test_semaphore_caps_in_flight_calls(tmp_path, monkeypatch):
    """同时在飞的 edit 数不超过 OPENAI_IMAGE_CONCURRENCY。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 2)
    state = {"in_flight": 0, "peak": 0}

    async def handler(prompt: str):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.02)
            return _FakeResponse(prompt)
        finally:
            state["in_flight"] -= 1

    prompts = [f"page-{i}" for i in range(7)]
    p = _provider(tmp_path, handler)
    results = await p.generate_batch(prompts, anchor_path=_anchor(tmp_path))

    assert all(r.success for r in results)
    assert state["peak"] <= 2, f"并发峰值 {state['peak']} 超过配置上限 2"
    assert state["peak"] > 1, "并发未生效(仍是串行)"


# ---------------- ③ 429 退避重试 ----------------


async def test_edit_retries_on_rate_limit_then_succeeds(tmp_path, monkeypatch):
    """edit 连撞两次 429 → 退避重试后第 3 次成功(不再让运营手工 --pages 重出)。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 5)
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.01)  # 测试不真等 2s
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Rate limit reached for images per min")
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert len(results) == 1 and results[0].success, results[0].error
    assert calls["n"] == 3  # 1 次原始 + 2 次重试


async def test_generate_retries_on_rate_limit(tmp_path, monkeypatch):
    """单张 generate 路径同样带 429 退避重试。"""
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    result = await p.generate("page-0")

    assert result.success, result.error
    assert calls["n"] == 2


async def test_rate_limit_gives_up_after_max_retries(tmp_path, monkeypatch):
    """一直 429 → 重试上限后落失败位(不是无限重试),总调用 1 + 3。"""
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        raise RuntimeError("rate limit exceeded")

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert not results[0].success
    assert calls["n"] == openai_image._RATE_LIMIT_MAX_RETRIES + 1


async def test_fallback_survives_save_crash(tmp_path, monkeypatch):
    """P1 失败走降级分支时,某张落盘炸掉**不得**掀翻整批(其余位仍在、长度不变)。

    评审实证缺陷:降级分支的 gather 缺 return_exceptions,而 generate 里 _save 的
    write_bytes 在 try 之外(磁盘满/目录被并发清理会原样抛),一炸就整批丢——含已成功
    已付费的张。此测试钉住修复。
    """
    async def handler(prompt: str):
        if prompt == "p1":                      # 锚点 P1 失败 → 触发降级分支
            raise RuntimeError("anchor boom")
        return _FakeResponse(f"img-{prompt}")

    p = _provider(tmp_path, handler)
    real_save = p._save
    calls = {"n": 0}

    def flaky_save(b64, prefix):
        calls["n"] += 1
        if calls["n"] == 2:                     # 第 2 张落盘炸(模拟磁盘满)
            raise OSError("No space left on device")
        return real_save(b64, prefix)

    p._save = flaky_save
    results = await p.generate_batch(["p1", "p2", "p3"])

    assert len(results) == 3, "降级批次长度必须仍等于 prompts 数"
    assert not results[0].success              # P1 本就失败
    assert all(r.metadata.get("consistency_fallback") for r in results)
    # 落盘炸的那位是失败占位,不是整批抛异常;其余位不受牵连
    assert sum(1 for r in results if r.success) >= 1


async def test_backoff_actually_sleeps_with_exponential_delays(tmp_path, monkeypatch):
    """退避**真的发生**且按指数递增:捕获 asyncio.sleep 的实参断言。

    评审实证:此前三条重试用例只断调用次数,把 `await asyncio.sleep(delay)` 整行删掉
    测试照样全绿——等于退避这层根本没被钉住(退化成撞 429 立刻连轰上游)。故补此条。
    """
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 2.0)
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(openai_image.asyncio, "sleep", fake_sleep)

    async def handler(prompt: str):
        raise RuntimeError("rate limit exceeded")

    p = _provider(tmp_path, handler)
    await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    # 重试 N 次 → 睡 N 次;基数 2.0 时各次落在 [2,3) / [4,6) / [8,12) (含 0~delay/2 抖动)
    assert len(slept) == openai_image._RATE_LIMIT_MAX_RETRIES
    for i, d in enumerate(slept):
        base = 2.0 * (2 ** i)
        assert base <= d < base * 1.5, f"第{i + 1}次退避 {d} 不在 [{base}, {base * 1.5}) 内"
    assert slept == sorted(slept), "退避必须递增(指数),不能是常量"


def test_is_rate_limit_error_structured_and_text():
    """限流判定双层:结构化(status_code/code)+ 中转拍扁后的纯文案。"""

    class _StatusExc(Exception):
        status_code = 429

    class _CodeExc(Exception):
        code = "rate_limit_exceeded"

    assert _is_rate_limit_error(_StatusExc("boom"))
    assert _is_rate_limit_error(_CodeExc("boom"))
    assert _is_rate_limit_error(Exception("Rate limit reached"))
    assert _is_rate_limit_error(Exception("429 Too Many Requests"))
    assert _is_rate_limit_error(Exception("上游限流,请稍后重试"))
    assert not _is_rate_limit_error(Exception("connection reset by peer"))
    assert not _is_rate_limit_error(
        Exception("rejected as a result of our safety system"))


# ---------------- ④ moderation 不重试 ----------------


async def test_moderation_is_not_retried(tmp_path, monkeypatch):
    """安全拦截是 prompt 本身的问题:只调一次就落失败位,重试纯烧钱。"""
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        raise RuntimeError("Your request was rejected as a result of our safety system")

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert calls["n"] == 1
    assert not results[0].success
    assert results[0].error.startswith("moderation_blocked")


async def test_moderation_hole_does_not_shift_other_pages(tmp_path, monkeypatch):
    """并发下某页被安全拦截,其余页仍各就各位(失败位不吞、不左移)。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 5)
    prompts = [f"page-{i}" for i in range(4)]

    async def handler(prompt: str):
        if prompt == "page-1":
            raise RuntimeError("content_policy violation")
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(prompts, anchor_path=_anchor(tmp_path))

    assert len(results) == 4
    assert results[1].error.startswith("moderation_blocked")
    for i in (0, 2, 3):
        assert results[i].success and _payload_of(results[i]) == prompts[i]


# ---------------- ⑤ usage 缺省不炸 ----------------


async def test_usage_none_does_not_break_generation(tmp_path, monkeypatch):
    """中转不返回 usage(None / 压根没这个属性)时照常成功落盘。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 3)

    class _NoUsageResponse:
        def __init__(self, payload: str):
            self.data = [_FakeImageData(base64.b64encode(payload.encode()).decode())]

    async def handler(prompt: str):
        if prompt == "page-0":
            return _FakeResponse(prompt, usage=None)
        return _NoUsageResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0", "page-1"], anchor_path=_anchor(tmp_path))

    assert all(r.success for r in results), [r.error for r in results]
    assert [_payload_of(r) for r in results] == ["page-0", "page-1"]


def test_log_usage_never_raises():
    """_log_usage 对任何形态的 usage 都不抛(成本日志不得反噬生图结果)。"""

    class _Weird:
        usage = object()  # 没有任何 token 字段

    openai_image._log_usage(_Weird(), kind="edit", index=3)
    openai_image._log_usage(object(), kind="generate", index=0)


# ---------------- 降级分支同样受信号量约束 ----------------


async def test_fallback_branch_is_bounded(tmp_path, monkeypatch):
    """锚点 P1 失败 → 整批降级独立生成,并发同样封顶(不再无界 gather)。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", 2)
    state = {"in_flight": 0, "peak": 0}
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次调用 = 锚点 P1,让它失败触发降级
            raise RuntimeError("upstream 500")
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.02)
            return _FakeResponse(prompt)
        finally:
            state["in_flight"] -= 1

    prompts = [f"page-{i}" for i in range(6)]
    p = _provider(tmp_path, handler)
    results = await p.generate_batch(prompts)

    assert len(results) == len(prompts)
    assert all(r.metadata.get("consistency_fallback") for r in results)
    assert state["peak"] <= 2, f"降级分支并发峰值 {state['peak']} 超上限 2"


# ---------------- 配置项 ----------------


def test_concurrency_setting_default():
    """新增配置项必须带默认值(缺省即可用,不依赖 .env;故断言字段默认而非实例值)。"""
    from app.core.config import Settings

    assert Settings.model_fields["OPENAI_IMAGE_CONCURRENCY"].default == 10


@pytest.mark.parametrize("bad", [0, -3])
async def test_concurrency_floor_is_one(tmp_path, monkeypatch, bad):
    """配置被填成 0/负数时退化为串行 1,而不是 ValueError 炸掉整批。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONCURRENCY", bad)

    async def handler(prompt: str):
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0", "page-1"], anchor_path=_anchor(tmp_path))
    assert all(r.success for r in results)
