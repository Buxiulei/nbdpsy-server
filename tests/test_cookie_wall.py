"""风控验证墙检测单测(不起真浏览器)。

背景缺陷(2026-07-31 真实事故):账号 NBDpsy-聊创伤 被小红书挂扫码验证墙,一访问**他人
主页**就被重定向到 ``website-login/captcha``,但 ``cookie_status`` 仍是 ``valid`` ——
登录检测只看首页有没有"我"导航栏,而墙是访问他人主页才弹的。系统据此继续给它派活,
任务全败;且 ``browser_jobs`` 全文检索「验证/风控/captcha」零命中,库里查不到任何证据。

覆盖:
- 撞墙 URL 判定 + 两种文案分型(扫码验证身份 / 请求太频繁),正常他人主页不误判;
- SyncClient.check_login 撞墙 → restricted 且带取证;探测目标取不到/是本号 → 跳过不报错;
- 探测异常不改判定(不把好号误标风控);
- cookie_check.execute 全链:写回 restricted + 风控事件落库内容正确;
- 探测目标选取:挑另一个账号的 user_id,库里没有可选目标 → None 不报错;
- 被风控的号不被选进矩阵互动、不进后台补采调度。
"""

import sqlite3
from datetime import datetime

import pytest
from sqlalchemy import create_engine

import app.core.db as db_module
from app.browser import login_detector
from app.browser.sync_client import SyncClient
from app.models.risk_event import RiskEvent
from app.models.xhs_account import XhsAccount
from app.services import cookie_check, matrix_interact as matrix_svc, risk_events

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

# 实测文案(2026-07-31):同一个 captcha URL,反复起会话后从扫码墙退化成限流墙
_WALL_URL = (
    "https://www.xiaohongshu.com/website-login/captcha"
    "?redirectPath=https%3A%2F%2Fwww.xiaohongshu.com%2Fuser%2Fprofile%2Fpeer"
    "&verifyUuid=x&verifyType=124&verifyBiz=461"
)
_TEXT_SCAN_QR = "安全验证 为保护账号安全,请使用已登录该账号的「小红书APP」扫码验证身份"
_TEXT_RATE_LIMIT = "安全验证 请求太频繁,请稍后再试"
_PEER_URL = "https://www.xiaohongshu.com/user/profile/peer-uid"


# ---------------- 纯判定:撞墙 URL + 文案分型 ----------------


def test_wall_url_detected_for_captcha_redirect():
    """被重定向到 captcha / website-login 即判定撞墙(URL 是硬判据)。"""
    assert login_detector.is_wall_url(_WALL_URL) is True
    assert login_detector.is_wall_url("https://www.xiaohongshu.com/website-login") is True


def test_normal_peer_profile_url_is_not_wall():
    """正常他人主页 URL 不误判成墙。"""
    assert login_detector.is_wall_url(_PEER_URL) is False
    assert login_detector.is_wall_url("https://www.xiaohongshu.com/explore") is False
    assert login_detector.is_wall_url(None) is False


def test_classify_scan_qr_wall_text():
    """「扫码验证身份」文案 → scan_qr(运营手机扫码即可恢复)。"""
    assert login_detector.classify_wall_text(_TEXT_SCAN_QR) == login_detector.WALL_SCAN_QR


def test_classify_rate_limit_wall_text():
    """「请求太频繁,请稍后再试」文案 → rate_limit(已限流,别再起会话)。"""
    assert (
        login_detector.classify_wall_text(_TEXT_RATE_LIMIT)
        == login_detector.WALL_RATE_LIMIT
    )


def test_classify_unknown_wall_text():
    """撞了墙但文案不认识 → unknown(仍算撞墙,留证等人看)。"""
    assert login_detector.classify_wall_text("某种没见过的拦截页") == (
        login_detector.WALL_UNKNOWN
    )
    assert login_detector.classify_wall_text("") == login_detector.WALL_UNKNOWN


# ---------------- SyncClient 探测(假 page,不起浏览器)----------------


class _FakePage:
    """只实现探测用到的 goto / url / evaluate 的假 page。

    ``goto`` 记录导航次数,用来锁死"只加一次导航"这条硬约束。
    """

    def __init__(self, landed_url: str, page_text: str, raise_on_goto: bool = False):
        self.landed_url = landed_url
        self.page_text = page_text
        self.raise_on_goto = raise_on_goto
        self.goto_calls: list[str] = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if self.raise_on_goto:
            raise RuntimeError("导航超时")

    @property
    def url(self):
        return self.landed_url

    def evaluate(self, js):
        return self.page_text


def _client_with_page(page) -> SyncClient:
    client = SyncClient(1, _COOKIES)
    client.page = page
    return client


def _stub_valid_login(monkeypatch, user_info=None):
    """把 check_login 的前置判定全部钉成"已登录",只留探测那一步真跑。"""
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: False)
    monkeypatch.setattr(SyncClient, "_api_login_status", lambda self: True)
    monkeypatch.setattr(
        SyncClient, "_detect_login", lambda self: {"is_logged_in": True, "profile_url": "own"}
    )
    monkeypatch.setattr(
        SyncClient,
        "_get_user_info",
        lambda self, url: user_info if user_info is not None else {"nickname": "本号", "user_id": "own-uid"},
    )


def test_check_login_probe_hits_scan_qr_wall_is_restricted(monkeypatch):
    """他人主页被重定向到验证墙 → restricted(不是 valid),带 scan_qr 取证。"""
    _stub_valid_login(monkeypatch)
    page = _FakePage(_WALL_URL, _TEXT_SCAN_QR)
    result = _client_with_page(page).check_login("peer-uid")

    assert result["status"] == "restricted"
    wall = result["wall"]
    assert wall["wall_type"] == login_detector.WALL_SCAN_QR
    assert wall["landed_url"] == _WALL_URL
    assert wall["target_url"] == "https://www.xiaohongshu.com/user/profile/peer-uid"
    assert "扫码验证身份" in wall["page_text"]
    # 只加一次导航:反复起会话/多加请求本身就会把号打成限流
    assert page.goto_calls == ["https://www.xiaohongshu.com/user/profile/peer-uid"]


def test_check_login_probe_hits_rate_limit_wall_is_restricted(monkeypatch):
    """同一 captcha URL 的「请求太频繁」文案 → 同样 restricted,但分型 rate_limit。"""
    _stub_valid_login(monkeypatch)
    result = _client_with_page(_FakePage(_WALL_URL, _TEXT_RATE_LIMIT)).check_login("peer-uid")

    assert result["status"] == "restricted"
    assert result["wall"]["wall_type"] == login_detector.WALL_RATE_LIMIT


def test_check_login_normal_peer_profile_stays_valid(monkeypatch):
    """他人主页正常打开 → 仍是 valid,不因为多探一步就误标风控。"""
    _stub_valid_login(monkeypatch)
    result = _client_with_page(_FakePage(_PEER_URL, "某某的主页 笔记 收藏")).check_login("peer-uid")

    assert result["status"] == "valid"
    assert "wall" not in result


def test_check_login_without_probe_target_skips_probe(monkeypatch):
    """探测目标取不到(None)→ 跳过探测、不报错,退化为原来的首页判定。"""
    _stub_valid_login(monkeypatch)
    page = _FakePage(_WALL_URL, _TEXT_SCAN_QR)  # 真去探就会撞墙,证明确实没探
    result = _client_with_page(page).check_login(None)

    assert result["status"] == "valid"
    assert page.goto_calls == []


def test_check_login_skips_probe_when_target_is_self(monkeypatch):
    """探测目标就是本号 → 跳过(自己主页不弹墙,白花一次导航)。"""
    _stub_valid_login(monkeypatch, user_info={"nickname": "本号", "user_id": "own-uid"})
    page = _FakePage(_WALL_URL, _TEXT_SCAN_QR)
    result = _client_with_page(page).check_login("own-uid")

    assert result["status"] == "valid"
    assert page.goto_calls == []


def test_probe_navigation_failure_does_not_flag_restricted(monkeypatch):
    """探测本身失败(导航异常)→ 保持原判定,绝不把好号误标风控。"""
    _stub_valid_login(monkeypatch)
    result = _client_with_page(
        _FakePage(_WALL_URL, _TEXT_SCAN_QR, raise_on_goto=True)
    ).check_login("peer-uid")

    assert result["status"] == "valid"


def test_captcha_branch_carries_wall_evidence(monkeypatch):
    """首页就撞验证码/墙的老 captcha 分支也带取证(同样要留痕)。"""
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: True)
    result = _client_with_page(_FakePage(_WALL_URL, _TEXT_SCAN_QR)).check_login("peer-uid")

    assert result["status"] == "captcha"
    assert result["wall"]["wall_type"] == login_detector.WALL_SCAN_QR
    assert result["wall"]["landed_url"] == _WALL_URL


# ---------------- 探测目标选取 ----------------


async def test_pick_probe_user_id_picks_another_account(db_factory):
    """挑矩阵内**另一个**账号的 user_id,不会挑到本号。"""
    async with db_factory() as session:
        session.add(XhsAccount(id=1, name="本号", user_id="own-uid", cookie_status="valid"))
        session.add(XhsAccount(id=2, name="友号", user_id="peer-uid", cookie_status="valid"))
        await session.commit()

    assert await cookie_check.pick_probe_user_id(db_factory, 1) == "peer-uid"
    assert await cookie_check.pick_probe_user_id(db_factory, 2) == "own-uid"


async def test_pick_probe_user_id_none_when_no_peer(db_factory):
    """库里只有本号 / 别的号没回填 user_id → None,不报错(调用方跳过探测)。"""
    async with db_factory() as session:
        session.add(XhsAccount(id=1, name="本号", user_id="own-uid", cookie_status="valid"))
        session.add(XhsAccount(id=2, name="没资料的号", user_id=None, cookie_status="unknown"))
        session.add(XhsAccount(id=3, name="空串号", user_id="", cookie_status="unknown"))
        await session.commit()

    assert await cookie_check.pick_probe_user_id(db_factory, 1) is None


async def test_pick_probe_user_id_swallows_db_error():
    """查库出错也按"取不到"处理:返回 None 且不上抛,不能因此把整次检测搞失败。"""

    def broken_factory():
        raise RuntimeError("库连不上")

    assert await cookie_check.pick_probe_user_id(broken_factory, 1) is None


# ---------------- execute 全链:写回 restricted + 事件落库 ----------------


async def test_execute_writes_restricted_and_records_risk_event(db_factory, monkeypatch):
    """撞墙 → cookie_status 写回 restricted,风控事件按账号/时间/墙型/访问目标落库。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    async with db_factory() as session:
        session.add(XhsAccount(id=11, name="挂墙号", user_id="own-uid", cookie_status="valid"))
        session.add(XhsAccount(id=12, name="友号", user_id="peer-uid", cookie_status="valid"))
        await session.commit()

    wall = {
        "wall_type": login_detector.WALL_SCAN_QR,
        "target_url": _PEER_URL,
        "landed_url": _WALL_URL,
        "page_text": _TEXT_SCAN_QR,
    }
    seen = {}

    def fake_check_login_once(account_id, cookies, probe_user_id=None):
        seen["probe_user_id"] = probe_user_id
        return {"status": "restricted", "user_info": None, "wall": wall}

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check_login_once)

    async def fake_load(account_id):
        return _COOKIES

    monkeypatch.setattr(cookie_check, "load_account_cookies", fake_load)

    result = await cookie_check.execute(11, {})

    assert result["status"] == "restricted"
    assert result["wall"] == wall
    # 探测目标确实是从库里挑的另一个号
    assert seen["probe_user_id"] == "peer-uid"

    async with db_factory() as session:
        account = await session.get(XhsAccount, 11)
        assert account.cookie_status == "restricted"
        assert account.last_check_at is not None
        events = (await session.execute(RiskEvent.__table__.select())).fetchall()

    assert len(events) == 1
    event = events[0]._mapping
    assert event["account_id"] == 11
    assert event["wall_type"] == login_detector.WALL_SCAN_QR
    assert event["source"] == "cookie_check"
    assert event["target_url"] == _PEER_URL
    assert event["landed_url"] == _WALL_URL
    assert "扫码验证身份" in event["page_text"]
    assert event["detected_at"] is not None


async def test_execute_valid_records_no_risk_event(db_factory, monkeypatch):
    """没撞墙就不留痕:valid 结果不产生 risk_events 行(避免噪音淹没真事件)。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    async with db_factory() as session:
        session.add(XhsAccount(id=21, name="好号", user_id="own-uid", cookie_status="valid"))
        session.add(XhsAccount(id=22, name="友号", user_id="peer-uid", cookie_status="valid"))
        await session.commit()

    def fake_check_login_once(account_id, cookies, probe_user_id=None):
        return {"status": "valid", "user_info": {"nickname": "好号", "user_id": "own-uid"}}

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check_login_once)

    async def fake_load(account_id):
        return _COOKIES

    monkeypatch.setattr(cookie_check, "load_account_cookies", fake_load)

    assert (await cookie_check.execute(21, {}))["status"] == "valid"

    async with db_factory() as session:
        assert (await session.get(XhsAccount, 21)).cookie_status == "valid"
        events = (await session.execute(RiskEvent.__table__.select())).fetchall()
    assert events == []


async def test_record_wall_never_raises_on_db_error():
    """留痕失败绝不上抛:证据可以丢,检测主流程不能崩(note_export 同款纪律)。"""

    def broken_factory():
        raise RuntimeError("库连不上")

    ok = await risk_events.record_wall(
        broken_factory, 1, {"wall_type": "scan_qr"}, "cookie_check"
    )
    assert ok is False


# ---------------- 被风控的号不再被派活 ----------------


@pytest.fixture
def matrix_db(tmp_path):
    """矩阵互动用的同步 sqlite 库(schema 与生产一致)。"""
    from app.core.db import Base
    import app.models  # noqa: F401 触发模型注册

    db_path = str(tmp_path / "matrix.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _add_account(db_path, account_id, name, cookie_status, user_id=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO xhs_accounts (id, name, user_id, status, cookie_status, created_at)"
            " VALUES (?, ?, ?, 'unknown', ?, ?)",
            (account_id, name, user_id, cookie_status, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def test_restricted_account_not_selected_for_matrix_interact(matrix_db):
    """被风控的号不进矩阵互动:矩阵按 cookie_status='valid' 选号,restricted 自然出局。

    这条是本次修复的落点——旧状态下挂墙号仍是 valid,会被选进来把互动任务全跑失败。
    """
    with sqlite3.connect(matrix_db) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_at)"
            " VALUES (88, 1, '边界感是练出来的', '正文', '[]', '[]', 'published', 0, ?)",
            (datetime.utcnow().isoformat(sep=" "),),
        )
        conn.commit()
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "好号", "valid", user_id="uid2")
    _add_account(matrix_db, 3, "挂墙号", "restricted", user_id="uid3")

    matrix_svc.schedule_matrix_interact(matrix_db, 88)

    with sqlite3.connect(matrix_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT account_id FROM browser_jobs WHERE kind='matrix_interact'"
        ).fetchall()
    assert [r["account_id"] for r in rows] == [2]


async def test_restricted_account_excluded_from_metrics_scan(db_factory, monkeypatch):
    """被风控的号不进后台补采调度:挂墙时继续起浏览器只会把限流催得更狠。"""
    from app.services.note_metrics_scheduler import NoteMetricsScheduler

    # enqueue 走 browser_jobs_repo 的 get_session(读 db_module.async_session),指到测试库
    monkeypatch.setattr(db_module, "async_session", db_factory)
    async with db_factory() as session:
        session.add(XhsAccount(id=31, name="好号", cookie_status="valid", login_cookies="enc"))
        session.add(
            XhsAccount(id=32, name="挂墙号", cookie_status="restricted", login_cookies="enc")
        )
        await session.commit()

    scheduler = NoteMetricsScheduler(db_factory, interval=0)
    await scheduler.scan_once()

    async with db_factory() as session:
        from app.models.browser_job import BrowserJob

        rows = (await session.execute(BrowserJob.__table__.select())).fetchall()
    assert [r._mapping["account_id"] for r in rows] == [31]
