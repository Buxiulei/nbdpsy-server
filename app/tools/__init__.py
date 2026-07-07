"""MCP 工具注册总入口。

register_all(mcp) 汇总注册各分组工具;此刻只注册 system(health),
后续 Task 各分组在此追加 register_*(mcp) 调用。
"""

from fastmcp import FastMCP

from app.tools.system import register_system


def register_all(mcp: FastMCP) -> None:
    """把所有 MCP 工具注册到给定的 FastMCP 实例上。"""
    register_system(mcp)
