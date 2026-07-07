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
        """返回中间件写入上下文的当前运营者 name/role;未认证返回 authenticated=False。

        用途兼诊断与 ContextVar 穿透验证:若中间件在父 FastAPI 上 set 的上下文
        能被挂载在 /mcp 的工具执行读到,则此处返回 authenticated=True。
        """
        # 延迟 import:避免 app.auth 与 app.tools 在包加载期形成循环依赖。
        from app.auth.context import AuthError, current_operator

        try:
            op = current_operator()
        except AuthError:
            return {"authenticated": False}
        return {"authenticated": True, "name": op.name, "role": op.role}
