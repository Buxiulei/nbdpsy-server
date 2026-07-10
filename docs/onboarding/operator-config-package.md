# 运营接入配置包 —— 生成规格(交给"管理后台 agent"实现)

**目标**:管理后台一键为每个被授权的运营用户生成一个连接配置包;运营把它发给自己电脑上的
Claude,Claude 就能**自动**把 nbdpsy-mcp 接进去、跑完连通性测试、确认可用——运营零手动配置,
开箱即用。

本文件是后台侧实现该功能的契约,也是配置包的成品模板。

---

## 1. 关于 nbdpsy-mcp 的硬事实

| 项 | 值 |
|---|---|
| 公网端点 | `https://mcp.nbdpsy.com/mcp/`（Streamable HTTP，**必须带结尾斜杠**，无斜杠 307） |
| 健康探活 | `https://mcp.nbdpsy.com/healthz` → `{"ok":true}`（免鉴权） |
| 鉴权 | 请求头 `Authorization: Bearer <该运营的-apikey>`（或 `X-API-Key`） |
| apikey | 每个运营一把独立 key，管理员工具生成，**只显示一次**（库内只存 hash，不可回读） |
| 工具 | 连上后 24 个工具 + 服务自述（`tools/list` 自解释） |

---

## 2. 后台生成配置包的流程（3 步）

后台自己持有一把 **admin apikey**（连 nbdpsy-mcp 的管理员密钥，配置在后台，**绝不写进发给运营的任何内容**）。
用它连 `https://mcp.nbdpsy.com/mcp/`（`Authorization: Bearer <admin-apikey>`）调管理员工具：

1. **建运营账号 + 拿 apikey**：`create_operator(name="<运营名>", role="operator")`
   → 返回 `{operator_id, apikey, ...}`。apikey 只此一次可拿；丢了用
   `rotate_operator_apikey(operator_id)` 重置（旧 key 立即失效）。
2. **授权可操作的小红书号**：对每个号 `grant_account_access(operator_id, xhs_account_id)`。
3. **填充下面模板的占位符**，产出配置包（一个 .md 文件或一段文本），私密交付给运营本人。

相关管理员工具：`update_operator(operator_id, enabled=false)` 停用运营；
`revoke_account_access(operator_id, xhs_account_id)` 收回授权；`list_operator_grants(operator_id)` 查已授权的号。

---

## 3. 配置包成品模板

> 设计意图：运营把这个文件**直接丢给自己电脑上的 Claude**，Claude 读到即自动执行安装 + 自检。
> 所以它是**写给 Claude 的祈使指令**，不是给人读的说明书。后台填好 `{{...}}` 占位符即为成品。

````markdown
# nbdpsy-mcp 运营接入配置包 —— 交给你电脑上的 Claude 自动安装

你好 Claude。请把下面这个 MCP 服务接入你自己、跑完连通性自检，让我（运营）开箱即用。
全程你自动完成，做完把结果用中文告诉我。若我本机没装 Claude Code，请先提示我
`npm i -g @anthropic-ai/claude-code`。

## 1. 注册 MCP 服务

**首选：Claude Code 插件 marketplace（URL 内置，装时填 apikey）**
```bash
claude plugin marketplace add Buxiulei/nbdpsy-mcp
claude plugin install nbdpsy-mcp@nbdpsy
# 安装时提示输入 apikey → 填：{{APIKEY}}
```

或（不走 marketplace）直接注册：
```bash
claude mcp add --transport http nbdpsy https://mcp.nbdpsy.com/mcp/ \
  --header "Authorization: Bearer {{APIKEY}}"
```
（其它客户端如 Desktop/Cursor：在其 mcpServers 配置里加
`{"nbdpsy":{"type":"http","url":"https://mcp.nbdpsy.com/mcp/","headers":{"Authorization":"Bearer {{APIKEY}}"}}}`）

## 2. 连通性自检（不依赖客户端加载，先跑，能立刻定位问题）
```bash
curl -s https://mcp.nbdpsy.com/healthz        # 期望 {"ok":true}
curl -s -o /dev/null -w "initialize=%{http_code}\n" -X POST https://mcp.nbdpsy.com/mcp/ \
  -H "Authorization: Bearer {{APIKEY}}" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# 期望 initialize=200（401=apikey 错；421=服务端问题，联系管理员；000/超时=网络/隧道）
```

## 3. 工具级验证（注册成功后，用 MCP 工具确认身份与权限）
- 调 `whoami` → 应得 `{authenticated:true, name:"{{OPERATOR_NAME}}", role:"operator"}`
- 调 `list_accounts` → 应看到我被授权的小红书号：{{ACCOUNT_HINT}}

## 4. 汇报
- 全通过 → 告诉我："nbdpsy-mcp 已接入，身份 {{OPERATOR_NAME}}，可操作 N 个账号，可以开始运营。"
- 任一步失败 → 贴出失败步骤 + 返回码，并按上面括注给我初判原因。

## 接入后怎么用（供你参考）
- 小红书运营 MCP：发布 / 账号 / cookie / 查询。tools/list 带完整工具说明与服务自述。
- **发布是异步**：`publish_note` 返回 job_id，用 `get_publish_status` 轮询到 published/failed。
- **登录靠 chrome 插件**（不是工具）：调 `get_extension_download` 拿插件包给我装、扫码登录，
  再用 `poll_login(since=...)` 等我登录完成、`check_cookies`→`get_cookie_check` 验 cookie。

> 安全：上面的 apikey 是我的专属密钥，别外传、别提交到任何仓库或公开聊天；泄露了找管理员轮换。
````

### 占位符对照（后台填充）

| 占位符 | 填什么 |
|---|---|
| `{{APIKEY}}` | `create_operator`/`rotate_operator_apikey` 返回的一次性明文 apikey |
| `{{OPERATOR_NAME}}` | 该运营的 name |
| `{{ACCOUNT_HINT}}` | 授权的号列表（如 "@某某、@某某" 或 "3 个账号"）；可选，帮 Claude 核对 |

---

## 4. 交付与安全要求

- 配置包**含明文 apikey = 密钥文件**：走私密渠道发给运营本人。**不要**群发 / 进公开仓库 /
  在后台日志打印明文。
- apikey 后台不可回读（库里只存 hash）；运营弄丢就 `rotate_operator_apikey` 重发新包。
- 建议后台把配置包做成**一键下载的 .md 文件**（文件名带运营名），运营下载后直接拖进自己的
  Claude 会话即可，最贴近"开箱即用"。

---

## 5. 连通性判据速查（自检返回码含义）

| 现象 | 含义 | 处理 |
|---|---|---|
| `/healthz` 非 `{"ok":true}` / 超时 | 服务未起 或 隧道/网络问题 | 联系管理员查服务与 Cloudflare 隧道 |
| initialize = 200 | 端点 + apikey + 传输都正常 | 通过 |
| initialize = 401 | apikey 错/被停用 | 核对 apikey；或让管理员 rotate 重发 |
| initialize = 421 | 服务端 Host 防护未关（不该出现，已在服务端修复） | 联系管理员 |
| initialize = 000/超时 | 网络不通 / DNS / 隧道断 | 查本机网络与域名解析 |
