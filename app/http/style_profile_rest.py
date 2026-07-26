"""style-profile 分组 REST(6 端点):每运营一份风格档案的读 / 写新版 / 列历史 / 取某版 /
回退,外加管理员默认档案的维护入口。

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。消费方是
nbdpsy-skills 全套内容创作 skill(安装引导回读、拆解参考图后固化、对话中更新、回退)。

身份一律取 current_operator()(apikey 由中间件校验后注入),**任何端点都不接受外部
传入的 operator/account id**——档案是个人资产,路径里能传 id 就等于开了越权读的口子。
唯一的例外是管理员默认档案端点:它写的是 operator_id IS NULL 那一行,require_admin 收口。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.db import get_session
from app.services import style_profile

router = APIRouter()

# 历史列表单页上限:超出**直接钳到它**而不是 422——分页参数写大了不该让 skill 侧调用失败
MAX_VERSIONS_LIMIT = 200

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/style-profile",
        "summary": "读当前运营的风格档案(无个人档案时回落管理员默认档案)",
        "admin_only": False, "params": {},
        "returns": "有个人档案 {exists:true,version,base_version,source,note,updated_at,profile};"
                   "无个人档案 {exists:false,source:\"admin_default\",base_version:0,"
                   "admin_default_version,updated_at,profile:管理员那套}",
        "errors": "",
        "notes": "**先看 exists 再说话**:true=这是他自己的档案(回读请他确认);false=他还没有,"
                 "profile 是管理员默认的(要问「先沿用管理员的风格,可以吗?」)。档案按 apikey "
                 "认人,不传也无法传别人的 id。profile 结构见需求文档第四节,server 原样存取"
                 "不校验语义;其中 density 的五个 key 是中文,不要改写成英文。"
                 "**base_version 是下一次 PUT 该传什么的唯一真源**(无档案给 0),直接透传,"
                 "不要自己推。exists:false 时**每次 GET 都实时读到最新的管理员默认档案**"
                 "(不是建档时的快照):管理员一改,所有还没建档的运营立刻跟着变,"
                 "admin_default_version/updated_at 就是给你察觉「默认档案变过了」的;"
                 "而已建个人档案的运营是各自独立的内容,完全不受管理员改动影响。",
    },
    {
        "method": "PUT", "path": "/api/style-profile",
        "summary": "写风格档案新版本(version 自增 + 留档快照,乐观锁)",
        "admin_only": False,
        "params": {"base_version": "body,int(你读到的当前 version;此前 exists:false 传 0)",
                   "profile": "body,object(完整档案,非增量;原样存,≤64KB)",
                   "source": "body,str=manual(manual|reference_sample|inherited_admin)",
                   "note": "body,str|None(本次改动说明,如「按参考样本 8 张实测更新」)"},
        "returns": "{exists:true,version:新版本号,source,note,updated_at,dropped_keys:[...]}",
        "errors": "400=profile 超 64KB(不静默截断);409=base_version 与当前 version 不符,"
                  "detail 带 current_version/updated_at;422=body 不符 schema",
        "notes": "**整份覆盖不是打补丁**:先 GET 拿 profile 改完整体回传。每次成功都 version+1 "
                 "并落一条完整快照(历史长期保存不清理)。409 时别重试同一份 body——运营可能"
                 "在另一个会话改过了,按 detail.current_version 提示他重新 GET 再改。"
                 "source=rollback 由回退端点自己写,PUT 不接受。"
                 "**dropped_keys 是本次覆盖丢掉的键**(相对上一版消失的顶层与二级键,如 "
                 "[\"tone\",\"visual.text_color\"];整段消失只报顶层名,不展开;无丢弃给 [] "
                 "而非省略,可无条件读)。它不是错误——server 不拦不改,但非空就该当场告诉运营"
                 "「这次同时丢了人物卡,是有意的吗?」,否则他要到下次出图才发现,那时已查不出"
                 "是哪次 PUT 弄丢的。只比两层,更深的丢失不报。",
    },
    {
        "method": "GET", "path": "/api/style-profile/versions",
        "summary": "列风格档案历史版本(倒序分页,不含 profile 全文)",
        "admin_only": False,
        "params": {"limit": "query,int=50(上限 200,超出直接钳到 200 不报错)",
                   "offset": "query,int=0"},
        "returns": "{versions:[{version,source,note,created_at,created_by}, ...],"
                   "total,limit,offset,has_more}",
        "errors": "422=limit<1 或 offset<0",
        "notes": "列表刻意做轻:要看某版内容走 GET /api/style-profile/versions/{version}。"
                 "运营说「回到上周那版」时:先列这个让他挑 → 预览 → 回退。"
                 "历史长期保存不清理,高频改风格的运营会攒到几百版,故默认只给最近 50 条:"
                 "has_more 为 true 时按 offset 翻页取更早的(total 是全部版本数)。",
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
        "returns": "{exists:true,version:新版本号,source:\"rollback\",note,updated_at,"
                   "dropped_keys:[...]}",
        "errors": "404=to_version 不存在;409=base_version 不符,detail 带 current_version",
        "notes": "**回退造新版而非拨指针**:回退 v3 会产生 v8(内容=v3),v4–v7 仍在历史里可取——"
                 "所以「回退后又后悔」还能再回退回去。历史永远只增不减。"
                 "dropped_keys 含义同 PUT:这次回退相对回退前少了哪些键(旧版没有的新增字段"
                 "会在这里现形),非空就跟运营确认一句。",
    },
    {
        "method": "PUT", "path": "/api/style-profile/admin-default",
        "summary": "[管理员] 覆盖管理员默认档案(所有未建档运营的实时回落内容)",
        "admin_only": True,
        "params": {"profile": "body,object(完整档案,非增量;原样存,≤64KB)",
                   "note": "body,str|None(本次改动说明)"},
        "returns": "{version:自增后的版本号,source:\"admin_default\",note,updated_at}",
        "errors": "400=profile 超 64KB;403=非管理员;422=body 不符 schema",
        "notes": "改的是 operator_id IS NULL 那一行,**影响所有还没建档的运营**(他们每次 GET "
                 "都实时读它),已建个人档案的运营不受影响。同样是整份覆盖不是打补丁。"
                 "**管理员默认档案不进版本历史表、不可回退**(版本表的 operator_id 是 NOT NULL "
                 "外键,塞 NULL 要改表结构,不值当)——改之前请自行留底,别指望 rollback。"
                 "无乐观锁:管理员改这行是低频独占操作,不设 base_version。",
    },
]


class StyleProfilePut(BaseModel):
    """PUT 请求体。profile 原样存取,server 不校验其语义(仅校大小)。"""

    base_version: int = Field(ge=0, description="你读到的当前 version;无档案时传 0")
    profile: dict = Field(description="完整风格档案(非增量)")
    # source 只收这三种:rollback 是回退端点的产物,不接受客户端自称回退
    source: Literal["manual", "reference_sample", "inherited_admin"] = "manual"
    note: str | None = Field(default=None, max_length=500, description="本次改动说明")


class StyleProfileAdminDefault(BaseModel):
    """管理员默认档案请求体:整份覆盖,profile 同样原样存取(仅校大小),无乐观锁。"""

    profile: dict = Field(description="完整管理员默认档案(非增量)")
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
async def list_style_profile_versions_endpoint(
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """列历史版本(倒序分页,不含 profile 全文);limit 超上限钳到 200 而不是报错。"""
    operator = current_operator()
    capped = min(limit, MAX_VERSIONS_LIMIT)
    async with get_session() as session:
        versions = await style_profile.list_versions(
            session, operator.id, limit=capped, offset=offset
        )
        total = await style_profile.count_versions(session, operator.id)
    return {
        "versions": versions,
        "total": total,
        "limit": capped,
        "offset": offset,
        "has_more": offset + len(versions) < total,
    }


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


@router.put("/api/style-profile/admin-default")
async def put_admin_default_endpoint(payload: StyleProfileAdminDefault) -> dict:
    """[管理员] 覆盖管理员默认档案:影响所有还没建档的运营(他们每次 GET 实时读它)。

    非 admin → AccessDenied(全局 handler 映 403)。这一行不进版本历史表,不可回退。
    """
    operator = current_operator()
    require_admin(operator)
    async with get_session() as session:
        return await style_profile.save_admin_default(
            session,
            profile=payload.profile,
            note=payload.note,
            updated_by=operator.id,
        )
