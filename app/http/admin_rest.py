"""admin 分组 REST(仅 admin 角色):运营者 CRUD / apikey 轮换 / 账号授权。

平移自 app/tools/admin.py 的 8 个 MCP 工具体,service 调用与返回 dict 逐键照抄。
每端点首行 `require_admin(current_operator())`——非 admin 抛 AccessDenied
(server.py 全局 handler 映 403)。

create/rotate 返回的明文 apikey 仅本次可见,库内只存 hash,不可回读。
授权是二元的:有 grant 即可全功能操作该号,无更细粒度的操作类型划分。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.db import get_session
from app.services import operator_service

router = APIRouter()

# apikey 明文仅本次返回的提示语(库内只存 hash,无法再次读取)
_APIKEY_NOTE = "apikey 仅本次显示,请立即保存;库内只存 hash,无法再次读取"


class OperatorCreateRequest(BaseModel):
    name: str
    role: Literal["operator", "admin"] = "operator"


class OperatorUpdateRequest(BaseModel):
    name: str | None = None
    role: Literal["operator", "admin"] | None = None
    enabled: bool | None = None


class GrantRequest(BaseModel):
    xhs_account_id: int


MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/operators",
        "summary": "[管理员] 新建运营者,返回其信息与一次性明文 apikey",
        "admin_only": True, "params": {"name": "body,str", "role": "body,operator|admin,默认 operator"},
        "returns": "{id, name, role, enabled, apikey, note}",
        "errors": "403=非管理员;422=缺 name",
        "notes": "apikey 仅此一次显示,请立即保存;库内只存 hash,无法再次读取。",
    },
    {
        "method": "GET", "path": "/api/operators",
        "summary": "[管理员] 列出全部运营者(不含 apikey)",
        "admin_only": True, "params": {},
        "returns": "{operators: [{id, name, role, enabled, created_at}]}",
        "errors": "403=非管理员",
        "notes": "",
    },
    {
        "method": "PATCH", "path": "/api/operators/{operator_id}",
        "summary": "[管理员] 局部更新运营者 role/enabled/name(留空的字段不改)",
        "admin_only": True, "params": {"operator_id": "path,int", "name": "body,str,可选", "role": "body,operator|admin,可选", "enabled": "body,bool,可选"},
        "returns": "{id, name, role, enabled}",
        "errors": "403=非管理员;404=运营者不存在",
        "notes": "",
    },
    {
        "method": "DELETE", "path": "/api/operators/{operator_id}",
        "summary": "[管理员] 删除运营者并级联清除其账号授权",
        "admin_only": True, "params": {"operator_id": "path,int"},
        "returns": "{deleted: operator_id}",
        "errors": "403=非管理员",
        "notes": "",
    },
    {
        "method": "POST", "path": "/api/operators/{operator_id}/rotate-apikey",
        "summary": "[管理员] 重置运营者 apikey,旧 key 立即失效,返回一次性明文新 key",
        "admin_only": True, "params": {"operator_id": "path,int"},
        "returns": "{id, apikey, note}",
        "errors": "403=非管理员;404=运营者不存在",
        "notes": "apikey 仅此一次显示,请立即保存;库内只存 hash,无法再次读取。",
    },
    {
        "method": "POST", "path": "/api/operators/{operator_id}/grants",
        "summary": "[管理员] 授予运营者对某小红书账号的操作权(幂等)",
        "admin_only": True, "params": {"operator_id": "path,int", "xhs_account_id": "body,int"},
        "returns": "{id, operator_id, xhs_account_id}",
        "errors": "403=非管理员",
        "notes": "授权是二元的:有 grant 即可全功能操作该号。",
    },
    {
        "method": "DELETE", "path": "/api/operators/{operator_id}/grants/{xhs_account_id}",
        "summary": "[管理员] 回收运营者对某小红书账号的操作权(幂等)",
        "admin_only": True, "params": {"operator_id": "path,int", "xhs_account_id": "path,int"},
        "returns": "{operator_id, xhs_account_id, revoked: true}",
        "errors": "403=非管理员",
        "notes": "授权是二元的:有 grant 即可全功能操作该号。",
    },
    {
        "method": "GET", "path": "/api/operators/{operator_id}/grants",
        "summary": "[管理员] 列出运营者已授权的小红书账号 id",
        "admin_only": True, "params": {"operator_id": "path,int"},
        "returns": "{operator_id, xhs_account_ids}",
        "errors": "403=非管理员",
        "notes": "",
    },
]


@router.post("/api/operators")
async def create_operator_endpoint(payload: OperatorCreateRequest) -> dict:
    """[管理员] 新建运营者,返回一次性明文 apikey(库里只存 hash,不可回读)。"""
    require_admin(current_operator())
    async with get_session() as session:
        op, apikey = await operator_service.create_operator(
            session, payload.name, role=payload.role
        )
        return {
            "id": op.id,
            "name": op.name,
            "role": op.role,
            "enabled": op.enabled,
            "apikey": apikey,
            "note": _APIKEY_NOTE,
        }


@router.get("/api/operators")
async def list_operators_endpoint() -> dict:
    """[管理员] 列出全部运营者(不含 apikey)。"""
    require_admin(current_operator())
    async with get_session() as session:
        ops = await operator_service.list_operators(session)
        return {
            "operators": [
                {
                    "id": o.id,
                    "name": o.name,
                    "role": o.role,
                    "enabled": o.enabled,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in ops
            ]
        }


@router.patch("/api/operators/{operator_id}")
async def update_operator_endpoint(
    operator_id: int, payload: OperatorUpdateRequest
) -> dict:
    """[管理员] 局部更新运营者 role/enabled/name(留空的字段不改)。"""
    require_admin(current_operator())
    async with get_session() as session:
        op = await operator_service.update_operator(
            session,
            operator_id,
            role=payload.role,
            enabled=payload.enabled,
            name=payload.name,
        )
        return {"id": op.id, "name": op.name, "role": op.role, "enabled": op.enabled}


@router.delete("/api/operators/{operator_id}")
async def delete_operator_endpoint(operator_id: int) -> dict:
    """[管理员] 删除运营者并级联清除其账号授权。"""
    require_admin(current_operator())
    async with get_session() as session:
        await operator_service.delete_operator(session, operator_id)
        return {"deleted": operator_id}


@router.post("/api/operators/{operator_id}/rotate-apikey")
async def rotate_operator_apikey_endpoint(operator_id: int) -> dict:
    """[管理员] 重置运营者 apikey,旧 key 立即失效,返回一次性明文新 key。"""
    require_admin(current_operator())
    async with get_session() as session:
        apikey = await operator_service.rotate_apikey(session, operator_id)
        return {"id": operator_id, "apikey": apikey, "note": _APIKEY_NOTE}


@router.post("/api/operators/{operator_id}/grants")
async def grant_account_access_endpoint(
    operator_id: int, payload: GrantRequest
) -> dict:
    """[管理员] 授予运营者对某小红书账号的操作权(幂等);granted_by 记为当前 admin。"""
    admin = current_operator()
    require_admin(admin)
    async with get_session() as session:
        access = await operator_service.grant_access(
            session, operator_id, payload.xhs_account_id, granted_by=admin.id
        )
        return {
            "id": access.id,
            "operator_id": access.operator_id,
            "xhs_account_id": access.xhs_account_id,
        }


@router.delete("/api/operators/{operator_id}/grants/{xhs_account_id}")
async def revoke_account_access_endpoint(
    operator_id: int, xhs_account_id: int
) -> dict:
    """[管理员] 回收运营者对某小红书账号的操作权(幂等)。"""
    require_admin(current_operator())
    async with get_session() as session:
        await operator_service.revoke_access(session, operator_id, xhs_account_id)
        return {
            "operator_id": operator_id,
            "xhs_account_id": xhs_account_id,
            "revoked": True,
        }


@router.get("/api/operators/{operator_id}/grants")
async def list_operator_grants_endpoint(operator_id: int) -> dict:
    """[管理员] 列出运营者已授权的小红书账号 id。"""
    require_admin(current_operator())
    async with get_session() as session:
        ids = await operator_service.list_grants(session, operator_id)
        return {"operator_id": operator_id, "xhs_account_ids": ids}
