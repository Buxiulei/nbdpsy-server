"""admin 分组 MCP 工具:管理员专属的运营者与账号授权管理面(RBAC 管理端)。

register_admin(mcp) 注册 8 个工具。每个工具的第一步都是
`require_admin(current_operator())`——非 admin 抛 AccessDenied(fastmcp 包成
ToolError 返回)——随后自开独立会话调 operator_service。

create/rotate 返回的明文 apikey 仅本次可见,库内只存 hash;工具响应里带 note 明示。
"""

from fastmcp import FastMCP

from app.auth.context import current_operator
from app.auth.guards import require_admin
from app.core.db import get_session
from app.services import operator_service

# apikey 明文仅本次返回的提示语(库内只存 hash,无法再次读取)
_APIKEY_NOTE = "apikey 仅本次显示,请立即保存;库内只存 hash,无法再次读取"


def register_admin(mcp: FastMCP) -> None:
    """把 admin 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def create_operator(name: str, role: str = "operator") -> dict:
        """[管理员] 新建运营者,返回其信息与一次性明文 apikey。"""
        require_admin(current_operator())
        async with get_session() as session:
            op, apikey = await operator_service.create_operator(
                session, name, role=role
            )
            return {
                "id": op.id,
                "name": op.name,
                "role": op.role,
                "enabled": op.enabled,
                "apikey": apikey,
                "note": _APIKEY_NOTE,
            }

    @mcp.tool
    async def list_operators() -> dict:
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
                        "created_at": (
                            o.created_at.isoformat() if o.created_at else None
                        ),
                    }
                    for o in ops
                ]
            }

    @mcp.tool
    async def update_operator(
        operator_id: int,
        role: str | None = None,
        enabled: bool | None = None,
        name: str | None = None,
    ) -> dict:
        """[管理员] 局部更新运营者 role/enabled/name(留空的字段不改)。"""
        require_admin(current_operator())
        async with get_session() as session:
            op = await operator_service.update_operator(
                session, operator_id, role=role, enabled=enabled, name=name
            )
            return {
                "id": op.id,
                "name": op.name,
                "role": op.role,
                "enabled": op.enabled,
            }

    @mcp.tool
    async def delete_operator(operator_id: int) -> dict:
        """[管理员] 删除运营者并级联清除其账号授权。"""
        require_admin(current_operator())
        async with get_session() as session:
            await operator_service.delete_operator(session, operator_id)
            return {"deleted": operator_id}

    @mcp.tool
    async def rotate_operator_apikey(operator_id: int) -> dict:
        """[管理员] 重置运营者 apikey,旧 key 立即失效,返回一次性明文新 key。"""
        require_admin(current_operator())
        async with get_session() as session:
            apikey = await operator_service.rotate_apikey(session, operator_id)
            return {"id": operator_id, "apikey": apikey, "note": _APIKEY_NOTE}

    @mcp.tool
    async def grant_account_access(operator_id: int, xhs_account_id: int) -> dict:
        """[管理员] 授予运营者对某小红书账号的操作权(幂等);granted_by 记为当前 admin。"""
        admin = current_operator()
        require_admin(admin)
        async with get_session() as session:
            access = await operator_service.grant_access(
                session, operator_id, xhs_account_id, granted_by=admin.id
            )
            return {
                "id": access.id,
                "operator_id": access.operator_id,
                "xhs_account_id": access.xhs_account_id,
            }

    @mcp.tool
    async def revoke_account_access(operator_id: int, xhs_account_id: int) -> dict:
        """[管理员] 回收运营者对某小红书账号的操作权(幂等)。"""
        require_admin(current_operator())
        async with get_session() as session:
            await operator_service.revoke_access(
                session, operator_id, xhs_account_id
            )
            return {
                "operator_id": operator_id,
                "xhs_account_id": xhs_account_id,
                "revoked": True,
            }

    @mcp.tool
    async def list_operator_grants(operator_id: int) -> dict:
        """[管理员] 列出运营者已授权的小红书账号 id。"""
        require_admin(current_operator())
        async with get_session() as session:
            ids = await operator_service.list_grants(session, operator_id)
            return {"operator_id": operator_id, "xhs_account_ids": ids}
