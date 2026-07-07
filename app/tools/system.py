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
