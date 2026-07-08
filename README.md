# nbdpsy-mcp

小红书矩阵账号运营的**纯 MCP 后台服务**。远程 AI agent 通过 MCP 协议调用工具完成
账号托管、cookie 共享、笔记发布;人不直接用 UI,登录只交给一个 chrome 插件。

---

## 架构总览

一句话:**单进程 FastAPI + FastMCP(Streamable HTTP)服务,apikey 鉴权,把发布/
账号/cookie 做成 MCP 工具,登录交给 chrome 插件,全服务只有一套 sync Camoufox 浏览器栈。**

```
远程 agent ──(MCP over Streamable HTTP, Bearer apikey)──▶  /mcp/
chrome 插件 ──(HTTP, Bearer apikey)──────────────────────▶  /cookies/import
                                        │
                        单进程 FastAPI + FastMCP
                        ├─ apikey 中间件(RBAC 上下文)
                        ├─ MCP 工具面(账号/cookie/发布/插件/管理员)
                        ├─ 发布队列(asyncio.Queue + per-account 锁 + to_thread)
                        │     └─▶ sync Camoufox(Xvfb :99)── xiaohongshu.com
                        └─ 可选 cookie 周期巡检(COOKIE_CHECK_INTERVAL>0 才起)
```

设计取舍:

- **去 celery / redis**:发布调度的真相源本就是 DB 状态机,单进程用 asyncio 队列 +
  内存 per-account 锁等价替代,甩掉两个重依赖。发布状态机 `pending → publishing →
  published | failed`,失败按 `PUBLISH_RETRY_SCHEDULE` 退避重试,进程重启自动恢复
  僵死 job。
- **单套 sync 浏览器栈**:只保留发布 + cookie 检测用的 sync Camoufox,采集/AI 栈整包不搬。
- **apikey + 二元 RBAC**:每个运营者(operator)一把 apikey;管理员(admin)全见,
  普通 operator 只能操作被 `grant_account_access` 授权的号。cookie 每个小红书账号**唯一
  一行**(共享 cookie),不按 operator 分裂。
- **登录外置**:服务端不做登录,chrome 插件把用户已登录的 cookie 推到 `/cookies/import`。

---

## 目录结构

```
app/
  server.py            # create_app():FastAPI + FastMCP 装配 + lifespan
  core/                # config(Settings) / db(async SQLAlchemy) / security(Fernet + apikey hash)
  auth/                # apikey 中间件 / ContextVar 运营者上下文 / RBAC guards / bootstrap root
  models/              # operator / operator_account_access / xhs_account / publish_job
  services/            # operator_service / account_service / cookie_service(纯业务层)
  tools/               # MCP 工具:system / admin / accounts / cookies / publish / extension
  http/                # REST:cookies_import(插件推 cookie) / downloads(插件包下载)
  browser/             # sync_client(Camoufox 发布/检测) / profile_guard / fingerprint / cookie_checker
  publish/             # queue(asyncio 队列 + 锁) / scheduler(状态机 + 恢复) / runtime(调度器单例)
alembic/               # DB 迁移
chrome-extension/      # Manifest V3 插件(推 cookie)
scripts/               # xvfb.sh / run.sh / pack_extension.sh
tests/                 # 单测 + tests/e2e(冒烟,含 slow)
```

---

## 启动步骤

前置:Python 3.12、`Xvfb`、`zip` 已装(`which Xvfb zip`)。

```bash
# 1. venv(依赖只装在项目 venv 内,不要用系统 python)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 配置:复制样例并按需改(生产必须换 SECRET_KEY / 设 ROOT_ADMIN_APIKEY)
cp .env.example .env

# 3. 一键起(迁移 DB → 打包插件 → 起 uvicorn;内部会先确保 Xvfb)
bash scripts/run.sh
```

`scripts/run.sh` 依次做:`xvfb.sh start`(确保虚拟显示)→ `alembic upgrade head`(迁移)
→ `pack_extension.sh`(生成 `DATA_DIR/extension.zip`)→ `uvicorn app.server:create_app
--factory --host $API_HOST --port $API_PORT`。全程用 `.venv/bin/` 下的解释器/工具。

也可手动分步:

```bash
bash scripts/xvfb.sh start                # 启 Xvfb :99(start/stop/status,幂等)
.venv/bin/alembic upgrade head            # DB 迁移
bash scripts/pack_extension.sh            # 打包插件 zip
.venv/bin/uvicorn app.server:create_app --factory --host 0.0.0.0 --port 8848
```

服务起来后:健康探活 `GET /healthz`(免鉴权),MCP 端点 `POST /mcp/`(需 apikey)。

---

## apikey 与首个管理员

- **首个 root 管理员**由 lifespan 的 `bootstrap_admin` 引导:
  - 配了 `ROOT_ADMIN_APIKEY`:用它建/对齐 root(幂等,重启同 key 不重复建)。
  - 没配:仅当库里还没 root 时,自动生成一把并在日志里**打印一次明文**(务必立即保存)。
- 之后用 root 的 apikey 调 `create_operator` 建其它运营者,每次返回**一次性明文 apikey**
  (库内只存 SHA256 hash,无法再次读取;忘了用 `rotate_operator_apikey` 重置)。
- 远程 agent / 插件带 apikey 的方式:HTTP 头 `Authorization: Bearer <apikey>`
  (或 `X-API-Key: <apikey>`)。

### 创建更多管理员

用 root(或任一 admin)的 apikey 让 agent 调管理员工具:

- 新建 admin:`create_operator(name="小李", role="admin")` → 返回一次性明文 apikey。
- 把已有运营者提权:`update_operator(operator_id=5, role="admin")`;停用:`update_operator(id, enabled=false)`。
- 分配小红书账号使用权:`grant_account_access(operator_id, xhs_account_id)`;回收:`revoke_account_access(...)`。
- 只有 admin 能调这些;普通 operator 调会被拒(AccessDenied / 403)。

### 查看账号 / 运营者状态

本服务无前端,查看都通过连了 **admin apikey** 的 agent 调工具:

- **所有小红书账号 + 登录状态**:`list_accounts()`(admin 全见)→ 每个含 `status` /
  `cookie_status`(valid/invalid/captcha/unknown)/ `last_check_at` / 昵称等(不含 cookie)。
- **刷新某号实时活性**:`check_cookies(account_id)` **异步**——返回 `{check_id}` 后用
  `get_cookie_check(check_id)` 轮询到 valid/invalid/captcha/error,把三态写回。
  想自动周期巡检:设 `COOKIE_CHECK_INTERVAL`(秒,>0 才起,默认 0)。
- **发布任务状态**:`list_publish_jobs(account_id?, status?)`。
- **所有运营者**:`list_operators()` → id/name/role/enabled(不含 apikey);某人授权了哪些号:
  `list_operator_grants(operator_id)`。

不经 agent 快速瞄一眼(直接查库):

```bash
.venv/bin/python -c "import sqlite3;[print(r) for r in sqlite3.connect('data/nbdpsy.db').execute('select id,name,nickname,status,cookie_status,last_check_at from xhs_accounts')]"
```

---

## 插件配置

1. 让 agent 调 `get_extension_download` 拿到 `download_url`(指向 `/downloads/extension.zip`,
   免鉴权可直接下)、版本与安装步骤。
2. 下载解压到固定目录 → `chrome://extensions` 开「开发者模式」→「加载已解压的扩展程序」。
3. 在插件弹窗填 `serverUrl`(本服务地址,即 `PUBLIC_BASE_URL`)与 `apikey`(连接本服务的
   同一把 key)。
4. 用户在 chrome 里登录好小红书后,插件把 cookie 推到 `/cookies/import`,服务端
   sameSite 规范化 + Fernet 加密后 upsert 到该账号唯一一行。

---

## MCP 工具清单

共 24 个工具,分 6 组。远程 agent 通过 MCP `tools/call` 调用;除白名单外均需 apikey,
且按 RBAC 收窄到 caller 有权的账号(admin 全见)。

### system(2)

| 工具 | 签名 | 说明 |
|---|---|---|
| `health` | `() -> {ok, version}` | 探活 + 版本 |
| `whoami` | `() -> {authenticated, name?, role?}` | 回显当前运营者(诊断) |

### admin(8,仅管理员)

| 工具 | 签名 | 说明 |
|---|---|---|
| `create_operator` | `(name, role="operator") -> {id, name, role, enabled, apikey, note}` | 建运营者,返一次性明文 apikey |
| `list_operators` | `() -> {operators: [...]}` | 列全部运营者(不含 apikey) |
| `update_operator` | `(operator_id, role?, enabled?, name?) -> {id, name, role, enabled}` | 局部更新(留空不改) |
| `delete_operator` | `(operator_id) -> {deleted}` | 删运营者并级联清授权 |
| `rotate_operator_apikey` | `(operator_id) -> {id, apikey, note}` | 重置 apikey,旧 key 立即失效 |
| `grant_account_access` | `(operator_id, xhs_account_id) -> {id, operator_id, xhs_account_id}` | 授权某号(幂等) |
| `revoke_account_access` | `(operator_id, xhs_account_id) -> {operator_id, xhs_account_id, revoked}` | 回收授权(幂等) |
| `list_operator_grants` | `(operator_id) -> {operator_id, xhs_account_ids}` | 列某运营者已授权的号 |

### accounts(5,RBAC 收窄)

| 工具 | 签名 | 说明 |
|---|---|---|
| `list_accounts` | `() -> {accounts: [...]}` | 列可见账号(不含 cookie) |
| `get_account` | `(account_id) -> {account view}` | 查单个账号元信息 |
| `update_account` | `(account_id, name?) -> {account view}` | 改内部展示名(安全字段) |
| `delete_account` | `(account_id) -> {deleted}` | 删账号并清其授权 |
| `poll_login` | `(since, account_id?) -> {done, accounts?/account?}` | 轮询登录完成信号(自 since 起有无新号/新登录) |

### cookies(4)

| 工具 | 签名 | 说明 |
|---|---|---|
| `import_cookies` | `(account_name, cookies_json, user_info?) -> {account_id, created}` | 灌 cookie,upsert 唯一号 |
| `get_cookies` | `(account_id) -> {account_id, cookies}` | 解密回读(需 access) |
| `check_cookies` | `(account_id) -> {check_id, status}` | 异步起浏览器巡检,立即返 check_id |
| `get_cookie_check` | `(check_id) -> {status, user_info?, reason?}` | 轮询 check_cookies 的检测结果 |

### publish(4,RBAC 收窄)

| 工具 | 签名 | 说明 |
|---|---|---|
| `publish_note` | `(account_id, title, content, images, topics, schedule_time?) -> {job_id, status}` | 建发布任务并入队 |
| `get_publish_status` | `(job_id) -> {status, note_id, note_url, error, retries}` | 查任务状态 |
| `list_publish_jobs` | `(account_id?, status?) -> {jobs: [...]}` | 列任务(按可见账号过滤) |
| `cancel_publish_job` | `(job_id) -> {ok}` | 取消(仅 pending 可取消) |

`publish_note` 的 `images` 每项为 http(s) URL / data URI / `{b64, ext}`;不传
`schedule_time` 立即入队,传 ISO8601 字符串则定时发布(调度器扫到期后自取)。图片在发布
runner 里再物料化成本地文件,工具本身不碰浏览器。

### extension(1)

| 工具 | 签名 | 说明 |
|---|---|---|
| `get_extension_download` | `() -> {download_url, version, apikey_hint, install_steps, server_time}` | 插件下载 + 安装引导 + poll_login 起点时间 |

---

## 安装 / 接入 MCP(远程 agent 如何连)

MCP 传输为 **Streamable HTTP**,端点 `POST <公网地址>/mcp/`(**注意结尾斜杠**,无斜杠会
307 重定向),鉴权头 `Authorization: Bearer <apikey>`。本部署的公网地址为
**`https://mcp.nbdpsy.com`**(经 Cloudflare 隧道回源 `localhost:8848`),下文示例即用它;
你自建部署时替换成自己的域名。

### 从另一台机器连接(三步)

别的机器上的 agent 接入本服务,只需要**公网地址 + 一把 operator apikey**(向管理员索取,见
「apikey 与首个管理员」)——**不需要在那台机器上装本项目代码或 chrome 插件**(插件只在"登录
小红书"时才用,且装在人工登录的那台真实浏览器上,与调用 MCP 的 agent 机器无关)。

1. **确认可达**(在该机器上):
   ```bash
   curl https://mcp.nbdpsy.com/healthz          # 应返回 {"ok":true}
   ```
2. **把 MCP 装进该机器的 agent 客户端**(见下方各客户端命令,把 `<你的-apikey>` 换成管理员发的 key)。
3. **验证**:让 agent 调 `whoami`(返回你的 operator 身份)、`list_accounts`(看你有权操作的号)。

> apikey 是密钥:别写进公开仓库 / 截图 / 聊天分享。泄露了让管理员用 `rotate_operator_apikey` 轮换。

### 各客户端安装步骤

前提:服务端已部署可达(见上)、你已拿到一把 operator apikey。把本 MCP 装进 agent 客户端:

**Claude Code(命令行,推荐)**:
```bash
claude mcp add --transport http nbdpsy https://mcp.nbdpsy.com/mcp/ \
  --header "Authorization: Bearer <你的-apikey>"
```
之后会话里即可用全部工具;`claude mcp list` 查看、`claude mcp remove nbdpsy` 卸载。

**Claude Desktop / Cursor / Windsurf 等(改配置文件)**:在客户端的 MCP 配置里加一个
`mcpServers` 条目:
```json
{
  "mcpServers": {
    "nbdpsy": {
      "type": "http",
      "url": "https://mcp.nbdpsy.com/mcp/",
      "headers": { "Authorization": "Bearer <你的-apikey>" }
    }
  }
}
```

**任意支持 Streamable HTTP 的客户端 / 自研 agent**:直接 `POST {PUBLIC_BASE_URL}/mcp/`
(带结尾斜杠),头 `Authorization: Bearer <apikey>`,走标准 JSON-RPC。连上后 `tools/list`
会带回本服务自述(server instructions)与每个工具的描述,agent 即可自解释地使用。

握手请求(JSON-RPC `initialize`)需带 `Accept: application/json, text/event-stream`
(Streamable HTTP 会以 SSE 帧返回)。

### Agent 使用工作流

连上后每个工具的描述会经 `tools/list` 自解释,下面给**全局编排顺序**(尤其两条容易踩的):

1. **确认身份**:`whoami` → 看当前 operator 的 name/role(admin 才能用管理员工具)。
2. **看有哪些号**:`list_accounts` → 你有权操作的小红书账号(admin 全见,不含 cookie)。
3. **远程登录 = 没有"登录工具"**(重要):小红书登录及各种验证由**人 + chrome 插件**在真实
   浏览器完成,agent **不自动化登录**。agent 调 `get_extension_download` 拿下载地址+安装步骤
   + `server_time`(记为 poll_login 的 since 起点),交给操作者:装插件 → 填本 operator 的
   apikey 与 serverUrl → 隐身窗口扫码登录;插件自动把 cookie(含 httpOnly)推回后台并 upsert
   账号。新号导入后当前 operator 自动获得 access。**等登录完成**用 `poll_login(since=server_time
   [, account_id])` 每 ~10s 轮询到 `done=true`(登新号不传 account_id,重登旧号传 account_id),
   建议 5-10 分钟超时;别用 check_cookies 探登录。
4. **验 cookie(异步)**:`check_cookies(account_id)` 返回 `{check_id}` → 轮询
   `get_cookie_check(check_id)` 到 valid/invalid/captcha/error(error=浏览器基础设施失败,
   不代表 cookie 真失效,别据此让人重登)。
5. **发布 = 异步,必须轮询**(重要):`publish_note(account_id, title, content, images,
   topics, schedule_time?)` 只返回 `{job_id}` → **轮询** `get_publish_status(job_id)` 到
   `published`(取 note_url)或 `failed`。图片可直接传 http URL 或 base64,agent 在别的机器上
   也能发。仅图文,无视频。
6. **管理(admin)**:`create_operator` 发一次性 apikey、`grant_account_access` 分配账号权限、
   `rotate_operator_apikey` 轮换、`list_operator_grants`/`revoke_account_access` 等。

要点:
- 除白名单(`/healthz`、`/downloads/`)外**所有调用都要带 operator 的 apikey**;访问不属于自己
  的账号抛 `AccessDenied`(403)。
- **别等 `publish_note` 直接返回结果**——它只给 job_id,结果靠 `get_publish_status` 轮询。
- **cookie 是共享的**:多个有 access 的 operator 共用同一份账号 cookie,谁更新写同一行。

---

## 部署

- **反向代理**把 `PUBLIC_BASE_URL`(对外域名)代理到本机 `API_PORT`(FastAPI 监听)。
  MCP 端点、插件推 cookie、插件包下载走同一入口。
- 用 systemd 托管 `scripts/run.sh`(`exec uvicorn` 让信号/退出码直通)。
- Xvfb 由 `scripts/run.sh` 内的 `xvfb.sh start` 确保;也可单独用 systemd 常驻。
- **改 `.env` / `app/core/config.py` 后必须重启进程**——pydantic `BaseSettings` 在进程
  启动时锁定字段集合,运行中改配置不生效。
- 可选后台 cookie 巡检:`COOKIE_CHECK_INTERVAL` 设为 >0(秒)时,lifespan 起一个轻量
  协程周期性对 `cookie_status=valid` 的号逐个跑登录检测并写回状态(号间隔 ≥5s 防频控);
  默认 0 关闭。

---

## 坑(务必先读)

- **`SECRET_KEY` 不能换**:cookie 用它派生的 Fernet key 加密落库。换了 key,存量
  `login_cookies` 全部解密失败(且 `decrypt_cookies` 静默返回空串,不报错),等于所有号
  掉登录。迁移/换机**原样沿用旧 `SECRET_KEY`**。
- **Xvfb `:99` 必须先起**:sync Camoufox 需要虚拟显示;`XVFB_DISPLAY` 改了要与
  `xvfb.sh` 一致。NVIDIA + Xvfb 环境下发布客户端已强制 `MOZ_HEADLESS` /
  `LIBGL_ALWAYS_SOFTWARE` 等,避免 glxtest 卡启动。
- **Camoufox profile 锁**:Firefox 系单写锁,同一号并发启动必挂 "Firefox is already
  running"。服务用 per-account 内存锁串行同号操作;若进程异常残留锁,`profile_guard`
  会清锁 + 精确杀孤儿(**argv 精确匹配**,`account_2` 不误杀 `account_20`)。
- **发布走服务器 IP**:cookie 虽在用户住宅浏览器诞生,发布操作却发生在服务器 IP + 固定
  指纹上。个别对 IP 敏感的号可能触发风控(发布中判 failed,可加冷却)。IP 不一致是既有
  现实,非本服务新增。
- **改代码 / prompt 后要重启**:uvicorn 进程启动时加载模块,改 `.py` 不 restart 不生效。

---

## 测试

```bash
# 全量单测(不含需真号的 slow/e2e,CI 用这条)
.venv/bin/pytest -m "not slow" -v

# e2e 冒烟:RBAC 链默认跑(纯 DB/工具链);发布链需真 cookie,缺则自动 skip
.venv/bin/pytest tests/e2e -v
# 手动跑发布链(需 Xvfb + 真 cookie):
NBDPSY_E2E_COOKIES='[{"name":"...","value":"..."}]' \
  .venv/bin/pytest tests/e2e/test_smoke.py -m slow -v
```

测试全程用隔离临时 sqlite(不碰生产库),`slow` 标记的用例默认不在 CI 跑。
