"""每运营多套风格档案服务层:套(profile set)维度的读 / 写新版本(乐观锁)/ 列历史 /
取某版 / 回退 + 套管理(列 / 建 / 改名设默认 / 删)+ 管理员默认档案(同构,可多套)。

需求 /home/roots/NBDpsy/文档/2026-07-28-server需求-风格档案多套独立版本链.md;
设计 docs/design-2026-07-28-profile-sets.md(§9 评审修订为准)。约定与 note_metrics_service
一致:纯业务逻辑,用调用方传入的 AsyncSession——只 add/query/commit,不自开引擎、不管理连接。
**唯一例外**是不变量硬保护(_ensure_single_active / 删到剩一套):判定不通过时 rollback
**整个事务**,故不要把这些函数串在已有未提交改动的会话里。

三条不可动摇的语义(做错即断 skill 侧链路):
- ``exists`` 区分"有个人档案"与"没有、这是管理员的"。运营 0 套时回落管理员默认 is_active 套。
- **回退 = 以旧版内容造一个新版本**,不拨版本指针(否则中间版本无处可寻)。
- ``profile`` **原样存取**:不改 key、不做规范化、不校验语义(density 五个中文 key 是跨端接口)。
  唯一例外是 B5 极窄哨兵——拦 profiles-v1 容器进套(见 _reject_container_sentinel)。

兼容性铁律(需求 §4.4 / 设计 §9):不带 ``set`` 的全部端点与今天逐字段一致(读/写/回退
is_active 那套);响应体**字段只增不减**(新增 set/kind 是增字段,老客户端取键不受影响)。

B1 铁律:管理员默认(operator_id IS NULL)行的 UPDATE/SELECT/DELETE 一律 ``.is_(None)``,
**禁参数化 ``= None``**(生成 ``= NULL`` 匹配零行的静默 bug)。owner 过滤统一走 _owner_clause。
"""

import json
import unicodedata
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.style_profile import StyleProfile, StyleProfileVersion

# profile 大小上限(UTF-8 字节):skill 侧预计 2–5 KB,64 KB 绰绰有余。
# 超限**明确报错**而不是静默截断——截断出来的档案是坏档案,运营无从察觉。
MAX_PROFILE_BYTES = 64 * 1024

# 套名规则:≤20 显示宽度(东亚宽字符计 2)、禁 URL 保留字符(套名进 path)。
_NAME_MAX_WIDTH = 20
_NAME_FORBIDDEN = ("/", "?")

# B3 / 迁移默认:0 套运营首写、老平铺迁移都落这套。
_DEFAULT_SET_NAME = "图文"
_DEFAULT_SET_KIND = "carousel"

# B5 哨兵:客户端 v1.48.0 的多套容器信封顶层键。
_CONTAINER_SCHEMA = "profiles-v1"

# 管理员默认档案的内容(需求文档第四节示例值):现有莫兰迪三色 + 固定人物卡那一套。
# 它同时被迁移 seed 到 operator_id IS NULL 行;此处的常量是 create_all 建库(测试/开发)
# 无 seed 行时的回落,并由 tests/test_style_profile.py 钉死两处逐字一致。
ADMIN_DEFAULT_PROFILE: dict = {
    "visual": {
        "palette": [
            {"name": "雾霾蓝灰", "hex": "#A8B5C4"},
            {"name": "暖米白", "hex": "#E8D8C4"},
            {"name": "鼠尾草绿", "hex": "#C9D6CE"},
        ],
        "text_color": "#5A6B7B",
        "character_card": "圆脸、齐肩微卷短发的东亚年轻女性,穿燕麦色针织衫,神情温和平静",
        "texture": "柔和扁平矢量、莫兰迪低饱和、暖米白纸质底、圆润线条、柔光无强阴影",
        "cover_layout": "上方留白区大标题 + 下方主体场景,标题占画面高约 1/3",
        "content_layout": "满版分 2–3 区块,每条信息配一个各不相同的具体场景小图",
    },
    # 这五个 key 是 skill 侧 v1.37.0 定死的中文字段名,创作端与审查端都按它读写,不得改写
    "density": {
        "信息密度档位": "默认",
        "每页文字量": "200–400 字",
        "每页信息点": "6–10 个",
        "版式档": "满版",
        "运营原话": "—",
    },
    "tone": {
        "person": "第二人称「你」为主",
        "sentence_length": "平均 14 字,最长 26 字",
        "emoji_target": "8–14",
        "emoji_style": "温和款 🌱🫧🌙☁️💭🤍🌿,避高唤起款",
        "quote_rule": "仅人物原话可带引号,全文 ≤4 对",
        "paragraph_rhythm": "单句段占 40%,每段后空一行",
    },
    "structure": {
        "title_structure": "核心议题词 + 场景钩子(议题词占前 10 字)",
        "hook_type": "具体场景句(时间 + 动作)",
        "ending": "一个今天能做、门槛低于付费的小动作",
    },
}


class VersionConflict(Exception):
    """乐观锁冲突:base_version 与当前 version 不符 → REST 层转 409。

    带上当前 version 与 updated_at,供 skill 侧提示运营「你的风格档案在别处被改过
    (v3 → v4),请重新读取后再改」——运营可能同时开着两个会话改风格。
    """

    def __init__(self, current_version: int, updated_at: str | None) -> None:
        super().__init__(
            f"风格档案版本冲突:当前 version={current_version},请重新读取后再改"
        )
        self.current_version = current_version
        self.updated_at = updated_at


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _assert_size(profile: dict) -> None:
    """超 64 KB 抛 ValueError(REST 层 → 400);ensure_ascii=False 按真实 UTF-8 字节量。"""
    size = len(json.dumps(profile, ensure_ascii=False).encode("utf-8"))
    if size > MAX_PROFILE_BYTES:
        raise ValueError(
            f"profile 过大:{size} 字节,上限 {MAX_PROFILE_BYTES} 字节(不做截断)"
        )


def _display_width(s: str) -> int:
    """显示宽度:东亚全角/宽字符(F/W)计 2,其余计 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in s)


def _validate_set_name(name: str) -> str:
    """套名规则:非空、trim 首尾空白、禁 URL 保留字符、≤20 显示宽度;非法抛 ValueError(400)。"""
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("套名不能为空")
    if any(ch in trimmed for ch in _NAME_FORBIDDEN):
        raise ValueError("套名不能包含 / 或 ? 等 URL 保留字符")
    if _display_width(trimmed) > _NAME_MAX_WIDTH:
        raise ValueError(f"套名过长:显示宽度上限 {_NAME_MAX_WIDTH}")
    return trimmed


def _reject_container_sentinel(profile: object) -> None:
    """B5 极窄哨兵:同时含 schema=='profiles-v1' 且有 profiles 键的整份多套容器 → 抛 ValueError(400)。

    迁移后 server 原生多套,滞后客户端(v1.48.0)把整份容器 PUT 回来会造成"套里套"、
    dropped_keys/版本链/读取全错乱,且一次性 alembic 不再拆它。这不违"不校验语义"——
    需求 §6 红线前提是 server 不做多套;原生多套后容器进套=确定性数据损坏,拦它恰是保护铁律。
    """
    if (
        isinstance(profile, dict)
        and profile.get("schema") == _CONTAINER_SCHEMA
        and "profiles" in profile
    ):
        raise ValueError("工具包版本过旧(检测到 profiles-v1 容器),请升级后重试")


# ---------------- owner 过滤 + 套解析(B1:NULL 一律 .is_(None)) ----------------


def _owner_clause(operator_id: int | None):
    """owner 过滤子句;operator_id None(管理员默认)→ IS NULL,实 operator → 等值。"""
    col = StyleProfile.operator_id
    return col.is_(None) if operator_id is None else (col == operator_id)


async def _active_set(session: AsyncSession, operator_id: int | None) -> StyleProfile | None:
    """该 owner 的 is_active 套;无则 None(0 套 / 尚无默认)。"""
    rows = (
        await session.execute(
            select(StyleProfile)
            .where(_owner_clause(operator_id), StyleProfile.is_active.is_(True))
            .order_by(StyleProfile.id)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _set_by_name(
    session: AsyncSession, operator_id: int | None, name: str
) -> StyleProfile | None:
    """该 owner 名为 name 的套;无则 None。"""
    return (
        await session.execute(
            select(StyleProfile).where(
                _owner_clause(operator_id), StyleProfile.name == name
            )
        )
    ).scalar_one_or_none()


async def _all_sets(session: AsyncSession, operator_id: int | None) -> list[StyleProfile]:
    return (
        await session.execute(
            select(StyleProfile)
            .where(_owner_clause(operator_id))
            .order_by(StyleProfile.id)
        )
    ).scalars().all()


async def _count_sets(session: AsyncSession, operator_id: int | None) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(StyleProfile)
            .where(_owner_clause(operator_id))
        )
    ).scalar_one()


async def _count_active(session: AsyncSession, operator_id: int | None) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(StyleProfile)
            .where(_owner_clause(operator_id), StyleProfile.is_active.is_(True))
        )
    ).scalar_one()


async def _ensure_single_active(session: AsyncSession, operator_id: int | None) -> None:
    """is_active「恒有且仅一个 True」不变量:先改后数(同 operator_service._ensure_admin_remains)。

    flush 使改动落到事务内(未提交),再统计 active 计数;!=1 则回滚整个事务并抛 409。
    "先改后数"而非"先数再改"堵住 TOCTOU:SQLite 写事务串行,后者 flush 要等前者提交后
    才拿到写锁,届时统计已能看到前者结果,两个并发不会双双通过。
    """
    await session.flush()
    count = await _count_active(session, operator_id)
    if count != 1:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="风格档案默认套状态异常(应恒有且仅一个默认套),请重新读取后重试",
        )


async def _require_set_for_versions(
    session: AsyncSession, operator_id: int | None, set_name: str | None
) -> StyleProfile | None:
    """版本类端点的套解析:不带 set → is_active 套(可能 None);指定但不存在 → 404。"""
    if set_name is None:
        return await _active_set(session, operator_id)
    row = await _set_by_name(session, operator_id, set_name)
    if row is None:
        raise NotFoundError(f"风格档案套「{set_name}」不存在")
    return row


def _set_summary(row: StyleProfile) -> dict:
    """套的轻量摘要(列表 / 建套 / 改套返回体);不含 profile 全文。"""
    return {
        "name": row.name,
        "kind": row.kind,
        "is_active": row.is_active,
        "version": row.version,
        "updated_at": _iso(row.updated_at),
    }


# ---------------- dropped_keys(套内部字段级,§6 数据模型对齐后自然回正) ----------------


def _dropped_keys(old: dict | None, new: dict) -> list[str]:
    """列出相对上一版消失了的顶层与二级键(如 ``["tone", "visual.text_color"]``)。

    这道比对**只有 server 做得了**:server 手上有上一版,skill 侧手上只有 agent 记得带的
    那一份。整份覆盖不是打补丁(既定语义,不改),但"只想改配色却漏带人物卡"这种静默丢失,
    运营往往要到下次出图才发现,那时已查不出是哪次 PUT 弄丢的——故**不拦截、不报错**,
    只如实告知调用方本次覆盖丢了什么。多套拆开后 PUT 的就是单套内容(不再是容器),
    故 dropped_keys 天然回到"套内部字段"的正确语义。

    只比两层:顶层键整段消失就记顶层名、**不再向下展开**(否则整段消失会刷出一串噪音);
    两边都在且都是 dict 时才比二级键。更深不管(skill 侧明确只要顶层与二级)。
    """
    if not old:  # 首次建档(此前无档案)无从比对
        return []
    dropped: list[str] = []
    for key, old_value in old.items():
        if key not in new:
            dropped.append(key)
            continue
        new_value = new[key]
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            dropped.extend(f"{key}.{sub}" for sub in old_value if sub not in new_value)
    return sorted(dropped)


# ---------------- 读 / 写 / 历史 / 回退(set 维度) ----------------


async def _admin_default_active_set(session: AsyncSession) -> StyleProfile | None:
    """管理员默认档案的 is_active 套(operator_id IS NULL);缺行返 None,由调用方回落常量。"""
    return await _active_set(session, None)


async def get_profile(
    session: AsyncSession, operator_id: int, *, set_name: str | None = None
) -> dict:
    """读某套当前档案;不带 set → is_active 套;运营 0 套时回落管理员默认 is_active 套。

    没有个人档案的运营**每次都实时读到最新的管理员默认档案**(不是建档时的快照),故管理员
    一改所有沿用者立刻跟着变。已建个人档案的运营是各自独立的内容,不受管理员改动影响。

    ``base_version`` 是"下一次 PUT 该传什么"的唯一真源(无档案给 0)。set/kind 告诉客户端
    读的是哪套(exists:false 时是管理员默认那套)。GET ?set=X 语义:运营无档案 → exists:false
    回落读默认;有档案但无此套 → 404(设计 §9)。
    """
    if set_name is None:
        row = await _active_set(session, operator_id)
    else:
        row = await _set_by_name(session, operator_id, set_name)
        # 有档案但无此套 → 404;无任何档案 → 落到下面回落分支
        if row is None and await _count_sets(session, operator_id) > 0:
            raise NotFoundError(f"风格档案套「{set_name}」不存在")
    if row is None:
        admin_row = await _admin_default_active_set(session)
        return {
            "exists": False,
            "source": "admin_default",
            "base_version": 0,
            "set": admin_row.name if admin_row else None,
            "kind": admin_row.kind if admin_row else None,
            "admin_default_version": admin_row.version if admin_row else 0,
            "updated_at": _iso(admin_row.updated_at) if admin_row else None,
            "profile": admin_row.profile if admin_row else ADMIN_DEFAULT_PROFILE,
        }
    return {
        "exists": True,
        "version": row.version,
        "base_version": row.version,
        "source": row.source,
        "note": row.note,
        "set": row.name,
        "kind": row.kind,
        "updated_at": _iso(row.updated_at),
        "profile": row.profile,
    }


async def _write_new_version(
    session: AsyncSession,
    operator_id: int,
    set_row: StyleProfile | None,
    *,
    base_version: int,
    profile: dict,
    source: str,
    note: str | None,
    set_name: str | None,
) -> dict:
    """乐观锁校验 → 套当前态升版 + 版本表落一条完整快照;PUT 与 rollback 共用此核心。

    ``set_row`` 为 None 表示运营 0 套且不带 set 的首写(B3):事务内自动建
    ``图文/carousel/is_active`` 套,version 1。
    """
    current_version = set_row.version if set_row is not None else 0  # 无套时当前版本记 0
    if base_version != current_version:
        raise VersionConflict(
            current_version, _iso(set_row.updated_at) if set_row is not None else None
        )
    _assert_size(profile)
    # 覆盖前先记下上一版内容,用于算本次整份覆盖丢掉了哪些键(下面 profile 即被改写)
    dropped = _dropped_keys(set_row.profile if set_row is not None else None, profile)

    new_version = current_version + 1
    now = datetime.utcnow()
    if set_row is None:
        # B3:0 套运营首写自动建套(与迁移默认一致)
        set_row = StyleProfile(
            operator_id=operator_id,
            name=_DEFAULT_SET_NAME,
            kind=_DEFAULT_SET_KIND,
            is_active=True,
        )
        session.add(set_row)
    set_row.version = new_version
    set_row.profile = profile
    set_row.source = source
    set_row.note = note
    set_row.updated_at = now
    set_row.updated_by = operator_id
    # 每次改动都留档:版本表 append-only,与套当前态同事务落地,不存在"升了版没快照"。
    # TOCTOU + B3 并发兜底:flush(建套撞 (operator_id,name) 唯一) 与 commit(版本撞 (set_id,version)
    # 唯一) 都可能在并发下抛 IntegrityError——两个会话同带 base_version、或**两个全新运营并发首写都建
    # 「图文」套**,后落地的撞唯一键。契约承诺 409 而非 500,故把 flush+add+commit 整段纳入兜底:
    # 撞唯一键 → 回滚脏事务 → 重读真实当前版本 → 抛 VersionConflict。
    try:
        await session.flush()  # 新建套时确保拿到 set_row.id;建套撞 (operator_id,name) 在此抛
        session.add(
            StyleProfileVersion(
                set_id=set_row.id,
                operator_id=operator_id,
                version=new_version,
                profile=profile,
                source=source,
                note=note,
                created_at=now,
                created_by=operator_id,
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        latest = (
            await _set_by_name(session, operator_id, set_name)
            if set_name is not None
            else await _active_set(session, operator_id)
        )
        raise VersionConflict(
            latest.version if latest is not None else 0,
            _iso(latest.updated_at) if latest is not None else None,
        ) from None
    return {
        "exists": True,
        "version": new_version,
        "source": source,
        "note": note,
        "set": set_row.name,
        "kind": set_row.kind,
        "updated_at": _iso(now),
        # 没丢弃时也给空列表(不省略这个键):skill 侧要能无条件读它
        "dropped_keys": dropped,
    }


async def save_profile(
    session: AsyncSession,
    operator_id: int,
    *,
    base_version: int,
    profile: dict,
    source: str,
    note: str | None = None,
    set_name: str | None = None,
) -> dict:
    """写某套新版本:version 自增 + 版本表落快照;base_version 不符抛 VersionConflict。

    不带 set → 写 is_active 套;运营 0 套时(B3)自动建 图文/carousel/is_active 套写 v1。
    带 set 但该套不存在 → NotFoundError(404;建套走 POST /sets,不在 PUT 里隐式建)。
    """
    _reject_container_sentinel(profile)  # B5:容器进套 → 400
    if set_name is not None:
        set_row = await _set_by_name(session, operator_id, set_name)
        if set_row is None:
            raise NotFoundError(f"风格档案套「{set_name}」不存在")
    else:
        set_row = await _active_set(session, operator_id)  # None → B3 首写自动建套
    return await _write_new_version(
        session,
        operator_id,
        set_row,
        base_version=base_version,
        profile=profile,
        source=source,
        note=note,
        set_name=set_name,
    )


async def count_versions(
    session: AsyncSession, operator_id: int, *, set_name: str | None = None
) -> int:
    """某套历史版本总数(分页要的 total);无 active 套(0 套运营,不带 set)返 0。"""
    set_row = await _require_set_for_versions(session, operator_id, set_name)
    if set_row is None:
        return 0
    return (
        await session.execute(
            select(func.count())
            .select_from(StyleProfileVersion)
            .where(StyleProfileVersion.set_id == set_row.id)
        )
    ).scalar_one()


async def list_versions(
    session: AsyncSession,
    operator_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    set_name: str | None = None,
) -> list[dict]:
    """某套历史版本列表(倒序分页);**不含 profile 全文**——列表要轻,预览走取某版端点。"""
    set_row = await _require_set_for_versions(session, operator_id, set_name)
    if set_row is None:
        return []
    rows = (
        await session.execute(
            select(StyleProfileVersion)
            .where(StyleProfileVersion.set_id == set_row.id)
            .order_by(StyleProfileVersion.version.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "version": r.version,
            "source": r.source,
            "note": r.note,
            "created_at": _iso(r.created_at),
            "created_by": r.created_by,
        }
        for r in rows
    ]


async def get_version(
    session: AsyncSession,
    operator_id: int,
    version: int,
    *,
    set_name: str | None = None,
) -> dict:
    """取某套某一版的完整 profile(回退前预览);套不存在或版本不存在抛 NotFoundError。"""
    set_row = await _require_set_for_versions(session, operator_id, set_name)
    if set_row is None:
        raise NotFoundError(f"风格档案版本 v{version} 不存在")
    row = (
        await session.execute(
            select(StyleProfileVersion).where(
                StyleProfileVersion.set_id == set_row.id,
                StyleProfileVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"风格档案版本 v{version} 不存在")
    return {
        "version": row.version,
        "source": row.source,
        "note": row.note,
        "created_at": _iso(row.created_at),
        "created_by": row.created_by,
        "set": set_row.name,
        "kind": set_row.kind,
        "profile": row.profile,
    }


async def rollback(
    session: AsyncSession,
    operator_id: int,
    *,
    to_version: int,
    base_version: int,
    set_name: str | None = None,
) -> dict:
    """回退某套:**以 to_version 的内容创建一个新版本**(source=rollback),不拨版本指针,不影响其他套。"""
    old = await get_version(
        session, operator_id, to_version, set_name=set_name
    )  # 套/版本不存在 → 404
    # B5 补漏:回退目标若是迁移拆容器时挂的整份 profiles-v1 容器快照,原样写回=套里套,
    # 与 PUT 哨兵拦的同型事故。命中给 400,让运营改用 GET /versions/{v} 取内容手工拆分。
    _reject_container_sentinel(old["profile"])
    set_row = await _require_set_for_versions(session, operator_id, set_name)
    return await _write_new_version(
        session,
        operator_id,
        set_row,
        base_version=base_version,
        profile=old["profile"],
        source="rollback",
        note=f"回退到 v{to_version}",
        set_name=set_name,
    )


# ---------------- 套管理:列 / 建 / 改名设默认 / 删 ----------------


async def list_sets(session: AsyncSession, operator_id: int | None) -> list[dict]:
    """列该 owner 全部套(轻量摘要,不含 profile 全文)。"""
    return [_set_summary(r) for r in await _all_sets(session, operator_id)]


async def create_set(
    session: AsyncSession,
    operator_id: int | None,
    *,
    name: str,
    kind: str,
    profile: dict | None = None,
    from_name: str | None = None,
    updated_by: int | None = None,
) -> dict:
    """新建一套(v1 快照,source=manual);from 复制某套当前 profile。

    - name 规则见 _validate_set_name;重名 → IntegrityError 转 409(不冒 500)。
    - from 指向不存在的套 → NotFoundError(404)。
    - 首套自动 is_active:建套(先置 False)flush 后复核 active 计数为 0 则置 True——
      覆盖"两个并发 POST 都读到 0 套双双自封"(§9)。
    - 管理员默认(operator_id None)的套不进版本表(operator_id NOT NULL,且不可回退)。
    - kind 建套后**不可改**(PATCH 仅 new_name/is_active,有意为之)。
    """
    name = _validate_set_name(name)
    kind = (kind or "").strip()
    if not kind:
        raise ValueError("kind 不能为空")
    if from_name is not None:
        src = await _set_by_name(session, operator_id, from_name)
        if src is None:
            raise NotFoundError(f"复制来源套「{from_name}」不存在")
        source_profile = src.profile
    else:
        source_profile = profile if profile is not None else {}
    _reject_container_sentinel(source_profile)  # B5:容器进套 → 400
    _assert_size(source_profile)

    now = datetime.utcnow()
    new_row = StyleProfile(
        operator_id=operator_id,
        name=name,
        kind=kind,
        profile=source_profile,
        source="manual",
        note=None,
        version=1,
        is_active=False,
        updated_at=now,
        updated_by=updated_by,
    )
    session.add(new_row)
    try:
        await session.flush()  # 触发 (operator_id,name) 唯一 / NULL 部分索引:重名在此抛
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"套名「{name}」已存在") from None

    # 版本快照:仅实运营(管理员默认 NULL 行不进版本表,operator_id NOT NULL)
    if operator_id is not None:
        session.add(
            StyleProfileVersion(
                set_id=new_row.id,
                operator_id=operator_id,
                version=1,
                profile=source_profile,
                source="manual",
                note=None,
                created_at=now,
                created_by=updated_by,
            )
        )
    # 首套自动 active(先改后数):当前 active 计数为 0 则把本套置默认
    if await _count_active(session, operator_id) == 0:
        new_row.is_active = True
    await _ensure_single_active(session, operator_id)  # flush + 复核恒一
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"套名「{name}」已存在") from None
    return _set_summary(new_row)


async def rename_or_activate_set(
    session: AsyncSession,
    operator_id: int | None,
    *,
    name: str,
    new_name: str | None = None,
    is_active: bool | None = None,
    updated_by: int | None = None,
) -> dict:
    """改名 / 设默认(可原子同时给);套不存在 → 404,重名 → 409,is_active=false 单发拒绝 → 400。

    设默认在事务内**先把该 owner 全部套 is_active=False 再置目标 True**,复核恒一。
    """
    target = await _set_by_name(session, operator_id, name)
    if target is None:
        raise NotFoundError(f"风格档案套「{name}」不存在")
    if is_active is False:
        # 不能把套设为非默认(否则 0 active);换默认套请对目标套设 is_active=true
        raise ValueError("不能把套设为非默认;要换默认套请对目标套设 is_active=true")

    # 先做设默认(不触碰 name,autoflush 无重名可撞),再改名,最后统一 flush 一次兜住重名。
    # 次序若反(先改名再 execute(update)),bulk update 的 autoflush 会把改名的 UPDATE 冲出去、
    # 重名 IntegrityError 从 session.execute 里冒出未被捕获 → 500。
    if is_active is True and not target.is_active:
        await session.execute(
            update(StyleProfile)
            .where(_owner_clause(operator_id))
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        target.is_active = True
    if new_name is not None:
        target.name = _validate_set_name(new_name)

    try:
        await session.flush()  # 改名撞唯一约束在此抛(与设默认原子)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"套名「{new_name}」已存在"
        ) from None
    if is_active is True:
        await _ensure_single_active(session, operator_id)  # 复核恒一(名已成功 flush,不再撞重名)
    await session.commit()
    return _set_summary(target)


async def delete_set(
    session: AsyncSession, operator_id: int | None, *, name: str
) -> dict:
    """删套;剩一套拒绝(409);删的是 active → 事务内把另一套设 active。

    B4:**同一事务内先删该套全部 style_profile_versions 再删 style_profiles 行**
    (应用层级联,本仓 PRAGMA foreign_keys 未开,CASCADE 只是声明)——否则孤儿版本留存 +
    SQLite rowid 复用会让新套撞死套孤儿历史的 (set_id, version) 唯一键,冒 500。
    """
    target = await _set_by_name(session, operator_id, name)
    if target is None:
        raise NotFoundError(f"风格档案套「{name}」不存在")
    if await _count_sets(session, operator_id) <= 1:
        raise HTTPException(
            status_code=409, detail="至少保留一套风格档案,不能删除仅剩的一套"
        )
    was_active = target.is_active
    target_id = target.id
    # B4:先删版本再删套(同事务)
    await session.execute(
        delete(StyleProfileVersion).where(StyleProfileVersion.set_id == target_id)
    )
    await session.delete(target)
    await session.flush()
    if was_active:
        # 事务内把剩余里的一套(id 最小)设 active,维持恒有一个默认套
        fallback = (
            await session.execute(
                select(StyleProfile)
                .where(_owner_clause(operator_id))
                .order_by(StyleProfile.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if fallback is not None:
            fallback.is_active = True
    await _ensure_single_active(session, operator_id)
    await session.commit()
    return {"deleted": name}


# ---------------- 管理员默认档案(operator_id IS NULL;同构,可多套) ----------------


async def get_admin_default(
    session: AsyncSession, *, set_name: str | None = None
) -> dict:
    """读管理员默认档案某套(不带 set → is_active 套);缺行回落常量、版本记 0。

    存在的理由是**留底**:这一行不进版本历史表、改了不可回退。回落行为与 get_profile 的
    exists:false 分支同口径。指定套不存在 → 404。
    """
    if set_name is None:
        row = await _admin_default_active_set(session)
    else:
        row = await _set_by_name(session, None, set_name)
        if row is None:
            raise NotFoundError(f"管理员默认套「{set_name}」不存在")
    return {
        "profile": row.profile if row else ADMIN_DEFAULT_PROFILE,
        "admin_default_version": row.version if row else 0,
        "updated_at": _iso(row.updated_at) if row else None,
        "set": row.name if row else None,
        "kind": row.kind if row else None,
    }


async def save_admin_default(
    session: AsyncSession,
    *,
    profile: dict,
    note: str | None,
    updated_by: int,
    set_name: str | None = None,
) -> dict:
    """覆盖管理员默认档案某套(不带 set → is_active 套;缺行则建 图文/carousel/is_active 套)。

    这套影响所有还没建档的运营(新人默认全走它),故调用方须先 require_admin。整份覆盖、
    profile 原样存取只校大小(B5 哨兵除外)。**管理员默认档案不进版本历史表、不可回退**
    (version 表 operator_id NOT NULL;改前请自行留底)。指定套不存在 → 404。
    """
    _reject_container_sentinel(profile)  # B5:容器进套 → 400
    _assert_size(profile)
    if set_name is None:
        row = await _admin_default_active_set(session)
        if row is None:  # create_all 建的库没有迁移 seed 的那一行,首次写即建 is_active 套
            row = StyleProfile(
                operator_id=None,
                name=_DEFAULT_SET_NAME,
                kind=_DEFAULT_SET_KIND,
                version=0,
                is_active=True,
            )
            session.add(row)
    else:
        row = await _set_by_name(session, None, set_name)
        if row is None:
            raise NotFoundError(f"管理员默认套「{set_name}」不存在")
    now = datetime.utcnow()
    row.version += 1
    row.profile = profile
    row.source = "admin_default"
    row.note = note
    row.updated_at = now
    row.updated_by = updated_by
    await session.commit()
    return {
        "version": row.version,
        "source": "admin_default",
        "note": note,
        "set": row.name,
        "kind": row.kind,
        "updated_at": _iso(now),
    }
