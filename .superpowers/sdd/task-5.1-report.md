# Task 5.1 报告:收官集成(lifespan + 部署脚本 + README + e2e 冒烟)

状态:**完成**。整个纯 MCP 后台服务端到端可运行。base=`8de334e`。

---

## 产出清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/browser/cookie_checker.py` | 新增 | `CookieChecker` 后台巡检类(可选周期 cookie 检测) |
| `app/server.py` | 修改 | lifespan 接入可选 cookie 巡检(默认关闭) |
| `scripts/xvfb.sh` | 新增 | Xvfb 启停(start/stop/status,幂等) |
| `scripts/run.sh` | 新增 | alembic → 打包插件 → uvicorn 工厂启动 |
| `README.md` | 新增 | 架构/启动/apikey/工具清单/连接/部署/坑 |
| `tests/e2e/__init__.py` | 新增 | e2e 包标记 |
| `tests/e2e/test_smoke.py` | 新增 | RBAC 链(不 slow)+ 发布链(slow/skip) |
| `tests/test_cookie_checker.py` | 新增 | CookieChecker 行为 + lifespan 开关 |
| `tests/test_env_example.py` | 新增 | `.env.example` 覆盖 Settings 全字段 |

`.env.example` 经核对已覆盖全部 18 个 Settings 字段,无需补充(由 `test_env_example.py` 断言守护)。

---

## Step 1:lifespan 可选周期 cookie 检测

- 新建 `CookieChecker`(`app/browser/cookie_checker.py`),设计对齐既有 `PublishScheduler`:
  注入 `session_factory`、`stop_event` + 后台 task、优雅 `stop`。
- `check_once` 选 `cookie_status='valid'` 的号,逐个解密 cookie → `asyncio.to_thread`
  跑 `sync_client.check_login_once` → 三态写回 `cookie_status`/`last_check_at` + 回填资料;
  号间隔 `account_gap`(默认 5s)防频控;无 cookie 的号跳过(不误改状态、不计入)。
- `app/server.py` lifespan 在 `settings.COOKIE_CHECK_INTERVAL > 0` 时才 `start()`,
  shutdown 干净 `stop()`。**默认 0 不起该循环,测试/CI 完全不受影响**。
- 为什么另起独立类而非复用 `check_cookies` MCP 工具:该工具第一步 `current_operator()`
  需要请求上下文,后台系统任务没有;且现有工具测试 monkeypatch 的是 `cookies_mod.sync_client`,
  改造工具去共享会破坏其锚点。独立类各自持 `sync_client` 引用,可独立 monkeypatch,
  与代码库既有小重复约定(`_decrypt_account_cookies` 在 cookies.py/scheduler.py 各一份)一致。

## Step 2:部署脚本

- `scripts/xvfb.sh`:`start|stop|status`,从 `XVFB_DISPLAY` 取 display(默认 :99),
  `1920x1080x24`;pgrep 带尾随空格避免 `:9` 误匹配 `:99`;实测启停幂等生命周期通过。
- `scripts/run.sh`:`xvfb.sh start`(失败仅告警)→ `.venv/bin/alembic upgrade head`
  → `pack_extension.sh` → `exec .venv/bin/uvicorn app.server:create_app --factory`;
  `HOST`/`PORT` 取 `API_HOST`/`API_PORT`;全程 `.venv/bin/`。
- 两脚本 `bash -n` 语法检查通过;`alembic upgrade head` 与 `pack_extension.sh` 组件已独立跑通。

## Step 3/6:测试与回归

- 新增 7 条 not-slow 测试全绿;全量 `pytest -m "not slow"` = **186 passed, 1 deselected**。
- e2e:`tests/e2e` = 1 passed(RBAC 链)+ 1 skipped(发布链,需真 cookie)。

## Step 4:e2e 冒烟

- **RBAC 链(不 slow,纯 DB/工具链)**:`create_operator` → 建两号 → `grant_account_access`
  其一 → operator `list_accounts` 只见被授权号 + 越权号 `get_account` 抛 `ToolError`。
  走真实 `register_all` + `mcp.call_tool`,隔离库,验证管理面 + 访问收窄自洽。
- **发布链(slow,需真号)**:`import_cookies` → `check_cookies`(断言 valid)→ `publish_note`
  → 轮询 `get_publish_status` 直到 `published`。由 `NBDPSY_E2E_COOKIES` 环境变量开关,
  未配则 `skipif` 跳过,**绝不阻塞 CI**;手动跑需 Xvfb + 真 cookie,产出的测试笔记需自行清理。

## Step 5:README

架构总览(纯 MCP/单进程 FastAPI+FastMCP/apikey RBAC/去 celery)、目录、启动步骤、
apikey 与首个管理员、插件配置、**22 个 MCP 工具分组签名清单**、远程 agent 连接(Streamable
HTTP `/mcp/` + Bearer)、部署(反代 `PUBLIC_BASE_URL`→`API_PORT`)、坑(`SECRET_KEY` 不能换 /
Xvfb / Camoufox profile 锁 / 发布走服务器 IP / 改码重启)。全中文,无 emoji。

---

## Concerns / 待核对

1. **发布链 e2e 从未真跑**:标 slow + 默认 skip,逻辑走查正确但未用真号验证过端到端发布;
   首次真跑时留意 `check_cookies` 是否稳定返回 valid、发布轮询超时窗口(当前 ~2 分钟)是否够。
2. **cookie 巡检 valid-only 语义**:只巡 `cookie_status='valid'` 的号——号一旦被判 invalid
   就不再被自动复检(需人工重新导入 cookie 或调 `check_cookies`)。这是刻意取舍(避免对已失效号
   反复起浏览器),若需要"invalid 号也定期重试"可后续放开筛选条件。
3. **`datetime.utcnow()` DeprecationWarning**:沿用代码库既有写法(scheduler/cookies 一致),
   未顺手改(surgical);全库统一迁移到 `datetime.now(UTC)` 属独立清理项。
