"""video-assets 分组 REST（4 端点）+ 资产产物免鉴权直链：长期镜头资产库。

需求 ``NBDpsy/文档/2026-08-06-server需求-制片级四缺口（单价估算-资产库-预算护栏-段帧提取）.md``
第三节。形态比照 ``content_archive_rest``（转存 = 独立副本 + /uploads 直链）与
``dreamina_rest``（``_can_access`` 归属 + HMAC token 目录防穿越），服务层在
``app/services/video_assets.py``。

三条取舍：

1. **转存是显式动作，不自动全存**。抽卡产物绝大多数是废片，自动全存等于拿磁盘换噪音；
   资产不设 TTL，存进来就一直占盘，所以只有运营审片后挑中的那条才进库。
2. **拷贝而非指针**。源 clip 到期由 ClipReaper 连目录删掉，指针型资产是假长期。
3. **幂等回原资产而不是报错**。同一条 clip 重复转存回原 asset_id（``deduplicated=true``），
   零新副本——重放不该刷爆磁盘。改名/改标签走「删了重存」，本组不做 PATCH。

router + MANIFEST_ENTRIES 接线 ``app.http.__init__``（漏接会被 tests/test_manifest.py 逮住）。
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.auth.context import current_operator
from app.core.config import settings
from app.core.db import get_session
from app.services import video_assets

router = APIRouter()

# ── /uploads/video-assets 直链的防路径穿越白名单（形态与 dreamina_rest 同款）──
# 目录段 = {asset_id}-{hmac16}（services.video_assets.asset_token_dir）；文件名恒定。
_TOKEN_DIR_RE = re.compile(r"^va_[0-9a-f]{10}-[0-9a-f]{16}$")
_NAME_RE = re.compile(r"^asset\.mp4$")


MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/video-assets",
        "summary": "把一条已完成的片段转存为**不过期**的长期资产",
        "admin_only": False,
        "params": {"clip_id": "body,str(源片段句柄 vc_*)",
                   "name": "body,str(1-120 字,资产名)",
                   "tags": "body,str[]=[](≤20 条,每条≤32 字)"},
        "returns": "{asset_id, name, tags, video_url(不过期直链), source_clip_id, "
                   "source_operation, source_model, source_prompt, duration, size_bytes, "
                   "created_by, created_at, deduplicated}",
        "errors": "400=片段未完成/产物已过期被清理;403=该片段不归你;404=片段不存在;"
                  "422=入参结构非法",
        "notes": "转存 = **拷一份独立副本**存长期目录,源片段到期被 ClipReaper 删掉也不影响。"
                 "**显式动作,不自动全存**:资产不设 TTL,存进来就一直占盘,请只存审片挑中的"
                 "好镜头(角色定妆那条、光线绝佳的空镜)。幂等:同一 clip 重复转存回原资产且"
                 "``deduplicated=true``,不产生第二份副本、也**不改名**——改名请删了重存。",
    },
    {
        "method": "GET", "path": "/api/video-assets",
        "summary": "列出/检索自己的长期视频资产",
        "admin_only": False,
        "params": {"tag": "query,str|None(标签精确匹配)",
                   "q": "query,str|None(模糊搜 名称/标签/源提示词)",
                   "limit": "query,int=50(≤200)"},
        "returns": "{assets:[{同 POST 返回,不含 deduplicated}]}",
        "errors": "401=apikey 无效",
        "notes": "按归属:只列自己转存的(admin 全见)。tag 与 q 可叠加。按创建时间倒序。",
    },
    {
        "method": "GET", "path": "/api/video-assets/{asset_id}",
        "summary": "取单条资产详情 + 不过期直链",
        "admin_only": False, "params": {"asset_id": "path,str(va_*)"},
        "returns": "{asset_id, name, tags, video_url, source_*, duration, size_bytes, "
                   "created_by, created_at}",
        "errors": "403=资产不归你;404=资产不存在",
        "notes": "video_url 是免鉴权公网直链(相对路径,拼 manifest 的 base_url 即得完整链),"
                 "**不过期**——除非你自己 DELETE。可直接下载复用。"
                 "**今天还不能直接塞进 POST /api/video-clips 的 images[]**:那条参数只收图片"
                 "(有魔数白名单,mp4 一律 4xx),这是安全闸不会为资产库放宽。要拿资产当参考,"
                 "现在走 GET /api/video-clips/{clip_id}/frame 从**尚未过期的源片段**抽一张 "
                 "png 再进 images[](注意那张 png 落在 clip 工作目录,跟着 clip 的 TTL 走);"
                 "等即梦 multimodal2video 的 videos[] 开放后本直链可直接当视频参考输入。",
    },
    {
        "method": "DELETE", "path": "/api/video-assets/{asset_id}",
        "summary": "删除一条资产(行 + 文件副本)",
        "admin_only": False, "params": {"asset_id": "path,str(va_*)"},
        "returns": "{ok:true}",
        "errors": "403=资产不归你;404=资产不存在",
        "notes": "**不可逆且无 TTL 兜底**:资产库不会自动清理,盘满了只能靠这个端点。"
                 "删除后直链立即 404。",
    },
]


class StoreAssetRequest(BaseModel):
    """转存入参。"""

    clip_id: str = Field(min_length=1, max_length=24)
    name: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        """纯空白不算名字——资产列表里一行空名等于这条素材从此找不回来。"""
        if not v.strip():
            raise ValueError("name 不能为空白")
        return v


@router.post("/api/video-assets", status_code=201)
async def store_video_asset(req: StoreAssetRequest) -> dict:
    """把一条 done 的片段转存成长期资产（拷贝副本）；重复转存回原资产。

    幂等命中时状态码仍是 201（与 POST /api/video-clips 的重放回 202 同款取舍：
    调用方按 ``deduplicated`` 区分，不必为重放单记一个状态码）。
    """
    op = current_operator()
    async with get_session() as session:
        payload, created = await video_assets.store_clip(
            session, op, clip_id=req.clip_id, name=req.name, tags=req.tags)
    return {**payload, "deduplicated": not created}


@router.get("/api/video-assets")
async def list_video_assets(
    tag: str | None = None, q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """列出自己的资产（admin 全见），可按 tag 精确筛 / q 模糊搜。"""
    op = current_operator()
    async with get_session() as session:
        return {"assets": await video_assets.list_assets(
            session, op, tag=tag, q=q, limit=limit)}


@router.get("/api/video-assets/{asset_id}")
async def get_video_asset(asset_id: str) -> dict:
    """取单条资产详情 + 不过期直链。"""
    op = current_operator()
    async with get_session() as session:
        return await video_assets.get_asset(session, op, asset_id)


@router.delete("/api/video-assets/{asset_id}")
async def delete_video_asset(asset_id: str) -> dict:
    """删除自己的资产（行 + 文件副本）。"""
    op = current_operator()
    async with get_session() as session:
        await video_assets.delete_asset(session, op, asset_id)
    return {"ok": True}


@router.get("/uploads/video-assets/{token_dir}/{name}")
async def serve_asset_product(token_dir: str, name: str) -> FileResponse:
    """取回资产副本 MP4（白名单免鉴权：HMAC token 目录即访问控制，与 clip 产物同款）。

    ``/uploads`` 前缀在鉴权中间件白名单内，故本路由免 apikey——不可猜的
    ``{asset_id}-{hmac16}`` 目录名（SECRET_KEY 派生）承担访问控制。
    正则白名单 + resolve/is_relative_to 双保险挡路径穿越；非文件 404。
    """
    if not _TOKEN_DIR_RE.fullmatch(token_dir) or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="资源不存在")
    root = (Path(settings.DATA_DIR) / "uploads" / "video-assets").resolve()
    file_path = (root / token_dir / name).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(file_path, media_type="video/mp4")
