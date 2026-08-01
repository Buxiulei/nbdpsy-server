"""咨询师推介引用推导单测:这篇笔记推介哪个咨询师 → 自动引用哪篇笔记。

被测规则(判定顺序,括号里是需求文档编号):

    显式 quoted_note_id       → 用它,压过一切
    标题形如「X咨询师-姓名，…」 → 引用「小助手联系方式」那篇          (规则 3)
    传了 related_counselor    → 引用该咨询师的公开推介笔记            (规则 1)
    标题里提到已知咨询师姓名  → 同上                                 (规则 2)
    都不满足                  → None                                (规则 4)

重点锁死的几条:

- **规则 2 与 3 互斥**:一篇标题是推介形态的笔记本身就是推介笔记,只能引用小助手那篇,
  **绝不自引用**;哪怕同时显式传了 related_counselor 也一样(规则 3 先判);
- **标题变体**:「粤语咨询师-黄安麟」(实测的发布笔误)必须照样解析得出;
- **只引用公开笔记**:私密/未知可见性的笔记永不被选为引用目标,小助手笔记也一样;
- **确定性**:同一咨询师在两个号各有一篇时,同输入必同输出(优先异号,再按 id 升序);
- **查不到就 None**:绝不猜;
- 小助手笔记 id 走 config,可被覆盖,**不硬编码在业务逻辑里**。

patch 纪律:打在被测模块的命名空间(``counselor_quote.settings`` 是它顶层 import 进来的)。
"""

import json
from datetime import datetime

import pytest

import app.core.db as db_module
from app.models import PublishJob
from app.models.published_note import PublishedNote
from app.services import counselor_quote as cq
from tests.rest_helpers import bearer, make_operator, rest_client, seed_account
from app.services import operator_service

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

# 「小助手联系方式」那篇的真实 id(运营手工发布,账号1 公开)。测试里当默认值用。
_ASSISTANT = "6a6d5ba1000000002701c2ca"


def _note(account_id, note_id, title, permission_code=0) -> dict:
    """台账候选行(推导函数只认这四个键)。"""
    return {
        "account_id": account_id,
        "note_id": note_id,
        "title": title,
        "permission_code": permission_code,
    }


# 九位咨询师中取三位铺一套双号台账:账号1 与账号6 各有一篇同一个人的推介笔记。
def _candidates() -> list[dict]:
    return [
        _note(1, "n_liyu_a1", "心理咨询师-李宇，陪你看见职场里的自己"),
        _note(6, "n_liyu_a6", "心理咨询师-李宇，陪你看见职场里的自己"),
        _note(1, "n_huang_a1", "粤语咨询师-黄安麟，陪你读懂依恋模式"),  # 实测的笔误标题
        _note(1, "n_liuqiong_a1", "心理咨询师-刘琼，情绪来了怎么办"),
        _note(1, _ASSISTANT, "这里查看引用的笔记哦"),
        _note(1, "n_kepu", "职场倦怠到底怎么修复"),  # 普通科普笔记,不是推介
    ]


def _derive(**kwargs):
    """默认参数齐全的推导调用(只覆盖用例关心的那几个)。"""
    params = dict(
        account_id=1,
        title=None,
        related_counselor=None,
        candidates=_candidates(),
        assistant_note_id=_ASSISTANT,
        self_note_id=None,
    )
    params.update(kwargs)
    return cq.derive_quoted_note_id(**params)


# ---------------- 标题解析(推介笔记的识别) ----------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("心理咨询师-李宇，陪你看见职场里的自己", "李宇"),
        # 实测台账里真有这一篇(疑似发布笔误),正则**不写死「心理咨询师-」**就是为了它
        ("粤语咨询师-黄安麟，陪你读懂依恋模式", "黄安麟"),
        ("咨询师-刘琼，情绪来了怎么办", "刘琼"),  # 没有前缀也认
        ("心理咨询师－彭旱雨，全角破折号", "彭旱雨"),
        ("心理咨询师-于施文", "于施文"),  # 标题就到姓名为止
        ("心理咨询师-李明聪 | 认知行为", "李明聪"),
        ("职场倦怠到底怎么修复", None),  # 普通科普笔记
        ("这里查看引用的笔记哦", None),  # 小助手那篇自己
        ("和李宇一起看依恋模式", None),  # 只是提到姓名,不是推介形态 → 归规则 2
        # 姓名后没有分隔符:宁可解析不出(→ 不引用),也不要把「陪你」吞进姓名
        ("心理咨询师-李宇陪你读懂依恋", None),
    ],
)
def test_parse_counselor_from_title(title, expected):
    """标题是不是推介笔记形态 + 姓名解析的容错(变体前缀 / 全角破折号 / 无分隔符)。"""
    assert cq.parse_counselor_from_title(title) == expected


# ---------------- 规则 3:本篇就是推介笔记 → 引用小助手 ----------------


def test_promo_note_quotes_assistant_not_itself():
    """规则 3:标题是推介形态 → 引用小助手那篇,**不是**自引用。"""
    assert _derive(title="心理咨询师-李宇，陪你看见职场里的自己") == _ASSISTANT


def test_rule3_beats_rule2_even_with_explicit_counselor():
    """**2 与 3 互斥的核心用例**:推介笔记即便显式传了 related_counselor 也走规则 3。

    否则「李宇的推介笔记」会去引用「李宇的推介笔记」——同号那篇就是它自己,异号那篇是
    同一个人的另一版,推介笔记引用推介笔记,荒谬。
    """
    assert _derive(
        title="心理咨询师-李宇，陪你看见职场里的自己", related_counselor="李宇"
    ) == _ASSISTANT


def test_promo_note_variant_title_also_quotes_assistant():
    """笔误变体「粤语咨询师-」同样被认成推介笔记(不能因为前缀不同就漏判成科普笔记)。"""
    assert _derive(title="粤语咨询师-黄安麟，陪你读懂依恋模式") == _ASSISTANT


def test_assistant_note_id_is_configurable():
    """小助手笔记 id 来自参数(生产走 config),换一个就引用另一篇 —— 没写死在逻辑里。"""
    other = "b0b0b0b0000000000000ffff"
    assert _derive(title="心理咨询师-刘琼，情绪来了怎么办", assistant_note_id=other) == other


def test_assistant_note_id_empty_disables_rule3():
    """小助手 id 配空 = 关掉规则 3:推介笔记就不引用任何笔记(留空,不乱引一篇)。"""
    assert _derive(title="心理咨询师-刘琼，x", assistant_note_id="") is None


def test_assistant_note_not_public_is_refused():
    """小助手笔记在台账里明确不是公开 → 不引用(读者点不开的笔记引了也没用)。"""
    candidates = [
        _note(1, _ASSISTANT, "这里查看引用的笔记哦", permission_code=1),  # 仅自己可见
    ]
    assert _derive(title="心理咨询师-刘琼，x", candidates=candidates) is None


def test_assistant_note_absent_from_ledger_is_still_used():
    """小助手笔记还没同步进台账 → 照常引用:这个 id 是运营配的,不是我们猜的。"""
    assert _derive(title="心理咨询师-刘琼，x", candidates=[]) == _ASSISTANT


# ---------------- 规则 1:显式 related_counselor ----------------


def test_related_counselor_picks_that_counselors_promo_note():
    """规则 1:科普笔记 + related_counselor → 引用该咨询师的公开推介笔记。"""
    assert _derive(
        account_id=1, title="职场倦怠到底怎么修复", related_counselor="李宇"
    ) == "n_liyu_a6"  # 优先异号(本篇在账号1)


def test_unknown_counselor_returns_none():
    """台账里没有这位咨询师的公开推介笔记 → None,**绝不猜**(不退而求其次引别人)。"""
    assert _derive(title="职场倦怠到底怎么修复", related_counselor="查无此人") is None


def test_private_promo_note_never_selected():
    """私密笔记不被选为引用目标:该咨询师只有一篇私密推介 → None。"""
    candidates = [_note(1, "n_secret", "心理咨询师-李宇，x", permission_code=1)]
    assert _derive(
        title="职场倦怠", related_counselor="李宇", candidates=candidates
    ) is None


def test_unknown_permission_never_selected():
    """``permission_code`` 为 null 是**未知不是公开**,同样不选。"""
    candidates = [_note(1, "n_unknown", "心理咨询师-李宇，x", permission_code=None)]
    assert _derive(
        title="职场倦怠", related_counselor="李宇", candidates=candidates
    ) is None


# ---------------- 同一咨询师两篇:选择规则确定 ----------------


def test_prefers_promo_note_on_a_different_account():
    """两个号各有一篇 → 优先选**异号**那篇(矩阵内互导)。"""
    assert _derive(account_id=1, related_counselor="李宇") == "n_liyu_a6"
    assert _derive(account_id=6, related_counselor="李宇") == "n_liyu_a1"


def test_falls_back_to_same_account_when_no_other():
    """只有同号那一篇 → 退而用同号的(有总比不引用强)。"""
    candidates = [_note(1, "n_liyu_a1", "心理咨询师-李宇，x")]
    assert _derive(
        account_id=1, related_counselor="李宇", candidates=candidates
    ) == "n_liyu_a1"


def test_selection_is_deterministic_regardless_of_input_order():
    """同输入必同输出:候选行顺序打乱、重复多跑,结果恒定(排序键是 account_id + note_id)。"""
    base = _candidates()
    picks = set()
    for shift in range(len(base)):
        rotated = base[shift:] + base[:shift]
        picks.add(_derive(account_id=1, related_counselor="李宇", candidates=rotated))
    assert picks == {"n_liyu_a6"}


def test_three_accounts_same_counselor_is_still_deterministic():
    """异号有多篇时按 account_id 升序取第一条 —— 不随机,不看时间。"""
    candidates = [
        _note(9, "n_c", "心理咨询师-李宇，x"),
        _note(6, "n_b", "心理咨询师-李宇，x"),
        _note(1, "n_a", "心理咨询师-李宇，x"),
    ]
    assert _derive(account_id=1, related_counselor="李宇", candidates=candidates) == "n_b"


# ---------------- 规则 2:标题提到某位已知咨询师 ----------------


def test_title_mentioning_counselor_quotes_their_promo_note():
    """规则 2:没传 related_counselor,标题提到已知咨询师 → 引用他的推介笔记。"""
    assert _derive(account_id=1, title="和李宇一起看依恋模式") == "n_liyu_a6"


def test_title_mentioning_two_counselors_returns_none():
    """标题同时提到两位咨询师 → None:引哪一位都是我们替运营做主,不猜。"""
    assert _derive(title="李宇和刘琼聊聊情绪") is None


def test_plain_title_without_anything_returns_none():
    """规则 4:什么都不满足 → 不引用。"""
    assert _derive(title="职场倦怠到底怎么修复") is None


def test_empty_related_counselor_is_treated_as_absent():
    """空串 / 纯空格的 related_counselor 视同没传,不去撞一个空姓名。"""
    assert _derive(title="职场倦怠到底怎么修复", related_counselor="   ") is None


# ---------------- 自引用兜底闸 ----------------


def test_self_quote_is_refused():
    """该咨询师只有本篇这一篇推介 → None,绝不引用自己(编辑已发布笔记时才撞得上)。"""
    candidates = [_note(6, "n_liyu_a6", "心理咨询师-李宇，x")]
    assert _derive(
        account_id=6, related_counselor="李宇", candidates=candidates,
        self_note_id="n_liyu_a6",
    ) is None


def test_self_excluded_but_other_account_copy_still_usable():
    """本篇被剔出候选后,异号那篇照常可选(不是一见自引用就整个放弃)。"""
    assert _derive(
        account_id=6, related_counselor="李宇", self_note_id="n_liyu_a6"
    ) == "n_liyu_a1"


def test_editing_the_assistant_note_never_quotes_itself():
    """小助手笔记 id 是配置来的、可能不在候选里 → 由 _guard_self 兜底掐掉自引用。"""
    assert _derive(
        title="心理咨询师-刘琼，x", candidates=[], self_note_id=_ASSISTANT
    ) is None


# ---------------- 台账查询 + 端到端(带 DB) ----------------


async def _seed_ledger(account_id, note_id, title, permission_code=0):
    async with db_module.async_session() as s:
        s.add(
            PublishedNote(
                account_id=account_id, note_id=note_id, title=title,
                permission_code=permission_code, published_at=datetime(2026, 7, 1),
                sync_status="linked",
            )
        )
        await s.commit()


async def _operator_with_access(key, *account_ids):
    op_id = await make_operator(key)
    async with db_module.async_session() as s:
        for acc in account_ids:
            await operator_service.grant_access(s, op_id, acc, op_id)
        await s.commit()
    return op_id


async def test_load_candidates_skips_rows_without_note_id(tmp_path, monkeypatch):
    """候选只收有 note_id 的行:pending_id(还没补上 id)的行进不来。"""
    async with rest_client(tmp_path, monkeypatch):
        acc = await seed_account("号A", "uA", _COOKIES)
        await _seed_ledger(acc, "n_ok", "心理咨询师-李宇，x")
        async with db_module.async_session() as s:
            s.add(
                PublishedNote(
                    account_id=acc, note_id=None, title="待补 id 的",
                    published_at=datetime(2026, 7, 1), sync_status="pending_id",
                )
            )
            await s.commit()

            rows = await cq.load_candidates(s)
        assert [r["note_id"] for r in rows] == ["n_ok"]


# ---------------- POST /api/publish-jobs 的推导落库 ----------------


async def _post_publish(client, acc, key, **extra):
    body = {
        "account_id": acc, "title": "T", "content": "C",
        "images": ["https://cdn/a.png"],
    }
    body.update(extra)
    return await client.post("/api/publish-jobs", json=body, headers=bearer(key))


async def test_publish_job_derives_quoted_note_from_related_counselor(
    tmp_path, monkeypatch
):
    """建 job 时按 related_counselor 推导出 quoted_note_id 并落库,同时留痕 related_counselor。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        other = await seed_account("号B", "uB", _COOKIES)
        key = "op-cq-derive"
        await _operator_with_access(key, acc, other)
        await _seed_ledger(other, "n_liyu_b", "心理咨询师-李宇，陪你看见职场里的自己")

        r = await _post_publish(c, acc, key, title="职场倦怠怎么修复", related_counselor="李宇")

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "n_liyu_b"
            assert job.related_counselor == "李宇"


async def test_publish_job_explicit_quote_beats_related_counselor(tmp_path, monkeypatch):
    """两者都传 → 以显式 quoted_note_id 为准(推导根本不跑)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-explicit"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_liyu_a", "心理咨询师-李宇，x")

        r = await _post_publish(
            c, acc, key, title="职场倦怠", related_counselor="李宇",
            quoted_note_id="n_manual",
        )

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "n_manual"
            assert job.related_counselor == "李宇"  # 留痕仍在


async def test_publish_promo_note_quotes_assistant(tmp_path, monkeypatch):
    """发一篇新的咨询师推介笔记 → 自动引用小助手那篇,而不是同一个人的老推介笔记。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-promo"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_liyu_old", "心理咨询师-李宇，老版")
        await _seed_ledger(acc, _ASSISTANT, "这里查看引用的笔记哦")

        r = await _post_publish(
            c, acc, key, title="心理咨询师-李宇，陪你看见职场里的自己",
            related_counselor="李宇",
        )

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == _ASSISTANT


async def test_publish_job_without_any_hint_leaves_quote_empty(tmp_path, monkeypatch):
    """推不出来就留空:普通科普笔记不传 related_counselor → quoted_note_id 为 None。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-none"
        await _operator_with_access(key, acc)

        r = await _post_publish(c, acc, key, title="职场倦怠怎么修复")

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id is None
            assert job.related_counselor is None


# ---------------- POST /api/accounts/{id}/note-components 的推导 ----------------


async def test_components_derives_quote_from_related_counselor(tmp_path, monkeypatch):
    """编辑已发布笔记:给 related_counselor 就够了,登记的 payload 里带推导出的 note_id。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")  # 只登记台账,不起浏览器
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        other = await seed_account("号B", "uB", _COOKIES)
        key = "op-cq-comp"
        await _operator_with_access(key, acc, other)
        await _seed_ledger(acc, "n_target", "职场倦怠怎么修复")
        await _seed_ledger(other, "n_liyu_b", "心理咨询师-李宇，x")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_target", "related_counselor": "李宇"},
            headers=bearer(key),
        )

        assert r.status_code == 202, r.text
        payload = await _job_payload(r.json()["job_id"])
        assert payload["quoted_note_id"] == "n_liyu_b"
        assert payload["related_counselor"] == "李宇"


async def test_components_promo_note_quotes_assistant_not_itself(tmp_path, monkeypatch):
    """**2/3 互斥的 REST 端用例**:对一篇咨询师推介笔记设组件 → 引用小助手,不自引用。

    标题从台账现查,调用方连 related_counselor 都不用给(存量笔记不必补录)。
    """
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-comp-promo"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_promo", "粤语咨询师-黄安麟，陪你读懂依恋模式")
        await _seed_ledger(acc, _ASSISTANT, "这里查看引用的笔记哦")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_promo", "related_counselor": "黄安麟"},
            headers=bearer(key),
        )

        assert r.status_code == 202, r.text
        payload = await _job_payload(r.json()["job_id"])
        assert payload["quoted_note_id"] == _ASSISTANT


async def test_components_explicit_quote_beats_related_counselor(tmp_path, monkeypatch):
    """两者都传 → 以显式 quoted_note_id 为准。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-comp-explicit"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_target", "职场倦怠怎么修复")
        await _seed_ledger(acc, "n_liyu_a", "心理咨询师-李宇，x")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={
                "note_id": "n_target", "related_counselor": "李宇",
                "quoted_note_id": "n_manual",
            },
            headers=bearer(key),
        )

        assert r.status_code == 202, r.text
        assert (await _job_payload(r.json()["job_id"]))["quoted_note_id"] == "n_manual"


async def test_components_undeducible_counselor_alone_is_422(tmp_path, monkeypatch):
    """只给 related_counselor 却推不出任何公开推介笔记 → 422,不建注定空改的任务。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-comp-422"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_target", "职场倦怠怎么修复")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_target", "related_counselor": "查无此人"},
            headers=bearer(key),
        )

        assert r.status_code == 422, r.text


async def test_components_still_422_when_nothing_given(tmp_path, monkeypatch):
    """四个字段一个都不给,仍然 422(原有校验不因新增字段被放松)。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-comp-empty"
        await _operator_with_access(key, acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_target"},
            headers=bearer(key),
        )

        assert r.status_code == 422, r.text


async def _job_payload(job_id: str) -> dict:
    """读 browser_jobs 台账里登记的 payload。"""
    from sqlalchemy import select

    from app.models.browser_job import BrowserJob

    async with db_module.async_session() as s:
        row = await s.scalar(select(BrowserJob).where(BrowserJob.id == job_id))
        return json.loads(row.payload)


# ---------------- 台账 REST 下发 related_counselor ----------------


async def test_published_notes_rest_exposes_related_counselor(tmp_path, monkeypatch):
    """台账 REST 把 related_counselor 下发出去(列表 + 单条),并在 meta 里给口径说明。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-ledger-rest"
        await _operator_with_access(key, acc)
        async with db_module.async_session() as s:
            s.add(
                PublishedNote(
                    account_id=acc, note_id="n1", title="职场倦怠",
                    related_counselor="李宇", published_at=datetime(2026, 7, 1),
                    sync_status="linked",
                )
            )
            await s.commit()

        listed = await c.get(f"/api/accounts/{acc}/published-notes", headers=bearer(key))
        assert listed.status_code == 200, listed.text
        assert listed.json()["notes"][0]["related_counselor"] == "李宇"
        assert "related_counselor" in listed.json()["meta"]["field_notes"]

        single = await c.get("/api/published-notes/n1", headers=bearer(key))
        assert single.json()["note"]["related_counselor"] == "李宇"
