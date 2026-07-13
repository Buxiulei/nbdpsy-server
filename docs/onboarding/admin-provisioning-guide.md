# nbdpsy-api 管理端运维指南 —— 交给 NBDpsy 管理后台的 agent

**读者**:NBDpsy 管理后台侧的 agent / 开发者。
**用途**:你负责为运营人员开账号、分配小红书号、发接入配置包。本文件告诉你:管理员账号怎么来、
怎么鉴权、怎么建一般用户账号、怎么授权、怎么用。全程只需一把 admin apikey + HTTP 调用。

> 本文件是"管理端"操作手册。给单个运营生成"开箱即用连接配置包"的成品模板见同目录
> `operator-config-package.md`(你建完运营 + 授权后,用它产出发给运营本人的配置包)。

---

## 0. 一句话架构

nbdpsy-api 是小红书运营能力后台(自动发布 / 多账号管理 / cookie 管理 / 远程登录),纯 REST。
两类角色:

- **admin**(管理员):能开/停运营账号、给运营授权小红书号、轮换 key。你(管理后台)持一把 admin apikey。
- **operator**(运营):只能看到/操作被授权的小红书号。每个运营一把独立 apikey。

一切调用带 `Authorization: Bearer <apikey>`。**apikey 库里只存 hash 不可回读**,创建/轮换时明文只返回一次。

| 项 | 值 |
|---|---|
| 公网基址 | `https://mcp.nbdpsy.com` |
| 健康探活 | `GET /healthz` → `{"ok":true}`(免鉴权) |
| 机器可读全契约 | `GET /api/manifest`(带 apikey)——**所有端点的权威说明,以它为准** |
| 鉴权头 | `Authorization: Bearer <apikey>` 或 `X-API-Key: <apikey>` |

---

## 1. 管理员账号怎么来(admin apikey)

管理员是**服务端启动时引导**的,不是通过 API 创建的第一个。机制(`bootstrap_admin`):

- 服务端 `.env` 里配了 `ROOT_ADMIN_APIKEY=<强随机串>` → 启动时 upsert 一个 `name="root", role="admin"`
  的管理员,apikey 就是这串(幂等:重启同 key 不重复建,换 key 则更新)。**这把就是你管理后台要持有的 admin apikey。**
- 若 `.env` 没配 `ROOT_ADMIN_APIKEY` → 启动时自动生成一把,**在服务端日志里明文打印一次**
  (`bootstrap: 未配置 ROOT_ADMIN_APIKEY,已生成 root 管理员 apikey(仅打印一次…): <明文>`),
  运维需从日志捞出保存。

**你(管理后台)要做的**:从运维手里拿到这把 root admin apikey,安全存进后台配置(**绝不写进任何发给运营的内容、不进公开仓库、不打明文日志**)。

**验证你手里的 admin key 可用**:

```bash
ADMIN_KEY="<你的-admin-apikey>"
curl -s https://mcp.nbdpsy.com/api/whoami -H "Authorization: Bearer $ADMIN_KEY"
# 期望 {"name":"root","role":"admin"}。得 401=key 错/被停用;得非 admin 的 role=这不是管理员 key。
```

### 需要更多管理员?

用已有 admin key 建一个 role=admin 的账号即可(见 §3,把 `role` 传 `admin`)。root 那把建议只作"根密钥"留存,日常另开子管理员用。

---

## 2. 鉴权规则(务必吃透)

- 除白名单外**每个请求**都要带 `Authorization: Bearer <apikey>`(或 `X-API-Key: <apikey>`)。
- 白名单(免鉴权):`GET /healthz`、`GET /downloads/*`(插件包下载)。其余全部要鉴权。
- 错误码契约(响应体统一 `{"error": "..."}`,除 401/422 见下):
  | 码 | 含义 |
  |---|---|
  | 401 | apikey 缺失/无效/该运营被停用。体为 `{"detail": ...}` |
  | 403 | 越权:operator 调管理员端点,或动了没被授权的小红书号。体 `{"error": ...}` |
  | 404 | 资源不存在(运营者/账号/任务/check_id)。体 `{"error": ...}` |
  | 400 | 入参非法(枚举错、格式错等)。体 `{"error": ...}` |
  | 422 | 请求体不符合 schema(缺字段/类型错,FastAPI 校验)。体 `{"detail": [...]}` |
  | 500 | 未预期异常,联系管理员查日志。体 `{"error": ...}` |

---

## 3. 建一般用户(运营)账号

`POST /api/operators`,body `{name, role}`(role 省略即 `operator`)。返回体里的 `apikey` 是**一次性明文**,库里只存 hash,丢了只能轮换重发。

```bash
curl -s -X POST https://mcp.nbdpsy.com/api/operators \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name":"张三"}'
# → {"id":5,"name":"张三","role":"operator","enabled":true,
#    "apikey":"<一次性明文,立即保存并私密交付运营本人>","note":"apikey 仅此一次显示…"}
```

建管理员就把 `role` 设 `admin`:`-d '{"name":"李四","role":"admin"}'`。

**列出所有运营/管理员**:

```bash
curl -s https://mcp.nbdpsy.com/api/operators -H "Authorization: Bearer $ADMIN_KEY"
# → {"operators":[{"id":1,"name":"root","role":"admin","enabled":true,"created_at":"..."}, ...]}
```

---

## 4. 给运营授权小红书号

运营默认看不到任何小红书号,必须逐个授权(二元:有 grant 即可全功能操作该号,无 grant 一律 403)。

前提:先有小红书号入库(号是运营用 chrome 插件扫码登录后自动推进来的,或已有号)。查号列表:

```bash
curl -s https://mcp.nbdpsy.com/api/accounts -H "Authorization: Bearer $ADMIN_KEY"
# admin 看全部;→ {"accounts":[{"id":1,"name":"...","nickname":"...","user_id":"...",
#    "cookie_status":"valid|invalid|unknown|...","last_login_at":"...", ...}]}  (不含 cookie 明文)
```

**授权**(operator_id=5 可操作 xhs_account_id=1):

```bash
curl -s -X POST https://mcp.nbdpsy.com/api/operators/5/grants \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"xhs_account_id":1}'
# → {"id":..,"operator_id":5,"xhs_account_id":1}
```

**查某运营已授权的号 / 收回授权**:

```bash
curl -s https://mcp.nbdpsy.com/api/operators/5/grants -H "Authorization: Bearer $ADMIN_KEY"
# → {"operator_id":5,"xhs_account_ids":[1,3]}

curl -s -X DELETE https://mcp.nbdpsy.com/api/operators/5/grants/1 -H "Authorization: Bearer $ADMIN_KEY"
# → {"operator_id":5,"xhs_account_id":1,"revoked":true}
```

---

## 5. 停用运营 / 轮换 apikey / 删除

```bash
# 停用(旧 key 立即 401,不删数据):
curl -s -X PATCH https://mcp.nbdpsy.com/api/operators/5 \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"enabled":false}'
# → {"id":5,"name":"张三","role":"operator","enabled":false}

# 轮换 apikey(旧 key 立即失效,返回新的一次性明文):
curl -s -X POST https://mcp.nbdpsy.com/api/operators/5/rotate-apikey \
  -H "Authorization: Bearer $ADMIN_KEY"
# → {"id":5,"apikey":"<新的一次性明文>","note":"..."}

# 删除运营(级联清其授权):
curl -s -X DELETE https://mcp.nbdpsy.com/api/operators/5 -H "Authorization: Bearer $ADMIN_KEY"
# → {"deleted":5}
```

PATCH 也能改 name/role:`-d '{"name":"新名","role":"admin"}'`(只改传了的字段)。

---

## 6. 完整开户流程(把上面串起来)

给一个新运营开账号 + 配好可用环境,标准三步:

1. **建运营**:`POST /api/operators {name}` → 记下返回的一次性 `apikey` 与 `id`。
2. **授权号**:对每个要给他的小红书号 `POST /api/operators/{id}/grants {xhs_account_id}`。
3. **发配置包**:用同目录 `operator-config-package.md` 的模板,把占位符(apikey / 运营名 / 号列表)
   填好,私密交付运营本人。运营把它丢给自己电脑上的 Claude 或任意 HTTP agent,即自动接入 + 自检。

若运营还没有小红书号可授:让运营先装 chrome 插件(下载见 §7)扫码登录,号会自动入库,再回到第 2 步授权。

---

## 7. 运营侧怎么用(你需要知道,以便指导运营)

运营接入后的能力全部在 `GET /api/manifest`(带他自己的 apikey)里自解释。要点:

- **登录靠人 + chrome 插件**(没有登录 API):调 `GET /api/extension` 拿插件包下载地址(`download_url`,
  免鉴权可直接下)+ 安装步骤 + `server_time`;运营装插件、无痕窗扫码登录,插件自动把 cookie(含 httpOnly)
  推回 `POST /api/cookies/import`。登录完成用 `GET /api/login/poll?since=<server_time>` 轮询判定。
- **cookie 验活**:`POST /api/accounts/{id}/cookie-checks`(202 回 check_id)→ 轮询
  `GET /api/cookie-checks/{check_id}` 到 valid/invalid/captcha/error(error=基础设施失败≠cookie 失效)。
  插件 v2.0.4+ 的账号卡片上也有「检测」按钮直接触发。
- **发布**:`POST /api/publish-jobs`(202 回 job_id)→ 轮询 `GET /api/publish-jobs/{job_id}` 到
  published/failed。仅图文,图片 1–18 张;标题≤20/正文≤900/话题≤10 静默截断;定时发布
  `schedule_time` 务必带时区偏移(如 `+08:00`)。

---

## 8. 安全红线

- admin apikey = 后台最高权限,泄露等于全站沦陷:只存后台配置,绝不进发给运营的内容 / 公开仓库 / 明文日志。
- 发给运营的配置包含明文 operator apikey = 密钥文件:走私密渠道给本人,别群发。
- apikey 不可回读:运营弄丢就 `rotate-apikey` 重发,别尝试"查回来"。
- `GET /api/accounts` 与账号视图**刻意不含 cookie 明文**;需要 cookie 的只有插件注入场景(`GET /api/accounts/{id}/cookies`,受授权限制)。

---

## 9. 权威事实来源(有出入以下面为准)

1. 线上 `GET https://mcp.nbdpsy.com/api/manifest`(带 apikey)—— 机器可读全端点契约,永远最新。
2. 本仓库 `docs/design/2026-07-13-rest-api-conversion-design.md`(设计)、`README.md`(接入)。
3. 运营配置包成品模板:`docs/onboarding/operator-config-package.md`。
