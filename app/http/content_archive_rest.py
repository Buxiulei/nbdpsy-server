"""content-archive 分组 REST(3 端点):内容资产库浏览 / 取用(取即刷新)/ 删除。

设计 docs/superpowers/specs/2026-07-25-content-archive-design.md。发布成功自动归档
(无 REST 建库端点,归档是发布副作用);本组仅消费——全局共享(跨账号复用的前提),
鉴权 operator 皆可浏览/取用,删除限 admin。
"""

from fastapi import APIRouter, Query

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.services import content_archive

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/content-archive",
        "summary": "浏览内容资产库(发布内容归档,跨账号复用)",
        "admin_only": False,
        "params": {"q": "query,str|None(模糊搜标题+标签)", "limit": "query,int=50(≤200)"},
        "returns": "{archives:[{id,title,topics,kind,source_account_id,media_count,"
                   "last_used_at,use_count,created_at}, ...]}",
        "errors": "",
        "notes": "全局共享库:任意授权 operator 可浏览。**浏览列表不刷新**使用时间(逛不算用);"
                 "要取用某条走 GET /api/content-archive/{id}。按最近使用降序。",
    },
    {
        "method": "GET", "path": "/api/content-archive/{archive_id}",
        "summary": "取单条归档完整内容(取即刷新使用时间)",
        "admin_only": False, "params": {"archive_id": "path,int"},
        "returns": "{id,title,content,topics,kind,media_urls:[免鉴权直链],source_note_url,"
                   "source_account_id,last_used_at,use_count}",
        "errors": "404=归档不存在",
        "notes": "**取内容即使用**:刷新 last_used_at=now + use_count+1,续 90 天 TTL。复用发布"
                 "(取文案图片去发)与借鉴参考都走此端点,故都自动续命。media_urls 是图片/视频"
                 "免鉴权公网直链,可直接下载复用或塞进 POST /api/publish-jobs 的 images。",
    },
    {
        "method": "DELETE", "path": "/api/content-archive/{archive_id}",
        "summary": "删除一条归档(行 + 媒体文件),admin only",
        "admin_only": True, "params": {"archive_id": "path,int"},
        "returns": "{ok:true}",
        "errors": "403=非 admin;404=归档不存在",
        "notes": "不可逆:删归档行与其独立媒体目录。日常不需要——超 90 天未使用会自动清理。",
    },
]


@router.get("/api/content-archive")
async def list_content_archive_endpoint(
    q: str | None = None, limit: int = Query(default=50, ge=1, le=200)
) -> dict:
    """浏览内容库摘要(不刷新使用时间);任意授权 operator 可访问。"""
    current_operator()  # 触发鉴权上下文(中间件已校验 apikey)
    async with get_session() as session:
        return {"archives": await content_archive.list_archives(session, q=q, limit=limit)}


@router.get("/api/content-archive/{archive_id}")
async def get_content_archive_endpoint(archive_id: int) -> dict:
    """取单条完整内容 + **刷新使用时间**(取内容=使用,续 TTL)。"""
    current_operator()
    async with get_session() as session:
        entry = await content_archive.get_and_touch(session, archive_id)
    if entry is None:
        raise NotFoundError(f"归档 {archive_id} 不存在")
    return entry


@router.delete("/api/content-archive/{archive_id}")
async def delete_content_archive_endpoint(archive_id: int) -> dict:
    """删除归档(admin only)。"""
    require_admin(current_operator())
    async with get_session() as session:
        ok = await content_archive.delete_archive(session, archive_id)
    if not ok:
        raise NotFoundError(f"归档 {archive_id} 不存在")
    return {"ok": True}
