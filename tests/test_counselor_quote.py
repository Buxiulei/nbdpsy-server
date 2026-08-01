"""咨询师推介引用推导单测:这篇笔记推介哪个咨询师 → 自动引用哪篇笔记。

被测规则(判定顺序,括号里是需求文档编号):

    显式 quoted_note_id       → 用它,压过一切
    标题形如「X咨询师-姓名，…」 → 引用「小助手联系方式」那篇          (规则 3)
    传了 related_counselor    → 引用该咨询师的公开推介笔记            (规则 1)
    标题里提到已知咨询师姓名  → 同上                                 (规则 2)
    都不满足                  → None                                (规则 4)

重点锁死的几条:

- **绝不跨账号引用**(硬业务约束,不是保守兜底):每个账号背后是不同运营,从该账号来的
  客户算其 KPI,引到别人的推介笔记就是把客户导到别人名下抢其绩效。故只认**本账号**的
  咨询师推介笔记,本账号没有就**留空**——哪怕别的账号明明有一篇同一位咨询师的;
- **唯一例外**是「接待员联系方式」笔记(含二维码有违规风险,集中在单一账号统一管理),
  由 config 指定,**出厂留空**;没配就是规则 3 不生效,**绝不 fallback 到别的笔记**;
- **规则 2 与 3 互斥**:一篇标题是推介形态的笔记本身就是推介笔记,只能引用接待员那篇,
  **绝不自引用**;哪怕同时显式传了 related_counselor 也一样(规则 3 先判);
- **标题变体**:「粤语咨询师-黄安麟」(实测的发布笔误)必须照样解析得出;
- **只引用公开笔记**:私密/未知可见性的笔记永不被选为引用目标,接待员笔记也一样;
- **确定性 + 可追查**:同输入必同输出;推不出来时给得出原因码(这是非幂等的全量覆盖
  提交,事后必须查得到当时为什么没引用)。

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

# 「接待员联系方式」笔记的 id。生产出厂留空(真实 id 待运营指认),测试里显式给一个值,
# 恰恰因为业务逻辑里没有默认值可依赖 —— 它只能从配置来。
_RECEPTIONIST = "n_receptionist_on_main"


def _note(account_id, note_id, title, permission_code=0) -> dict:
    """台账候选行(推导函数只认这四个键)。"""
    return {
        "account_id": account_id,
        "note_id": note_id,
        "title": title,
        "permission_code": permission_code,
    }


# 一套双号台账:账号 1 与账号 6 **各有一篇同一位咨询师(李宇)的推介笔记**。
# 这正是最容易出错的形状——本账号选自己那篇,绝不能选到对方号上去。
# 接待员笔记在第三个号(7,NBDpsy 主号)上,是唯一允许跨账号引用的一篇。
def _candidates() -> list[dict]:
    return [
        _note(1, "n_liyu_a1", "心理咨询师-李宇，陪你看见职场里的自己"),
        _note(6, "n_liyu_a6", "心理咨询师-李宇，陪你看见职场里的自己"),
        _note(1, "n_huang_a1", "粤语咨询师-黄安麟，陪你读懂依恋模式"),  # 实测的笔误标题
        _note(1, "n_liuqiong_a1", "心理咨询师-刘琼，情绪来了怎么办"),
        _note(7, _RECEPTIONIST, "加接待员领取评估"),
        _note(1, "n_kepu", "职场倦怠到底怎么修复"),  # 普通科普笔记,不是推介
    ]


def _derive(**kwargs):
    """默认参数齐全的推导调用(只覆盖用例关心的那几个)。"""
    params = dict(
        account_id=1,
        title=None,
        related_counselor=None,
        candidates=_candidates(),
        receptionist_note_id=_RECEPTIONIST,
        self_note_id=None,
    )
    params.update(kwargs)
    return cq.derive_quoted_note_id(**params)


def _reason(**kwargs) -> str:
    """同 ``_derive``,但取原因码(推不出来时说清楚卡在哪一步)。"""
    params = dict(
        account_id=1,
        title=None,
        related_counselor=None,
        candidates=_candidates(),
        receptionist_note_id=_RECEPTIONIST,
        self_note_id=None,
    )
    params.update(kwargs)
    return cq.derive_quote_decision(**params)[1]


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


# ---------------- 规则 3:本篇就是推介笔记 → 引用接待员那篇 ----------------


def test_promo_note_quotes_receptionist_not_itself():
    """规则 3:标题是推介形态 → 引用接待员那篇,**不是**自引用。

    这是唯一允许跨账号的一条(接待员笔记在主号 7 上,本篇在账号 1)。
    """
    assert _derive(title="心理咨询师-李宇，陪你看见职场里的自己") == _RECEPTIONIST


def test_rule3_beats_rule2_even_with_explicit_counselor():
    """**2 与 3 互斥的核心用例**:推介笔记即便显式传了 related_counselor 也走规则 3。

    否则「李宇的推介笔记」会去引用「本账号李宇的推介笔记」——那就是它自己,荒谬。
    """
    assert _derive(
        title="心理咨询师-李宇，陪你看见职场里的自己", related_counselor="李宇"
    ) == _RECEPTIONIST


def test_promo_note_variant_title_also_quotes_receptionist():
    """笔误变体「粤语咨询师-」同样被认成推介笔记(不能因为前缀不同就漏判成科普笔记)。"""
    assert _derive(title="粤语咨询师-黄安麟，陪你读懂依恋模式") == _RECEPTIONIST


def test_receptionist_note_id_is_configurable():
    """接待员笔记 id 全靠参数(生产走 config),换一个就引用另一篇 —— 逻辑里没有默认值。"""
    other = "b0b0b0b0000000000000ffff"
    assert _derive(
        title="心理咨询师-刘琼，情绪来了怎么办", receptionist_note_id=other
    ) == other


def test_receptionist_note_not_configured_returns_none_with_reason():
    """**未配置(出厂态)→ 留空不引用,并给出原因码**,绝不 fallback 到任何其它笔记。"""
    assert _derive(title="心理咨询师-刘琼，x", receptionist_note_id="") is None
    assert _reason(title="心理咨询师-刘琼，x", receptionist_note_id="") == (
        cq.REASON_RECEPTIONIST_NOT_CONFIGURED
    )


def test_receptionist_not_configured_never_falls_back_to_a_promo_note():
    """未配置时也**不许**退而引用本账号那篇同名咨询师推介笔记(那就成自引用了)。"""
    assert _derive(
        title="心理咨询师-刘琼，情绪来了怎么办", related_counselor="刘琼",
        receptionist_note_id="",
    ) is None


def test_receptionist_note_not_public_is_refused():
    """接待员笔记在台账里明确不是公开 → 不引用(读者点不开的笔记引了也没用)。"""
    candidates = [_note(7, _RECEPTIONIST, "加接待员领取评估", permission_code=1)]
    assert _derive(title="心理咨询师-刘琼，x", candidates=candidates) is None
    assert _reason(title="心理咨询师-刘琼，x", candidates=candidates) == (
        cq.REASON_RECEPTIONIST_NOT_PUBLIC
    )


def test_receptionist_note_absent_from_ledger_is_still_used():
    """接待员笔记所在账号台账从没同步过(实测就是这样)→ 照常引用:id 是运营配的,不是猜的。"""
    assert _derive(title="心理咨询师-刘琼，x", candidates=[]) == _RECEPTIONIST


# ---------------- 规则 1:显式 related_counselor ----------------


def test_related_counselor_picks_own_accounts_promo_note():
    """规则 1:科普笔记 + related_counselor → 引用**本账号**该咨询师的公开推介笔记。

    台账里账号 1 与账号 6 各有一篇李宇的推介笔记,选的必须是自己号上那篇。
    """
    assert _derive(
        account_id=1, title="职场倦怠到底怎么修复", related_counselor="李宇"
    ) == "n_liyu_a1"
    assert _derive(
        account_id=6, title="职场倦怠到底怎么修复", related_counselor="李宇"
    ) == "n_liyu_a6"


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


# ---------------- 绝不跨账号(绩效归属硬约束) ----------------


def test_never_quotes_another_accounts_promo_note():
    """**本条最重要**:本账号没有该咨询师的推介笔记 → 留空,哪怕别的号明明有一篇。

    跨账号引用会把客户导到别的运营名下、窃取其绩效归属(每个账号背后是不同运营,从该
    账号来的客户算其 KPI)。所以这里期望的是 None,不是 "n_liyu_a6"。
    """
    candidates = [_note(6, "n_liyu_a6", "心理咨询师-李宇，x")]  # 只有账号 6 有
    assert _derive(
        account_id=1, title="职场倦怠", related_counselor="李宇", candidates=candidates
    ) is None


def test_cross_account_miss_gives_reason():
    """跨账号未命中要给得出原因码,便于事后追查为什么这篇没引用。"""
    candidates = [_note(6, "n_liyu_a6", "心理咨询师-李宇，x")]
    assert _reason(
        account_id=1, related_counselor="李宇", candidates=candidates
    ) == cq.REASON_COUNSELOR_PROMO_NOT_IN_ACCOUNT


def test_other_accounts_note_never_leaks_via_title_mention():
    """规则 2 也不许跨账号:标题提到的咨询师只在别的号有推介笔记 → 留空。"""
    candidates = [_note(6, "n_liyu_a6", "心理咨询师-李宇，x")]
    assert _derive(account_id=1, title="和李宇一起看依恋模式", candidates=candidates) is None


def test_selection_is_deterministic_regardless_of_input_order():
    """同输入必同输出:候选行顺序打乱、重复多跑,结果恒定。"""
    base = _candidates()
    picks = set()
    for shift in range(len(base)):
        rotated = base[shift:] + base[:shift]
        picks.add(_derive(account_id=1, related_counselor="李宇", candidates=rotated))
    assert picks == {"n_liyu_a1"}


def test_same_account_multiple_promo_notes_is_deterministic():
    """同账号同一位咨询师有多篇(重发过)→ 按 note_id 升序取第一条,不随机不看时间。"""
    candidates = [
        _note(1, "n_c", "心理咨询师-李宇，x"),
        _note(1, "n_a", "心理咨询师-李宇，x"),
        _note(1, "n_b", "心理咨询师-李宇，x"),
    ]
    assert _derive(account_id=1, related_counselor="李宇", candidates=candidates) == "n_a"


# ---------------- 规则 2:标题提到某位已知咨询师 ----------------


def test_title_mentioning_counselor_quotes_their_promo_note():
    """规则 2:没传 related_counselor,标题提到已知咨询师 → 引用**本账号**他那篇推介笔记。"""
    assert _derive(account_id=1, title="和李宇一起看依恋模式") == "n_liyu_a1"


def test_title_mentioning_two_counselors_returns_none():
    """标题同时提到两位咨询师 → None:引哪一位都是我们替运营做主,不猜。"""
    assert _derive(title="李宇和刘琼聊聊情绪") is None
    assert _reason(title="李宇和刘琼聊聊情绪") == cq.REASON_AMBIGUOUS_TITLE


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


def test_self_exclusion_does_not_open_a_cross_account_door():
    """本篇被剔出候选后**也不会**跑去引用别的号那篇 —— 剔自己 ≠ 放开跨账号。"""
    assert _derive(
        account_id=6, related_counselor="李宇", self_note_id="n_liyu_a6"
    ) is None


def test_editing_the_receptionist_note_never_quotes_itself():
    """接待员笔记 id 是配置来的、可能不在候选里 → 由 _guard_self 兜底掐掉自引用。"""
    assert _derive(
        title="心理咨询师-刘琼，x", candidates=[], self_note_id=_RECEPTIONIST
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
    """建 job 时按 related_counselor 推导出**本账号**的 quoted_note_id 并落库 + 留痕。

    另一个号上也有一篇同一位咨询师的推介笔记,必须**不**被选中。
    """
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        other = await seed_account("号B", "uB", _COOKIES)
        key = "op-cq-derive"
        await _operator_with_access(key, acc, other)
        await _seed_ledger(acc, "n_liyu_a", "心理咨询师-李宇，陪你看见职场里的自己")
        await _seed_ledger(other, "n_liyu_b", "心理咨询师-李宇，陪你看见职场里的自己")

        r = await _post_publish(c, acc, key, title="职场倦怠怎么修复", related_counselor="李宇")

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == "n_liyu_a"  # 自己号那篇,不是 n_liyu_b
            assert job.related_counselor == "李宇"


async def test_publish_job_never_borrows_another_accounts_promo_note(
    tmp_path, monkeypatch
):
    """**绩效归属闸的端到端用例**:只有别的号有该咨询师推介笔记 → quoted_note_id 留空。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        other = await seed_account("号B", "uB", _COOKIES)
        key = "op-cq-noborrow"
        await _operator_with_access(key, acc, other)
        await _seed_ledger(other, "n_liyu_b", "心理咨询师-李宇，陪你看见职场里的自己")

        r = await _post_publish(c, acc, key, title="职场倦怠怎么修复", related_counselor="李宇")

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id is None
            assert job.related_counselor == "李宇"  # 留痕仍在,只是没引用


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


async def test_publish_promo_note_quotes_receptionist(tmp_path, monkeypatch):
    """发一篇新的咨询师推介笔记 → 自动引用接待员那篇,而不是同一个人的老推介笔记。"""
    monkeypatch.setattr(cq.settings, "RECEPTIONIST_CONTACT_NOTE_ID", _RECEPTIONIST)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-promo"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_liyu_old", "心理咨询师-李宇，老版")

        r = await _post_publish(
            c, acc, key, title="心理咨询师-李宇，陪你看见职场里的自己",
            related_counselor="李宇",
        )

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id == _RECEPTIONIST


async def test_publish_promo_note_leaves_quote_empty_when_unconfigured(
    tmp_path, monkeypatch
):
    """接待员 id 未配置(出厂态)→ 推介笔记 quoted_note_id 留空,不 fallback 到老推介笔记。"""
    monkeypatch.setattr(cq.settings, "RECEPTIONIST_CONTACT_NOTE_ID", "")
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-promo-unset"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_liyu_old", "心理咨询师-李宇，老版")

        r = await _post_publish(
            c, acc, key, title="心理咨询师-李宇，陪你看见职场里的自己",
            related_counselor="李宇",
        )

        assert r.status_code == 202, r.text
        async with db_module.async_session() as s:
            job = await s.get(PublishJob, r.json()["job_id"])
            assert job.quoted_note_id is None


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
    """编辑已发布笔记:给 related_counselor 就够了,登记的 payload 里带推导出的 note_id。

    别的号上也有一篇李宇的推介笔记,同样必须**不**被选中。
    """
    monkeypatch.setenv("NBDPSY_ROLE", "api")  # 只登记台账,不起浏览器
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        other = await seed_account("号B", "uB", _COOKIES)
        key = "op-cq-comp"
        await _operator_with_access(key, acc, other)
        await _seed_ledger(acc, "n_target", "职场倦怠怎么修复")
        await _seed_ledger(acc, "n_liyu_a", "心理咨询师-李宇，x")
        await _seed_ledger(other, "n_liyu_b", "心理咨询师-李宇，x")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_target", "related_counselor": "李宇"},
            headers=bearer(key),
        )

        assert r.status_code == 202, r.text
        payload = await _job_payload(r.json()["job_id"])
        assert payload["quoted_note_id"] == "n_liyu_a"
        assert payload["related_counselor"] == "李宇"


async def test_components_promo_note_quotes_receptionist_not_itself(tmp_path, monkeypatch):
    """**2/3 互斥的 REST 端用例**:对一篇咨询师推介笔记设组件 → 引用接待员,不自引用。

    标题从台账现查(还是那个笔误变体),调用方连 related_counselor 都不用给。
    """
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    monkeypatch.setattr(cq.settings, "RECEPTIONIST_CONTACT_NOTE_ID", _RECEPTIONIST)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", _COOKIES)
        key = "op-cq-comp-promo"
        await _operator_with_access(key, acc)
        await _seed_ledger(acc, "n_promo", "粤语咨询师-黄安麟，陪你读懂依恋模式")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "n_promo", "related_counselor": "黄安麟"},
            headers=bearer(key),
        )

        assert r.status_code == 202, r.text
        payload = await _job_payload(r.json()["job_id"])
        assert payload["quoted_note_id"] == _RECEPTIONIST


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
