"""GET /api/manifest —— 服务自描述:agent 带 apikey 调这一个接口即获全部上手信息。

组成:服务叙事(workflows/constraints,平移自原 MCP_INSTRUCTIONS)+ 错误契约 +
全部端点元数据(聚合各 router 模块的 MANIFEST_ENTRIES)+ caller 身份与权限摘要。
端点集合与实际路由的一致性由 tests/test_manifest.py 防漂移测试钉死。
"""

from fastapi import APIRouter
from sqlalchemy import func, select

from app import __version__
from app.auth.context import current_operator
from app.auth.guards import visible_account_ids
from app.core.config import settings
from app.core.db import get_session
from app.models.xhs_account import XhsAccount

router = APIRouter()

_DESCRIPTION = (
    "nbdpsy-api:小红书运营能力后台(自动发布 / 多账号管理 / cookie 管理 / 远程登录)。"
    "消费方是运营侧 agent:先读本 manifest,再按 endpoints 直接调 REST。"
)

_AUTH = {
    "scheme": "每个 /api/* 请求带请求头 Authorization: Bearer <apikey>(或 X-API-Key: <apikey>)",
    "whitelist": "/healthz 与 /downloads/* 免鉴权",
    "errors": "401 见 error_contract;403=越权(动了没授权的账号或非 admin 调管理端点)",
}

_WORKFLOWS = [
    "接入自检:GET /healthz(通)→ GET /api/manifest(200 即 key 有效,响应含你的身份与可操作账号数)。",
    "远程登录(没有登录接口,登录靠人 + chrome 插件):GET /api/extension 拿 server_time + 插件下载地址 + "
    "安装步骤,把插件递给操作者装好扫码;之后每 ~10s GET /api/login/poll?since=<server_time> 直到 "
    "done=true——登新号不传 account_id,重登旧号传 account_id;建议设 5-10 分钟超时。",
    "cookie 活性:POST /api/accounts/{id}/cookie-checks 发起(202 回 check_id),每 2-5s "
    "GET /api/cookie-checks/{check_id} 轮询到 valid/invalid/captcha/error;error 是基础设施失败,"
    "不代表 cookie 失效。别用它探登录进度——等登录用 /api/login/poll。",
    "发布:POST /api/publish-jobs(202 回 job_id)→ 每 5-10s GET /api/publish-jobs/{job_id} 轮询到 "
    "published/failed;publishing 常态 1-3 分钟,失败自动重试最多 3 次(退避约 2/10/30 分钟),"
    "单条任务最长约 40 分钟才落 failed。同一账号的发布自动串行。",
    "典型编排:manifest → GET /api/accounts →(操作者用插件登录,login/poll 收口)→ cookie-checks 验活 "
    "→ publish-jobs → 轮询终态。",
]

_CONSTRAINTS = [
    "发布仅支持图文:图片 ≥1 且 ≤18 张(越界立即 400);不支持视频。",
    "标题按显示长度截断 ≤20、正文截断 ≤900、话题去重后截断 ≤10——长度类均静默硬截断不报错,请自行控长。",
    "schedule_time 定时发布务必带时区偏移(如 2026-01-01T09:00:00+08:00);不带偏移按 UTC 解释,会早/晚 8 小时。",
    "图片三形态:http(s) URL 字符串 / data URI 字符串 / {b64, ext} 对象;服务端自行下载/解码。",
    "RBAC:非 admin 只能看到/操作被授权的账号;admin 全见。",
]

_ERROR_CONTRACT = {
    "400": '{"error": ...} 入参非法(图片张数越界、status 枚举错、since 非 ISO8601 等)',
    "401": '{"detail": ...} apikey 缺失/无效/运营者被停用(中间件层,注意键是 detail)',
    "403": '{"error": ...} 越权(没授权的账号 / 非 admin 调管理端点)',
    "404": '{"error": ...} 资源不存在(账号/任务/运营者/check_id)',
    "409": '{"detail": ...} 状态冲突,当前状态下该操作不允许(运行中重试/删、修订未完成的成片;'
           '注意键是 detail——HTTPException 走 Starlette 默认体,与 401/422 一致)',
    "422": '{"detail": [...]} 请求体不符合 schema(FastAPI 校验)',
    "500": '{"error": ...} 未预期异常,联系管理员查日志',
}

MANIFEST_ENTRIES = [{
    "method": "GET", "path": "/api/manifest",
    "summary": "本自描述接口:服务叙事 + 全部端点元数据 + caller 身份",
    "admin_only": False, "params": {},
    "returns": "{service, version, description, base_url, auth, caller, workflows, constraints, error_contract, endpoints}",
    "errors": "401=apikey 缺失/无效/停用",
    "notes": "接入后第一站,一次拿全上手信息。",
}]


@router.get("/api/manifest")
async def manifest() -> dict:
    """服务自描述 + caller 身份(须鉴权:验 key 与上手一步完成)。"""
    # 延迟导入聚合表,避免与 app.http 包 __init__ 循环导入。
    from app.http import ALL_MANIFEST_ENTRIES

    op = current_operator()
    async with get_session() as session:
        ids = await visible_account_ids(op, session)
        if ids is None:  # admin:全量账号数
            account_count = (
                await session.execute(select(func.count()).select_from(XhsAccount))
            ).scalar_one()
        else:
            account_count = len(ids)
    return {
        "service": "nbdpsy-api",
        "version": __version__,
        "description": _DESCRIPTION,
        "base_url": settings.PUBLIC_BASE_URL,
        "auth": _AUTH,
        "caller": {
            "operator_id": op.id,
            "name": op.name,
            "role": op.role,
            "account_count": account_count,
        },
        "workflows": _WORKFLOWS,
        "constraints": _CONSTRAINTS,
        "error_contract": _ERROR_CONTRACT,
        "endpoints": ALL_MANIFEST_ENTRIES,
    }
