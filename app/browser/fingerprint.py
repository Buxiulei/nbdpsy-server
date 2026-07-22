"""per-account 稳定浏览器指纹生成 + 持久化。

移植自旧仓 ``smart_browser/fingerprint_factory.py`` + ``schemas.py``,做两点收敛:

1. **确定性播种**:旧仓用全局 ``random`` + 运行时去重(Robot-4 并发场景);
   新仓只需"同一账号恒定、不同账号相异",故改为按 ``account_id`` 播种的
   局部 ``random.Random`` —— 无需去重集合,无需全局随机状态污染,且删档后
   重新生成结果可复现。
2. **统一持久化路径**:指纹落盘到 ``profile_guard.profile_dir(id)/fingerprint.json``,
   与 profile 目录同源,不再单开 ``browser_data/profiles/`` 目录。

数据源:``app/browser/data/*.json``(真实 Firefox UA / 分辨率 / WebGL 渲染器)。
UA 已从 Chrome 切为 Firefox(对齐 camoufox 内核 Firefox/135),消除引擎↔UA↔JA3 三方矛盾。
"""
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List

from loguru import logger

from app.browser.profile_guard import profile_dir

# 指纹数据目录(随包发布)
_DATA_DIR = Path(__file__).parent / "data"


@dataclass
class BrowserFingerprint:
    """浏览器指纹(字段与旧仓 smart_browser/schemas 对齐)。"""

    user_agent: str
    viewport: Dict[str, int]             # {width, height}
    locale: str
    timezone: str
    platform: str
    hardware_concurrency: int
    device_memory: int
    screen_resolution: Dict[str, int]    # {width, height}
    webgl_vendor: str
    webgl_renderer: str
    canvas_noise_seed: int
    audio_noise_seed: int


# 平台标识映射
_PLATFORM_MAP = {
    "windows": "Win32",
    "macos": "MacIntel",
    "linux": "Linux x86_64",
}
# 兜底 UA(数据文件缺失时)
# Firefox UA(对齐 camoufox 内核 Firefox/135;camoufox 是 Firefox 引擎,UA 报 Chrome 会与
# JA3/navigator.* 三方矛盾被一眼识破,故 fallback 也必须是 Firefox)。
_FALLBACK_UA = {
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
    "linux": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
}


def _load_json(filename: str) -> dict:
    """加载数据文件,缺失/损坏时返回空 dict 走兜底。"""
    filepath = _DATA_DIR / filename
    try:
        return json.loads(filepath.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"[fingerprint] 数据文件加载失败: {filepath} - {e}")
        return {}


def _seed_for(account_id: int) -> int:
    """由 account_id 派生稳定随机种子(md5,保证相邻 id 也充分离散)。"""
    return int(hashlib.md5(f"fp_seed_{account_id}".encode()).hexdigest()[:8], 16)


def _weighted_choice(rng: random.Random, items: List[dict], key: str = "weight") -> dict:
    """按权重随机选择(用传入的局部 rng,保证确定性)。"""
    if not items:
        return {}
    weights = [item.get(key, 1) for item in items]
    return rng.choices(items, weights=weights, k=1)[0]


def _build_fingerprint(account_id: int) -> BrowserFingerprint:
    """按 account_id 确定性生成一份内部一致的指纹。"""
    rng = random.Random(_seed_for(account_id))

    user_agents = _load_json("user_agents.json")
    screen_data = _load_json("screen_resolutions.json")
    webgl_data = _load_json("webgl_renderers.json")

    # 1. 平台分布(80% Windows / 15% macOS / 5% Linux)
    roll = rng.random()
    if roll < 0.80:
        platform_key = "windows"
    elif roll < 0.95:
        platform_key = "macos"
    else:
        platform_key = "linux"

    # 2. 按权重选 UA + 平台标识
    ua_entry = _weighted_choice(rng, user_agents.get(platform_key, []))
    user_agent = ua_entry.get("ua", _FALLBACK_UA[platform_key])
    platform = _PLATFORM_MAP[platform_key]

    # 3. 按权重选屏幕分辨率
    res_entry = _weighted_choice(rng, screen_data.get("resolutions", []))
    screen_width = res_entry.get("width", 1920)
    screen_height = res_entry.get("height", 1080)

    # 4. viewport = 屏幕高 - 工具栏偏移(宽度不变)
    offsets = screen_data.get("viewport_offsets", {})
    lo, hi = offsets.get("height_offset_range", [40, 120])
    viewport_height = screen_height - rng.randint(lo, hi)
    viewport_width = screen_width

    # 5. 按平台选 WebGL 渲染器
    webgl_entry = _weighted_choice(
        rng, webgl_data.get("renderers", {}).get(platform_key, [])
    )
    webgl_vendor = webgl_entry.get("vendor", "Google Inc. (Intel)")
    webgl_renderer = webgl_entry.get(
        "renderer", "ANGLE (Intel, Intel(R) UHD Graphics 630)"
    )

    # 6. 硬件参数(与分辨率关联:高分屏通常配置更高)
    if screen_width >= 2560:
        hardware_concurrency = rng.choice([8, 12, 16])
        device_memory = rng.choice([8, 16, 32])
    elif screen_width >= 1920:
        hardware_concurrency = rng.choice([4, 8, 12])
        device_memory = rng.choice([8, 16])
    else:
        hardware_concurrency = rng.choice([4, 8])
        device_memory = rng.choice([4, 8])

    # 7. Canvas/Audio 噪声种子:直接由 account_id 派生 → 不同账号恒异
    canvas_noise_seed = int(
        hashlib.md5(f"fp_{account_id}_canvas".encode()).hexdigest()[:8], 16
    )
    audio_noise_seed = int(
        hashlib.md5(f"fp_{account_id}_audio".encode()).hexdigest()[:8], 16
    )

    return BrowserFingerprint(
        user_agent=user_agent,
        viewport={"width": viewport_width, "height": viewport_height},
        locale="zh-CN",
        timezone="Asia/Shanghai",
        platform=platform,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        screen_resolution={"width": screen_width, "height": screen_height},
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        canvas_noise_seed=canvas_noise_seed,
        audio_noise_seed=audio_noise_seed,
    )


def get_fingerprint(account_id: int) -> BrowserFingerprint:
    """获取账号的稳定指纹:已落盘则复用,否则生成并落盘。

    同一 ``account_id`` 多次调用返回一致指纹(先靠磁盘缓存,即使删档也因
    确定性播种而可复现);不同 ``account_id`` 因 Canvas 噪声种子直接派生而恒异。
    """
    pdir = profile_dir(account_id)
    fp_path = pdir / "fingerprint.json"

    # 已持久化 → 复用,保证跨调用/跨进程/跨重启一致
    if fp_path.exists():
        try:
            data = json.loads(fp_path.read_text(encoding="utf-8"))
            return BrowserFingerprint(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"[fingerprint] 指纹文件损坏,重新生成: {fp_path} - {e}")

    fp = _build_fingerprint(account_id)
    pdir.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(
        json.dumps(asdict(fp), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"[fingerprint] 账号 {account_id} 生成新指纹: UA={fp.user_agent[:50]}...")
    return fp
