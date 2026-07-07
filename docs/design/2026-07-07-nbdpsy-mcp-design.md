# nbdpsy-mcp 设计文档：小红书运营能力的纯 MCP 后台服务

- 日期：2026-07-07
- 状态：设计待批准
- 源仓库（抽取来源）：`/home/roots/小红书运营工具`
- 目标仓库：`nbdpsy-mcp`（https://github.com/Buxiulei/nbdpsy-mcp.git）

## 1. 背景与目标

现有「小红书运营工具」仓库过重（backend/app ≈ 17.8 万行、110 张表、148 个 config 字段、22 个 AI Agent、Vue 前端 5.3 万行、celery+redis+RAG+本地大模型全家桶）。本项目做一次**超级大瘦身**：只保留小红书**机械操作**核心能力，做成 **MCP 工具**，本机跑后台，别的机器上的 agent 通过 **apikey** 远程连接调用。所有"创作大脑"与前端交互交给远程 agent 自带。

### 保留的核心能力（4 类）

1. **自动发布**：图文笔记发布（标题/正文/图片/话题/可选定时）。
2. **多账号管理**：小红书账号的增删改查。
3. **cookies 管理**：接收插件推送的 cookie、加密落库、有效性检测；**后台共享同一套 cookie**。
4. **远程登录**：交给 chrome 插件在操作者真实浏览器完成（含各种验证），插件把 cookie 推回后台。

外加一层贯穿全局的：

5. **登录/权限管理系统（RBAC）**：管理员创建操作员账号、分配小红书账号使用权。

### 非目标（明确砍掉，不进本仓）

- ~~**笔记数据采集**~~（自有笔记同步 + 关键词搜他人笔记）——**本轮明确舍弃**。
- 全部 AI 能力：内容生成、RAG/知识库、封面/信息图生成、心跳引擎、热点雷达、长尾雷达、OKR、养号互动等。
- Vue 前端、celery、redis、本地大模型/vLLM、Gemini TabPool/真 Chrome/CDP 栈。
- 服务端登录自动化（扫码/滑块/手机号验证脚本）——登录整件事交给插件 + 人工。
- 多租户的 JWT/密码 web 登录——由 apikey RBAC 替代。

## 2. 总体架构

一句话：**单进程 FastAPI + FastMCP（Streamable HTTP）服务，apikey 鉴权，把发布/账号/cookie 做成 MCP 工具，登录交给 chrome 插件，全服务只有一套 sync Camoufox 浏览器栈。**

```
                         ┌─────────────── 本机 MCP 后台（单 uvicorn 进程）───────────────┐
远程 agent(A 机) ──apikey──▶  FastAPI                                                    │
远程 agent(B 机) ──apikey──▶   ├─ /mcp           FastMCP Streamable HTTP（全部 MCP 工具） │
                          │   ├─ /api/cookies/import   插件推 cookie（apikey）          │
                          │   └─ /downloads/extension.zip  插件下载                      │
                          │  apikey 中间件 → 解析 Operator → 逐调用鉴权                    │
                          │                                                              │
                          │  发布队列(asyncio.Queue + per-account 锁 + to_thread)          │
                          │      └─▶ sync Camoufox（Xvfb :99）── xiaohongshu.com          │
                          │  SQLite（operators / access / xhs_accounts / publish_jobs）   │
                          └──────────────────────────────────────────────────────────────┘
操作者本机浏览器 + chrome 插件 ──扫码/过验证/收割 cookie──▶ POST /api/cookies/import
```

关键取舍与被否方案：

- **传输层 = Streamable HTTP（非 stdio）**：远程机器 agent 必须走网络传输，stdio 只能本机同进程。
- **去 celery/redis**：旧仓唯一真用 celery 的是发布调度，而真相源本就是 DB 状态机；单进程用 asyncio 队列 + 内存锁等价替代，甩掉两个重依赖。
- **单套 sync 浏览器栈**：舍弃采集后，Camoufox 只服务发布 + cookie 检测，async 采集栈整包不搬。
- **服务端零登录代码**：小红书登录验证是随机组合拳（手机号/二次扫码/滑块/图形），服务端自动化追不上；交给人 + 插件在真实浏览器过，服务端只收割 cookie。

## 3. 身份与权限（RBAC）

### 两类身份（务必区分）

| 概念 | 含义 | 谁管理 |
|---|---|---|
| **Operator（操作员账号）** | 连接 MCP 的身份，持一把 apikey | 管理员创建/授权/停用 |
| **XHS account（小红书账号）** | cookie + 发布目标 | 管理员/导入者把使用权分配给 Operator |

### 认证

- 每个 Operator 一把 **apikey**（高熵随机串）。库里只存 hash（sha256），创建/轮换时明文**只显示一次**。
- 请求头 `Authorization: Bearer <apikey>`（MCP 端点与插件推送端点统一）。
- **引导管理员**：env `ROOT_ADMIN_APIKEY`。启动时确保存在一个 `role=admin` 的内置 Operator 绑定该 key；admin 用它创建其余 Operator。若未设置则启动时生成一把并写入日志（仅首启一次）。
- apikey 中间件：apikey → 解析 Operator；未知/`enabled=false` → 401。解析出的 Operator 挂到调用上下文，供逐工具鉴权。

### 授权（二元 grant）

- Operator `role`：`admin` | `operator`。
- `admin`：隐式拥有全部小红书账号 + 全部管理员工具。
- `operator`：能用哪些小红书账号由 `operator_account_access` 决定，**二元**——有记录 = 该号可用（发布/管理 cookie 都能做），无记录 = 不可见不可用。**不做能力位细分**。
- 新号归属：**Operator 可自导入**（插件带其 apikey 推入新号 / 调 `import_cookies`），导入者自动获得该号 access；admin 恒可见、可再分配给他人。

### 逐工具鉴权矩阵

| 工具 | 要求 |
|---|---|
| `list_accounts` / `get_account` | 返回/允许 caller 有 access 的号（admin：全部） |
| `update_account` / `delete_account` | caller 对该号有 access（admin：全部） |
| `import_cookies` / `check_cookies` / `get_cookies` | 同上；导入新号则自动建 access |
| `publish_note` / `get_publish_status` / `list_publish_jobs` / `cancel_publish_job` | caller 对目标号有 access（job 也归属于 access） |
| `get_extension_download` / `health` | 任意合法 Operator |
| `create_operator` / `list_operators` / `update_operator` / `delete_operator` / `rotate_operator_apikey` / `grant_account_access` / `revoke_account_access` / `list_operator_grants` | **admin only** |

## 4. MCP 工具面（完整清单）

> 签名为示意；返回体统一含 `ok` 与错误码。发布/cookie 的耗时操作走"入队 + 轮询"。

### 4.1 账号管理

- `list_accounts() -> [{id, name, nickname, user_id, status, cookie_status, last_check_at}]`
- `get_account(account_id) -> {...}`
- `update_account(account_id, {name?, ...}) -> {...}`
- `delete_account(account_id) -> {ok}`

### 4.2 cookie 管理

- `import_cookies(account_name, cookies_json, user_info?) -> {account_id, created: bool}`
  - 同一逻辑也由 HTTP `POST /api/cookies/import` 暴露给插件。
  - sameSite 规范化 → Fernet 加密 → **upsert `xhs_accounts` 唯一一行**（共享 cookie）。
  - 新号：建 `xhs_accounts` + 给导入 Operator 建 access。
- `check_cookies(account_id) -> {status: valid|invalid|captcha, user_info?}`
  - 复用 sync Camoufox：注入 cookie → 开 /explore → `login_detector` 判定 + 校验 user_id → 写回 `cookie_status`/`last_check_at`。
- `get_cookies(account_id) -> {cookies_json}`（解密返回，供调试/迁移；受 access 限制）

### 4.3 发布

- `publish_note(account_id, title, content, images[], topics[], schedule_time?) -> {job_id, status: queued}`
  - `images` 每项为 **http URL 或 base64 数据**；服务端落成本地临时文件再喂 Camoufox。
  - `schedule_time` 省略 = 尽快发；给定 = 到点发。视频 **v1 不支持**。
- `get_publish_status(job_id) -> {status, note_id?, note_url?, error?, retries}`
  - status：`queued|publishing|published|failed|canceled`。
- `list_publish_jobs(account_id?, status?) -> [...]`
- `cancel_publish_job(job_id) -> {ok}`（仅 `queued` 可取消）

### 4.4 插件

- `get_extension_download() -> {download_url, version, apikey, install_steps}`
  - `download_url` 指向 `PUBLIC_BASE_URL/downloads/extension.zip`；`apikey` 为 caller 自己的 key，便于 agent 交付"装好即用"包。

### 4.5 管理员（admin only）

- `create_operator(name, role='operator') -> {operator_id, apikey}`（apikey 只显示这一次）
- `list_operators() -> [{id, name, role, enabled, created_at}]`
- `update_operator(operator_id, {role?, enabled?, name?}) -> {...}`
- `delete_operator(operator_id) -> {ok}`
- `rotate_operator_apikey(operator_id) -> {apikey}`
- `grant_account_access(operator_id, xhs_account_id) -> {ok}`
- `revoke_account_access(operator_id, xhs_account_id) -> {ok}`
- `list_operator_grants(operator_id) -> [xhs_account_id...]`

### 4.6 系统

- `health() -> {ok, version, xvfb: bool, browser: bool}`

## 5. 登录与共享 cookie

### 5.1 插件流程（登录唯一路径）

1. 操作者本机装 chrome 插件（`get_extension_download` 拿包 + apikey）。
2. 插件开隐身窗口进小红书 → **人工完成登录及任何验证**（手机号/二次扫码/滑块/图形，人来过）。
3. 插件用 `chrome.cookies.getAll` + `webRequest.onHeadersReceived` 拦 `Set-Cookie` **补抓 httpOnly**（如 `web_session`）。
4. 插件 `POST /api/cookies/import`（`Authorization: Bearer <operator apikey>`）。
5. 服务端 sameSite 规范化 → 加密 → upsert 共享 cookie 行；新号建 access。

### 5.2 插件适配改造点（从旧仓搬 + 改）

- `serverUrl` 指向 MCP 主机（`PUBLIC_BASE_URL`）。
- 鉴权从 JWT 换成 **Operator apikey**（配置项里填一次）。
- 推送端点统一为 `/api/cookies/import`（旧仓分散的 save-cookies/create-with-cookies 收敛为一个 upsert）。
- 保留：隐身窗口、跨 cookieStore 采集、webRequest 补抓 httpOnly、user_info 采集。

### 5.3 共享 cookie 语义

- cookie 存 `xhs_accounts.login_cookies`（Fernet），**每个小红书账号唯一一行**，不按 Operator 分裂。
- 任何有 access 的 Operator（或其插件）更新 cookie → **写这同一行** → 所有能用该号的 Operator 立即共享最新 cookie。`check_cookies` 刷新出的资料/状态也写这行。

## 6. 发布子系统

### 6.1 抽取集（从旧仓）

- `xhs_publish_atomic_tasks.py`（step1–7 落地层，~2000 行核心，去掉 orchestrator 专用尾部函数）
- `xhs_playwright_client.py` 精简：`__init__`/`start`/`_load_cookies`（双域规范化必带）/`publish_note`/`stop`（互动方法全丢）
- `sync_human_actions.py`（拟人化，反检测核心）
- `fingerprint_factory.py` + `smart_browser/schemas.py`（per-account 稳定指纹）
- `text_formatter.py`（清 Markdown / 显示宽度 / 截断）
- `login_detector.py`（`DETECT_LOGIN_JS`）
- 抽 `profile_guard`（见 §8）供 sync 启动路径调用

### 6.2 执行模型

- sync Playwright **跑在独立线程**（MCP 是 async，sync Camoufox 不能进 event loop）。
- **同账号进程内互斥锁**（Firefox 单写锁，同号并发必挂 "Firefox is already running"），单进程用 `threading.Lock`/`asyncio.Lock`，无需 redis。
- cookie 从 DB 解密注入。
- 图片：URL → httpx 下载临时文件；base64 → 解码临时文件；发布后清理临时文件。

### 6.3 异步队列与状态机（替代 celery）

- lifespan 起**调度协程**：每 30–60s 扫 `publish_jobs` 里 `status=pending 且 (schedule_time 为空或已到期)` → 投 `asyncio.Queue`。
- 一组 worker 协程（数量 = `PUBLISH_CONCURRENCY`）取任务 → 拿 per-account 锁 → `asyncio.to_thread` 跑 sync 发布。
- **DB 状态机为真相源**：`pending → publishing(started_at) → published/failed`；`get_publish_status` 读 DB。
- **启动恢复**：启动时把卡在 `publishing` 且 `started_at` 超 `PUBLISH_JOB_TIMEOUT` 的 job 复位（回 `pending` 或判 `failed`）。
- **重试**：失败按 `PUBLISH_RETRY_SCHEDULE`（默认 2m/10m/30m）×3；耗尽判 `failed`。

### 6.4 必带的历史坑（新仓不可丢）

1. **成功页 3 秒跳转**：`publish_confirmed` 后**立即收口返回**，禁止再进等待循环——否则与 /publish/success→发布页的自动跳转赛跑，误判 failed→重试→**重复发布**。
2. **发布按钮 closed Shadow DOM**：保留 JS 诊断 + PIL 截图按"小红书红"像素求 centroid 定位（DPR 自适应）+ 级联点击 + 点后 `_published()` 验证。
3. **创作中心默认「上传视频」tab**：图文必须先切 tab（JS 文本定位坐标点击 + URL `?type=normal` 兜底），否则 file input 是视频的。
4. **话题上限与匹配**：正文剥 `#` 串（单一来源）+ 去重截断 ≤10 + 下拉精确/完整前缀匹配 + 失败回删，四件套缺一不可（超 10 会被弹窗拦发布）。
5. **note_id 可为空**：返回契约允许 `published` 但 `note_id=""`（仅 `note_url`）。
6. **长度硬限**：标题 20 显示宽（emoji=2），正文安全截断 900 字。
7. **sync 客户端 start/publish/stop 必须同一线程**。

### 6.5 明确丢弃

AI 封面/排版生成、post-publish hooks（助推/回填/Note upsert/engagement）、矩阵策略闸门、`publish_orchestrator`/`policies`/`job`、`auto_publish_service`/`unified_publish_scheduler`/`publish_time_optimizer`、`op_publish`/`publish_schedule` HTTP 层、`SmartLocator` AI 兜底（降级为直接失败）。

## 7. cookie 有效性检测

- **on-demand**：`check_cookies(account_id)` 工具主动触发。
- **可选周期检测**：`COOKIE_CHECK_INTERVAL` **默认 0=关闭**（on-demand 为主）；设为正整数秒时，lifespan 起一个轻量 asyncio 循环按此间隔逐号 `check_cookies` 并写回状态，号间隔 ≥5s 防频控。
- 实现：复用发布用的 sync client 的"启动+注入 cookie+开 /explore+`login_detector` 判定+user_id 校验"路径（把这段从 `start()` 抽成可独立调用的 `check_login`），不引入 async 栈。

## 8. 浏览器地基与部署

- **引擎 = Camoufox**，`requirements.txt` **写死版本 0.4.11**（旧仓漏写，venv 手装过）。
- **Xvfb `:99`**：部署脚本启停 `Xvfb :99 -screen 0 1920x1080x24`；启动 camoufox 强制 `DISPLAY=:99` + `LIBGL_ALWAYS_SOFTWARE=1` + `MOZ_DISABLE_GFX_SANITY_TEST=1` + `MOZ_HEADLESS=1`（NVIDIA+Xvfb 卡死 / glxtest 卡启动修复）。
- **统一 profile 目录**：旧仓 `account_{id}` 与 `camoufox_account_{id}` 分裂，新仓统一为一套（如 `DATA_DIR/browser/account_{id}`），并单独存指纹。
- `profile_guard`（从 `camoufox_helper` 抽）：
  - 启动前清 `lock`/`.parentlock`（残留会死等 180s）。
  - per-account 锁（进程内）。
  - 启动前删 `cookies.sqlite`（持久上下文旧 cookie 会覆盖新注入 → 登成别人号；且 `clear_cookies()` 在持久上下文会挂起，不可用）。
  - 异常时 `pw.stop()` + 精确杀孤儿 camoufox-bin（**argv 精确匹配**，`account_2` 是 `account_20` 前缀，子串匹配会误杀兄弟号）。
  - `proxy=None` 键必须 `pop`（Firefox 把 None 当空代理 → 连接被拒）。
- `CHROME_EXECUTABLE_PATH` **不需要**（那是 Gemini 真 Chrome 用的，Camoufox 不用）。

## 9. 数据模型（4 张表）

```
operators
  id PK, name, apikey_hash(unique), role('admin'|'operator'),
  enabled(bool, default true), created_at, created_by(nullable)

operator_account_access            -- 二元 grant
  id PK, operator_id FK, xhs_account_id FK, granted_by, created_at
  UNIQUE(operator_id, xhs_account_id)

xhs_accounts                        -- 精简至 ~12 列
  id PK, name, nickname, user_id, red_id, avatar,
  status, cookie_status, last_check_at,
  login_cookies(Fernet 加密), last_login_at, created_at

publish_jobs                        -- 替代旧 schedules + generated_contents
  id PK, account_id FK, title, content,
  images_json, topics_json, schedule_time(nullable),
  status('pending'|'publishing'|'published'|'failed'|'canceled'),
  started_at, note_id, note_url, error, retries(int default 0),
  next_retry_at, created_by(operator_id), created_at
```

- Alembic 管理迁移；SQLite（`aiosqlite`）默认，`DATABASE_URL` 可切。
- 旧仓多租户表（`system_users`/`user_account_relation`/`account_matrix_link`）**不搬**，由上面两张 RBAC 表替代。

## 10. 配置（~18 个 env 字段）

`APP_NAME` / `DEBUG` / `LOG_LEVEL` / `LOG_FILE` / `API_HOST` / `API_PORT` / `PUBLIC_BASE_URL` / `DATABASE_URL` / `SECRET_KEY`（Fernet，**原样沿用旧值否则存量 cookie 全废且静默返回空串**）/ `ROOT_ADMIN_APIKEY` / `DATA_DIR` / `UPLOAD_DIR` / `XVFB_DISPLAY` / `PUBLISH_CONCURRENCY` / `PUBLISH_RETRY_SCHEDULE` / `PUBLISH_JOB_TIMEOUT` / `COOKIE_CHECK_INTERVAL` / `DEBUG_SCREENSHOTS_ENABLED`。

配置用 `pydantic-settings`，字段全带默认值；`.env.example` 覆盖所有字段。

## 11. 目录结构

```
nbdpsy-mcp/
  app/
    server.py            # FastAPI + FastMCP 挂载 + apikey 中间件 + lifespan
    core/
      config.py          # pydantic-settings
      security.py        # Fernet encrypt/decrypt + apikey hash
      db.py              # engine/session
    models/              # operators / access / xhs_accounts / publish_jobs
    auth/                # apikey 中间件 + Operator 解析 + 鉴权装饰
    tools/
      accounts.py cookies.py publish.py admin.py extension.py system.py
    http/
      cookies_import.py  # POST /api/cookies/import（插件）
      downloads.py       # GET /downloads/extension.zip
    browser/
      profile_guard.py   # 锁清理/杀孤儿/删 cookies.sqlite
      fingerprint.py     # fingerprint_factory + schemas
      sync_client.py     # 精简 xhs_playwright_client：start/check_login/publish_note/stop
      atomic_tasks.py    # step1-7
      sync_human_actions.py
      login_detector.py
      text_formatter.py
    publish/
      queue.py           # asyncio 队列 + per-account 锁 + worker
      scheduler.py       # 扫表调度协程 + 启动恢复 + 重试
    services/
      cookie_service.py  # import/check/get + 共享语义
      operator_service.py
  chrome-extension/      # 从旧仓搬 + apikey 化
  scripts/               # xvfb 启停 / alembic / 打包 extension.zip
  tests/
  requirements.txt
  .env.example
```

## 12. 从旧仓抽取 / 丢弃清单

**抽取（改造后进新仓）**：`xhs_publish_atomic_tasks`、`xhs_playwright_client`（精简）、`sync_human_actions`、`fingerprint_factory`+`smart_browser/schemas`、`text_formatter`、`login_detector`、`camoufox_helper` 的锁清理逻辑（抽成 `profile_guard`）、`core/security` 的 Fernet、`cookies_import` 的 sameSite 规范化与 upsert 逻辑、`chrome-extension/`。

**丢弃（不搬）**：全部 22 个 AI Agent、RAG/知识库/kb_core、Gemini TabPool/真 Chrome/CDP 全家（~5000 行）、`xhs_automation` 采集栈（4523 行）、`note_sync`/`keyword_research`/`note_cover_localizer`、async `camoufox_helper`/`human_actions`、`smart_locator` 系（AI 定位）、celery/redis/beat、Vue 前端、多租户 JWT/密码、服务端登录三套（`xhs_qrcode_login`/`xhs_login`/`browser_stream`/孤儿 `qrcode_login_service`）、`media_downloader`（死码）。

## 13. 测试策略

- **unit（无账号）**：apikey 中间件与鉴权矩阵、cookie sameSite 规范化、Fernet 往返、`text_formatter` 显示宽/截断、话题去重截断 ≤10、发布状态机流转与启动恢复、`import_cookies` upsert/新号建 access、二元 grant 可见性过滤、`get_extension_download` 返回体、profile_guard 的孤儿 argv 精确匹配（不误杀 account_20）。
- **e2e（真账号，`@slow` 手动跑）**：`import_cookies → check_cookies → publish_note → get_publish_status` 打通；`create_operator → grant_account_access → 该 operator 只见被授权号` 打通。
- 每个测试自带清理（删测试建的 operator/account/job 与临时文件）。

## 14. 部署与运维

- `scripts` 提供 Xvfb 启停 + uvicorn 启动 + alembic upgrade + 打包 extension.zip。
- 反向代理把 `PUBLIC_BASE_URL` 代理到本机 `API_PORT`（MCP 端点 + 插件推送 + 下载同一入口）。
- 改 config/`.env` 后重启进程（pydantic 启动锁定字段）。

## 15. 实现编排（供 writing-plans / opus workflow）

spec 批准后转 `writing-plans` 出计划，再用 **Workflow 多 agent（opus）** 分组并行落地，每组独立 worktree、PR 回 `nbdpsy-mcp`：

1. **地基组**：仓库骨架、`core`（config/security/db）、`models`+alembic、`server.py`+FastMCP 挂载+apikey 中间件、`health`。
2. **RBAC 组**：`operators`/`access` 服务 + 管理员工具 + 鉴权矩阵 + bootstrap admin。
3. **浏览器/发布组**：`profile_guard`+`fingerprint`+`sync_client`+`atomic_tasks`+`sync_human_actions`+发布队列/状态机 + `publish_*` 工具（含全部历史坑）。
4. **cookie/账号组**：`cookie_service`（import/check/get 共享语义）+ 账号工具 + HTTP `/api/cookies/import`。
5. **插件组**：`chrome-extension` apikey 化 + `/downloads` + `get_extension_download` + 打包脚本。
6. **集成组**：串起来 + unit 全绿 + e2e 打通 + `.env.example` + 部署脚本 + README。

## 16. 风险与未决

- **发布走服务器 IP**：账号在服务器 IP + 固定指纹上被操作；若某号对该 IP 敏感可能触发风控（发布中 `SecurityRestrictionError` → 判 failed + 可加冷却）。cookie 虽在住宅浏览器诞生，操作却在服务器——IP 不一致是既有现实，非本设计新增。
- **单进程扩展性**：发布并发被浏览器实例数与账号互斥天然封顶，单进程足够；未来要横向扩展需引入外部队列（暂不做）。
- **FastMCP/mcp SDK 版本 API**：Streamable HTTP + 自定义 apikey 中间件 + 与 FastAPI 共挂的具体写法，实现前用 context7/官网核对最新版本，不臆造。
