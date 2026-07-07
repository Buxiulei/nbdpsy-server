"""HTTP(REST)端点包:承载 chrome 插件推送等非 MCP 的 HTTP 接口。

各端点在 server.create_app 里 include_router 挂到父 FastAPI;鉴权由 apikey 中间件统一
承担(端点路径不在白名单 → 自动受保护),端点内用 current_operator() 读取当前运营者。
"""
