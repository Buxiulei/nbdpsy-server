"""发布后评论互动单测(不起真浏览器),锁《评论互动话术池-v5》的规则与合规红线。

- 话术池合规:矩阵号池子**零引流指向**(v5 第一节的红线,v4 那批已删)、全池无联系方式;
- 分配规则(v5 第六节):所属账号那条最先发、矩阵号只从本号定位池取不串池、同篇内各号
  话术不重复(品牌综合三个号共用一池)、跨笔记按历史用量轮换不重复(**不随机抽**);
- 咨询师姓名三级降级:``related_counselor`` 字段 → 标题解析 → 通用版;
- 排期:评论之间有随机间隔,不是六个号同时涌进评论区;
- 登记纪律:幂等、无 user_id 不猜、库坏了也不抛错阻断发布终态;
- ``note_comment_task`` 非幂等,不得进 ``_IDEMPOTENT_KINDS``(重复执行会再发一条评论)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import sqlite3
from datetime import datetime

import pytest

from app.services import browser_jobs_repo as repo
from app.services import comment_phrases as phrases
from app.services import note_comment_task as svc


# ---------------- 话术池合规红线(v5 第七节) ----------------


def _all_matrix_phrases() -> list[str]:
    return [p for pool in phrases.MATRIX_POOLS.values() for p in pool]


def _all_phrases() -> list[str]:
    return [*_all_matrix_phrases(), *phrases.OWNER_WITH_NAME, *phrases.OWNER_NO_NAME]


def test_matrix_pools_have_zero_traffic_diversion():
    """矩阵号池子里不得出现任何把读者往自己号带的表述(v5 第一节,v4 的错)。

    每个账号背后是不同的运营、各自算 KPI,矩阵号在同事的笔记底下引流 = 抢单。
    """
    banned = (
        "我们那边", "我们这边", "去看看", "看看我们", "主页", "关注",
        "对照着看", "写过", "更多内容", "详见", "点我", "戳我", "置顶",
    )
    for phrase in _all_matrix_phrases():
        for word in banned:
            assert word not in phrase, f"矩阵号话术出现引流指向「{word}」:{phrase}"


def test_only_owner_pool_carries_conversion_guidance():
    """转化引导(预约/私信)只能出现在笔记所属账号的池子里,矩阵号一句都不许有。"""
    for phrase in _all_matrix_phrases():
        assert "私信" not in phrase and "预约" not in phrase, phrase
    for phrase in (*phrases.OWNER_WITH_NAME, *phrases.OWNER_NO_NAME):
        assert "私信留言" in phrase


def test_no_contact_info_and_no_medical_claims_anywhere():
    """全池不得出现联系方式与医疗功效表述(v5 第七节 1、2)。"""
    banned = (
        "微信", "QQ", "邮箱", "二维码", "http", "www.", "@", "电话", "手机号",
        "治疗", "治愈", "痊愈", "疗效", "确诊", "诊断", "抑郁症", "焦虑症", "病",
    )
    for phrase in _all_phrases():
        for word in banned:
            assert word not in phrase, f"话术触碰合规红线「{word}」:{phrase}"


def test_phrases_are_unique_across_all_pools():
    """池子之间不许有重复句子,否则「不串池」的判定会失去意义。"""
    all_phrases = _all_phrases()
    assert len(all_phrases) == len(set(all_phrases))


# ---------------- 定位判定 ----------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("NBDpsy-聊创伤", phrases.POS_TRAUMA),
        ("NBDpsy-亲密关系", phrases.POS_RELATIONSHIP),
        ("NBDpsy-好好生活", phrases.POS_DAILY),
        ("NBDpsy聊心理", phrases.POS_POPSCI),
        ("NBDpsy", phrases.POS_BRAND),
        ("NBDpsy-我们都有病", phrases.POS_BRAND),
        ("NBDpsy-夕夕", phrases.POS_BRAND),
        ("某个没见过的号", phrases.POS_BRAND),
    ],
)
def test_position_of_account_name(name, expected):
    """账号名字就是它的定位;认不出一律归品牌综合(最泛、最安全)。"""
    assert phrases.position_of(name) == expected


def test_position_falls_back_to_nickname():
    """展示名被改过时用平台昵称兜。"""
    assert phrases.position_of("三号机", "NBDpsy-聊创伤") == phrases.POS_TRAUMA


# ---------------- 分配规则(v5 第六节) ----------------


_OWNER = {"account_id": 1, "name": "NBDpsy聊心理"}
_MATRIX = [
    {"account_id": 2, "name": "NBDpsy-聊创伤"},
    {"account_id": 3, "name": "NBDpsy-亲密关系"},
    {"account_id": 4, "name": "NBDpsy-好好生活"},
    {"account_id": 5, "name": "NBDpsy-我们都有病"},
    {"account_id": 6, "name": "NBDpsy-夕夕"},
]


def test_owner_comes_first_and_is_the_only_conversion_guidance():
    """所属账号那条排第一(占第一条评论位),且只有它是预约引导。"""
    assigned = phrases.assign_phrases(_OWNER, _MATRIX, "李宇")

    assert assigned[0]["account_id"] == 1
    assert assigned[0]["position"] == "owner"
    assert assigned[0]["text"] == "想要预约 李宇 可以私信留言"
    for item in assigned[1:]:
        assert "私信" not in item["text"]


def test_matrix_accounts_only_draw_from_their_own_pool():
    """矩阵号只能从本号定位的池子里取,串池就露馅。"""
    assigned = {
        item["account_id"]: item for item in phrases.assign_phrases(_OWNER, _MATRIX, None)
    }

    assert assigned[2]["template"] in phrases.MATRIX_POOLS[phrases.POS_TRAUMA]
    assert assigned[3]["template"] in phrases.MATRIX_POOLS[phrases.POS_RELATIONSHIP]
    assert assigned[4]["template"] in phrases.MATRIX_POOLS[phrases.POS_DAILY]
    assert assigned[5]["template"] in phrases.MATRIX_POOLS[phrases.POS_BRAND]
    assert assigned[6]["template"] in phrases.MATRIX_POOLS[phrases.POS_BRAND]


def test_no_duplicate_phrase_within_one_note():
    """同一篇底下各号话术互不重复 —— 品牌综合三个号共用一池,是这里的真考点。"""
    matrix = [
        {"account_id": 5, "name": "NBDpsy-我们都有病"},
        {"account_id": 6, "name": "NBDpsy-夕夕"},
        {"account_id": 7, "name": "NBDpsy"},
    ]
    texts = [item["text"] for item in phrases.assign_phrases(_OWNER, matrix, None)]

    assert len(texts) == 4
    assert len(set(texts)) == 4


def test_no_duplicate_phrase_across_notes():
    """跨笔记不重复:同一个号连发几篇,每篇取本池里没用过的下一句(不随机抽)。"""
    history: dict[int, dict[str, int]] = {}
    seen: list[str] = []
    for _ in range(len(phrases.MATRIX_POOLS[phrases.POS_TRAUMA])):
        item = phrases.assign_phrases(_OWNER, _MATRIX[:1], None, history)[1]
        seen.append(item["template"])
        counts = history.setdefault(item["account_id"], {})
        counts[item["template"]] = counts.get(item["template"], 0) + 1

    assert seen == list(phrases.MATRIX_POOLS[phrases.POS_TRAUMA])  # 按池序轮换一整轮
    assert len(set(seen)) == len(seen)


def test_rotation_wraps_after_pool_exhausted():
    """一轮用完从头再来(用得最少的优先),而不是分不出来直接不发。"""
    pool = phrases.MATRIX_POOLS[phrases.POS_TRAUMA]
    history = {2: {p: 1 for p in pool}}

    item = phrases.assign_phrases(_OWNER, _MATRIX[:1], None, history)[1]

    assert item["template"] == pool[0]


def test_account_is_skipped_when_its_pool_is_used_up_in_this_note():
    """本篇候选被同池的号占光 → 该号这篇不发,宁可少一条也不发重复的。"""
    pool_size = len(phrases.MATRIX_POOLS[phrases.POS_BRAND])
    matrix = [
        {"account_id": 10 + i, "name": "NBDpsy"} for i in range(pool_size + 2)
    ]

    assigned = phrases.assign_phrases(_OWNER, matrix, None)

    assert len(assigned) == pool_size + 1  # 所属账号 1 条 + 品牌池刚好发完


def test_owner_pool_switches_on_counselor_name():
    """有咨询师姓名走带姓名版,没有走通用版(通用版里没有占位符)。"""
    with_name = phrases.assign_phrases(_OWNER, [], "王芳")[0]
    without = phrases.assign_phrases(_OWNER, [], None)[0]

    assert with_name["template"] in phrases.OWNER_WITH_NAME
    assert "王芳" in with_name["text"] and phrases.NAME_PLACEHOLDER not in with_name["text"]
    assert without["template"] in phrases.OWNER_NO_NAME
    assert phrases.NAME_PLACEHOLDER not in without["text"]


# ---------------- 咨询师姓名三级降级 ----------------


def test_counselor_name_prefers_related_counselor_field():
    assert svc._resolve_counselor("李宇", "心理咨询师-黄安麟，陪你读懂依恋") == "李宇"


def test_counselor_name_falls_back_to_title_parsing():
    """字段没给 → 标题解析;正则容错「粤语咨询师-」这类前缀变体。"""
    assert svc._resolve_counselor(None, "粤语咨询师-黄安麟，陪你读懂依恋模式") == "黄安麟"
    assert svc._resolve_counselor("  ", "心理咨询师-李宇，边界感是练出来的") == "李宇"


def test_counselor_name_falls_back_to_generic_version():
    """两级都拿不到 → None,用不带姓名的通用版(绝不猜个假名字)。"""
    assert svc._resolve_counselor(None, "边界感是练出来的") is None


# ---------------- 排期(评论之间要有随机间隔) ----------------


def test_comment_times_are_spread_with_random_gaps():
    """所属账号最早、矩阵号在其后散开,两两至少隔 MIN_GAP_SECONDS。"""
    now = datetime(2026, 8, 1, 10, 0, 0)

    times = svc._comment_times(now, 6)

    assert len(times) == 6
    assert times == sorted(times)
    owner_delay = (times[0] - now).total_seconds()
    assert svc.OWNER_DELAY_MIN <= owner_delay <= svc.OWNER_DELAY_MAX
    # 所属账号那条与第一个矩阵号之间留足身位,让它稳稳占住第一条评论位
    assert (times[1] - times[0]).total_seconds() >= svc.MATRIX_LEAD_SECONDS
    gaps = [(b - a).total_seconds() for a, b in zip(times[1:], times[2:])]
    assert all(gap >= svc.MIN_GAP_SECONDS for gap in gaps)
    assert len(set(gaps)) > 1  # 随机间隔,不是等距节律


def test_comment_times_single_entry():
    """只有所属账号一条(没有有效矩阵号)时不排多余时刻。"""
    assert len(svc._comment_times(datetime(2026, 8, 1), 1)) == 1


# ---------------- 登记钩子(schedule_note_comments) ----------------


@pytest.fixture
def comment_db(tmp_path):
    """建一个带全部表的临时 sqlite 文件库,返回路径(sync 侧直连用)。"""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401  触发模型注册
    from app.core.db import Base

    db_path = str(tmp_path / "comment.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _add_account(db_path: str, account_id: int, name: str, cookie_status: str,
                 user_id: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO xhs_accounts (id, name, user_id, status, cookie_status, created_at)"
            " VALUES (?, ?, ?, 'unknown', ?, ?)",
            (account_id, name, user_id, cookie_status,
             datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _add_published_job(db_path: str, job_id: int, account_id: int, title: str,
                       note_id: str | None = None,
                       related_counselor: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, note_id, related_counselor, created_at)"
            " VALUES (?, ?, ?, '正文', '[]', '[]', 'published', 0, ?, ?, ?)",
            (job_id, account_id, title, note_id, related_counselor,
             datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _read_jobs(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM browser_jobs WHERE kind='note_comment_task'"
            " ORDER BY created_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def _payloads(db_path: str) -> list[dict]:
    return [repo.get_job_sync(db_path, row["id"])["payload"] for row in _read_jobs(db_path)]


def _seed_matrix(db_path: str) -> None:
    _add_account(db_path, 1, "NBDpsy聊心理", "valid", user_id="pub-uid")
    _add_account(db_path, 2, "NBDpsy-聊创伤", "valid")
    _add_account(db_path, 3, "NBDpsy-亲密关系", "valid")
    _add_account(db_path, 4, "NBDpsy-好好生活", "invalid")  # 失效号不派
    _add_account(db_path, 5, "NBDpsy-夕夕", "unknown")      # 未检号不派


def test_schedule_registers_owner_and_valid_matrix_accounts(comment_db):
    """所属账号 + 全部 cookie 有效的矩阵号各一条;失效/未检号不派。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 77, 1, "边界感是练出来的")

    job_ids = svc.schedule_note_comments(comment_db, 77)

    rows = _read_jobs(comment_db)
    assert len(job_ids) == 3
    assert sorted(r["account_id"] for r in rows) == [1, 2, 3]
    assert all(r["status"] == "queued" and r["operator_id"] == 0 for r in rows)


def test_schedule_payload_carries_locator_text_and_schedule(comment_db):
    """payload 带主页定位(note_id 优先)、文案、模板原文与排期时刻。"""
    _seed_matrix(comment_db)
    _add_published_job(
        comment_db, 88, 1, "焦虑发作时的五个自救动作",
        note_id="66aabbcc", related_counselor="李宇",
    )

    svc.schedule_note_comments(comment_db, 88)

    payloads = _payloads(comment_db)
    owner = next(p for p in payloads if p["position"] == "owner")
    assert owner["text"] == "想要预约 李宇 可以私信留言"
    assert owner["template"] in phrases.OWNER_WITH_NAME  # 模板原文另存,供跨笔记去重
    for payload in payloads:
        assert payload["publisher_user_id"] == "pub-uid"
        assert payload["title"] == "焦虑发作时的五个自救动作"
        assert payload["note_id"] == "66aabbcc"  # 定位优先 note_id
        assert payload["source_publish_job_id"] == 88
        datetime.fromisoformat(payload["not_before"])  # 排期可解析


def test_schedule_owner_comment_is_scheduled_first(comment_db):
    """所属账号那条排期最早 —— 它要占住第一条评论位。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 89, 1, "拖延的三个成因")

    svc.schedule_note_comments(comment_db, 89)

    payloads = _payloads(comment_db)
    owner = next(p for p in payloads if p["position"] == "owner")
    others = [p for p in payloads if p["position"] != "owner"]
    assert all(owner["not_before"] < p["not_before"] for p in others)


def test_schedule_does_not_repeat_phrases_across_notes(comment_db):
    """连发两篇,同一个号两次拿到的话术不同(历史用量从台账现查)。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 90, 1, "第一篇")
    _add_published_job(comment_db, 91, 1, "第二篇")

    svc.schedule_note_comments(comment_db, 90)
    svc.schedule_note_comments(comment_db, 91)

    per_account: dict[int, list[str]] = {}
    for row, payload in zip(_read_jobs(comment_db), _payloads(comment_db)):
        per_account.setdefault(row["account_id"], []).append(payload["template"])
    assert len(per_account) == 3
    for account_id, templates in per_account.items():
        assert len(templates) == 2, account_id
        assert templates[0] != templates[1], account_id


def test_schedule_falls_back_to_generic_owner_phrase(comment_db):
    """既没有 related_counselor 也解析不出姓名 → 通用版预约引导。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 92, 1, "边界感是练出来的")

    svc.schedule_note_comments(comment_db, 92)

    owner = next(p for p in _payloads(comment_db) if p["position"] == "owner")
    assert owner["template"] in phrases.OWNER_NO_NAME
    assert phrases.NAME_PLACEHOLDER not in owner["text"]


def test_schedule_takes_counselor_from_title(comment_db):
    """字段没给但标题是推介形态 → 用标题里解析出的姓名。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 93, 1, "粤语咨询师-黄安麟，陪你读懂依恋模式")

    svc.schedule_note_comments(comment_db, 93)

    owner = next(p for p in _payloads(comment_db) if p["position"] == "owner")
    assert "黄安麟" in owner["text"]


def test_schedule_is_idempotent_per_publish_job(comment_db):
    """同一发布重复调不重复登记(钩子幂等)。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 94, 1, "拖延的三个成因")

    assert len(svc.schedule_note_comments(comment_db, 94)) == 3
    assert svc.schedule_note_comments(comment_db, 94) == []
    assert len(_read_jobs(comment_db)) == 3


def test_schedule_skips_when_publisher_has_no_user_id(comment_db):
    """发布者没有 user_id → 主页路径无从走起,直接放弃(不猜、不登记)。"""
    _add_account(comment_db, 1, "NBDpsy聊心理", "valid", user_id=None)
    _add_account(comment_db, 2, "NBDpsy-聊创伤", "valid")
    _add_published_job(comment_db, 95, 1, "标题在这里")

    assert svc.schedule_note_comments(comment_db, 95) == []
    assert _read_jobs(comment_db) == []


def test_schedule_registers_owner_even_without_valid_matrix(comment_db):
    """没有可用矩阵号也要发所属账号那条 —— 转化引导只能由它发出。"""
    _add_account(comment_db, 1, "NBDpsy聊心理", "unknown", user_id="pub-uid")
    _add_published_job(comment_db, 96, 1, "标题在这里")

    assert len(svc.schedule_note_comments(comment_db, 96)) == 1
    assert _read_jobs(comment_db)[0]["account_id"] == 1


def test_schedule_never_raises_on_broken_db():
    """登记绝不抛错阻断发布终态:库路径都坏了也只返回空表。"""
    assert svc.schedule_note_comments("/nonexistent/dir/nope.db", 1) == []


def test_schedule_skips_missing_or_untitled_job(comment_db):
    """publish job 不存在 / 没标题 → 不登记(标题是主页定位的兜底判据)。"""
    _seed_matrix(comment_db)
    _add_published_job(comment_db, 97, 1, "   ")

    assert svc.schedule_note_comments(comment_db, 404) == []
    assert svc.schedule_note_comments(comment_db, 97) == []
    assert _read_jobs(comment_db) == []


# ---------------- 台账纪律 ----------------


def test_note_comment_task_is_not_idempotent():
    """重复执行会再发一条一模一样的评论,绝不能进 _IDEMPOTENT_KINDS 自动重跑。"""
    assert "note_comment_task" not in repo._IDEMPOTENT_KINDS
    assert "note_comment" not in repo._IDEMPOTENT_KINDS


def test_worker_dispatches_note_comment_task_to_note_comment_execute():
    """kind 映射复用已真号验证过的 note_comment.execute,不另起一套浏览器动作。"""
    import asyncio

    from app import account_worker
    from app.services import note_comment

    seen: dict = {}

    async def _fake_execute(account_id, payload):
        seen["args"] = (account_id, payload)
        return {"commented": True}

    original = note_comment.execute
    note_comment.execute = _fake_execute
    try:
        run = account_worker._resolve_execute("note_comment_task")
        assert asyncio.run(run(7, {"text": "x"})) == {"commented": True}
    finally:
        note_comment.execute = original
    assert seen["args"] == (7, {"text": "x"})
