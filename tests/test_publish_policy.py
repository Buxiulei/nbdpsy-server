"""发布终态/冷却纯函数(app.publish.policy)单测。

核心断言:decide_finish 与 scheduler.finish 语义逐字节一致——
- success → published、error 清空、不重试不递增;
- need_manual_login(I1)/ account_restricted(F3)→ 立即 failed、缺省文案、不递增;
- 普通失败有额度 → pending + retry_delays[retries]×(0.8~1.5) 抖动排期 + 递增;
- 重试耗尽 → failed;
- cooldown_remaining_s / daily_cap_reached 的边界。
"""

from datetime import datetime, timedelta

from app.publish.policy import (
    ACCOUNT_RESTRICTED_ERROR,
    NEED_MANUAL_LOGIN_ERROR,
    cooldown_remaining_s,
    daily_cap_reached,
    decide_finish,
)

DELAYS = [120, 600, 1800]


# ---------------- decide_finish:成功 ----------------


def test_decide_success():
    """成功 → published,error 清空,不排重试、不递增。"""
    d = decide_finish(True, False, False, "残留错误应被清", 0, DELAYS)
    assert d == {
        "status": "published",
        "error": None,
        "next_retry_delta_s": None,
        "retries_increment": False,
    }


# ---------------- decide_finish:I1 / F3 立即终态 ----------------


def test_decide_need_manual_login_fails_immediately():
    """I1:need_manual_login → 立即 failed,不排重试、不递增(即便还有重试额度)。"""
    d = decide_finish(False, True, False, "创作中心未登录", 0, DELAYS)
    assert d["status"] == "failed"
    assert d["error"] == "创作中心未登录"
    assert d["next_retry_delta_s"] is None
    assert d["retries_increment"] is False


def test_decide_need_manual_login_default_error():
    """I1:error 为空时用缺省文案(与 scheduler.finish 逐字一致)。"""
    d = decide_finish(False, True, False, None, 0, DELAYS)
    assert d["error"] == NEED_MANUAL_LOGIN_ERROR


def test_decide_account_restricted_fails_immediately():
    """F3:account_restricted → 立即 failed,不排重试、不递增。"""
    d = decide_finish(False, False, True, "违规禁发", 1, DELAYS)
    assert d["status"] == "failed"
    assert d["error"] == "违规禁发"
    assert d["next_retry_delta_s"] is None
    assert d["retries_increment"] is False


def test_decide_account_restricted_default_error():
    """F3:error 为空时用缺省文案(与 scheduler.finish 逐字一致)。"""
    d = decide_finish(False, False, True, "", 0, DELAYS)
    assert d["error"] == ACCOUNT_RESTRICTED_ERROR


def test_decide_manual_login_takes_precedence_over_restricted():
    """两信号并存时 need_manual_login 先判(与 scheduler.finish 的 elif 次序一致)。"""
    d = decide_finish(False, True, True, None, 0, DELAYS)
    assert d["error"] == NEED_MANUAL_LOGIN_ERROR


# ---------------- decide_finish:普通失败重试 ----------------


def test_decide_failure_schedules_retry_with_jitter_bounds():
    """有额度:按 retry_delays[retries] 排期,抖动落在 0.8~1.5 倍区间,递增 True。"""
    for retries in range(len(DELAYS)):
        d = decide_finish(False, False, False, f"boom{retries}", retries, DELAYS)
        assert d["status"] == "pending"
        assert d["error"] == f"boom{retries}"
        assert d["retries_increment"] is True
        base = DELAYS[retries]
        assert base * 0.8 <= d["next_retry_delta_s"] <= base * 1.5


def test_decide_failure_deterministic_jitter():
    """注入确定 jitter:delta 恰为 retry_delays[retries] × 抖动系数。"""
    d = decide_finish(
        False, False, False, "boom", 1, DELAYS, jitter=lambda a, b: 1.0
    )
    assert d["next_retry_delta_s"] == DELAYS[1] * 1.0


def test_decide_failure_exhausted_to_failed():
    """重试耗尽(retries == len(delays))→ 终态 failed,不递增。"""
    d = decide_finish(False, False, False, "final", len(DELAYS), DELAYS)
    assert d == {
        "status": "failed",
        "error": "final",
        "next_retry_delta_s": None,
        "retries_increment": False,
    }


def test_decide_failure_empty_delays_fails_directly():
    """retry_delays 为空:首次失败即 failed(无额度可排)。"""
    d = decide_finish(False, False, False, "boom", 0, [])
    assert d["status"] == "failed"
    assert d["retries_increment"] is False


# ---------------- cooldown_remaining_s ----------------


def test_cooldown_no_history_passes():
    """无历史发布(None)→ 剩余 0,视作冷却已满足。"""
    assert cooldown_remaining_s(None, datetime.utcnow(), 1800) == 0.0


def test_cooldown_elapsed_over_interval_passes():
    """距上次发布已超间隔 → 剩余 <= 0(可发)。"""
    now = datetime.utcnow()
    last = now - timedelta(seconds=2000)
    assert cooldown_remaining_s(last, now, 1800) <= 0


def test_cooldown_elapsed_within_interval_blocks():
    """距上次发布不足间隔 → 剩余 = 间隔 - 已过(> 0,不可发)。"""
    now = datetime.utcnow()
    last = now - timedelta(seconds=600)
    remaining = cooldown_remaining_s(last, now, 1800)
    assert abs(remaining - 1200) < 1


# ---------------- daily_cap_reached ----------------


def test_daily_cap_boundaries():
    """未达上限 False;达到/超过上限 True。"""
    assert daily_cap_reached(0, 8) is False
    assert daily_cap_reached(7, 8) is False
    assert daily_cap_reached(8, 8) is True
    assert daily_cap_reached(9, 8) is True
