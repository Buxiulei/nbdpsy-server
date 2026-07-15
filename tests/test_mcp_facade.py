"""薄 MCP facade 单测:monkeypatch httpx 边界 + get_http_headers,不真起 REST 服务。

facade 每个工具从 MCP 请求头取 apikey → httpx 打本机 REST → 原样回 JSON。这里用假的
httpx.AsyncClient 记录 facade 转发的 method/path/headers/json/params 并按序回放预置响应,
再 monkeypatch get_http_headers 模拟 MCP 连接器带来的 apikey 头,验证:转发路径正确、
apikey 透传、image_urls 映射为 images、REST 非 2xx 错误原样带回、无 apikey 返未认证、
check_cookie 内部轮询到终态 / 超时回 checking。
"""

import app.mcp_facade as facade


class _FakeResponse:
    """最小 httpx.Response 替身:只暴露 facade._forward 用到的 status_code / json() / text。"""

    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("无 JSON body")
        return self._json_data


class _Recorder:
    """记录 facade 每次转发的请求参数,并按序回放预置响应(替代真 httpx.AsyncClient)。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def install(self, monkeypatch):
        recorder = self

        class _FakeAsyncClient:
            def __init__(self, *args, base_url=None, **kwargs):
                self.base_url = base_url

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(
                self, method, path, *, headers=None, json=None, params=None
            ):
                recorder.calls.append(
                    {
                        "method": method,
                        "path": path,
                        "headers": headers,
                        "json": json,
                        "params": params,
                    }
                )
                return recorder.responses.pop(0)

        monkeypatch.setattr(facade.httpx, "AsyncClient", _FakeAsyncClient)
        return self


def _set_headers(monkeypatch, headers):
    """monkeypatch facade 的 get_http_headers 返回给定头(模拟 MCP 连接器 apikey 头)。"""
    monkeypatch.setattr(facade, "get_http_headers", lambda **kwargs: headers)


async def test_publish_note_forwards_image_urls(monkeypatch):
    """publish_note:image_urls 映射为 body.images,转发到 POST /api/publish-jobs,apikey 透传。"""
    _set_headers(monkeypatch, {"authorization": "Bearer k"})
    rec = _Recorder([_FakeResponse(202, {"job_id": 7, "status": "pending"})])
    rec.install(monkeypatch)

    result = await facade.mcp.call_tool(
        "publish_note",
        {
            "account_id": 3,
            "title": "标题",
            "content": "正文",
            "image_urls": ["u1"],
        },
    )

    assert result.structured_content == {"job_id": 7, "status": "pending"}
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/publish-jobs"
    # image_urls 映射为 images,body 不出现 image_urls
    assert call["json"]["images"] == ["u1"]
    assert "image_urls" not in call["json"]
    assert call["json"]["account_id"] == 3
    # apikey 头透传到本机 REST
    assert call["headers"] == {"Authorization": "Bearer k"}


async def test_whoami_and_list_accounts_forward(monkeypatch):
    """whoami / list_accounts 转发到对应 GET 路径并原样回 JSON。"""
    _set_headers(monkeypatch, {"authorization": "Bearer k"})

    rec = _Recorder([_FakeResponse(200, {"name": "root", "role": "admin"})])
    rec.install(monkeypatch)
    result = await facade.mcp.call_tool("whoami", {})
    assert result.structured_content == {"name": "root", "role": "admin"}
    assert rec.calls[0]["method"] == "GET"
    assert rec.calls[0]["path"] == "/api/whoami"

    rec2 = _Recorder([_FakeResponse(200, {"accounts": []})])
    rec2.install(monkeypatch)
    result2 = await facade.mcp.call_tool("list_accounts", {})
    assert result2.structured_content == {"accounts": []}
    assert rec2.calls[0]["method"] == "GET"
    assert rec2.calls[0]["path"] == "/api/accounts"


async def test_rest_error_passthrough(monkeypatch):
    """REST 返 403 {"error":...} → 工具结果原样带回该错误,不吞。"""
    _set_headers(monkeypatch, {"authorization": "Bearer k"})
    rec = _Recorder([_FakeResponse(403, {"error": "无该账号 access"})])
    rec.install(monkeypatch)

    result = await facade.mcp.call_tool("get_publish_status", {"job_id": 1})
    assert result.structured_content == {"error": "无该账号 access"}
    assert rec.calls[0]["path"] == "/api/publish-jobs/1"


async def test_apikey_missing_returns_unauthenticated(monkeypatch):
    """无 authorization / x-api-key 头 → 工具返回未认证错误,且不发起任何 HTTP。"""
    _set_headers(monkeypatch, {})
    rec = _Recorder([])
    rec.install(monkeypatch)

    result = await facade.mcp.call_tool("whoami", {})
    sc = result.structured_content
    assert "error" in sc
    assert "未认证" in sc["error"]
    # 未取到 apikey 不静默转发
    assert rec.calls == []


async def test_check_cookie_polls_to_terminal(monkeypatch):
    """check_cookie:起检拿 check_id → 轮询 checking → valid,回终态 valid。"""
    _set_headers(monkeypatch, {"authorization": "Bearer k"})
    monkeypatch.setattr(facade, "_POLL_INTERVAL_S", 0)
    rec = _Recorder(
        [
            _FakeResponse(202, {"check_id": "c1", "status": "checking"}),
            _FakeResponse(200, {"status": "checking"}),
            _FakeResponse(200, {"status": "valid", "user_info": {"nickname": "n"}}),
        ]
    )
    rec.install(monkeypatch)

    result = await facade.mcp.call_tool("check_cookie", {"account_id": 5})
    assert result.structured_content == {
        "status": "valid",
        "user_info": {"nickname": "n"},
    }
    # 起检 POST + 两次轮询 GET(轮询打到 check_id)
    assert rec.calls[0]["method"] == "POST"
    assert rec.calls[0]["path"] == "/api/accounts/5/cookie-checks"
    assert rec.calls[1]["path"] == "/api/cookie-checks/c1"
    assert rec.calls[2]["path"] == "/api/cookie-checks/c1"


async def test_check_cookie_timeout_returns_checking(monkeypatch):
    """check_cookie:等待预算耗尽仍未出终态 → 回 {status:checking, check_id}。"""
    _set_headers(monkeypatch, {"authorization": "Bearer k"})
    monkeypatch.setattr(facade, "_POLL_TIMEOUT_S", 0)
    rec = _Recorder([_FakeResponse(202, {"check_id": "c2", "status": "checking"})])
    rec.install(monkeypatch)

    result = await facade.mcp.call_tool("check_cookie", {"account_id": 5})
    assert result.structured_content == {"status": "checking", "check_id": "c2"}
    # 仅起检,未进入轮询
    assert len(rec.calls) == 1
