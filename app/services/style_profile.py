"""每用户风格档案服务层:读当前档案 / 写新版本(乐观锁)/ 列历史 / 取某版 / 回退。

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。约定与
note_metrics_service 一致:纯业务逻辑,用调用方传入的 AsyncSession——只 add/query/commit,
不自开引擎、不管理连接。

三条不可动摇的语义(做错即断 skill 侧链路):
- ``exists`` 必须能区分"有个人档案"与"没有、这是管理员的"——skill 安装引导据此决定
  对运营说「回读你的风格请确认」还是「你还没有自己的档案,先沿用管理员的可以吗?」。
- **回退 = 以旧版内容造一个新版本**,不是把版本指针拨回去:否则"回退到 v3 后又后悔"
  时 v4–v7 无处可寻,且与"历史只增不减"矛盾。
- ``profile`` **原样存取**:不改 key、不做规范化、不校验语义(density 五个中文 key 是
  skill 侧定死的跨端接口)。
"""

import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.style_profile import StyleProfile, StyleProfileVersion

# profile 大小上限(UTF-8 字节):skill 侧预计 2–5 KB,64 KB 绰绰有余。
# 超限**明确报错**而不是静默截断——截断出来的档案是坏档案,运营无从察觉。
MAX_PROFILE_BYTES = 64 * 1024

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


async def _current_row(session: AsyncSession, operator_id: int) -> StyleProfile | None:
    """取该运营的当前档案行;无则 None。operator_id 恒为真实 int,取不到管理员 NULL 行。"""
    return (
        await session.execute(
            select(StyleProfile).where(StyleProfile.operator_id == operator_id)
        )
    ).scalar_one_or_none()


async def admin_default_profile(session: AsyncSession) -> dict:
    """管理员默认档案内容:优先读 operator_id IS NULL 的 seed 行,缺行回落到常量。

    回落不是冗余:测试与开发库走 create_all 建表(不跑 alembic),没有迁移 seed 的那一行。
    """
    row = (
        await session.execute(
            select(StyleProfile).where(StyleProfile.operator_id.is_(None))
        )
    ).scalars().first()
    return row.profile if row is not None else ADMIN_DEFAULT_PROFILE


async def get_profile(session: AsyncSession, operator_id: int) -> dict:
    """读当前档案;无个人档案时返回管理员默认档案并显式标注 exists=False。"""
    row = await _current_row(session, operator_id)
    if row is None:
        return {
            "exists": False,
            "source": "admin_default",
            "profile": await admin_default_profile(session),
        }
    return {
        "exists": True,
        "version": row.version,
        "source": row.source,
        "note": row.note,
        "updated_at": _iso(row.updated_at),
        "profile": row.profile,
    }


async def _write_new_version(
    session: AsyncSession,
    operator_id: int,
    *,
    base_version: int,
    profile: dict,
    source: str,
    note: str | None,
) -> dict:
    """乐观锁校验 → 当前档案升版 + 版本表落一条完整快照;PUT 与 rollback 共用此核心。"""
    row = await _current_row(session, operator_id)
    current_version = row.version if row is not None else 0  # 无档案时当前版本记 0
    if base_version != current_version:
        raise VersionConflict(current_version, _iso(row.updated_at) if row else None)
    _assert_size(profile)

    new_version = current_version + 1
    now = datetime.utcnow()
    if row is None:
        row = StyleProfile(operator_id=operator_id)
        session.add(row)
    row.version = new_version
    row.profile = profile
    row.source = source
    row.note = note
    row.updated_at = now
    row.updated_by = operator_id
    # 每次改动都留档:版本表 append-only,与当前档案同事务落地,不存在"升了版没快照"
    session.add(
        StyleProfileVersion(
            operator_id=operator_id,
            version=new_version,
            profile=profile,
            source=source,
            note=note,
            created_at=now,
            created_by=operator_id,
        )
    )
    # 乐观锁的 TOCTOU 兜底:上面「读 current_version → 比对 base_version」与这里的写入
    # **不是一个原子操作**。两个会话同时带同一 base_version 时,双方都能通过比对、都算出
    # 同一个 new_version,最终只有唯一约束 uq_style_profile_versions_op_ver 拦得住后落地的
    # 那个 —— 但它抛的是 IntegrityError,不是 VersionConflict,会一路冒到全局兜底变成
    # **500**,而契约承诺的是 409(带 current_version 供 skill 侧提示"请重新读取")。
    # 故在此把撞唯一键转成正牌冲突:回滚脏事务 → 重读真实当前版本 → 抛 VersionConflict。
    # 数据本身不会坏(败者整个事务连同当前档案的 update 一起回滚),坏的只是错误语义。
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        latest = await _current_row(session, operator_id)
        raise VersionConflict(
            latest.version if latest is not None else 0,
            _iso(latest.updated_at) if latest is not None else None,
        ) from None
    return {
        "exists": True,
        "version": new_version,
        "source": source,
        "note": note,
        "updated_at": _iso(now),
    }


async def save_profile(
    session: AsyncSession,
    operator_id: int,
    *,
    base_version: int,
    profile: dict,
    source: str,
    note: str | None = None,
) -> dict:
    """写新版本:version 自增 + 版本表落快照;base_version 不符抛 VersionConflict。

    首次写(此前 exists=False)传 base_version=0,创建 version 1。
    """
    return await _write_new_version(
        session,
        operator_id,
        base_version=base_version,
        profile=profile,
        source=source,
        note=note,
    )


async def list_versions(session: AsyncSession, operator_id: int) -> list[dict]:
    """历史版本列表(倒序);**不含 profile 全文**——列表要轻,预览走取某版端点。"""
    rows = (
        await session.execute(
            select(StyleProfileVersion)
            .where(StyleProfileVersion.operator_id == operator_id)
            .order_by(StyleProfileVersion.version.desc())
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
    session: AsyncSession, operator_id: int, version: int
) -> dict:
    """取某一版的完整 profile(回退前预览);不存在抛 NotFoundError。"""
    row = (
        await session.execute(
            select(StyleProfileVersion).where(
                StyleProfileVersion.operator_id == operator_id,
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
        "profile": row.profile,
    }


async def rollback(
    session: AsyncSession, operator_id: int, *, to_version: int, base_version: int
) -> dict:
    """回退:**以 to_version 的内容创建一个新版本**(source=rollback),不拨版本指针。

    指针式回退会让中间版本无处可寻(回退到 v3 后又后悔就回不到 v7),且与"历史只增不减"
    矛盾。同样受 base_version 乐观锁保护。
    """
    old = await get_version(session, operator_id, to_version)  # 不存在 → 404
    return await _write_new_version(
        session,
        operator_id,
        base_version=base_version,
        profile=old["profile"],
        source="rollback",
        note=f"回退到 v{to_version}",
    )
