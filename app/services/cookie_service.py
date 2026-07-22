"""共享 cookie 服务:sameSite 规范化 + 每号唯一行 upsert + 加密落库 + import/get。

"插件推 cookie / 共享 cookie" 的核心。约定:
- 纯业务逻辑,使用调用方传入的 AsyncSession——只 add/query/commit,不自开引擎/事务边界。
- cookie 一律先 normalize_cookies 规范 sameSite,再 json.dumps → encrypt_cookies 加密存
  login_cookies;库内永不落明文(见 app.core.security,Fernet)。
- 每个小红书账号唯一一行:import 优先按 user_info.user_id 匹配既有号,否则按 account_name;
  account_name 兜底仅在不与既有 user_id 冲突时采用(避免把两个不同身份并成一行);
  命中则更新,未命中则新建,新建时给导入 operator 建 access(grant_access)。
- user_id 有 DB 级部分唯一索引(见模型):新建撞索引(并发/重复)时回滚 → 按 user_id
  重新命中既有行走更新路径,不产生两行也不把 IntegrityError 抛给调用方。
- get 先 assert_account_access 鉴权(admin 放行/无权抛 AccessDenied),再解密回读。
"""

import json
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import assert_account_access
from app.core.config import settings
from app.core.security import decrypt_cookies, encrypt_cookies
from app.models.operator import Operator, OperatorAccountAccess
from app.models.xhs_account import XhsAccount
from app.services.operator_service import grant_access

# sameSite 值映射表(小写/别名 → Camoufox/Playwright 要求的首字母大写形式)。
# 'unspecified' 与未识别值统一落到默认 'Lax'。
_SAME_SITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",  # Chrome 扩展导出格式
    "unspecified": "Lax",  # 浏览器默认
}

# 从 user_info 回填到 XhsAccount 的字段(仅这几个,其余账号配置不在此职责内)。
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")


def normalize_cookies(raw: list[dict]) -> list[dict]:
    """规范每条 cookie 的 sameSite:小写/别名/缺失/未识别 一律归到 'Strict'/'Lax'/'None'。

    缺 sameSite 或无法识别 → 补默认 'Lax'。逐条浅拷贝,不就地修改入参;
    name/value/domain/path/httpOnly/secure/expires 等其余字段原样保留。
    """
    normalized: list[dict] = []
    for cookie in raw:
        new_cookie = dict(cookie)
        value = new_cookie.get("sameSite")
        if isinstance(value, str) and value.lower() in _SAME_SITE_MAP:
            new_cookie["sameSite"] = _SAME_SITE_MAP[value.lower()]
        else:
            new_cookie["sameSite"] = "Lax"
        normalized.append(new_cookie)
    return normalized


def _apply_user_info(account: XhsAccount, user_info: dict | None) -> None:
    """把 user_info 中的非空字段回填到账号;空/缺失字段跳过,不覆盖既有值。"""
    if not user_info:
        return
    for field in _USER_INFO_FIELDS:
        value = user_info.get(field)
        if value:
            setattr(account, field, value)


def _apply_cookie_state(
    account: XhsAccount, user_info: dict | None, encrypted: str
) -> None:
    """把 user_info 回填 + 加密 cookie + 刷新 last_login_at 写到账号行。

    只动 cookie / user_info / 登录时间,不改内部展示名与巡检态。新建与更新两条
    路径共用,保证写入语义一致。
    """
    _apply_user_info(account, user_info)
    account.login_cookies = encrypted
    account.last_login_at = datetime.utcnow()


async def _clean_placeholder_accounts(
    session: AsyncSession, operator_id: int, kept_account_id: int
) -> int:
    """真登录成功后清理同 operator 近窗内的占位废账号行及其授权行,返回删除的账号数。

    仅当调用方本次落库的是"真身份"行(user_id 非空)时才调用。清理判据(与需求 §7.2 逐字一致):
    ``user_id IS NULL AND name LIKE 'xhs_account_%'``,且 created_at 在近
    ``PLACEHOLDER_CLEAN_WINDOW_MINUTES`` 分钟窗口内,且该行在 operator_account_access 里有
    ``operator_id == 本次导入 operator`` 的授权行(同 operator 限定,保证并发不误删他人),
    且 ``id != kept_account_id``(绝不误删本次落库的真号)。命中则删授权行 + 删账号行。
    """
    cutoff = datetime.utcnow() - timedelta(
        minutes=settings.PLACEHOLDER_CLEAN_WINDOW_MINUTES
    )
    rows = (
        await session.execute(
            select(XhsAccount.id, XhsAccount.name)
            .join(
                OperatorAccountAccess,
                OperatorAccountAccess.xhs_account_id == XhsAccount.id,
            )
            .where(
                XhsAccount.user_id.is_(None),
                XhsAccount.name.like("xhs_account_%"),
                XhsAccount.created_at >= cutoff,
                OperatorAccountAccess.operator_id == operator_id,
                XhsAccount.id != kept_account_id,
            )
        )
    ).all()
    if not rows:
        return 0
    ids = [row.id for row in rows]
    await session.execute(
        delete(OperatorAccountAccess).where(
            OperatorAccountAccess.xhs_account_id.in_(ids)
        )
    )
    await session.execute(delete(XhsAccount).where(XhsAccount.id.in_(ids)))
    await session.commit()
    logger.info(
        f"[cookie_import] 真登录成功清理占位废账号 operator={operator_id} "
        f"删除 {len(ids)} 行: "
        + ", ".join(f"{row.id}:{row.name}" for row in rows)
    )
    return len(ids)


async def import_cookies(
    session: AsyncSession,
    operator: Operator,
    account_name: str,
    cookies: list[dict],
    user_info: dict | None,
) -> tuple[XhsAccount, bool, int]:
    """upsert 唯一账号行并加密落库 cookie;返回 (账号, 是否新建, 清理的占位废账号数)。

    匹配顺序:优先按 user_info.user_id 命中既有号,否则按 account_name。account_name
    兜底仅在不会把两个不同身份并成一行时采用——若 incoming 带 user_id 且同名行已绑定
    另一个 user_id,则视为不同号、不并入。命中则更新(回填 user_info、加密写
    login_cookies、刷新 last_login_at);未命中则新建,并给导入 operator 建 access。
    新建撞 user_id 唯一索引(并发/重复)时回滚 → 按 user_id 重新命中走更新路径。
    cookie 规范化后再 json.dumps 加密。

    占位自愈:三条落库路径结束前,当且仅当本次落库行 user_id 非空(真身份)时,清理同
    operator 近窗内的占位废账号(见 _clean_placeholder_accounts)。占位推送本身(本次行
    user_id 为空)不触发清理——cookie 保留到 TTL,由 placeholder_reaper 兜底回收。
    """
    encrypted = encrypt_cookies(
        json.dumps(normalize_cookies(cookies), ensure_ascii=False)
    )

    user_id = (user_info or {}).get("user_id")

    # 匹配既有唯一行:先 user_id(最精确)。
    existing: XhsAccount | None = None
    if user_id:
        existing = (
            await session.execute(
                select(XhsAccount).where(XhsAccount.user_id == user_id)
            )
        ).scalars().first()
    # 回退 account_name:仅当不会误并两个不同身份时才采用——incoming 带 user_id 且
    # 同名行已绑定另一个 user_id,说明是不同号,不并入(当作新号新建)。
    if existing is None and account_name:
        cand = (
            await session.execute(
                select(XhsAccount).where(XhsAccount.name == account_name)
            )
        ).scalars().first()
        if cand is not None and not (
            user_id and cand.user_id and cand.user_id != user_id
        ):
            existing = cand

    if existing is not None:
        # S1:命中既有号走更新路径前必须鉴权 —— 否则低权 operator 猜到某号 user_id 即可
        # 覆盖其 login_cookies(发布劫持 / DoS)。admin 走 assert 内部放行。无权抛
        # AccessDenied,cookie 未变(此刻尚未 _apply_cookie_state,无待提交改动)。
        await assert_account_access(operator, existing.id, session)
        _apply_cookie_state(existing, user_info, encrypted)
        await session.commit()
        cleaned = (
            await _clean_placeholder_accounts(session, operator.id, existing.id)
            if existing.user_id
            else 0
        )
        return existing, False, cleaned

    # 新建账号并给导入者授权。
    account = XhsAccount(name=account_name)
    _apply_cookie_state(account, user_info, encrypted)
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        # 并发/重复新建撞 user_id 唯一索引:回滚后按 user_id 重新命中既有行走更新,
        # 保证不产生两行、也不把 IntegrityError 抛给调用方。
        await session.rollback()
        existing = (
            await session.execute(
                select(XhsAccount).where(XhsAccount.user_id == user_id)
            )
        ).scalars().first()
        # S1:回滚重命中同样走更新路径,同步补鉴权(不能因并发绕过 access 校验)。
        await assert_account_access(operator, existing.id, session)
        _apply_cookie_state(existing, user_info, encrypted)
        await session.commit()
        cleaned = (
            await _clean_placeholder_accounts(session, operator.id, existing.id)
            if existing.user_id
            else 0
        )
        return existing, False, cleaned
    # expire_on_commit=False,commit 后 account.id 已可安全读取。
    await grant_access(session, operator.id, account.id, operator.id)
    cleaned = (
        await _clean_placeholder_accounts(session, operator.id, account.id)
        if account.user_id
        else 0
    )
    return account, True, cleaned


async def get_cookies(
    session: AsyncSession, operator: Operator, account_id: int
) -> list[dict]:
    """鉴权后解密回读某号 cookie;无 access 抛 AccessDenied,解密空串返回 []。"""
    await assert_account_access(operator, account_id, session)
    account = await session.get(XhsAccount, account_id)
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)
