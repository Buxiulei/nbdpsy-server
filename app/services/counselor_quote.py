"""咨询师推介引用推导:这篇笔记推介哪个咨询师 → 自动引用哪篇笔记。

调用方此前只能显式给 ``quoted_note_id``(引用哪篇笔记),等于把"该引用谁"这条业务规则
甩给了每一个外部 skill。本模块把它收口成一处规则,发布(``POST /api/publish-jobs``)与
编辑已发布笔记(``POST /api/accounts/{id}/note-components``)两条路共用。

**判定顺序(自上而下,命中即止;括号里是需求文档的规则编号)**::

    显式给了 quoted_note_id       → 用它,根本不进本模块(优先级最高)
    标题形如「X咨询师-姓名，…」   → 本篇**自己就是**推介笔记 → 引用小助手那篇  (规则 3)
    传了 related_counselor        → 引用该咨询师的公开推介笔记                 (规则 1)
    标题里出现某位已知咨询师姓名  → 同上(存量笔记不用补录 related_counselor)  (规则 2)
    都不满足                      → None,不引用                               (规则 4)

**为什么规则 3 被提到最前**(需求点名的 2/3 互斥):
一篇标题形如「心理咨询师-李宇，…」的笔记本身就是李宇的推介笔记,再去引用"李宇的推介
笔记"只会引用到它自己、或矩阵里另一个号上同一个人的那篇 —— 推介笔记引用推介笔记,荒谬。
这类笔记该引用的是小助手联系方式那篇,所以这一条必须先判。它同时覆盖了显式传
``related_counselor="李宇"`` 去发李宇推介笔记的情形(那种传法同样只该走小助手)。

于是规则 2 落在这里就是"标题**提到**某位咨询师"(姓名包含),而不是"标题**是**推介笔记
形态"(那已被规则 3 吃掉)—— 两条用的是不同机制,判定顺序一定,天然互斥。

**铁律**:
- 查不到就返回 ``None``,**绝不猜**(与台账"关联不上留 NULL"同一纪律);
- 只引用 ``permission_code == 0`` 的**公开**笔记 —— 私密笔记引用了读者也打不开;
- 咨询师名单**不写死**,每次从 ``published_notes`` 台账现查;小助手笔记 id 走 config;
- 纯规则,不引入任何 LLM 判断。
"""

import re
from typing import Iterable, Optional, Sequence

from sqlalchemy import select

from app.core.config import settings
from app.models.published_note import PublishedNote

# published_notes.permission_code 的平台原值:**只有 0 是公开**(null=未知,不等于公开)。
PUBLIC_PERMISSION_CODE = 0

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


def _public_promo_notes(candidates: Iterable[dict]) -> dict[str, list[dict]]:
    """台账候选行 → ``{咨询师姓名: [该咨询师的公开推介笔记, ...]}``。

    只收**公开**(``permission_code == 0``)且**有 note_id**、且标题是推介形态的行。
    咨询师名单就是这个字典的键 —— 名单永远来自台账现查,系统里没有任何死名单。
    """
    index: dict[str, list[dict]] = {}
    for row in candidates:
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
    """挑 ``counselor`` 的公开推介笔记,返回 note_id;查不到返回 None(绝不猜)。

    同一位咨询师在账号 1 与账号 6 各有一篇,**选择规则必须确定**(同输入必同输出,
    否则同一篇笔记每次跑会引到不同的地方):

    1. 优先选**与本篇不同账号**的那篇 —— 矩阵内互导,让读者从 A 号的笔记走到 B 号去,
       两个号互相导流的价值大于自己引自己;
    2. 同一档内按 ``(account_id, note_id)`` 升序取第一条 —— 纯字典序,不看时间。
       不用"最新一篇"是因为台账的 ``published_at`` 会被同步纠正、``last_synced_at``
       更是每次同步都在变,拿会变的字段排序等于把不确定性引进来。
    """
    matches = _public_promo_notes(candidates).get(counselor or "", [])
    if not matches:
        return None
    ordered = sorted(
        matches,
        key=lambda r: (
            r.get("account_id") == account_id,  # False(异号)排在 True(同号)前
            r.get("account_id") or 0,
            str(r.get("note_id") or ""),
        ),
    )
    return str(ordered[0]["note_id"])


def _counselor_mentioned_in_title(
    title: Optional[str], promo_index: dict[str, list[dict]]
) -> Optional[str]:
    """标题里提到了哪位已知咨询师?**唯一命中**才返回姓名,0 个或多个都返回 None。

    多个命中时不猜:一篇同时提到两位咨询师的笔记,引哪一位都是我们替运营做主。
    """
    hits = [name for name in promo_index if name and name in (title or "")]
    return hits[0] if len(hits) == 1 else None


def _assistant_note(
    candidates: Iterable[dict], assistant_note_id: Optional[str]
) -> Optional[str]:
    """「小助手联系方式」笔记:配置为空、或台账里明确**不是**公开的,一律不引用。

    台账里**根本没有**这一行时照常引用:这个 id 是运营在 config 里显式配的(不是我们
    猜的),多半只是台账还没同步到它,不该被同步进度卡住。但一旦台账里有这行且
    ``permission_code`` 不是 0(私密/未知),就说明它当下读者点不开,宁可不引用。
    """
    note_id = (assistant_note_id or "").strip()
    if not note_id:
        return None
    for row in candidates:
        if str(row.get("note_id") or "") == note_id:
            return note_id if row.get("permission_code") == PUBLIC_PERMISSION_CODE else None
    return note_id


def derive_quoted_note_id(
    *,
    account_id: Optional[int],
    title: Optional[str],
    related_counselor: Optional[str],
    candidates: Sequence[dict],
    assistant_note_id: Optional[str],
    self_note_id: Optional[str] = None,
) -> Optional[str]:
    """按四条规则推导这篇笔记该引用哪篇笔记;推不出来返回 None(纯函数,便于测试)。

    参数:
        account_id: 本篇笔记所属账号(选推介笔记时用来做"优先异号"判断)。
        title: 本篇笔记标题(规则 3 与规则 2 的输入)。
        related_counselor: 调用方显式声明本篇推介哪位咨询师(规则 1)。
        candidates: 台账候选行,每项 ``{account_id, note_id, title, permission_code}``。
        assistant_note_id: 小助手联系方式笔记的平台 id(通常来自 settings)。
        self_note_id: 本篇笔记自己的平台 id(编辑已发布笔记时才有)。**先从候选里剔掉**,
            这样"这位咨询师只有本篇这一篇推介"时干脆推不出来(→ 留空),而不是引用自己;
            两个号各有一篇时也还能正常选到异号那篇。规则 3 那条另走 ``_guard_self``,
            因为小助手笔记 id 是配置来的,不一定在候选里。
    """
    counselor = (related_counselor or "").strip()
    if self_note_id:
        candidates = [
            row
            for row in candidates
            if str(row.get("note_id") or "") != str(self_note_id)
        ]

    # 规则 3:本篇自己就是咨询师推介笔记 → 引用小助手联系方式那篇。
    # 必须排在 related_counselor 之前,理由见模块 docstring。
    if parse_counselor_from_title(title):
        return _guard_self(_assistant_note(candidates, assistant_note_id), self_note_id)

    # 规则 1:调用方显式声明推介哪位咨询师。
    if counselor:
        return _guard_self(pick_promo_note(candidates, counselor, account_id), self_note_id)

    # 规则 2:标题里提到某位已知咨询师(存量笔记不用补录 related_counselor 的兜底)。
    mentioned = _counselor_mentioned_in_title(title, _public_promo_notes(candidates))
    if mentioned:
        return _guard_self(pick_promo_note(candidates, mentioned, account_id), self_note_id)

    # 规则 4:都不满足 → 不引用。
    return None


def _guard_self(resolved: Optional[str], self_note_id: Optional[str]) -> Optional[str]:
    """推出来的就是本篇自己 → 返回 None。自引用没有意义,宁可留空。"""
    if resolved and self_note_id and str(resolved) == str(self_note_id):
        return None
    return resolved


async def load_candidates(session, account_id: Optional[int] = None) -> list[dict]:
    """从 ``published_notes`` 台账捞推导用的候选行。

    **不在 SQL 里按 ``permission_code`` 过滤**:公开与否的判定放在纯函数里做,一来
    小助手笔记要靠"台账里有没有、是不是公开"分三种情况处理,二来过滤规则集中在一处
    好测。``account_id`` 只是可选的收窄口子,推介笔记本就要跨号找,默认全量。
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


async def resolve_quoted_note_id(
    session,
    account_id: int,
    title: Optional[str],
    related_counselor: Optional[str],
    self_note_id: Optional[str] = None,
) -> Optional[str]:
    """查台账 + 跑规则,给出该引用的 note_id;推不出来 None。发布新笔记走这个入口。"""
    candidates = await load_candidates(session)
    return derive_quoted_note_id(
        account_id=account_id,
        title=title,
        related_counselor=related_counselor,
        candidates=candidates,
        assistant_note_id=settings.ASSISTANT_CONTACT_NOTE_ID,
        self_note_id=self_note_id,
    )


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
    return derive_quoted_note_id(
        account_id=account_id,
        title=title,
        related_counselor=related_counselor,
        candidates=candidates,
        assistant_note_id=settings.ASSISTANT_CONTACT_NOTE_ID,
        self_note_id=note_id,
    )
