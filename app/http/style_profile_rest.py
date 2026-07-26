"""style-profile 分组 REST(5 端点):每运营一份风格档案的读 / 写新版 / 列历史 / 取某版 / 回退。

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。消费方是
nbdpsy-skills 全套内容创作 skill(安装引导回读、拆解参考图后固化、对话中更新、回退)。

身份一律取 current_operator()(apikey 由中间件校验后注入),**任何端点都不接受外部
传入的 operator/account id**——档案是个人资产,路径里能传 id 就等于开了越权读的口子。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.core.db import get_session
from app.services import style_profile

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/style-profile",
        "summary": "读当前运营的风格档案(无个人档案时回落管理员默认档案)",
        "admin_only": False, "params": {},
        "returns": "有个人档案 {exists:true,version,source,note,updated_at,profile};"
                   "无个人档案 {exists:false,source:\"admin_default\",profile:管理员那套}",
        "errors": "",
        "notes": "**先看 exists 再说话**:true=这是他自己的档案(回读请他确认);false=他还没有,"
                 "profile 是管理员默认的(要问「先沿用管理员的风格,可以吗?」)。档案按 apikey "
                 "认人,不传也无法传别人的 id。profile 结构见需求文档第四节,server 原样存取"
                 "不校验语义;其中 density 的五个 key 是中文,不要改写成英文。",
    },
    {
        "method": "PUT", "path": "/api/style-profile",
        "summary": "写风格档案新版本(version 自增 + 留档快照,乐观锁)",
        "admin_only": False,
        "params": {"base_version": "body,int(你读到的当前 version;此前 exists:false 传 0)",
                   "profile": "body,object(完整档案,非增量;原样存,≤64KB)",
                   "source": "body,str=manual(manual|reference_sample|inherited_admin)",
                   "note": "body,str|None(本次改动说明,如「按参考样本 8 张实测更新」)"},
        "returns": "{exists:true,version:新版本号,source,note,updated_at}",
        "errors": "400=profile 超 64KB(不静默截断);409=base_version 与当前 version 不符,"
                  "detail 带 current_version/updated_at;422=body 不符 schema",
        "notes": "**整份覆盖不是打补丁**:先 GET 拿 profile 改完整体回传。每次成功都 version+1 "
                 "并落一条完整快照(历史长期保存不清理)。409 时别重试同一份 body——运营可能"
                 "在另一个会话改过了,按 detail.current_version 提示他重新 GET 再改。"
                 "source=rollback 由回退端点自己写,PUT 不接受。",
    },
    {
        "method": "GET", "path": "/api/style-profile/versions",
        "summary": "列风格档案历史版本(倒序,不含 profile 全文)",
        "admin_only": False, "params": {},
        "returns": "{versions:[{version,source,note,created_at,created_by}, ...]}",
        "errors": "",
        "notes": "列表刻意做轻:要看某版内容走 GET /api/style-profile/versions/{version}。"
                 "运营说「回到上周那版」时:先列这个让他挑 → 预览 → 回退。",
    },
    {
        "method": "GET", "path": "/api/style-profile/versions/{version}",
        "summary": "取某一版的完整 profile(回退前预览)",
        "admin_only": False, "params": {"version": "path,int"},
        "returns": "{version,source,note,created_at,created_by,profile}",
        "errors": "404=该版本不存在(或不是你的档案)",
        "notes": "只读历史快照,不改当前档案;给运营确认「要回到的是不是这一版」。",
    },
    {
        "method": "POST", "path": "/api/style-profile/rollback",
        "summary": "回退到某一版(以该版内容创建新版本)",
        "admin_only": False,
        "params": {"to_version": "body,int(要回到哪一版)",
                   "base_version": "body,int(你读到的当前 version)"},
        "returns": "{exists:true,version:新版本号,source:\"rollback\",note,updated_at}",
        "errors": "404=to_version 不存在;409=base_version 不符,detail 带 current_version",
        "notes": "**回退造新版而非拨指针**:回退 v3 会产生 v8(内容=v3),v4–v7 仍在历史里可取——"
                 "所以「回退后又后悔」还能再回退回去。历史永远只增不减。",
    },
]


class StyleProfilePut(BaseModel):
    """PUT 请求体。profile 原样存取,server 不校验其语义(仅校大小)。"""

    base_version: int = Field(ge=0, description="你读到的当前 version;无档案时传 0")
    profile: dict = Field(description="完整风格档案(非增量)")
    # source 只收这三种:rollback 是回退端点的产物,不接受客户端自称回退
    source: Literal["manual", "reference_sample", "inherited_admin"] = "manual"
    note: str | None = Field(default=None, max_length=500, description="本次改动说明")


class StyleProfileRollback(BaseModel):
    """回退请求体:to_version 是要回到的版本,base_version 参与乐观锁。"""

    to_version: int = Field(ge=1, description="要回到哪一版")
    base_version: int = Field(ge=0, description="你读到的当前 version")


def _conflict(exc: style_profile.VersionConflict) -> HTTPException:
    """乐观锁冲突 → 409,体内带当前 version 与 updated_at(skill 侧据此提示重新读取)。"""
    return HTTPException(
        status_code=409,
        detail={
            "error": str(exc),
            "current_version": exc.current_version,
            "updated_at": exc.updated_at,
        },
    )


@router.get("/api/style-profile")
async def get_style_profile_endpoint() -> dict:
    """读当前运营的档案;无个人档案则返管理员默认档案 + exists=false。"""
    operator = current_operator()
    async with get_session() as session:
        return await style_profile.get_profile(session, operator.id)


@router.put("/api/style-profile")
async def put_style_profile_endpoint(payload: StyleProfilePut) -> dict:
    """写新版本:version 自增 + 落快照;base_version 不符 → 409。"""
    operator = current_operator()
    async with get_session() as session:
        try:
            return await style_profile.save_profile(
                session,
                operator.id,
                base_version=payload.base_version,
                profile=payload.profile,
                source=payload.source,
                note=payload.note,
            )
        except style_profile.VersionConflict as exc:
            raise _conflict(exc) from exc


@router.get("/api/style-profile/versions")
async def list_style_profile_versions_endpoint() -> dict:
    """列历史版本(倒序,不含 profile 全文)。"""
    operator = current_operator()
    async with get_session() as session:
        return {"versions": await style_profile.list_versions(session, operator.id)}


@router.get("/api/style-profile/versions/{version}")
async def get_style_profile_version_endpoint(version: int) -> dict:
    """取某一版完整 profile;不存在(或不属于本人)→ 404。"""
    operator = current_operator()
    async with get_session() as session:
        return await style_profile.get_version(session, operator.id, version)


@router.post("/api/style-profile/rollback")
async def rollback_style_profile_endpoint(payload: StyleProfileRollback) -> dict:
    """回退:以 to_version 的内容创建新版本(source=rollback);乐观锁同样生效。"""
    operator = current_operator()
    async with get_session() as session:
        try:
            return await style_profile.rollback(
                session,
                operator.id,
                to_version=payload.to_version,
                base_version=payload.base_version,
            )
        except style_profile.VersionConflict as exc:
            raise _conflict(exc) from exc
