"""Operator 认证上下文:基于 ContextVar 在单次请求内传递当前运营者。

中间件校验 apikey 成功后 set_current_operator(op),受保护的路由用
current_operator() 读取;未认证时抛 AuthError(由上层转 401)。

ContextVar 而非线程局部:异步单线程下天然按 task 隔离,且能被 asyncio
task 创建时的 copy_context() 继承——这是本方案能穿透中间件→下游路由的前提。
"""

from contextvars import ContextVar, Token

from app.models.operator import Operator


class AuthError(Exception):
    """未认证或认证失败:current_operator() 在无当前运营者时抛出。"""


class AccessDenied(Exception):
    """授权拒绝:已认证但无权执行该操作(RBAC guards 抛出,上层转 403)。

    专用类而非内置 PermissionError——后者是 OSError 子类,会与文件 I/O 等真实
    OS 权限错误语义碰撞,导致 403 处理器误吞掉真因。
    """


# 当前请求的运营者;默认 None 表示尚未认证。
_current_operator: ContextVar[Operator | None] = ContextVar(
    "current_operator", default=None
)


def set_current_operator(op: Operator | None) -> Token:
    """设置当前运营者,返回可用于 reset 的 token(供中间件 finally 复位)。"""
    return _current_operator.set(op)


def reset_current_operator(token: Token) -> None:
    """把 ContextVar 复位到 set 之前的状态,避免请求间上下文泄漏。"""
    _current_operator.reset(token)


def current_operator() -> Operator:
    """读取当前运营者;未认证(上下文为空)时抛 AuthError。"""
    op = _current_operator.get()
    if op is None:
        raise AuthError("未认证:当前请求无运营者上下文")
    return op
