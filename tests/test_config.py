import pytest

from app.core import config as config_module
from app.core.config import (
    DEFAULT_SECRET_KEY,
    Settings,
    assert_secret_key_configured,
    settings,
)


def test_defaults_present():
    assert settings.APP_NAME == "nbdpsy-mcp"
    assert settings.PUBLISH_CONCURRENCY >= 1
    assert settings.retry_delays == [120, 600, 1800]


def test_selfheal_config_defaults(monkeypatch):
    """自愈配置默认值:默认关、key 空、DashScope 默认 base_url。

    **必须与部署环境隔离**(2026-08-03 踩过):裸 `Settings()` 会读仓库根的 `.env` 与
    进程环境变量 —— 生产 `.env` 一旦把 `SELFHEAL_ENABLED` 开成 true,这条测"默认值"的
    用例就在生产机上无缘无故变红,而且红的原因与代码毫无关系。
    测默认值就要真的只看默认值:`_env_file=None` 掐掉 .env,monkeypatch 掐掉环境变量。
    """
    for var in ("SELFHEAL_ENABLED", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.SELFHEAL_ENABLED is False
    assert s.LLM_API_KEY == ""
    assert s.LLM_BASE_URL == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.LLM_MODEL == "qwen-flash"
    assert s.LLM_TIMEOUT == 15


# ---------------- N2:SECRET_KEY 生产启动闸 ----------------


def test_secret_key_guard_triggers_in_production(monkeypatch):
    """N2:DEBUG=False + 默认 SECRET_KEY → fail-fast RuntimeError。"""
    monkeypatch.setattr(config_module.settings, "DEBUG", False)
    monkeypatch.setattr(config_module.settings, "SECRET_KEY", DEFAULT_SECRET_KEY)
    with pytest.raises(RuntimeError):
        assert_secret_key_configured()


def test_secret_key_guard_passes_when_debug(monkeypatch):
    """N2:DEBUG=True 放行(即便仍是默认 key),便于本地/测试。"""
    monkeypatch.setattr(config_module.settings, "DEBUG", True)
    monkeypatch.setattr(config_module.settings, "SECRET_KEY", DEFAULT_SECRET_KEY)
    assert_secret_key_configured()  # 不抛


def test_secret_key_guard_passes_with_custom_key(monkeypatch):
    """N2:非默认 SECRET_KEY 即便 DEBUG=False 也放行。"""
    monkeypatch.setattr(config_module.settings, "DEBUG", False)
    monkeypatch.setattr(
        config_module.settings, "SECRET_KEY", "a-real-strong-random-secret-key-xyz"
    )
    assert_secret_key_configured()  # 不抛
