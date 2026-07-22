"""EMDR 双侧提示音：与球共享同一全局相位函数（spec §8）。

球位置 = sin(2π * t_global / period)；端点时刻 t_k = period*(0.25 + 0.5k)，
sin=+1 → 球在右 → 右声道响；sin=-1 → 左声道响。与画面渲染同公式，不做二次对齐。
"""
import math
import wave
from pathlib import Path

import numpy as np

_RATE = 44100          # 采样率，与 muxer 混音链一致
_FREQ = 220.0          # 提示音频率（低频柔和）
_TONE_S = 0.12         # 单次提示音时长
_AMP = 0.2             # 峰值幅度（约 -14 dBFS，垫在配音下不刺耳）


def endpoint_times(t0: float, t1: float, period_s: float) -> list[tuple[float, int]]:
    """区间 [t0,t1] 内球到达左右端点的全局时刻序列。

    端点 t_k = period*(0.25 + 0.5k)（全局相位原点 t=0）；k 偶数 sin=+1（右）、奇数 -1（左）。
    """
    out: list[tuple[float, int]] = []
    k = math.ceil((t0 / period_s - 0.25) / 0.5)
    k = max(k, 0)
    while True:
        t = period_s * (0.25 + 0.5 * k)
        if t > t1:
            break
        if t >= t0:
            out.append((t, +1 if k % 2 == 0 else -1))
        k += 1
    return out


def _tone() -> np.ndarray:
    """单声道提示音样本：正弦 + 升余弦包络（无爆音）。"""
    n = int(_RATE * _TONE_S)
    t = np.arange(n) / _RATE
    envelope = 0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / n))
    return (_AMP * np.sin(2 * np.pi * _FREQ * t) * envelope)


def bilateral_track(ball_scenes: list[dict], total_duration: float,
                    out_wav: Path) -> Path | None:
    """全片双侧提示音立体声轨；无（运动）球场景返回 None（compose 跳过混提示音）。

    静止休息球段（params.static，wave2 问题③：EMDR 组间休息）不产提示音，过滤掉。
    """
    scenes = [s for s in ball_scenes
              if (s.get("params") or {}).get("period_s")
              and not (s.get("params") or {}).get("static")]
    if not scenes:
        return None
    n_total = int(_RATE * total_duration)
    track = np.zeros((n_total, 2), dtype=np.float64)
    tone = _tone()
    for sc in scenes:
        period = float(sc["params"]["period_s"])
        for t, side in endpoint_times(float(sc["t0"]), float(sc["t1"]), period):
            i0 = int(t * _RATE)
            i1 = min(i0 + len(tone), n_total)
            if i0 >= n_total:
                continue
            ch = 1 if side > 0 else 0          # 右端点→右声道(index 1)
            track[i0:i1, ch] += tone[: i1 - i0]
    pcm = (np.clip(track, -1.0, 1.0) * 32767).astype(np.int16)
    out_wav = Path(out_wav)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(pcm.tobytes())
    return out_wav
