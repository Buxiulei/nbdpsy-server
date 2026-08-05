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


# ---------------- ③.5 上游超时重试(2026-08-05 生图超时加固) ----------------


def _timeout_exc() -> Exception:
    """构造真实 openai.APITimeoutError(生产里那句 "Request timed out." 的来源)。"""
    import httpx
    from openai import APITimeoutError

    return APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/images/edits"))


async def test_timeout_retried_then_succeeds_and_reports_attempts(tmp_path, monkeypatch):
    """上游超时 → 退避重试后成功;该页 metadata 带 upstream_attempts=2。

    生产实证:失败耗时 18/20.7/33s **短于**成功耗时 43-76s,即挂的是瞬时网络抖动
    而非"生成太慢";重试跨过抖动窗口即可救回,原先超时直接判死是白丢一页。
    """
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _timeout_exc()
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert results[0].success, results[0].error
    assert calls["n"] == 2
    assert results[0].metadata["upstream_attempts"] == 2


async def test_timeout_exhausted_keeps_error_prefix_and_attempts(tmp_path, monkeypatch):
    """一直超时 → 重试耗尽后落失败位,error 前缀保持 openai_image_edit_failed。

    前缀是运营侧的匹配依据(skill 按前缀分流),重试改造不得把它改掉。
    """
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT_RETRIES", 2)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        raise RuntimeError("Request timed out.")   # 中转把结构化异常拍扁成纯文案

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert not results[0].success
    assert results[0].error.startswith("openai_image_edit_failed")
    assert calls["n"] == 3                          # 1 次原始 + 2 次超时重试
    assert results[0].metadata["upstream_attempts"] == 3


async def test_generate_timeout_keeps_call_failed_prefix(tmp_path, monkeypatch):
    """generate 路径超时耗尽 → 前缀 openai_image_call_failed(同为运营匹配依据)。"""
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT_RETRIES", 2)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        raise _timeout_exc()

    p = _provider(tmp_path, handler)
    result = await p.generate("page-0")

    assert not result.success
    assert result.error.startswith("openai_image_call_failed")
    assert calls["n"] == 3
    assert result.metadata["upstream_attempts"] == 3


async def test_local_wait_for_timeout_is_also_retried(tmp_path, monkeypatch):
    """本地 asyncio.wait_for 超时(裸 TimeoutError,文案为空)同样走超时重试分支。

    钉住"靠 isinstance 判定"而非只靠文案关键词——裸 TimeoutError 的 str 是空串。
    """
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT_RETRIES", 2)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        raise asyncio.TimeoutError()

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert not results[0].success
    assert calls["n"] == 3


async def test_timeout_backoff_base_differs_from_rate_limit(tmp_path, monkeypatch):
    """超时退避真的更长(5s/15s),与 429 的 2s/4s/8s 是两套基数、两套预算。

    照 test_backoff_actually_sleeps_with_exponential_delays 的手法捕获 sleep 实参:
    只断次数会让"把 await sleep 删掉"照样绿。
    """
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 5.0)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT_RETRIES", 2)
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(openai_image.asyncio, "sleep", fake_sleep)

    async def handler(prompt: str):
        raise _timeout_exc()

    p = _provider(tmp_path, handler)
    await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert len(slept) == 2, f"超时重试次数应为 2,实际睡了 {len(slept)} 次"
    for i, d in enumerate(slept):
        base = 5.0 * (openai_image._TIMEOUT_BACKOFF_FACTOR ** i)   # 5s / 15s
        assert base <= d < base * 1.5, f"第{i + 1}次超时退避 {d} 不在 [{base}, {base * 1.5}) 内"
    # 与 429 基数(2.0)明确不同:超时首退避必须显著更长,才跨得过抖动窗口
    assert slept[0] > openai_image._RATE_LIMIT_BACKOFF_BASE * 2


async def test_timeout_and_rate_limit_have_separate_budgets(tmp_path, monkeypatch):
    """429 与超时各计各的预算:先撞几次限流不会把超时重试额度花光。"""
    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(openai_image, "_TIMEOUT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT_RETRIES", 2)
    calls = {"n": 0}

    async def handler(prompt: str):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("rate limit exceeded")   # 用掉全部 3 次 429 预算
        if calls["n"] <= 5:
            raise _timeout_exc()                        # 再用掉 2 次超时预算
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert results[0].success, results[0].error
    assert calls["n"] == 6
    assert results[0].metadata["upstream_attempts"] == 6


def test_timeout_settings_defaults():
    """新增配置项必须带默认值(缺省即可用,不依赖 .env)。"""
    from app.core.config import Settings

    assert Settings.model_fields["OPENAI_IMAGE_CONNECT_TIMEOUT"].default == 30
    assert Settings.model_fields["OPENAI_IMAGE_TIMEOUT_RETRIES"].default == 2


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


# ── 进程级生图闸(image_gate)────────────────────────────────────────────────

async def test_global_gate_caps_across_batches(tmp_path, monkeypatch):
    """进程级闸封顶**跨批次**总在飞:两批各 4 路同时跑,全局闸=3 时峰值不得超 3。

    这是"不依赖调用方守约"的兜底:页级并发只管一篇之内,篇级并行由调用方决定,
    二者相乘可能冲破 tier 限额;本闸是进程内最后一道。
    """
    from app.imagegen import image_gate

    monkeypatch.setattr(openai_image.settings, "OPENAI_IMAGE_CONCURRENCY", 4)
    monkeypatch.setattr(image_gate.settings, "OPENAI_IMAGE_GLOBAL_CONCURRENCY", 3)
    image_gate._reset_for_test()
    try:
        cur = {"n": 0, "peak": 0}

        async def handler(prompt: str):
            cur["n"] += 1
            cur["peak"] = max(cur["peak"], cur["n"])
            await asyncio.sleep(0.05)
            cur["n"] -= 1
            return _FakeResponse(f"img-{prompt}")

        p1 = _provider(tmp_path / "a", handler)
        p2 = _provider(tmp_path / "b", handler)
        anchor = _anchor(tmp_path)
        r1, r2 = await asyncio.gather(
            p1.generate_batch([f"a{i}" for i in range(4)], anchor_path=anchor),
            p2.generate_batch([f"b{i}" for i in range(4)], anchor_path=anchor),
        )

        assert len(r1) == 4 and len(r2) == 4
        assert all(r.success for r in r1 + r2), "排队而非拒绝:全部应成功"
        assert cur["peak"] <= 3, f"全局在飞峰值 {cur['peak']} 超过闸值 3"
        assert cur["peak"] > 1, "闸不该把并发压成串行"
    finally:
        image_gate._reset_for_test()


async def test_global_gate_released_during_backoff(tmp_path, monkeypatch):
    """429 退避期间必须**归还**全局名额:否则限流时占着坑干等,拖垮其他任务。"""
    from app.imagegen import image_gate

    monkeypatch.setattr(openai_image, "_RATE_LIMIT_BACKOFF_BASE", 0.05)
    monkeypatch.setattr(image_gate.settings, "OPENAI_IMAGE_GLOBAL_CONCURRENCY", 1)
    image_gate._reset_for_test()
    try:
        order: list[str] = []

        async def handler(prompt: str):
            if prompt == "slow" and order.count("slow-429") < 1:
                order.append("slow-429")
                raise RuntimeError("rate limit exceeded")
            order.append(prompt)
            return _FakeResponse(f"img-{prompt}")

        p = _provider(tmp_path, handler)
        anchor = _anchor(tmp_path)

        async def other():
            await asyncio.sleep(0.01)          # 让 slow 先撞 429 进入退避
            return await p.generate_batch(["other"], anchor_path=anchor)

        slow_res, other_res = await asyncio.gather(
            p.generate_batch(["slow"], anchor_path=anchor), other())

        assert slow_res[0].success and other_res[0].success
        # 闸值=1;若退避期间仍占名额,other 只能等 slow 全部重试完 → other 必在最后。
        # 归还了名额,other 就能插在 slow 的两次尝试之间。
        assert order.index("other") < order.index("slow"), (
            f"退避期间未归还全局名额(顺序 {order})")
    finally:
        image_gate._reset_for_test()


# ── 锚点图瘦身(2026-08-06 改口径:尺寸与体积**两个独立判据**)────────────────
#
# 上一版只按长边判,而上传耗时取决于**体积**不是像素数:gpt-image 原图 1024x1536
# 就有 1.6-2.4MB(长边不超阈值),按老口径一律不动。现在两条各管各的:
# 长边超阈值才缩(保画面细节),体积超阈值才转 JPEG(尺寸零损失)。

def _png_bytes(width: int, height: int, mode: str = "RGB") -> bytes:
    """造一张**体积很大**的 PNG(随机噪点不可压缩,1024x1536 约 4.5MB)。"""
    import io
    import os

    from PIL import Image

    im = Image.frombytes(mode, (width, height),
                         os.urandom(width * height * len(mode)))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _light_png_bytes(width: int, height: int) -> bytes:
    """造一张**体积很小**的 PNG(平滑渐变,1024x1536 仅约 8KB):两个判据都不触发。"""
    import io

    from PIL import Image

    im = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_compress_anchor_shrinks_oversized_image():
    """长边 > 1536 → 等比缩到 1536 + 转 JPEG,文件名/mime 同步改成 jpeg。"""
    import io

    from PIL import Image

    raw = _png_bytes(1152, 3072)          # 长边 3072,原图约数 MB
    name, data, mime = openai_image._compress_anchor(raw)   # SDK file tuple 顺序

    assert name.endswith(".jpg") and mime == "image/jpeg"
    assert len(data) < len(raw)
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (576, 1536)     # 等比:1152 * 1536/3072 = 576


def test_compress_anchor_converts_rgba_before_jpeg():
    """RGBA 必须先转 RGB —— JPEG 不支持 alpha,不转会直接抛。"""
    import io

    from PIL import Image

    name, data, mime = openai_image._compress_anchor(_png_bytes(2048, 2048, mode="RGBA"))

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as im:
        assert im.mode == "RGB" and max(im.size) == 1536


def test_heavy_but_normal_size_is_transcoded_without_resize():
    """**本轮回归锁**:尺寸不超阈值但体积超阈值 → 尺寸一个像素不动,只转 JPEG。

    这正是 gpt-image 原图当锚点的形态(1024x1536 / 1.6-2.4MB):老口径只看长边,
    这类图一律不动,而它才是真正的大载荷。判据必须是体积。
    """
    import io

    from PIL import Image

    raw = _png_bytes(1152, 1536)          # 长边正好 1536(不超),体积约 5MB(超)
    assert len(raw) > openai_image._ANCHOR_MAX_BYTES
    name, data, mime = openai_image._compress_anchor(raw)

    assert (name, mime) == ("anchor.jpg", "image/jpeg")
    assert len(data) < len(raw), "体积必须真的降下来"
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (1152, 1536), "尺寸不许动——只转码,不缩放"


def test_compress_anchor_leaves_light_small_image_untouched():
    """尺寸与体积**都**不超阈值 → 原样返回,不做多余重编码(mime 仍是 png)。"""
    raw = _light_png_bytes(1024, 1536)
    assert len(raw) < openai_image._ANCHOR_MAX_BYTES
    assert openai_image._compress_anchor(raw) == ("anchor.png", raw, "image/png")


def test_transcode_skipped_when_it_would_grow_the_payload():
    """源本就是高压缩 JPEG 时,转 q90 反而更大(实测 664KB → 1363KB)→ 保留原字节。

    瘦身的目的是减体积,产出更大就是负收益(还白搭一次有损重编码)。
    """
    import io
    import os

    from PIL import Image

    im = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=40)      # 低质高压缩源,体积超阈值
    raw = buf.getvalue()
    assert len(raw) > openai_image._ANCHOR_MAX_BYTES

    name, data, mime = openai_image._compress_anchor(raw)

    assert data == raw, "转码反而更大时必须原样返回"
    assert (name, mime) == ("anchor.png", "image/png")


def test_compress_anchor_falls_back_on_pil_failure():
    """PIL 认不出的字节 → 回退原字节,绝不因为压缩把生图搞挂。"""
    raw = b"not-an-image-at-all"
    assert openai_image._compress_anchor(raw) == ("anchor.png", raw, "image/png")


async def test_oversized_anchor_is_compressed_before_upstream(tmp_path, monkeypatch):
    """generate_batch 真的把压缩后的锚点喂给 images.edit(不是读了原字节直发)。"""
    anchor = tmp_path / "big.png"
    raw = _png_bytes(1152, 3072)
    anchor.write_bytes(raw)

    async def handler(prompt: str):
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=str(anchor))

    assert results[0].success, results[0].error
    name, data, mime = p._client.images.edit_calls[0]["image"]
    assert (name, mime) == ("anchor.jpg", "image/jpeg")
    assert len(data) < len(raw)


async def test_unreadable_anchor_bytes_still_sent_as_is(tmp_path, monkeypatch):
    """锚点压不动(非图片字节)时原样上传,行为与改造前一致。"""
    async def handler(prompt: str):
        return _FakeResponse(prompt)

    p = _provider(tmp_path, handler)
    results = await p.generate_batch(["page-0"], anchor_path=_anchor(tmp_path))

    assert results[0].success, results[0].error
    assert p._client.images.edit_calls[0]["image"] == (
        "anchor.png", b"anchor-bytes", "image/png")


# ── 客户端显式超时(2026-08-05 根因:SDK 默认 connect 只有 5s)───────────────────

def test_client_sets_explicit_timeouts_and_caps_sdk_retries(tmp_path, monkeypatch):
    """AsyncOpenAI 必须拿到分项 httpx 超时 + max_retries=1(重试节奏归本模块管)。

    根因锚:此前不传 timeout → SDK 默认 connect=5s,抖动期毫无缓冲,加上 SDK 自身
    三次快速重试(约 16s)跨不过抖动窗口,于是"失败比成功还快"。
    """
    monkeypatch.setattr(settings, "OPENAI_IMAGE_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "OPENAI_IMAGE_PROXY", "")
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONNECT_TIMEOUT", 30)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT", 300)

    client = OpenAIImageProvider(str(tmp_path / "out")).client

    assert client is not None
    assert client.max_retries == 1, "SDK 自身重试必须压到 1,否则与本模块重试相乘"
    t = client.timeout
    assert (t.connect, t.read, t.write) == (30.0, 300.0, 120.0)


def test_proxy_branch_also_gets_explicit_timeouts(tmp_path, monkeypatch):
    """走 HTTP 代理那支同样挂上分项超时——两支分支不能只改一支。"""
    monkeypatch.setattr(settings, "OPENAI_IMAGE_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "OPENAI_IMAGE_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "OPENAI_IMAGE_CONNECT_TIMEOUT", 30)
    monkeypatch.setattr(settings, "OPENAI_IMAGE_TIMEOUT", 300)

    client = OpenAIImageProvider(str(tmp_path / "out")).client

    assert client is not None and client.max_retries == 1
    inner = client._client.timeout           # 自带 httpx.AsyncClient 上的超时
    assert (inner.connect, inner.read, inner.write) == (30.0, 300.0, 120.0)
