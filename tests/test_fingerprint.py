"""fingerprint 纯逻辑单测(不起真浏览器)。

覆盖:
- 同 account_id 多次调用返回一致指纹
- 指纹持久化到 profile_dir/fingerprint.json
- 不同 account_id 指纹不同
- 内部一致性(UA 平台 ↔ platform 字段)
"""
import json

import pytest

from app.browser.fingerprint import BrowserFingerprint, get_fingerprint


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    """把 DATA_DIR 指向临时目录,避免污染真实 data/。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_same_account_consistent():
    """同一账号两次调用返回完全一致的指纹。"""
    fp1 = get_fingerprint(1)
    fp2 = get_fingerprint(1)
    assert fp1 == fp2


def test_persists_fingerprint_json(_isolated_data_dir):
    """指纹落盘到 profile_dir/fingerprint.json,内容可反序列化。"""
    fp = get_fingerprint(42)
    fp_path = _isolated_data_dir / "browser" / "account_42" / "fingerprint.json"
    assert fp_path.exists()
    data = json.loads(fp_path.read_text(encoding="utf-8"))
    assert data["user_agent"] == fp.user_agent
    assert data["canvas_noise_seed"] == fp.canvas_noise_seed


def test_reload_from_disk_consistent(_isolated_data_dir):
    """落盘后重新读取(手动改内存无关)仍一致 —— 复现跨进程/重启场景。"""
    fp_first = get_fingerprint(8)
    # 第二次调用应命中磁盘缓存而非重新随机
    fp_again = get_fingerprint(8)
    assert fp_first == fp_again


def test_different_accounts_differ():
    """不同账号指纹必不同(canvas 噪声种子直接由 account_id 派生,恒异)。"""
    fp1 = get_fingerprint(1)
    fp2 = get_fingerprint(2)
    assert fp1 != fp2
    assert fp1.canvas_noise_seed != fp2.canvas_noise_seed


def test_deterministic_across_regeneration(_isolated_data_dir):
    """删掉落盘文件后重新生成 —— 因按 account_id 播种,结果可复现。"""
    fp1 = get_fingerprint(123)
    (_isolated_data_dir / "browser" / "account_123" / "fingerprint.json").unlink()
    fp2 = get_fingerprint(123)
    assert fp1 == fp2


def test_internal_platform_consistency():
    """UA 声明的操作系统必须与 platform 字段一致。"""
    for acc in range(1, 30):
        fp = get_fingerprint(acc)
        ua = fp.user_agent.lower()
        if "windows" in ua:
            assert "Win" in fp.platform
        elif "macintosh" in ua or "mac os" in ua:
            assert "Mac" in fp.platform
        elif "linux" in ua:
            assert "Linux" in fp.platform
        # 屏幕分辨率 >= viewport
        assert fp.screen_resolution["width"] >= fp.viewport["width"]
        assert fp.screen_resolution["height"] >= fp.viewport["height"]


def test_returns_browser_fingerprint_type():
    """返回类型为 BrowserFingerprint 且字段齐全。"""
    fp = get_fingerprint(1)
    assert isinstance(fp, BrowserFingerprint)
    assert fp.locale == "zh-CN"
    assert fp.timezone == "Asia/Shanghai"
    assert 2 <= fp.hardware_concurrency <= 32
    assert 2 <= fp.device_memory <= 32
