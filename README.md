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

共 22 个工具,分 6 组。远程 agent 通过 MCP `tools/call` 调用;除白名单外均需 apikey,
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

### accounts(4,RBAC 收窄)

| 工具 | 签名 | 说明 |
|---|---|---|
| `list_accounts` | `() -> {accounts: [...]}` | 列可见账号(不含 cookie) |
| `get_account` | `(account_id) -> {account view}` | 查单个账号元信息 |
| `update_account` | `(account_id, name?) -> {account view}` | 改内部展示名(安全字段) |
| `delete_account` | `(account_id) -> {deleted}` | 删账号并清其授权 |

### cookies(3)

| 工具 | 签名 | 说明 |
|---|---|---|
| `import_cookies` | `(account_name, cookies_json, user_info?) -> {account_id, created}` | 灌 cookie,upsert 唯一号 |
| `get_cookies` | `(account_id) -> {account_id, cookies}` | 解密回读(需 access) |
| `check_cookies` | `(account_id) -> {status, user_info?}` | 起浏览器巡检活性,三态写回 |

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
| `get_extension_download` | `() -> {download_url, version, apikey_hint, install_steps}` | 插件下载 + 安装引导 |

---

## 远程 agent 如何连

MCP 传输为 **Streamable HTTP**,端点 `POST {PUBLIC_BASE_URL}/mcp/`(**注意结尾斜杠**,
无斜杠会 307 重定向),鉴权头 `Authorization: Bearer <apikey>`。

以 Claude Code / 兼容客户端为例的连接配置:

```json
{
  "mcpServers": {
    "nbdpsy": {
      "type": "http",
      "url": "https://你的域名/mcp/",
      "headers": { "Authorization": "Bearer <你的-apikey>" }
    }
  }
}
```

握手请求(JSON-RPC `initialize`)需带 `Accept: application/json, text/event-stream`
(Streamable HTTP 会以 SSE 帧返回)。

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
