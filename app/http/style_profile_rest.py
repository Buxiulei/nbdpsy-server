"""style-profile 分组 REST:每运营**多套**风格档案 —— 每套独立当前态 + 独立版本链。

需求 /home/roots/NBDpsy/文档/2026-07-28-server需求-风格档案多套独立版本链.md;
设计 docs/design-2026-07-28-profile-sets.md(§9 评审修订为准)。消费方是 nbdpsy-skills
全套内容创作 skill(安装引导回读、拆解参考图后固化、对话中更新、回退、多套管理)。

身份一律取 current_operator()(apikey 由中间件校验后注入),**任何端点都不接受外部
传入的 operator/account id**——档案是个人资产。例外是管理员默认档案:
- ``/admin-default`` 两端点碰 operator_id IS NULL 那些行:**写**由 require_admin 收口,**读**不设门。
- 套管理四端点(/sets、/sets/{name})带 ``?scope=admin-default`` 时作用于 NULL 行(B2):
  写侧 require_admin,读侧(GET /sets)不设门(客户端守卫依赖)。

兼容性铁律(需求 §4.4 / 设计 §9):不带 ``?set`` 的全部端点行为与今天一致(is_active 那套);
响应体**字段只增不减**(新增 set/kind 是增字段)。``base_version``/409 语义完全不变。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

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
        "summary": "读当前运营某套风格档案(不带 set 读 is_active 套;无个人档案回落管理员默认)",
        "admin_only": False,
        "params": {"set": "query,str|None(套名;不带 → is_active 那套)"},
        "returns": "有个人档案 {exists:true,version,base_version,source,note,set,kind,updated_at,profile};"
                   "无个人档案 {exists:false,source:\"admin_default\",base_version:0,set,kind,"
                   "admin_default_version,updated_at,profile:管理员默认 is_active 套}",
        "errors": "404=运营已有档案但无此套(运营无任何档案时不 404,回落默认)",
        "notes": "**先看 exists 再说话**:true=他自己的档案;false=他还没有,profile 是管理员默认的。"
                 "**base_version 是下一次 PUT 该传什么的唯一真源**(无档案给 0),直接透传。"
                 "多套:不带 set 恒读 is_active 那套(与老客户端逐字节一致);set/kind 告诉你读的是哪套。"
                 "profile 原样存取不校验语义,density 五个中文 key 不要改写成英文。"
                 "exists:false 时每次 GET 都实时读到最新的管理员默认 is_active 套。",
    },
    {
        "method": "PUT", "path": "/api/style-profile",
        "summary": "写某套风格档案新版本(version 自增 + 留档快照,乐观锁)",
        "admin_only": False,
        "params": {"set": "query,str|None(套名;不带 → is_active 套;运营 0 套时自动建 图文 套写 v1)",
                   "base_version": "body,int(你读到的该套当前 version;首写传 0)",
                   "profile": "body,object(完整单套档案,非增量;原样存,≤64KB)",
                   "source": "body,str=manual(manual|reference_sample|inherited_admin)",
                   "note": "body,str|None(本次改动说明)"},
        "returns": "{exists:true,version:新版本号,source,note,set,kind,updated_at,dropped_keys:[...]}",
        "errors": "400=profile 超 64KB / 检测到 profiles-v1 容器(工具包过旧);"
                  "404=带 set 但该套不存在(建套走 POST /sets);"
                  "409=base_version 与该套当前 version 不符,detail 带 current_version/updated_at;422=body 不符 schema",
        "notes": "**整份覆盖不是打补丁**:先 GET 拿该套 profile 改完整体回传;base_version 是**该套自己的**。"
                 "多套后 dropped_keys 回到套内部字段级(visual 整段弄没 = [\"visual\"])。"
                 "两套各自 PUT base_version 互不干扰,不再假冲突。"
                 "**别把 profiles-v1 多套容器整份 PUT 回来**(会 400):升级工具包后按套逐个 PUT。"
                 "source=rollback 由回退端点自己写,PUT 不接受。",
    },
    {
        "method": "GET", "path": "/api/style-profile/versions",
        "summary": "列某套历史版本(倒序分页,不含 profile 全文)",
        "admin_only": False,
        "params": {"set": "query,str|None(套名;不带 → is_active 套)",
                   "limit": "query,int=50(上限 200,超出直接钳到 200 不报错)",
                   "offset": "query,int=0"},
        "returns": "{versions:[{version,source,note,created_at,created_by}, ...],total,limit,offset,has_more}",
        "errors": "404=带 set 但该套不存在;422=limit<1 或 offset<0",
        "notes": "每套独立版本链:列的是该套(不带 set 即 is_active 套)的历史。要看某版内容走 "
                 "GET /api/style-profile/versions/{version}?set=。历史长期保存不清理,默认给最近 50 条。",
    },
    {
        "method": "GET", "path": "/api/style-profile/versions/{version}",
        "summary": "取某套某一版的完整 profile(回退前预览)",
        "admin_only": False,
        "params": {"version": "path,int", "set": "query,str|None(套名;不带 → is_active 套)"},
        "returns": "{version,source,note,created_at,created_by,set,kind,profile}",
        "errors": "404=该套不存在或该版本不存在(或不是你的档案)",
        "notes": "只读历史快照,不改当前档案;给运营确认「要回到的是不是这一版」。",
    },
    {
        "method": "POST", "path": "/api/style-profile/rollback",
        "summary": "回退某套到某一版(以该版内容创建新版本,不影响其他套)",
        "admin_only": False,
        "params": {"set": "query,str|None(套名;不带 → is_active 套)",
                   "to_version": "body,int(要回到哪一版)",
                   "base_version": "body,int(你读到的该套当前 version)"},
        "returns": "{exists:true,version:新版本号,source:\"rollback\",note,set,kind,updated_at,dropped_keys:[...]}",
        "errors": "404=该套不存在或 to_version 不存在;409=base_version 不符,detail 带 current_version",
        "notes": "**只退这一套**(不再连带其它套)。回退造新版而非拨指针:回退 v3 会产生 v8(内容=v3),"
                 "v4–v7 仍在历史里可取。dropped_keys 含义同 PUT。",
    },
    {
        "method": "GET", "path": "/api/style-profile/admin-default",
        "summary": "读管理员默认档案某套(改它之前的留底手段;可多套)",
        "admin_only": False,
        "params": {"set": "query,str|None(套名;不带 → 管理员默认 is_active 套)"},
        "returns": "{profile,admin_default_version,updated_at,set,kind}",
        "errors": "404=带 set 但该管理员默认套不存在",
        "notes": "**PUT /api/style-profile/admin-default 之前先拉一份存起来**:那些行不进版本历史表、"
                 "改了不可回退。非管理员也能读(不设 admin 门):内容与未建档运营 GET 到的相同,无敏感性。"
                 "管理员默认可多套(GET/POST /sets?scope=admin-default 管理);"
                 "库里还没有那些行时返回内置常量、版本给 0、updated_at 给 null。",
    },
    {
        "method": "PUT", "path": "/api/style-profile/admin-default",
        "summary": "[管理员] 覆盖管理员默认档案某套(所有未建档运营的实时回落内容)",
        "admin_only": True,
        "params": {"set": "query,str|None(套名;不带 → 管理员默认 is_active 套)",
                   "profile": "body,object(完整单套档案,非增量;原样存,≤64KB)",
                   "note": "body,str|None(本次改动说明)"},
        "returns": "{version:自增后的版本号,source:\"admin_default\",note,set,kind,updated_at}",
        "errors": "400=profile 超 64KB / profiles-v1 容器;403=非管理员;404=带 set 但该套不存在;422=body 不符 schema",
        "notes": "改的是 operator_id IS NULL 那套,**影响所有还没建档的运营**;整份覆盖不是打补丁。"
                 "**管理员默认档案不进版本历史表、不可回退**——改之前请自行留底。无乐观锁(低频独占)。",
    },
    {
        "method": "GET", "path": "/api/style-profile/sets",
        "summary": "列该运营(或管理员默认)全部套",
        "admin_only": False,
        "params": {"scope": "query,str=self(self|admin-default;admin-default 作用于管理员默认 NULL 行,读不设门)"},
        "returns": "{sets:[{name,kind,is_active,version,updated_at}, ...]}",
        "errors": "",
        "notes": "多套总览:name 是套名、kind 是套类型(carousel 图文 / typeset 文字版)、is_active 是当前默认套。"
                 "scope=admin-default 列管理员默认那几套(客户端 onboarding 据此按默认套数为新运营建套)。"
                 "**server 不自动为运营预建档案**:运营首套由客户端 onboarding 时 POST /sets(from 复制默认各套)建。",
    },
    {
        "method": "POST", "path": "/api/style-profile/sets",
        "summary": "新建一套(v1 快照;from 可复制某套当前 profile)",
        "admin_only": False,
        "params": {"scope": "query,str=self(admin-default 作用于管理员默认 NULL 行,写需 admin)",
                   "name": "body,str(套名;非空、trim、禁 / ? 、≤20 显示宽度、允许中文;同 owner 内唯一)",
                   "kind": "body,str=carousel(carousel|typeset;建套后不可改)",
                   "profile": "body,object=(完整单套档案,≤64KB;from 提供时忽略此字段)",
                   "from": "body,str|None(复制来源套名;不存在 → 404)"},
        "returns": "{name,kind,is_active,version:1,updated_at}",
        "errors": "400=name 非法 / profile 超 64KB / profiles-v1 容器;403=scope=admin-default 非管理员;"
                  "404=from 指向的套不存在;409=套名已存在",
        "notes": "首套自动成 is_active;之后新建套默认非 is_active,设默认走 PATCH。kind 建套后不可改。",
    },
    {
        "method": "PATCH", "path": "/api/style-profile/sets/{name}",
        "summary": "改套名 / 设为默认套(可原子同时给)",
        "admin_only": False,
        "params": {"scope": "query,str=self(admin-default 作用于管理员默认 NULL 行,写需 admin)",
                   "name": "path,str(要改的套名)",
                   "new_name": "body,str|None(新套名;规则同建套,重名 → 409)",
                   "is_active": "body,bool|None(true=设为默认套,事务内把旧默认置 False;false 会被拒 400)"},
        "returns": "{name,kind,is_active,version,updated_at}",
        "errors": "400=new_name 非法 / is_active=false 单发(不能把套设为非默认);403=scope=admin-default 非管理员;"
                  "404=该套不存在;409=new_name 已存在",
        "notes": "设默认恒有且仅一个:对目标套 is_active=true 即可,旧默认自动让位。rename 后旧套名 404。",
    },
    {
        "method": "DELETE", "path": "/api/style-profile/sets/{name}",
        "summary": "删除一套(历史随套删;剩一套拒绝)",
        "admin_only": False,
        "params": {"scope": "query,str=self(admin-default 作用于管理员默认 NULL 行,写需 admin)",
                   "name": "path,str(要删的套名)"},
        "returns": "{deleted:套名}",
        "errors": "403=scope=admin-default 非管理员;404=该套不存在;409=只剩一套,不能删到清空",
        "notes": "**删套=该套历史一并删**(不可恢复)。删的是当前默认套时,事务内自动把另一套设为默认。"
                 "至少保留一套:剩一套时再删返 409。",
    },
]


class StyleProfilePut(BaseModel):
    """PUT 请求体。profile 原样存取,server 不校验其语义(仅校大小 + B5 容器哨兵)。"""

    base_version: int = Field(ge=0, description="你读到的该套当前 version;无档案时传 0")
    profile: dict = Field(description="完整单套风格档案(非增量)")
    # source 只收这三种:rollback 是回退端点的产物,不接受客户端自称回退
    source: Literal["manual", "reference_sample", "inherited_admin"] = "manual"
    note: str | None = Field(default=None, max_length=500, description="本次改动说明")


class StyleProfileAdminDefault(BaseModel):
    """管理员默认档案请求体:整份覆盖,profile 同样原样存取(仅校大小 + 哨兵),无乐观锁。"""

    profile: dict = Field(description="完整单套管理员默认档案(非增量)")
    note: str | None = Field(default=None, max_length=500, description="本次改动说明")


class StyleProfileRollback(BaseModel):
    """回退请求体:to_version 是要回到的版本,base_version 参与乐观锁。"""

    to_version: int = Field(ge=1, description="要回到哪一版")
    base_version: int = Field(ge=0, description="你读到的该套当前 version")


class StyleProfileSetCreate(BaseModel):
    """建套请求体。``from`` 是 Python 关键字,用 alias 收(可传 from 或 from_name)。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="套名(非空、trim、禁 / ?、≤20 显示宽度、允许中文)")
    kind: str = Field(default="carousel", description="套类型 carousel|typeset(建套后不可改)")
    profile: dict = Field(default_factory=dict, description="完整单套档案(from 提供时忽略)")
    from_name: str | None = Field(
        default=None, alias="from", description="复制来源套名(可选)"
    )


class StyleProfileSetPatch(BaseModel):
    """改套请求体:new_name 改名 / is_active=true 设默认(两者可同时给,原子生效)。"""

    new_name: str | None = Field(default=None, description="新套名(可选)")
    is_active: bool | None = Field(
        default=None, description="true=设为默认套;false 会被拒(不能把套设为非默认)"
    )


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


def _scope_owner(operator, scope: str, *, write: bool) -> int | None:
    """套管理端点的 owner 解析:scope=admin-default → NULL 行(写需 admin);否则调用者本人。"""
    if scope == "admin-default":
        if write:
            require_admin(operator)
        return None
    return operator.id


@router.get("/api/style-profile")
async def get_style_profile_endpoint(
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """读当前运营某套档案(不带 set 读 is_active 套);无个人档案则回落管理员默认 + exists=false。"""
    operator = current_operator()
    async with get_session() as session:
        return await style_profile.get_profile(session, operator.id, set_name=set_name)


@router.put("/api/style-profile")
async def put_style_profile_endpoint(
    payload: StyleProfilePut,
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """写某套新版本:version 自增 + 落快照;base_version 不符 → 409。"""
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
                set_name=set_name,
            )
        except style_profile.VersionConflict as exc:
            raise _conflict(exc) from exc


@router.get("/api/style-profile/versions")
async def list_style_profile_versions_endpoint(
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """列某套历史版本(倒序分页,不含 profile 全文);limit 超上限钳到 200 而不是报错。"""
    operator = current_operator()
    capped = min(limit, MAX_VERSIONS_LIMIT)
    async with get_session() as session:
        versions = await style_profile.list_versions(
            session, operator.id, limit=capped, offset=offset, set_name=set_name
        )
        total = await style_profile.count_versions(
            session, operator.id, set_name=set_name
        )
    return {
        "versions": versions,
        "total": total,
        "limit": capped,
        "offset": offset,
        "has_more": offset + len(versions) < total,
    }


@router.get("/api/style-profile/versions/{version}")
async def get_style_profile_version_endpoint(
    version: int,
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """取某套某一版完整 profile;套/版本不存在(或不属于本人)→ 404。"""
    operator = current_operator()
    async with get_session() as session:
        return await style_profile.get_version(
            session, operator.id, version, set_name=set_name
        )


@router.post("/api/style-profile/rollback")
async def rollback_style_profile_endpoint(
    payload: StyleProfileRollback,
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """回退某套:以 to_version 的内容创建新版本(source=rollback);乐观锁同样生效。"""
    operator = current_operator()
    async with get_session() as session:
        try:
            return await style_profile.rollback(
                session,
                operator.id,
                to_version=payload.to_version,
                base_version=payload.base_version,
                set_name=set_name,
            )
        except style_profile.VersionConflict as exc:
            raise _conflict(exc) from exc


@router.get("/api/style-profile/admin-default")
async def get_admin_default_endpoint(
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """读管理员默认档案某套(改它之前的留底手段);**不设 admin 门**。"""
    async with get_session() as session:
        return await style_profile.get_admin_default(session, set_name=set_name)


@router.put("/api/style-profile/admin-default")
async def put_admin_default_endpoint(
    payload: StyleProfileAdminDefault,
    set_name: str | None = Query(default=None, alias="set"),
) -> dict:
    """[管理员] 覆盖管理员默认档案某套:影响所有还没建档的运营。非 admin → 403。"""
    operator = current_operator()
    require_admin(operator)
    async with get_session() as session:
        return await style_profile.save_admin_default(
            session,
            profile=payload.profile,
            note=payload.note,
            updated_by=operator.id,
            set_name=set_name,
        )


@router.get("/api/style-profile/sets")
async def list_sets_endpoint(
    scope: Literal["self", "admin-default"] = Query(default="self"),
) -> dict:
    """列该运营(scope=self)或管理员默认(scope=admin-default)全部套;读不设 admin 门。"""
    operator = current_operator()
    owner_id = _scope_owner(operator, scope, write=False)
    async with get_session() as session:
        return {"sets": await style_profile.list_sets(session, owner_id)}


@router.post("/api/style-profile/sets")
async def create_set_endpoint(
    payload: StyleProfileSetCreate,
    scope: Literal["self", "admin-default"] = Query(default="self"),
) -> dict:
    """新建一套(v1 快照);scope=admin-default 需 admin;重名 → 409、from 不存在 → 404、name 非法 → 400。"""
    operator = current_operator()
    owner_id = _scope_owner(operator, scope, write=True)
    async with get_session() as session:
        return await style_profile.create_set(
            session,
            owner_id,
            name=payload.name,
            kind=payload.kind,
            profile=payload.profile,
            from_name=payload.from_name,
            updated_by=operator.id,
        )


@router.patch("/api/style-profile/sets/{name}")
async def patch_set_endpoint(
    name: str,
    payload: StyleProfileSetPatch,
    scope: Literal["self", "admin-default"] = Query(default="self"),
) -> dict:
    """改套名 / 设默认;scope=admin-default 需 admin;套不存在 → 404、重名 → 409、is_active=false → 400。"""
    operator = current_operator()
    owner_id = _scope_owner(operator, scope, write=True)
    async with get_session() as session:
        return await style_profile.rename_or_activate_set(
            session,
            owner_id,
            name=name,
            new_name=payload.new_name,
            is_active=payload.is_active,
            updated_by=operator.id,
        )


@router.delete("/api/style-profile/sets/{name}")
async def delete_set_endpoint(
    name: str,
    scope: Literal["self", "admin-default"] = Query(default="self"),
) -> dict:
    """删套(历史随套删);scope=admin-default 需 admin;套不存在 → 404、剩一套 → 409。"""
    operator = current_operator()
    owner_id = _scope_owner(operator, scope, write=True)
    async with get_session() as session:
        return await style_profile.delete_set(session, owner_id, name=name)
