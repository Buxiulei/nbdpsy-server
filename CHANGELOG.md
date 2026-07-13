# Changelog

## 0.3.0 (2026-07-13)

两个新特性:发布流程选择器自愈、账号笔记数据采集。

- **选择器自愈(默认关)**:发布流程硬编码 CSS 选择器全失败时,LLM(Qwen/DashScope 文本)
  看页面精简 DOM 指认正确元素并用它,学到的稳定选择器持久化(`data/selector_registry.json`)
  下次直接命中,自我维护。`_find_element_with_retry` 收口 + 6 输入点 + step7 发布按钮兜底;
  bbox 同一性校验 + 发布按钮文案校验双防线防误点/毒化 registry;registry 进程级单例 + 原子写。
  默认 `SELFHEAL_ENABLED=False` + 空 `LLM_API_KEY` 强制关,关闭时发布流程字节等价。开启需
  `.env` 配 `SELFHEAL_ENABLED=true` + `LLM_API_KEY`(+ 可选 `LLM_BASE_URL`/`LLM_MODEL`)后 restart。
- **账号笔记数据采集**:移植创作中心 Excel 导出——同步 Camoufox 登录创作中心 → 数据看板 →
  内容分析 → 导出 Excel → openpyxl 解析,拿每条已发布笔记(含手工发布历史)的 11 项指标
  (点赞/收藏/评论/弹幕/分享/转载/涨粉/封面点击率/曝光/观看量/人均观看时长)。落库为最新快照
  `note_metrics` + 每日趋势 `note_metrics_daily` 两表(按账号+标题+发布时间存,无 note_id/封面 URL)。
  3 个 REST 端点:`POST /api/accounts/{id}/note-exports`(202 异步触发)、
  `GET /api/note-exports/{export_id}`(轮询)、`GET /api/accounts/{id}/notes`(读快照 /
  `?trend=daily` 读日序列)。导出任务照 cookie 巡检 ephemeral 台账 + account_locks 同号串行。
  新增依赖 `openpyxl`。**部署须先 `alembic upgrade head` 再 restart**(lifespan create_all 会抢建表)。

## 0.2.1 (2026-07-13)

仓库更名 `nbdpsy-mcp` → `nbdpsy-server`(MCP 已在 0.2.0 移除,旧名不再贴切;仓库
新地址 https://github.com/Buxiulei/nbdpsy-server.git,服务对外名 `nbdpsy-api` 不变)。

- 修复:补广谱 `Exception` 异常处理器,兑现 `GET /api/manifest` error_contract 声明的
  `500 → {"error": ...}`。此前未预期异常落 Starlette 默认 `text/plain "Internal Server Error"`,
  会让"照 manifest 统一 `resp.json()["error"]`"的 agent 消费方在 500 路径 JSONDecodeError;
  兜底不回显内部细节(真异常落 loguru),精确类分派(401/403/404/400)不受影响。
- 新增(chrome 插件 v2.0.4):账号卡片「检测」按钮——调 `POST /api/accounts/{id}/cookie-checks`
  起后端验活并在弹窗轮询到终态(有效/失效/验证/异常),`error` 态标注"非 cookie 失效"不误伤。
- 修复(chrome 插件 v2.0.4):`chrome.windows.create({incognito:true})` 在未授予无痕权限时返回
  `null`,原先直接读 `.id` 报 `Cannot read properties of null` 天书;两处开窗补 null 守卫,
  改为中文指引(去 `chrome://extensions` 开启"在无痕模式下启用")。
- 文档:新增 `docs/onboarding/admin-provisioning-guide.md`——给管理后台 agent 的管理端运维指南
  (admin 账号来源/鉴权/建运营/授权/停用轮换/开户流程,含实测 curl)。

## 0.2.0 (2026-07-13)

**BREAKING:** MCP 接入方式作废。`/mcp/` 端点已彻底删除(返回 404),`fastmcp` 依赖移除,
`app/tools/`(MCP 工具)、`.claude-plugin/`、`plugins/`(Claude Code 插件 marketplace)全部删除。
远程 agent 必须改走纯 REST:`Authorization: Bearer <apikey>` 带同一把 apikey 调
`GET /api/manifest` 一次性拿到全部端点契约 + 工作流叙事 + 错误契约 + caller 身份,
按 manifest 返回的 `endpoints` 直接调对应 REST 端点(不再需要 `tools/list` 自解释)。

- 新增:`GET /api/manifest` 自描述接口(Task 1),以及 24 个 REST 端点覆盖此前全部 MCP 工具能力
  (system/manifest/accounts/admin/cookies/cookie-checks/extension/publish 八组)。
- 新增:`tests/test_manifest.py` 防漂移测试——manifest 声明的端点集合与实际注册路由双向全等。
- 新增:`tests/test_mcp_removed.py` 回归钉——`/mcp/` 返回 404、`app/` 不再引用 `fastmcp`。
- 删除:`app/server.py` 里的 FastMCP 装配(`FastMCP` 实例、`MCP_INSTRUCTIONS`、
  `combine_lifespans`、`app.mount("/mcp", ...)`);`FastAPI` title 由 `nbdpsy-mcp` 改为 `nbdpsy-api`。
- 删除:MCP 工具测试(`test_admin_tools.py`/`test_account_tools.py`/`test_publish_tools.py`)及
  各测试文件里的 MCP 专用用例,等价覆盖已平移到对应 REST 测试文件。
- 文档:README/`docs/onboarding/operator-config-package.md`/`docs/DEPLOY.md` 全部重写为 REST 版,
  删除 Claude Code 插件 marketplace 安装方式与相关探针。
