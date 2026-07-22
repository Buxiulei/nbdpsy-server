"""视频管线薄 provider 层契约单测（全 mock，不打真 API / 不跑真 ffmpeg）。

覆盖五能力签名与关键保真点：
- mt_translate：N 进 N 出保序 + translation_options（含 terms 映射）经 extra_body 直传 + model 正确；
- llm_chat：temperature/model 透传（默认 0.3）；
- vl_describe：本地图内联成 base64 data URL + prompt 附文；
- asr_transcribe：两跳 SDK + 转写 JSON 解析成 [{start,end,text}]（秒）；提交失败抛原异常；
- tts_synthesize：豆包 v3 流式拼片 → 时长（秒）；请求头/体正确；
- 纯解析工具 _parse_transcription / _parse_stream / _measure_wav_duration 直测。
"""

import base64
import json
import wave

import pytest

from app.core.config import settings
from app.video import providers


# ── openai 兼容替身（mt/llm/vl 共用）─────────────────────────────

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, calls, content_fn):
        self._calls = calls
        self._content_fn = content_fn

    async def create(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeResp(self._content_fn(kwargs))


class _FakeChat:
    def __init__(self, calls, content_fn):
        self.completions = _FakeCompletions(calls, content_fn)


class _FakeAsyncOpenAI:
    """替身 AsyncOpenAI：记录构造入参与每次 create kwargs，按 content_fn 生成回复。"""

    shared_calls: list = []
    shared_content_fn = staticmethod(lambda kw: "")
    last_init_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_init_kwargs = kwargs
        self.chat = _FakeChat(type(self).shared_calls, type(self).shared_content_fn)


def _install_fake_openai(monkeypatch, content_fn):
    calls: list = []
    _FakeAsyncOpenAI.shared_calls = calls
    _FakeAsyncOpenAI.shared_content_fn = staticmethod(content_fn)
    _FakeAsyncOpenAI.last_init_kwargs = None
    monkeypatch.setattr(providers, "AsyncOpenAI", _FakeAsyncOpenAI)
    return calls


# ── mt_translate ────────────────────────────────────────────────

async def test_mt_translate_preserves_order_and_direct_passes_options(monkeypatch):
    calls = _install_fake_openai(
        monkeypatch, lambda kw: "ZH:" + kw["messages"][0]["content"])
    term_sheet = [
        {"en": "CPTSD", "zh": "复杂性创伤后应激障碍", "source": "manual"},
        {"en": "", "zh": "空源丢弃", "source": "auto"},  # en 空 → 不进 terms
    ]
    out = await providers.mt_translate(["hello", "world"], term_sheet=term_sheet)

    assert out == ["ZH:hello", "ZH:world"]  # N 进 N 出且保序
    assert len(calls) == 2
    for c in calls:
        assert c["model"] == settings.VIDEO_MT_MODEL
        opts = c["extra_body"]["translation_options"]
        assert opts["source_lang"] == "English"
        assert opts["target_lang"] == "Chinese"
        # 仅 en/zh 齐全的 term 进 terms，映射成 source/target
        assert opts["terms"] == [{"source": "CPTSD", "target": "复杂性创伤后应激障碍"}]
    # 客户端用 DashScope key + base_url 构造
    assert _FakeAsyncOpenAI.last_init_kwargs["api_key"] == settings.DASHSCOPE_API_KEY
    assert _FakeAsyncOpenAI.last_init_kwargs["base_url"] == settings.DASHSCOPE_BASE_URL


async def test_mt_translate_optional_domains_and_tm_list(monkeypatch):
    """domains/tm_list 非 None 才进 translation_options；缺省则不出现（provider 无状态）。"""
    calls = _install_fake_openai(monkeypatch, lambda kw: "z")
    domains = 'A Chinese psychology popular-science video about "CPTSD".'
    tm = [{"source": "prior en", "target": "上文译文"}]
    await providers.mt_translate(
        ["s"], term_sheet=[], domains=domains, tm_list=tm)
    opts = calls[0]["extra_body"]["translation_options"]
    assert opts["domains"] == domains
    assert opts["tm_list"] == tm

    # 缺省不塞 domains/tm_list（只留 source_lang/target_lang/terms）
    calls2 = _install_fake_openai(monkeypatch, lambda kw: "z")
    await providers.mt_translate(["s"], term_sheet=[])
    opts2 = calls2[0]["extra_body"]["translation_options"]
    assert "domains" not in opts2
    assert "tm_list" not in opts2


async def test_mt_translate_empty_returns_empty(monkeypatch):
    _install_fake_openai(monkeypatch, lambda kw: "x")
    assert await providers.mt_translate([], term_sheet=[]) == []


async def test_mt_translate_raises_original_exception(monkeypatch):
    def _boom(kw):
        raise RuntimeError("qwen-mt 429")

    _install_fake_openai(monkeypatch, _boom)
    with pytest.raises(RuntimeError, match="qwen-mt 429"):
        await providers.mt_translate(["hi"], term_sheet=[])


# ── llm_chat ────────────────────────────────────────────────────

async def test_llm_chat_passes_model_and_temperature(monkeypatch):
    calls = _install_fake_openai(monkeypatch, lambda kw: "回复")
    out = await providers.llm_chat(
        [{"role": "user", "content": "问"}], temperature=0.7)
    assert out == "回复"
    assert calls[0]["model"] == settings.VIDEO_LLM_MODEL
    assert calls[0]["temperature"] == 0.7
    assert calls[0]["messages"] == [{"role": "user", "content": "问"}]


async def test_llm_chat_default_temperature(monkeypatch):
    calls = _install_fake_openai(monkeypatch, lambda kw: "ok")
    await providers.llm_chat([{"role": "user", "content": "q"}])
    assert calls[0]["temperature"] == 0.3


# ── vl_describe ─────────────────────────────────────────────────

async def test_vl_describe_inlines_local_image(monkeypatch, tmp_path):
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nFAKEDATA")
    calls = _install_fake_openai(monkeypatch, lambda kw: "画面描述")

    out = await providers.vl_describe(str(img), "描述这一帧")
    assert out == "画面描述"
    assert calls[0]["model"] == settings.VIDEO_VL_MODEL
    content = calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    url = content[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # data URL 尾部确为原图 base64
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG\r\n\x1a\nFAKEDATA"
    assert content[1] == {"type": "text", "text": "描述这一帧"}


# ── asr_transcribe ──────────────────────────────────────────────

def test_parse_transcription_ms_to_seconds():
    payload = {
        "transcripts": [
            {"sentences": [
                {"begin_time": 0, "end_time": 1500, "text": " Hello "},
                {"begin_time": 1500, "end_time": 3200, "text": "world"},
            ]},
        ]
    }
    segs = providers._parse_transcription(payload)
    assert segs == [
        {"start": 0.0, "end": 1.5, "text": "Hello"},
        {"start": 1.5, "end": 3.2, "text": "world"},
    ]
    assert providers._parse_transcription({}) == []


class _FakeAsrOutput:
    task_id = "task-123"


class _FakeAsrTask:
    output = _FakeAsrOutput()
    status_code = 200
    code = None
    message = None


class _FakeAsrResult:
    output = {
        "task_status": "SUCCEEDED",
        "results": [{"transcription_url": "http://x/transcription.json"}],
    }
    status_code = 200


def _install_fake_asr(monkeypatch, *, task=None, result=None, payload=None):
    captured: dict = {}

    class _FakeTranscription:
        @staticmethod
        def async_call(**kw):
            captured["async_call"] = kw
            return task or _FakeAsrTask()

        @staticmethod
        def wait(**kw):
            captured["wait"] = kw
            return result or _FakeAsrResult()

    monkeypatch.setattr(providers, "_load_transcription", lambda: _FakeTranscription)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload or {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            captured["get_url"] = url
            return _Resp()

    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    return captured


async def test_asr_transcribe_two_hops_and_parse(monkeypatch):
    payload = {"transcripts": [{"sentences": [
        {"begin_time": 200, "end_time": 900, "text": "hi"}]}]}
    cap = _install_fake_asr(monkeypatch, payload=payload)

    segs = await providers.asr_transcribe("http://pub/audio.mp3")
    assert segs == [{"start": 0.2, "end": 0.9, "text": "hi"}]
    assert cap["async_call"]["model"] == settings.VIDEO_ASR_MODEL
    assert cap["async_call"]["file_urls"] == ["http://pub/audio.mp3"]
    assert cap["async_call"]["api_key"] == settings.DASHSCOPE_API_KEY
    assert cap["wait"]["task"] == "task-123"
    assert cap["get_url"] == "http://x/transcription.json"


async def test_asr_transcribe_raises_on_submit_failure(monkeypatch):
    class _NoOutputTask:
        output = None
        status_code = 403
        code = "Model.AccessDenied"
        message = "not granted"

    _install_fake_asr(monkeypatch, task=_NoOutputTask())
    with pytest.raises(RuntimeError, match="ASR 任务提交失败"):
        await providers.asr_transcribe("http://pub/audio.mp3")


# ── tts_synthesize ──────────────────────────────────────────────

def _stream_bytes(chunks: list[bytes], err: str | None = None) -> bytes:
    """构造豆包 v3 多行 JSON 流：每 chunk 一行 data(base64)，可选末行 error。"""
    lines = [json.dumps({"data": base64.b64encode(c).decode()}).encode() for c in chunks]
    if err:
        lines.append(json.dumps({"header": {"message": err}}).encode())
    return b"\n".join(lines)


def test_parse_stream_accumulates_and_errors():
    raw = _stream_bytes([b"abc", b"def"])
    assert providers._parse_stream(raw) == b"abcdef"
    # 无 data 仅 error → 抛
    only_err = json.dumps({"header": {"message": "grant not found"}}).encode()
    with pytest.raises(RuntimeError, match="grant not found"):
        providers._parse_stream(only_err)


def test_measure_wav_duration(tmp_path):
    path = str(tmp_path / "a.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 24000)  # 24000 帧 @24kHz = 1.0s
    assert providers._measure_wav_duration(path) == pytest.approx(1.0)


async def test_tts_synthesize_streams_and_returns_duration(monkeypatch, tmp_path):
    raw = _stream_bytes([b"MP3PART1", b"MP3PART2"])
    captured: dict = {}

    class _StreamResp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield raw

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, *, headers, json):
            captured.update(method=method, url=url, headers=headers, body=json)
            return _StreamResp()

    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)

    # 拦截 ffmpeg 转 wav 缝：写一个 0.5s 已知 wav
    async def _fake_conv(audio, out_path):
        captured["audio"] = audio
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 12000)  # 0.5s

    monkeypatch.setattr(providers, "_mp3_bytes_to_wav", _fake_conv)

    out_path = str(tmp_path / "seg.wav")
    dur = await providers.tts_synthesize("你好", voice="S_hoiqVFN72", out_path=out_path)

    assert dur == pytest.approx(0.5)
    assert captured["audio"] == b"MP3PART1MP3PART2"  # 分片按行拼接
    assert captured["url"] == providers._DOUBAO_ENDPOINT
    assert captured["headers"]["X-Api-Key"] == settings.DOUBAO_TTS_TOKEN
    assert captured["headers"]["X-Api-App-Id"] == settings.DOUBAO_TTS_APPID
    assert captured["headers"]["X-Api-Resource-Id"] == settings.DOUBAO_TTS_RESOURCE_ID
    assert captured["body"]["req_params"]["speaker"] == "S_hoiqVFN72"


@pytest.mark.parametrize(
    "rate,expected",
    [(1.0, 0), (1.5, 50), (0.5, -50), (2.0, 100), (3.0, 100), (0.1, -50)],
)
async def test_tts_synthesize_rate_maps_to_speech_rate(
    monkeypatch, tmp_path, rate, expected):
    """rate（1.0 基准倍率）→ 火山 speech_rate（整数百分比，钳 [-50,100]）。"""
    raw = _stream_bytes([b"MP3"])
    captured: dict = {}

    class _StreamResp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield raw

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, *, headers, json):
            captured["body"] = json
            return _StreamResp()

    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)

    async def _fake_conv(audio, out_path):
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000)

    monkeypatch.setattr(providers, "_mp3_bytes_to_wav", _fake_conv)

    await providers.tts_synthesize(
        "x", voice="v", out_path=str(tmp_path / "o.wav"), rate=rate)
    assert captured["body"]["req_params"]["audio_params"]["speech_rate"] == expected


async def test_tts_synthesize_raises_on_empty_audio(monkeypatch, tmp_path):
    class _StreamResp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield b""  # 无任何 data event

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **kw):
            return _StreamResp()

    monkeypatch.setattr(providers.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="空音频"):
        await providers.tts_synthesize("x", voice="v", out_path=str(tmp_path / "o.wav"))
