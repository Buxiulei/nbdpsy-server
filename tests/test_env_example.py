""".env.example 完整性:必须覆盖 Settings 的全部字段(防新增字段漏进样例)。

只校验字段名覆盖,不校验值——样例值是占位符,真实值由部署方填。properties
(如 retry_delays)不是 pydantic 字段,不在 model_fields 里,天然不参与校验。
"""

from pathlib import Path

from app.core.config import Settings

_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _parse_env_keys(path: Path) -> set[str]:
    """解析 .env 文件里的键名(忽略空行与 # 注释)。"""
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_env_example_covers_all_settings_fields():
    """.env.example 覆盖 Settings 每一个字段(缺任何字段即失败并列出)。"""
    example_keys = _parse_env_keys(_ENV_EXAMPLE)
    field_names = set(Settings.model_fields)
    missing = field_names - example_keys
    assert not missing, f".env.example 缺以下 Settings 字段: {sorted(missing)}"
