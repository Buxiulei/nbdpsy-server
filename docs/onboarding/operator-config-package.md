# 运营接入配置包 —— 生成规格(交给"管理后台 agent"实现)

**目标**:管理后台一键为每个被授权的运营用户生成一个连接配置包;运营把它发给自己电脑上的
Claude,Claude 就能**自动**把 nbdpsy-api 接进去、跑完连通性测试、确认可用——运营零手动配置,
开箱即用。

本文件是后台侧实现该功能的契约,也是配置包的成品模板。

---

## 1. 关于 nbdpsy-api 的硬事实

| 项 | 值 |
|---|---|
| 公网入口 | `https://mcp.nbdpsy.com`(纯 REST,任何请求都是普通 HTTP,无需特殊客户端) |
| 健康探活 | `https://mcp.nbdpsy.com/healthz` → `{"ok":true}`(免鉴权) |
| 自描述接口 | `https://mcp.nbdpsy.com/api/manifest`（须鉴权）→ 一次返回全部端点契约 + 工作流叙事 + 错误契约 + caller 身份 |
| 鉴权 | 请求头 `Authorization: Bearer <该运营的-apikey>`（或 `X-API-Key`） |
| apikey | 每个运营一把独立 key，管理员端点生成，**只显示一次**（库内只存 hash，不可回读） |
| 端点 | 24 个 REST 端点，分 6 组；完整契约以 `GET /api/manifest` 为准 |

---

## 2. 后台生成配置包的流程（3 步）

后台自己持有一把 **admin apikey**（连 nbdpsy-api 的管理员密钥，配置在后台，**绝不写进发给运营的任何内容**）。
用它带 `Authorization: Bearer <admin-apikey>` 调管理端点：

1. **建运营账号 + 拿 apikey**：`POST /api/operators` `{"name":"<运营名>","role":"operator"}`
   → 返回 `{id, name, role, enabled, apikey, note}`。apikey 只此一次可拿；丢了用
   `POST /api/operators/{id}/rotate-apikey` 重置（旧 key 立即失效）。
2. **授权可操作的小红书号**：对每个号 `POST /api/operators/{id}/grants` `{"xhs_account_id": <号id>}`。
3. **填充下面模板的占位符**，产出配置包（一个 .md 文件或一段文本），私密交付给运营本人。

相关管理端点：`PATCH /api/operators/{id}` `{"enabled": false}` 停用运营；
`DELETE /api/operators/{id}/grants/{xhs_account_id}` 收回授权；
`GET /api/operators/{id}/grants` 查已授权的号。

---

## 3. 配置包成品模板

> 设计意图：运营把这个文件**直接丢给自己电脑上的 Claude**，Claude 读到即自动执行安装 + 自检。
> 所以它是**写给 Claude 的祈使指令**，不是给人读的说明书。后台填好 `{{...}}` 占位符即为成品。

````markdown
# nbdpsy-api 运营接入配置包 —— 交给你电脑上的 Claude 自动接入

你好 Claude。请把下面这个 REST API 服务接入你自己、跑完连通性自检，让我（运营）开箱即用。
全程你自动完成，做完把结果用中文告诉我。

## 1. 记住这两个值

- base URL：`https://mcp.nbdpsy.com`
- apikey：`{{APIKEY}}`（每次请求带请求头 `Authorization: Bearer {{APIKEY}}`）

## 2. 连通性自检（先跑，能立刻定位问题）
```bash
curl -s https://mcp.nbdpsy.com/healthz        # 期望 {"ok":true}
curl -s -o /dev/null -w "manifest=%{http_code}\n" \
  -H "Authorization: Bearer {{APIKEY}}" https://mcp.nbdpsy.com/api/manifest
# 期望 manifest=200(401=apikey 错;404=路径拼错;000/超时=网络/隧道)
```

## 3. 读 manifest,拿到全部上手信息
```bash
curl -s -H "Authorization: Bearer {{APIKEY}}" https://mcp.nbdpsy.com/api/manifest
```
返回体含 `endpoints`（全部端点的 method/path/params/returns/errors/notes）、`workflows`
（编排顺序叙事）、`constraints`（硬约束）、`error_contract`（错误码含义）、`caller`
（你的身份与可操作账号数）。**照 manifest 的 workflows 干活即可**，不需要额外文档。

## 4. 工具级验证（确认身份与权限）
- `GET /api/whoami` → 应得 `{"name":"{{OPERATOR_NAME}}","role":"operator"}`
- `GET /api/accounts` → 应看到我被授权的小红书号：{{ACCOUNT_HINT}}

## 5. 汇报
- 全通过 → 告诉我："nbdpsy-api 已接入，身份 {{OPERATOR_NAME}}，可操作 N 个账号，可以开始运营。"
- 任一步失败 → 贴出失败步骤 + 返回码，并按上面括注给我初判原因。

## 接入后怎么用（供你参考）
- 小红书运营 REST API：发布 / 账号 / cookie / 查询，`GET /api/manifest` 带完整端点说明与服务自述。
- **发布是异步**：`POST /api/publish-jobs` 返回 job_id，用 `GET /api/publish-jobs/{job_id}` 轮询到 published/failed。
- **登录靠 chrome 插件**（不是接口）：调 `GET /api/extension` 拿插件包给我装、扫码登录，
  再用 `GET /api/login/poll?since=...` 等我登录完成、`POST /api/accounts/{id}/cookie-checks`→
  `GET /api/cookie-checks/{check_id}` 验 cookie。

> 安全：上面的 apikey 是我的专属密钥，别外传、别提交到任何仓库或公开聊天；泄露了找管理员轮换。
````

### 占位符对照（后台填充）

| 占位符 | 填什么 |
|---|---|
| `{{APIKEY}}` | `POST /api/operators` / `POST /api/operators/{id}/rotate-apikey` 返回的一次性明文 apikey |
| `{{OPERATOR_NAME}}` | 该运营的 name |
| `{{ACCOUNT_HINT}}` | 授权的号列表（如 "@某某、@某某" 或 "3 个账号"）；可选，帮 Claude 核对 |

---

## 4. 交付与安全要求

- 配置包**含明文 apikey = 密钥文件**：走私密渠道发给运营本人。**不要**群发 / 进公开仓库 /
  在后台日志打印明文。
- apikey 后台不可回读（库里只存 hash）；运营弄丢就 `rotate-apikey` 重发新包。
- 建议后台把配置包做成**一键下载的 .md 文件**（文件名带运营名），运营下载后直接拖进自己的
  Claude 会话即可，最贴近"开箱即用"。

---

## 5. 连通性判据速查（自检返回码含义）

| 现象 | 含义 | 处理 |
|---|---|---|
| `/healthz` 非 `{"ok":true}` / 超时 | 服务未起 或 隧道/网络问题 | 联系管理员查服务与反向代理 |
| manifest = 200 | 端点 + apikey + 网络都正常 | 通过 |
| manifest = 401 | apikey 错/被停用 | 核对 apikey；或让管理员 rotate 重发 |
| manifest = 404 | 路径拼错(检查 `/api/manifest` 有没有多打或漏打字符) | 核对 URL |
| manifest = 000/超时 | 网络不通 / DNS / 隧道断 | 查本机网络与域名解析 |
