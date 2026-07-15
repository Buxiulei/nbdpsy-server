"""薄 MCP facade:7 工具把 MCP 请求透传到本机 REST,facade 零业务逻辑。

claude.ai 网页/App 只能经 MCP 连接器接入本服务;本模块就是那层薄壳:每个工具从 MCP
请求头取 apikey(Authorization / X-API-Key)→ httpx 打 http://127.0.0.1:{API_PORT} 的
REST 端点 → 原样把 JSON 回给调用方。**REST 是唯一真源**:facade 不碰 DB、不复制端点逻辑、
不加缓存/重试,只做转发与 apikey 透传。工具集刻意只暴露运营侧要用的 7 个(不含建号/授权等
admin 能力)。

挂载方(server.py,由另一 task 负责)会 import 本模块的 `mcp` 实例,经 mcp.http_app() 挂到 /mcp。
"""

import asyncio
import time

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from app.core.config import settings

# 本机 REST 基址:直连本进程回环端口,绕开公网隧道(apikey 透传后由 REST 中间件鉴权)。
_BASE_URL = f"http://127.0.0.1:{settings.API_PORT}"

# check_cookie 内部轮询参数:每 _POLL_INTERVAL_S 秒轮一次,总预算 _POLL_TIMEOUT_S 秒仍未出
# 终态就放弃等待、回 {status:checking, check_id}(留给调用方后续自行凭 check_id 继续查),
# 避免单次工具调用阻塞超过 300s。
_POLL_INTERVAL_S = 3.0
_POLL_TIMEOUT_S = 250.0

mcp = FastMCP("nbdpsy")


def _apikey_headers() -> dict | None:
    """从 MCP 请求头取 apikey,构造转发给本机 REST 的鉴权头;取不到返回 None。

    get_http_headers 默认剔除 authorization,必须显式 include 才拿得到。优先用
    Authorization: Bearer,回退 X-API-Key(与 REST 中间件 _extract_apikey 的两种取法对齐)。
    """
    headers = get_http_headers(include={"authorization", "x-api-key"})
    auth = headers.get("authorization")
    if auth and auth.strip():
        return {"Authorization": auth}
    x_api_key = headers.get("x-api-key")
    if x_api_key and x_api_key.strip():
        return {"X-API-Key": x_api_key}
    return None


async def _forward(method: str, path: str, *, json=None, params=None) -> dict:
    """把请求原样转发到本机 REST 并回其 JSON;facade 的唯一转发通道。

    无 apikey → 直接回未认证错误(不静默发无鉴权请求)。非 2xx → 回
    {"error": REST 的 error/detail 文案或原始 text}(不吞掉 REST 的 401/403/404 语义)。
    """
    api_headers = _apikey_headers()
    if api_headers is None:
        return {
            "error": "未认证:MCP 请求缺少 apikey(请在连接器配置 Authorization 或 X-API-Key)"
        }
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        response = await client.request(
            method, path, headers=api_headers, json=json, params=params
        )
    if response.status_code // 100 != 2:
        body = response.json()
        return {"error": body.get("error") or body.get("detail") or response.text}
    return response.json()


@mcp.tool
async def whoami() -> dict:
    """返回当前 apikey 对应的运营者身份 {name, role},用于轻量校验连接是否有效。"""
    return await _forward("GET", "/api/whoami")


@mcp.tool
async def list_accounts() -> dict:
    """列出当前 apikey 可操作的小红书账号 {accounts:[...]}(不含 cookie 明文)。

    publish_note / check_cookie 需要的 account_id 从这里取。
    """
    return await _forward("GET", "/api/accounts")


@mcp.tool
async def publish_note(
    account_id: int,
    title: str,
    content: str,
    image_urls: list[str],
    topics: list[str] | None = None,
    schedule_time: str | None = None,
) -> dict:
    """向小红书公开平台发布一条真实图文笔记(写操作)。

    这是对外公开发布真实内容的写操作,调用前必须先向用户确认发布意图与文案,不要擅自发布。
    图片只接受可访问的 http(s) URL 列表(image_urls),**绝不接受 base64**——先把图片传成 URL
    再调本工具。异步语义:本工具入队后立即返回 {job_id, status:"pending"},不代表已发布成功;
    拿到 job_id 后请轮询 get_publish_status(job_id) 直到 published/failed,不要干等。topics 是
    话题标签列表(可选),schedule_time 是定时发布时刻的 ISO8601 串(可选,建议带时区偏移;
    不传即立即入队)。
    """
    body = {
        "account_id": account_id,
        "title": title,
        "content": content,
        # image_urls 映射为 REST 端点的 images 字段(每项为可访问 URL 字符串)。
        "images": image_urls,
        "topics": topics or [],
        "schedule_time": schedule_time,
    }
    return await _forward("POST", "/api/publish-jobs", json=body)


@mcp.tool
async def get_publish_status(job_id: int) -> dict:
    """轮询某发布任务的状态(配合 publish_note 的异步契约)。

    返回 {job_id, account_id, title, status, note_id, note_url, error, retries, ...}。
    status 五态:pending(排队中)/publishing(发布中,常态 1-3 分钟)/published(成功,有 note_url)/
    failed(重试耗尽的终态,error 给原因)/canceled。建议每 5-10s 轮一次直到 published/failed。
    """
    return await _forward("GET", f"/api/publish-jobs/{job_id}")


@mcp.tool
async def list_publish_jobs(
    account_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict:
    """列出发布任务 {jobs:[...]},可按 account_id / status 过滤,limit 控制返回条数(默认 20)。

    status 可选值:pending|publishing|published|failed|canceled(传非法值 REST 会返回错误)。
    """
    params: dict = {"limit": limit}
    if account_id is not None:
        params["account_id"] = account_id
    if status is not None:
        params["status"] = status
    return await _forward("GET", "/api/publish-jobs", params=params)


@mcp.tool
async def check_cookie(account_id: int) -> dict:
    """检测某账号登录态(cookie)是否有效,内部轮询到终态后一次性返回结果。

    发起检测约 20-40s,本工具内部每隔几秒轮询一次直到出终态:valid(登录有效,附 user_info)/
    invalid(已失效,需人重新扫码登录)/captcha(被验证码拦截,需人工处理)/error(浏览器起不来
    等基础设施失败,不代表 cookie 失效)。若超过内部等待预算仍未出结果,返回 {status:"checking",
    check_id},调用方可稍后自行凭 check_id 继续查。
    """
    started = await _forward("POST", f"/api/accounts/{account_id}/cookie-checks")
    if "error" in started:
        return started
    check_id = started.get("check_id")
    if not check_id:
        return started
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        result = await _forward("GET", f"/api/cookie-checks/{check_id}")
        if "error" in result:
            return result
        if result.get("status") != "checking":
            return result
        await asyncio.sleep(_POLL_INTERVAL_S)
    return {"status": "checking", "check_id": check_id}


@mcp.tool
async def get_extension_info() -> dict:
    """返回 chrome 插件包信息 {download_url, version, apikey_hint, install_steps, server_time}。

    用于指导操作者安装浏览器插件、扫码登录小红书账号(远程登录闭环的起点)。
    """
    return await _forward("GET", "/api/extension")
