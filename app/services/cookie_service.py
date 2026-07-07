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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import assert_account_access
from app.core.security import decrypt_cookies, encrypt_cookies
from app.models.operator import Operator
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


async def import_cookies(
    session: AsyncSession,
    operator: Operator,
    account_name: str,
    cookies: list[dict],
    user_info: dict | None,
) -> tuple[XhsAccount, bool]:
    """upsert 唯一账号行并加密落库 cookie;返回 (账号, 是否新建)。

    匹配顺序:优先按 user_info.user_id 命中既有号,否则按 account_name。account_name
    兜底仅在不会把两个不同身份并成一行时采用——若 incoming 带 user_id 且同名行已绑定
    另一个 user_id,则视为不同号、不并入。命中则更新(回填 user_info、加密写
    login_cookies、刷新 last_login_at);未命中则新建,并给导入 operator 建 access。
    新建撞 user_id 唯一索引(并发/重复)时回滚 → 按 user_id 重新命中走更新路径。
    cookie 规范化后再 json.dumps 加密。
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
        _apply_cookie_state(existing, user_info, encrypted)
        await session.commit()
        return existing, False

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
        _apply_cookie_state(existing, user_info, encrypted)
        await session.commit()
        return existing, False
    # expire_on_commit=False,commit 后 account.id 已可安全读取。
    await grant_access(session, operator.id, account.id, operator.id)
    return account, True


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
