"""system 分组 MCP 工具:health 探活。

register_system(mcp) 把工具注册到给定 FastMCP 实例(装饰器需闭包内的 mcp)。
"""

from fastmcp import FastMCP

from app import __version__


def register_system(mcp: FastMCP) -> None:
    """把 system 分组工具注册到 mcp 实例。"""

    @mcp.tool
    def health() -> dict:
        """MCP 探活工具:返回服务存活标记与应用版本。"""
        return {"ok": True, "version": __version__}

    @mcp.tool
    def whoami() -> dict:
        """确认当前 operator 身份与是否 admin,编排起点。

        返回当前运营者 {authenticated: True, name, role};role=='admin' 表示可见全部账号。
        未认证(缺/错 apikey)返回 {authenticated: False}。典型编排的第一步:先 whoami 确认
        身份与权限,再 list_accounts / publish_note 等。
        """
        # 延迟 import:避免 app.auth 与 app.tools 在包加载期形成循环依赖。
        from app.auth.context import AuthError, current_operator

        try:
            op = current_operator()
        except AuthError:
            return {"authenticated": False}
        return {"authenticated": True, "name": op.name, "role": op.role}
