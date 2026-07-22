"""视频管线薄 AI provider 层：五能力直连，不搬运营工具的 AIScheduler/registry/batch 大机器。

设计意图（方案 C 的"薄"体现）：
- 每个能力是一个 async 顶层函数，直连 DashScope/豆包，只做"发一次请求 + 解析"，**不做重试、
  不做降级兜底**——失败抛原异常，重试与回退由调用方（M3 pipeline）按源语义决定。这是跨 track
  冻结契约（docs/plans §接口契约），签名逐字实现，不得增删参数。
- ASR/翻译/LLM/VL 共用一把 DASHSCOPE_API_KEY；翻译/LLM/VL 走 openai 兼容 compatible-mode，
  ASR 走 dashscope SDK 自带的录音文件识别端点。TTS 走豆包声音复刻 v3。

踩过的坑（保真要点）：
- **qwen-mt 三件套必须直传**：翻译走 model=VIDEO_MT_MODEL + extra_body.translation_options
  同时到位。运营工具曾因 worker 走 HTTP 代理把 translation_options+model 一起丢了，静默降级成
  qwen3.7-plus 裸句聊天（实测出寒暄回复），三件套全失效。此处直连 AsyncOpenAI 保证二者都在。
- **dashscope 延迟导入**：dashscope SDK 尚未进宿主 requirements（M4 补），故 ASR 在函数内经
  `_load_transcription()` 惰性导入，使本模块在无 dashscope 环境下仍可 import + 被 mock 测试。
- 豆包 v3 是 chunked 流式：响应体多行 JSON，音频在各行 data（base64 mp3 分片），须按行拼接。

测试缝（seam）：`AsyncOpenAI`（模块级，仿宿主 self_heal 便于 monkeypatch）、`_load_transcription`、
`_mp3_bytes_to_wav` 均可替身；纯函数 `_parse_transcription`/`_parse_stream`/`_measure_wav_duration`/
`_image_to_data_url` 直接单测。
"""

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import wave
from pathlib import Path

import httpx
from openai import AsyncOpenAI  # 模块级导入，便于测试 monkeypatch providers.AsyncOpenAI

from app.core.config import settings

# 豆包声音复刻 v3 单向流式 TTS 端点
_DOUBAO_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

# qwen-mt 逐句翻译并发上限（与源 translator 一致；纯 provider 直连，语义闸由调用方另算）
_MT_CONCURRENCY = 8


# ── 客户端 / 依赖缝 ──────────────────────────────────────────────────

def _openai_client() -> AsyncOpenAI:
    """DashScope openai 兼容异步客户端（翻译/LLM/VL 共用）。"""
    return AsyncOpenAI(
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.DASHSCOPE_BASE_URL,
    )


def _load_transcription():
    """惰性导入 dashscope 录音文件识别类（SDK 未装环境下不拖垮整模块 import）。"""
    from dashscope.audio.asr import Transcription

    return Transcription


# ── 纯解析工具（可直接单测）────────────────────────────────────────

def _parse_transcription(payload: dict) -> list[dict]:
    """paraformer 转写正文 JSON → [{start,end,text}]（秒，保留 3 位小数）。

    官方结构：transcripts[].sentences[].{begin_time,end_time,text}，时间单位毫秒。
    缺字段/空列表返回 [] 不抛。
    """
    segments: list[dict] = []
    for transcript in payload.get("transcripts") or []:
        for s in transcript.get("sentences") or []:
            segments.append({
                "start": round(s["begin_time"] / 1000.0, 3),
                "end": round(s["end_time"] / 1000.0, 3),
                "text": (s.get("text") or "").strip(),
            })
    return segments


def _parse_stream(raw: bytes) -> bytes:
    """豆包 v3 chunked 响应：多行 JSON，累积各 event 的 data（base64 mp3 分片）拼成完整 mp3。

    无 data 且带 error message 的响应抛出（如 resource not granted / grant not found）。
    """
    audio = b""
    last_err = ""
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        d = ev.get("data")
        if d:
            with contextlib.suppress(Exception):
                audio += base64.b64decode(d)
        else:
            hdr = ev.get("header") or {}
            msg = hdr.get("message") or ev.get("message")
            if msg:
                last_err = msg
    if not audio and last_err:
        raise RuntimeError(f"豆包合成失败: {last_err}")
    return audio


def _measure_wav_duration(path: str) -> float:
    """读 wav 头算时长（帧数 / 采样率），秒。"""
    with contextlib.closing(wave.open(path, "rb")) as w:
        return w.getnframes() / float(w.getframerate())


def _image_to_data_url(image_path: str) -> str:
    """本地图片 → base64 data URL（qwen-vl 多模态内联，免公网托管）。"""
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    raw = Path(image_path).read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _mp3_bytes_to_wav(audio: bytes, out_path: str) -> None:
    """mp3 分片拼好的字节 → ffmpeg 转 wav 24kHz mono 落 out_path（dubber 拼轨格式一致）。

    独立成缝：测试可 monkeypatch 本函数写入已知 wav，免依赖真 ffmpeg。
    """
    tmp_mp3 = out_path + ".tmp.mp3"
    with open(tmp_mp3, "wb") as f:
        f.write(audio)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", tmp_mp3, "-ar", "24000", "-ac", "1", out_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_mp3)
        raise RuntimeError("豆包 wav 转换超时")
    if proc.returncode != 0:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_mp3)
        raise RuntimeError(f"豆包 wav 转换失败: {stderr.decode()[-300:]}")
    os.unlink(tmp_mp3)


# ── 五能力契约（跨 track 冻结签名，逐字实现）──────────────────────

async def asr_transcribe(audio_url: str) -> list[dict]:
    """DashScope paraformer 录音文件识别：收公网 URL，返回 [{start,end,text}]（秒）。

    两跳：async_call 提交拿 task_id → wait 轮询到终态拿 transcription_url → GET 取转写正文 JSON。
    SDK 同步阻塞，用 asyncio.to_thread 包异步避免卡 event loop。任一步失败抛 RuntimeError（原异常）。
    """
    transcription = _load_transcription()

    def _call():
        # api_key 显式下传，避免污染进程级 dashscope.api_key 全局
        task = transcription.async_call(
            model=settings.VIDEO_ASR_MODEL,
            file_urls=[audio_url],
            language_hints=["en"],
            api_key=settings.DASHSCOPE_API_KEY,
        )
        if task.output is None or getattr(task.output, "task_id", None) is None:
            raise RuntimeError(
                f"ASR 任务提交失败: status_code={task.status_code} "
                f"code={task.code} message={task.message}"
            )
        return transcription.wait(task=task.output.task_id, api_key=settings.DASHSCOPE_API_KEY)

    result = await asyncio.to_thread(_call)
    output = result.output if isinstance(result.output, dict) else dict(result.output or {})
    if output.get("task_status") != "SUCCEEDED":
        raise RuntimeError(
            f"ASR 转写失败: status_code={result.status_code} output={str(output)[:500]}"
        )

    results = output.get("results") or []
    if not results or not results[0].get("transcription_url"):
        raise RuntimeError(f"ASR 转写成功但缺 transcription_url: {str(output)[:500]}")
    url = results[0]["transcription_url"]

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()

    return _parse_transcription(payload)


async def mt_translate(texts: list[str], *, term_sheet: list[dict]) -> list[str]:
    """qwen-mt 逐句翻译（英→中），N 进 N 出保时间轴。

    内部构造 translation_options（source_lang/target_lang/terms）经 extra_body 直传——
    model=VIDEO_MT_MODEL 与 translation_options 必须同时到位（三件套保真的命门）。
    term_sheet 元素形如 {"en","zh","source"}，取 en/zh 组装 terms。并发上限 _MT_CONCURRENCY，
    任一句失败抛原异常（不重试、不回退，由调用方按源语义兜底）。
    """
    if not texts:
        return []
    terms = [
        {"source": t["en"], "target": t["zh"]}
        for t in term_sheet or []
        if t.get("en") and t.get("zh")
    ]
    options = {"source_lang": "English", "target_lang": "Chinese", "terms": terms}
    client = _openai_client()
    sem = asyncio.Semaphore(_MT_CONCURRENCY)

    async def _one(text: str) -> str:
        async with sem:
            resp = await client.chat.completions.create(
                model=settings.VIDEO_MT_MODEL,
                messages=[{"role": "user", "content": text}],
                extra_body={"translation_options": options},
            )
            return (resp.choices[0].message.content or "").strip()

    return list(await asyncio.gather(*[_one(t) for t in texts]))


async def llm_chat(messages: list[dict], *, temperature: float = 0.3) -> str:
    """通用 LLM 对话（重写/解析/本地化），走 openai 兼容 VIDEO_LLM_MODEL。返回正文字符串。"""
    client = _openai_client()
    resp = await client.chat.completions.create(
        model=settings.VIDEO_LLM_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


async def vl_describe(image_path: str, prompt: str) -> str:
    """qwen-vl 关键帧视觉理解：本地图转 base64 data URL 内联 + prompt，返回描述文本。"""
    data_url = _image_to_data_url(image_path)
    client = _openai_client()
    resp = await client.chat.completions.create(
        model=settings.VIDEO_VL_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return resp.choices[0].message.content or ""


async def tts_synthesize(text: str, *, voice: str, out_path: str) -> float:
    """豆包声音复刻 v3 合成 → wav 落 out_path，返回音频时长（秒）。

    v3 chunked 流式：拼各行 data（base64 mp3 分片）成完整 mp3 → ffmpeg 转 wav 24kHz mono。
    空音频/带 error 的响应抛 RuntimeError（原异常，不重试）。
    """
    headers = {
        "X-Api-Key": settings.DOUBAO_TTS_TOKEN,
        "X-Api-Resource-Id": settings.DOUBAO_TTS_RESOURCE_ID,
        "X-Api-App-Id": settings.DOUBAO_TTS_APPID,
        "Content-Type": "application/json",
    }
    body = {
        "user": {"uid": "nbdpsy_video"},
        "req_params": {
            "text": text,
            "speaker": voice,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        },
    }
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", _DOUBAO_ENDPOINT, headers=headers, json=body) as r:
            raw = b"".join([chunk async for chunk in r.aiter_bytes()])
    audio = _parse_stream(raw)
    if not audio:
        raise RuntimeError(f"豆包合成返回空音频: text[:50]={text[:50]!r} raw={raw[:150]!r}")

    await _mp3_bytes_to_wav(audio, out_path)
    return _measure_wav_duration(out_path)
