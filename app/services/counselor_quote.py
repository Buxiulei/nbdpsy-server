"""咨询师推介引用推导:这篇笔记推介哪个咨询师 → 自动引用哪篇笔记。

调用方此前只能显式给 ``quoted_note_id``(引用哪篇笔记),等于把"该引用谁"这条业务规则
甩给了每一个外部 skill。本模块把它收口成一处规则,发布(``POST /api/publish-jobs``)与
编辑已发布笔记(``POST /api/accounts/{id}/note-components``)两条路共用。

**判定顺序(自上而下,命中即止;括号里是需求文档的规则编号)**::

    显式给了 quoted_note_id       → 用它,根本不进本模块(优先级最高)
    标题形如「X咨询师-姓名，…」   → 本篇**自己就是**推介笔记 → 引用接待员那篇  (规则 3)
    传了 related_counselor        → 引用**本账号**该咨询师的公开推介笔记       (规则 1)
    标题里出现某位已知咨询师姓名  → 同上(存量笔记不用补录 related_counselor)  (规则 2)
    都不满足                      → None,不引用                               (规则 4)

**只引用本账号自己的咨询师推介笔记 —— 这是硬业务约束,不是保守兜底。**
跨账号引用会把客户导到别的运营名下、窃取其绩效归属:每个账号背后是不同的运营,从该账号
来的客户算其 KPI。账号 A 的笔记引用了账号 B 的咨询师推介笔记,读者顺着过去成交,业绩就
记到 B 头上了 —— 这不是"矩阵互导",是**从同事那里抢单**。故本账号查不到该咨询师的公开
推介笔记时一律留空,**绝不跨账号兜底**(别再把这条改回跨账号)。

唯一的例外是「接待员联系方式」笔记:它**含二维码、有违规风险**,正因如此才集中在单一
账号上统一管理,由配置项 ``RECEPTIONIST_CONTACT_NOTE_ID`` 指定。规则 3 引用它是跨账号
的,这是有意为之,动它要谨慎。

**为什么规则 3 被提到最前**(需求点名的 2/3 互斥):
一篇标题形如「心理咨询师-李宇，…」的笔记本身就是李宇的推介笔记,再去引用"李宇的推介
笔记"就是引用它自己 —— 推介笔记引用推介笔记,荒谬。这类笔记该引用的是接待员联系方式
那篇,所以这一条必须先判。它同时覆盖了显式传 ``related_counselor="李宇"`` 去发李宇推介
笔记的情形(那种传法同样只该走接待员)。

于是规则 2 落在这里就是"标题**提到**某位咨询师"(姓名包含),而不是"标题**是**推介笔记
形态"(那已被规则 3 吃掉)—— 两条用的是不同机制,判定顺序一定,天然互斥。

**铁律**:
- 查不到就返回 ``None``,**绝不猜**(与台账"关联不上留 NULL"同一纪律);
- 只引用 ``permission_code == 0`` 的**公开**笔记 —— 私密笔记引用了读者也打不开;
- 咨询师名单**不写死**,每次从 ``published_notes`` 台账现查;接待员笔记 id 走 config
  且**出厂留空**(真实 id 待运营指认),没配就是规则 3 不生效,**绝不 fallback 到别的笔记**;
- 纯规则,不引入任何 LLM 判断。
"""

import re
from typing import Iterable, Optional, Sequence

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.models.published_note import PublishedNote

# published_notes.permission_code 的平台原值:**只有 0 是公开**(null=未知,不等于公开)。
PUBLIC_PERMISSION_CODE = 0

# 推导结论的原因码:推不出来时说清楚是**卡在哪一步**。这是一次非幂等的全量覆盖提交,
# 事后追查"当时为什么没引用/引了这篇"必须有据可查,不能只留一个 None。
REASON_EXPLICIT = "explicit_quoted_note_id"
REASON_PROMO_QUOTES_RECEPTIONIST = "promo_note_quotes_receptionist"
REASON_RECEPTIONIST_NOT_CONFIGURED = "receptionist_note_not_configured"
REASON_RECEPTIONIST_NOT_PUBLIC = "receptionist_note_not_public"
REASON_COUNSELOR_PROMO_PICKED = "counselor_promo_picked"
REASON_COUNSELOR_PROMO_NOT_IN_ACCOUNT = "counselor_promo_not_found_in_account"
REASON_AMBIGUOUS_TITLE = "multiple_counselors_in_title"
REASON_NO_SIGNAL = "no_counselor_signal"
REASON_SELF_QUOTE = "self_quote_refused"

# 咨询师推介笔记的标题形态:「(至多四字)咨询师-姓名，……」,锚在标题开头。
#
# 三处刻意为之:
# ① **不写死「心理咨询师-」**——实测台账里有一篇「粤语咨询师-黄安麟，陪你读懂依恋模式」
#    (疑似发布时笔误),写死前缀就会把它漏掉;故前缀放宽成"至多四个非分隔符字符";
# ② 破折号收四种写法(半角/全角/两种破折号),运营手打哪个都认;
# ③ 姓名后必须跟分隔符或标题结束,否则「心理咨询师-李宇陪你读懂」会把「陪你」一起吞进
#    姓名。宁可这种没分隔符的标题解析不出来(→ 返回 None 不引用),也不要解析出个假名字。
#
# 锚在开头(``^``)是规则 3 与规则 2 互斥的支点:标题**以**推介形态开头 = 这篇就是推介笔记;
# 只是在标题中间提到某位咨询师的,归规则 2。
_PROMO_TITLE_RE = re.compile(
    r"^\s*[^\s\-－—–]{0,4}咨询师\s*[-－—–]\s*"
    r"([一-龥]{2,4}?)(?=[，,。、!！?？:：|｜·\s]|$)"
)


def parse_counselor_from_title(title: Optional[str]) -> Optional[str]:
    """标题是不是**咨询师推介笔记**的形态?是则返回被推介的咨询师姓名,否则 None。

    只认"标题以推介形态开头"这一种(见 ``_PROMO_TITLE_RE`` 的锚定说明)。
    """
    match = _PROMO_TITLE_RE.match(title or "")
    return match.group(1) if match else None


def _public_promo_notes(
    candidates: Iterable[dict], account_id: Optional[int]
) -> dict[str, list[dict]]:
    """**本账号**台账候选行 → ``{咨询师姓名: [该咨询师的公开推介笔记, ...]}``。

    只收本账号(``account_id`` 相等)、**公开**(``permission_code == 0``)、有 note_id、
    且标题是推介形态的行。别的账号的推介笔记从这里就被挡掉了 —— 见模块 docstring 里的
    绩效归属约束,它们**不是**候选,不是"优先级低的候选"。

    咨询师名单就是这个字典的键 —— 名单永远来自台账现查,系统里没有任何死名单。
    """
    index: dict[str, list[dict]] = {}
    for row in candidates:
        if row.get("account_id") != account_id:
            continue
        if row.get("permission_code") != PUBLIC_PERMISSION_CODE:
            continue
        if not row.get("note_id"):
            continue
        name = parse_counselor_from_title(row.get("title"))
        if name:
            index.setdefault(name, []).append(row)
    return index


def pick_promo_note(
    candidates: Iterable[dict], counselor: str, account_id: Optional[int]
) -> Optional[str]:
    """挑**本账号**里 ``counselor`` 的公开推介笔记,返回 note_id;没有则 None。

    没有就是没有,**不去别的账号找**(跨账号引用 = 抢同事绩效,见模块 docstring)。
    同一账号同一位咨询师万一有多篇(重发过),按 note_id 升序取第一条 —— 纯字典序,同输入
    必同输出。不用"最新一篇"是因为台账的 ``published_at`` 会被同步纠正,拿会变的字段排序
    等于把不确定性引进来。
    """
    matches = _public_promo_notes(candidates, account_id).get(counselor or "", [])
    if not matches:
        return None
    return str(sorted(matches, key=lambda r: str(r.get("note_id") or ""))[0]["note_id"])


def _counselor_mentioned_in_title(
    title: Optional[str], promo_index: dict[str, list[dict]]
) -> Optional[str]:
    """标题里提到了哪位已知咨询师?**唯一命中**才返回姓名,0 个或多个都返回 None。

    多个命中时不猜:一篇同时提到两位咨询师的笔记,引哪一位都是我们替运营做主。
    """
    hits = [name for name in promo_index if name and name in (title or "")]
    return hits[0] if len(hits) == 1 else None


def _receptionist_note(
    candidates: Iterable[dict], receptionist_note_id: Optional[str]
) -> tuple[Optional[str], str]:
    """「接待员联系方式」笔记 → ``(note_id 或 None, 原因码)``。

    这是唯一允许跨账号引用的一篇:它含二维码、有违规风险,故集中在单一账号上统一管理。

    三种情况:
    - **配置为空** → 不引用(出厂就是空,真实 id 待运营指认)。**绝不 fallback 到任何
      其它笔记** —— 猜一篇挂上去比不引用糟得多;
    - 台账里有这行且 ``permission_code`` 不是 0(私密/未知)→ 不引用,读者点不开;
    - 台账里**根本没有**这行 → 照常引用:这个 id 是运营显式配的(不是我们猜的),多半只是
      那个账号的台账还没同步过,不该被同步进度卡住。
    """
    note_id = (receptionist_note_id or "").strip()
    if not note_id:
        return None, REASON_RECEPTIONIST_NOT_CONFIGURED
    for row in candidates:
        if str(row.get("note_id") or "") == note_id:
            if row.get("permission_code") == PUBLIC_PERMISSION_CODE:
                return note_id, REASON_PROMO_QUOTES_RECEPTIONIST
            return None, REASON_RECEPTIONIST_NOT_PUBLIC
    return note_id, REASON_PROMO_QUOTES_RECEPTIONIST


def derive_quote_decision(
    *,
    account_id: Optional[int],
    title: Optional[str],
    related_counselor: Optional[str],
    candidates: Sequence[dict],
    receptionist_note_id: Optional[str],
    self_note_id: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """按四条规则推导该引用哪篇笔记 → ``(note_id 或 None, 原因码)``(纯函数)。

    带原因码的完整版;只要 note_id 的调用方用 ``derive_quoted_note_id``。

    参数:
        account_id: 本篇笔记所属账号。**它是筛选条件不是排序偏好** —— 只有这个账号的
            咨询师推介笔记才是候选(绩效归属,见模块 docstring)。
        title: 本篇笔记标题(规则 3 与规则 2 的输入)。
        related_counselor: 调用方显式声明本篇推介哪位咨询师(规则 1)。
        candidates: 台账候选行,每项 ``{account_id, note_id, title, permission_code}``。
        receptionist_note_id: 「接待员联系方式」笔记的平台 id(生产来自 settings,可为空)。
        self_note_id: 本篇笔记自己的平台 id(编辑已发布笔记时才有)。**先从候选里剔掉**,
            这样"本账号这位咨询师只有本篇这一篇推介"时干脆推不出来(→ 留空),而不是引用
            自己。规则 3 那条另走 ``_guard_self``,因为接待员笔记 id 是配置来的,不在候选里。
    """
    counselor = (related_counselor or "").strip()
    if self_note_id:
        candidates = [
            row
            for row in candidates
            if str(row.get("note_id") or "") != str(self_note_id)
        ]

    # 规则 3:本篇自己就是咨询师推介笔记 → 引用接待员联系方式那篇。
    # 必须排在 related_counselor 之前,理由见模块 docstring。
    if parse_counselor_from_title(title):
        note_id, reason = _receptionist_note(candidates, receptionist_note_id)
        return _guard_self(note_id, self_note_id, reason)

    # 规则 1:调用方显式声明推介哪位咨询师。
    if counselor:
        return _guard_self(
            pick_promo_note(candidates, counselor, account_id),
            self_note_id,
            REASON_COUNSELOR_PROMO_PICKED,
            miss_reason=REASON_COUNSELOR_PROMO_NOT_IN_ACCOUNT,
        )

    # 规则 2:标题里提到某位已知咨询师(存量笔记不用补录 related_counselor 的兜底)。
    promo_index = _public_promo_notes(candidates, account_id)
    mentioned = _counselor_mentioned_in_title(title, promo_index)
    if mentioned:
        return _guard_self(
            pick_promo_note(candidates, mentioned, account_id),
            self_note_id,
            REASON_COUNSELOR_PROMO_PICKED,
            miss_reason=REASON_COUNSELOR_PROMO_NOT_IN_ACCOUNT,
        )
    if any(name in (title or "") for name in promo_index):
        return None, REASON_AMBIGUOUS_TITLE

    # 规则 4:都不满足 → 不引用。
    return None, REASON_NO_SIGNAL


def derive_quoted_note_id(
    *,
    account_id: Optional[int],
    title: Optional[str],
    related_counselor: Optional[str],
    candidates: Sequence[dict],
    receptionist_note_id: Optional[str],
    self_note_id: Optional[str] = None,
) -> Optional[str]:
    """只要结论的薄封装:推不出来返回 None。原因码用 ``derive_quote_decision``。"""
    note_id, _reason = derive_quote_decision(
        account_id=account_id,
        title=title,
        related_counselor=related_counselor,
        candidates=candidates,
        receptionist_note_id=receptionist_note_id,
        self_note_id=self_note_id,
    )
    return note_id


def _guard_self(
    resolved: Optional[str],
    self_note_id: Optional[str],
    reason: str,
    miss_reason: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """推出来的就是本篇自己 → None。自引用没有意义,宁可留空。"""
    if resolved and self_note_id and str(resolved) == str(self_note_id):
        return None, REASON_SELF_QUOTE
    if resolved is None and miss_reason:
        return None, miss_reason
    return resolved, reason


async def load_candidates(session, account_id: Optional[int] = None) -> list[dict]:
    """从 ``published_notes`` 台账捞推导用的候选行。

    **不在 SQL 里按 ``permission_code`` / ``account_id`` 过滤**:筛选规则集中在纯函数里做
    才好测,而且接待员笔记在**别的账号**上,按账号过滤会把它连带滤掉。``account_id`` 参数
    只是给调用方留的收窄口子,默认全量。
    """
    stmt = select(
        PublishedNote.account_id,
        PublishedNote.note_id,
        PublishedNote.title,
        PublishedNote.permission_code,
    ).where(PublishedNote.note_id.is_not(None))
    if account_id is not None:
        stmt = stmt.where(PublishedNote.account_id == account_id)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "account_id": r.account_id,
            "note_id": r.note_id,
            "title": r.title,
            "permission_code": r.permission_code,
        }
        for r in rows
    ]


def _log(where: str, account_id: int, note_id: Optional[str], reason: str) -> None:
    """把推导结论记进日志:非幂等的全量覆盖提交,事后必须查得到当时为什么引了/没引。"""
    logger.info(
        f"[counselor_quote] {where} account={account_id} "
        f"quoted_note_id={note_id or '(留空)'} reason={reason}"
    )


async def resolve_quoted_note_id(
    session,
    account_id: int,
    title: Optional[str],
    related_counselor: Optional[str],
    self_note_id: Optional[str] = None,
) -> Optional[str]:
    """查台账 + 跑规则,给出该引用的 note_id;推不出来 None。发布新笔记走这个入口。"""
    candidates = await load_candidates(session)
    note_id, reason = derive_quote_decision(
        account_id=account_id,
        title=title,
        related_counselor=related_counselor,
        candidates=candidates,
        receptionist_note_id=settings.RECEPTIONIST_CONTACT_NOTE_ID,
        self_note_id=self_note_id,
    )
    _log("publish", account_id, note_id, reason)
    return note_id


async def resolve_for_published_note(
    session, account_id: int, note_id: str, related_counselor: Optional[str]
) -> Optional[str]:
    """编辑已发布笔记时的推导入口:标题**从台账现查**(调用方只给 note_id,不给标题)。

    台账 title 会过期(实测平台显示「粤语咨询师-黄安麟…」而台账里是「心理咨询师-…」),
    但推介笔记的识别正则本就不认死「心理咨询师-」这个前缀,两种写法都能解析出姓名,
    所以过期的标题在这里不影响判定。台账里查不到这篇 → 标题按未知处理,只剩
    related_counselor 那条规则还能用。
    """
    candidates = await load_candidates(session)
    title = next(
        (
            row.get("title")
            for row in candidates
            if str(row.get("note_id") or "") == str(note_id)
            and row.get("account_id") == account_id
        ),
        None,
    )
    quoted, reason = derive_quote_decision(
        account_id=account_id,
        title=title,
        related_counselor=related_counselor,
        candidates=candidates,
        receptionist_note_id=settings.RECEPTIONIST_CONTACT_NOTE_ID,
        self_note_id=note_id,
    )
    _log(f"components(note={note_id})", account_id, quoted, reason)
    return quoted
