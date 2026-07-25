"""egress_guard 自检测试:规则判定纯函数 + 告警分支(不起真浏览器)。

背景:sing-box 里 camoufox → direct-out 的直连规则是**磁盘配置**,代理软件更新/重装会
覆盖掉;规则一丢 camoufox 出国 → 小红书风控 401 踢登录,症状与 ark-401 一模一样极易误判。
本组件让它当天暴露。
"""
import json

import pytest

from app.browser import egress_guard
from app.browser.egress_guard import EgressGuard, check_singbox_rule

_RULE_OK = {
    "route": {
        "rules": [
            {"protocol": "dns", "action": "hijack-dns"},
            {"process_path": ["/home/roots/.cache/camoufox/camoufox-bin"],
             "outbound": "direct-out"},
        ]
    }
}
_RULE_GONE = {
    "route": {
        "rules": [
            {"protocol": "dns", "action": "hijack-dns"},
            {"port": [22], "outbound": "direct-out"},
        ]
    }
}


def _write(tmp_path, payload) -> str:
    p = tmp_path / "singbox.json"
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload,
                 encoding="utf-8")
    return str(p)


def test_rule_present_by_process_path(tmp_path):
    ok, msg = check_singbox_rule(_write(tmp_path, _RULE_OK))
    assert ok, msg


def test_rule_present_by_process_name(tmp_path):
    """process_name 形式同样算在位(配置里两种写法都有)。"""
    cfg = {"route": {"rules": [
        {"process_name": ["cloudflared", "camoufox-bin"], "outbound": "direct-out"}]}}
    ok, _ = check_singbox_rule(_write(tmp_path, cfg))
    assert ok


def test_rule_missing_detected(tmp_path):
    """规则被代理重装覆盖掉 → 必须判为丢失(这是本组件存在的唯一理由)。"""
    ok, msg = check_singbox_rule(_write(tmp_path, _RULE_GONE))
    assert not ok
    assert "camoufox" in msg


def test_rule_wrong_outbound_is_missing(tmp_path):
    """有 camoufox 但 outbound 不是 direct-out(比如被改成走代理)→ 同样算丢失。"""
    cfg = {"route": {"rules": [
        {"process_path": ["/home/roots/.cache/camoufox/camoufox-bin"],
         "outbound": "proxy-out"}]}}
    ok, _ = check_singbox_rule(_write(tmp_path, cfg))
    assert not ok


def test_unreadable_config_does_not_alarm(tmp_path):
    """配置读不到 → 不告警(未知 != 故障,否则换机器/换路径就天天误报)。"""
    ok, msg = check_singbox_rule(str(tmp_path / "不存在.json"))
    assert ok
    assert "跳过" in msg


def test_malformed_config_falls_back_to_substring(tmp_path):
    """配置非法 JSON → 退回子串判定,不因解析失败误报丢失。"""
    ok, _ = check_singbox_rule(_write(tmp_path, "{坏掉的 json camoufox-bin"))
    assert ok


async def test_check_once_flags_foreign_egress(tmp_path, monkeypatch):
    """出口非国内 → egress_ok=False(该分支会打 ERROR 告警)。"""
    monkeypatch.setattr(egress_guard.settings, "SINGBOX_CONFIG_PATH",
                        _write(tmp_path, _RULE_OK))
    monkeypatch.setattr(egress_guard, "_probe_egress_sync",
                        lambda: "当前 IP:138.128.195.22  来自于:美国 加利福尼亚州")

    result = await EgressGuard(interval=999).check_once()
    assert result["rule_ok"] is True
    assert result["egress_ok"] is False


async def test_check_once_ok_when_domestic(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_guard.settings, "SINGBOX_CONFIG_PATH",
                        _write(tmp_path, _RULE_OK))
    monkeypatch.setattr(egress_guard, "_probe_egress_sync",
                        lambda: "当前 IP:111.201.214.83  来自于:中国 北京 联通")

    result = await EgressGuard(interval=999).check_once()
    assert result["rule_ok"] is True and result["egress_ok"] is True


async def test_probe_failure_does_not_judge(tmp_path, monkeypatch):
    """探测取不到结果(网络/浏览器抖动)→ egress_ok=None,本轮不下判定、不误告警。"""
    monkeypatch.setattr(egress_guard.settings, "SINGBOX_CONFIG_PATH",
                        _write(tmp_path, _RULE_OK))
    monkeypatch.setattr(egress_guard, "_probe_egress_sync", lambda: "")

    result = await EgressGuard(interval=999).check_once()
    assert result["egress_ok"] is None
