"""视频资产库服务：把一条 clip 产物**拷贝**成不过期的长期资产 + 归属检索 / 删除。

需求见 ``app/models/video_asset.py`` 的模块 docstring。目录布局与直链思路比照
``services/content_archive``（媒体独立副本落 ``uploads`` 下复用免鉴权直链路由）与
``services/dreamina``（HMAC token 目录名即访问控制），不另起炉灶：

- 文件落 ``DATA_DIR/uploads/assets/{asset_id}-{hmac16}/asset.mp4``。落在 ``uploads`` 下
  即被免鉴权 ``/uploads`` 路由暴露，故父段用 SECRET_KEY 派生的 HMAC token——攻击者无
  key 无法由 asset_id 枚举他人素材。
- **不被任何清理机制覆盖**：uploads 的 7 天懒清理只删有 ``UploadBatch`` 行的批次目录
  （``upload_service.sweep_expired``），ClipReaper 的孤儿扫描只遍历 ``uploads/clips/``。
  资产目录两者都不沾——这正是 content_archive 当初规避 7 天清理的同一手法。
- 生命周期只有一条出口：运营显式 ``DELETE``（行 + 目录一起删）。**不设 TTL**。

归属规则与 ``dreamina_rest._can_access`` 逐字同义：admin 全见，其余仅本人 ``created_by``。
"""

import hashlib
import hmac
import json
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.auth.context import AccessDenied
from app.core.config import settings
from app.core.errors import NotFoundError
from app.models.operator import Operator
from app.models.video_asset import VideoAsset
from app.models.video_clip import VideoClip

# 副本文件名恒定（直链白名单据此收窄，见 http/video_assets_rest._NAME_RE）
ASSET_FILE_NAME = "asset.mp4"

_MAX_TAGS = 20
_MAX_TAG_LEN = 32


def new_asset_id() -> str:
    """对外句柄 ``va_<10hex>``（形态与 clip 的 ``vc_`` 同族，互不撞形）。"""
    return "va_" + secrets.token_hex(5)


def _asset_token(asset_id: str) -> str:
    """SECRET_KEY 派生的不可猜 token（手法与 dreamina._clip_token 一致）。"""
    return hmac.new(
        settings.SECRET_KEY.encode(), asset_id.encode(), hashlib.sha256
    ).hexdigest()[:16]


def asset_token_dir(asset_id: str) -> str:
    """副本目录名 ``{asset_id}-{hmac16}``（直链路径里的那一段）。"""
    return f"{asset_id}-{_asset_token(asset_id)}"


def assets_root() -> Path:
    """资产根：``DATA_DIR/uploads/assets``。请求时读 settings（不在 import 期绑定），
    使测试对 DATA_DIR 的 monkeypatch 生效，与 dreamina.clips_root 同惯例。"""
    return (Path(settings.DATA_DIR) / "uploads" / "assets").resolve()


def asset_dir(asset_id: str) -> Path:
    """确保并返回单条资产的目录（**会 mkdir**，只在写路径上调）。"""
    d = assets_root() / asset_token_dir(asset_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_public_url(asset_id: str) -> str:
    """免鉴权直链相对路径（与 clip 的 video_url 同形，调用方拼 base_url 即得公网链）。"""
    return f"/uploads/assets/{asset_token_dir(asset_id)}/{ASSET_FILE_NAME}"


def _can_access(row: VideoAsset, op: Operator) -> bool:
    return op.role == "admin" or row.created_by == op.id


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """去空白 / 丢空串 / 去重保序 / 逐条截断；超 _MAX_TAGS 条截断。"""
    out: list[str] = []
    for raw in tags or []:
        tag = (raw or "").strip()[:_MAX_TAG_LEN]
        if tag and tag not in out:
            out.append(tag)
    return out[:_MAX_TAGS]


def _payload(row: VideoAsset) -> dict:
    """对外单条视图（列表与详情同形，省得调用方记两套字段）。"""
    return {
        "asset_id": row.asset_id,
        "name": row.name,
        "tags": json.loads(row.tags_json or "[]"),
        "video_url": asset_public_url(row.asset_id),
        "source_clip_id": row.source_clip_id,
        "source_operation": row.source_operation,
        "source_model": row.source_model,
        "source_prompt": row.source_prompt,
        "duration": row.duration,
        "size_bytes": row.size_bytes,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def store_clip(session, op: Operator, *, clip_id: str, name: str,
                     tags: list[str] | None = None) -> tuple[dict, bool]:
    """把一条 done 的 clip 转存成长期资产；返回 ``(资产视图, 是否新建)``。

    - 源 clip 不存在 → NotFoundError(404)；不归本人（且非 admin）→ AccessDenied(403)。
    - clip 未完成、或产物已被 TTL 清理/文件不在 → ValueError(400)。**绝不建一条指向
      空文件的资产行**：那样列表里会躺着一条点开就 404 的假资产。
    - 幂等：同一 ``(caller, clip_id)`` 已存过 → 直接回原资产，``created=False``，
      零拷贝零改名（重放不该刷爆磁盘，改名走「删了重存」）。
    """
    clip = (await session.execute(
        select(VideoClip).where(VideoClip.clip_id == clip_id))).scalar_one_or_none()
    if clip is None:
        raise NotFoundError(f"片段 {clip_id} 不存在")
    if not (op.role == "admin" or clip.created_by == op.id):
        raise AccessDenied("无权访问该片段任务")

    existing = (await session.execute(
        select(VideoAsset)
        .where(VideoAsset.created_by == op.id)
        .where(VideoAsset.source_clip_id == clip_id))).scalar_one_or_none()
    if existing is not None:
        return _payload(existing), False

    if clip.status != "done":
        raise ValueError(f"片段 {clip_id} 未完成（当前 {clip.status}），无可转存的产物")
    src = Path(clip.video_path) if clip.video_path else None
    if src is None or not src.is_file():
        raise ValueError(
            f"片段 {clip_id} 的产物已过期或已被清理（clip 产物 TTL "
            f"{settings.CLIP_TTL_DAYS} 天），无法转存"
        )

    asset_id = new_asset_id()
    dest = asset_dir(asset_id) / ASSET_FILE_NAME
    shutil.copy2(src, dest)                     # 独立副本：源 clip 到期被删也不影响
    try:
        row = VideoAsset(
            asset_id=asset_id,
            name=name.strip(),
            tags_json=json.dumps(_normalize_tags(tags), ensure_ascii=False),
            source_clip_id=clip_id,
            source_operation=clip.operation,
            source_model=clip.model,
            source_prompt=clip.prompt,
            duration=clip.duration,
            size_bytes=dest.stat().st_size,
            created_by=op.id,
            created_at=datetime.utcnow(),
        )
        session.add(row)
        await session.commit()
    except Exception:
        # 插行失败（并发重放撞唯一键等）→ 撤掉刚拷的副本，不留孤儿目录
        await session.rollback()
        shutil.rmtree(assets_root() / asset_token_dir(asset_id), ignore_errors=True)
        raise
    logger.info(f"[video_assets] {clip_id} → {asset_id} 转存 {row.size_bytes} 字节")
    return _payload(row), True


async def list_assets(session, op: Operator, *, tag: str | None = None,
                      q: str | None = None, limit: int = 50) -> list[dict]:
    """按归属列资产（admin 全见），可叠加 ``tag`` 精确筛与 ``q`` 模糊搜；按新到旧。

    ``q`` 搜名称 / 标签 / 源提示词三处——挑镜头时记得住的往往是当初那句提示词
    （「黄昏走廊的空镜」），只搜名称会让检索形同虚设。
    """
    stmt = select(VideoAsset).order_by(VideoAsset.created_at.desc(), VideoAsset.id.desc())
    if op.role != "admin":
        stmt = stmt.where(VideoAsset.created_by == op.id)
    if tag:
        # tags_json 是 JSON 数组，带引号匹配即整条标签相等（不会被前缀词误命中）
        stmt = stmt.where(VideoAsset.tags_json.like(f'%"{tag.strip()}"%'))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            VideoAsset.name.like(like)
            | VideoAsset.tags_json.like(like)
            | VideoAsset.source_prompt.like(like)
        )
    stmt = stmt.limit(max(1, min(limit, 200)))
    return [_payload(r) for r in (await session.execute(stmt)).scalars().all()]


async def get_asset(session, op: Operator, asset_id: str) -> dict:
    """取单条资产（含不过期直链）；不存在 404，不归本人 403。"""
    row = await _load(session, op, asset_id)
    return _payload(row)


async def delete_asset(session, op: Operator, asset_id: str) -> None:
    """删资产（行 + 副本目录）；不存在 404，不归本人 403。不可逆，也没有 TTL 兜底。"""
    row = await _load(session, op, asset_id)
    await session.delete(row)
    await session.commit()
    shutil.rmtree(assets_root() / asset_token_dir(asset_id), ignore_errors=True)
    logger.info(f"[video_assets] 删除资产 {asset_id}")


async def _load(session, op: Operator, asset_id: str) -> VideoAsset:
    row = (await session.execute(
        select(VideoAsset).where(VideoAsset.asset_id == asset_id))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"资产 {asset_id} 不存在")
    if not _can_access(row, op):
        raise AccessDenied("无权访问该资产")
    return row
