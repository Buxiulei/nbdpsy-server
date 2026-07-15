# claude.ai 接入(图片上传端点 + 薄 MCP facade)设计

**日期**:2026-07-15
**决策**:让运营在 claude.ai 网页/手机 App 的聊天里也能发小红书。两块一起做:
**A 图片上传端点 + 上传页**(解决 base64 塞不进 MCP 工具参数),**B 薄 MCP facade**
(claude.ai 唯一官方通道,只转发到自己的 REST)。C(博客工具)、OAuth、视频链均不做。

## 背景与硬前提

- **claude.ai 沙箱打不到我们的 API**(只放行包管理器域名),web_fetch 不能带 header/构造 URL
  → 网页/App 唯一官方通道是 **MCP 连接器**(调用在 Anthropic 服务端发起,apikey 不进沙箱,更安全)。
- **base64 传图走 MCP 工具参数数学上不可能**(单张 500KB ≈ 20 万 token)→ 必须"图先变 URL"。
- server 现有 `app/browser/images.py::materialize_images` **已支持 http(s) URL 形态**(httpx 下载)
  → 图变 URL 后**发布链零改动**。
- `fastmcp 3.4.3` 仍在 venv(d3c1dc7 只删了代码/requirements 声明,包没卸)→ B 重加省事。
- 上周删 MCP 的提交 d3c1dc7 是"删 MCP 直连业务逻辑",**本次是薄转发**,不整体 revert。
- **鉴权走 static_headers**(claude.ai 注入 `Authorization: Bearer <apikey>`,需 Anthropic beta):
  服务端零额外工作,`/mcp` 走现有 `ApiKeyMiddleware`。拿不到 beta 再单独议 OAuth,本设计不含。

## A. 图片上传端点 + 上传页

### A.1 `POST /api/uploads/images`(apikey 鉴权,operator 可用)

- multipart/form-data,`files`:1–18 个图片(png/jpg/jpeg/webp,单张 ≤10MB)。
- 校验:张数 1–18(越界 400);逐张 **Pillow `Image.open` + `verify()` 真解**确认是图片(防上传任意文件伪装扩展名);单张 >10MB 400。
- 落盘:`DATA_DIR/uploads/{batch_id}/{NN}.{ext}`,NN 从 01 递增(**顺序即页序** = 上传顺序)。
  ext 由 Pillow 识别的真实格式定(不信客户端扩展名);batch_id = `secrets.token_urlsafe(12)`。
- 归属 + TTL:插一行 `UploadBatch`(见 A.3);`expires_at = now + 7 天`。
- 返回 200:`{batch_id, urls: ["<PUBLIC_BASE_URL>/uploads/{batch_id}/01.png", ...], expires_at}`。
  urls 顺序 = 页序,发布时原样传 `publish_note` 的 image_urls。
- **懒清理**:每次上传成功后顺带扫 `upload_batches` 里 `expires_at < now` 的批次,删其目录 + 行
  (不新增后台任务;低频操作增长慢,懒清理足够)。

### A.2 `GET /uploads/{batch_id}/{n}.{ext}`(白名单免鉴权)

- 加入中间件白名单 `/uploads` 前缀(与 `/downloads` 同款带斜杠边界)。
- FileResponse 服务 `DATA_DIR/uploads/{batch_id}/{n}.{ext}`;不存在/过期 → 404。
- 免鉴权安全性:batch_id 是 `token_urlsafe(12)`(~72bit)不可枚举 + 7 天 TTL;仅供发布器 httpx
  取图与运营复制。**不做长期图床**。

### A.3 存储:表 `app/models/upload_batch.py` + alembic 迁移

```python
class UploadBatch(Base):
    __tablename__ = "upload_batches"
    id: int PK
    batch_id: str  UNIQUE          # token_urlsafe(12)
    operator_id: int               # 归属(FK operators.id)
    file_count: int
    created_at: datetime
    expires_at: datetime
```

服务 `app/services/upload_service.py`:
- `save_images(session, operator, files: list[(filename, bytes)], now) -> dict`:校验+落盘+插行+懒清理,
  返回 `{batch_id, urls, expires_at}`(urls 用 settings.PUBLIC_BASE_URL 拼)。
- `list_batches(session, operator) -> list[dict]`:列该 operator 未过期批次(可选端点 `GET /api/uploads` 用)。
- `sweep_expired(session, now) -> int`:删 expires_at<now 的批次目录+行,返回删除数。

### A.4 `/upload` 静态页(白名单免鉴权页面,页内填 apikey)

- 加白名单 `/upload`(exact);`GET /upload` 返回内联 HTML(单文件,无外部依赖,禁 emoji,遵项目 UI 规范用 SVG)。
- 页内:一个 apikey 输入框(password 型)+ 拖拽/选择图片区 → 调 `POST /api/uploads/images`
  (Authorization: Bearer <页内 apikey>)→ 展示返回的 batch_id + urls 列表 + 一键复制。
- 安全:apikey 只在运营自己浏览器 + 本域 HTTPS,与粘进 curl 同级;页面不存 apikey(不落 localStorage 默认;可选"记住"由运营勾)。

## B. 薄 MCP facade

### B.1 架构:HTTP 自转发,零业务逻辑

facade 每个工具 = 读 MCP 请求头里的 apikey → httpx 打**本机** REST → 原样回 JSON。
**REST 是唯一真源**,facade 不碰 DB、不复制端点逻辑(publish 的图片校验/建 job/入队全在 REST)。

- 内部基址:`http://127.0.0.1:{settings.API_PORT}`(默认 8848),避免绕公网隧道。
- apikey 获取:`from fastmcp.server.dependencies import get_http_headers`;取 `Authorization`/`X-API-Key`。
  **实现前先小验** `get_http_headers()` 是否含 Authorization;若被默认剔除,用 `get_http_headers(include_all=True)`。
  取不到 → 工具返回明确错误(未认证),不静默。
- 转发:async `httpx.AsyncClient` 把 apikey 头透传到本机 REST;REST 的 ApiKeyMiddleware 再校验一次(cheap)。
- 错误映射:REST 返回非 2xx 时,facade 把 `{"error"/"detail"}` 文案原样带回工具结果(不吞、不裸 500)。
- 单条 tool result 控制在 claude.ai 上限(~150k 字符)内;list 类端点 REST 本就有 limit。

### B.2 工具清单(只暴露这些)

| 工具 | 参数 | 转发到 |
|---|---|---|
| `whoami` | — | `GET /api/whoami` |
| `list_accounts` | — | `GET /api/accounts` |
| `publish_note` | `account_id:int, title:str, content:str, image_urls:list[str], topics:list[str]=[], schedule_time:str\|None` | `POST /api/publish-jobs`(body `images` = image_urls 原样;**绝不收 base64**) |
| `get_publish_status` | `job_id:int` | `GET /api/publish-jobs/{job_id}` |
| `list_publish_jobs` | `account_id:int\|None, status:str\|None, limit:int=20` | `GET /api/publish-jobs?...` |
| `check_cookie` | `account_id:int` | `POST /api/accounts/{id}/cookie-checks` 起检 → 内部轮询 `GET /api/cookie-checks/{check_id}` 到终态;**>250s 未终态就回 {status:checking, check_id}** 让模型再问 |
| `get_extension_info` | — | `GET /api/extension` |

- 工具 description 写清:**异步语义**(publish_note 回 job_id 后要用 get_publish_status 轮询;不要干等);
  **publish_note 是写操作**——"向公开平台发布真实笔记,调用前须向用户确认账号与内容",不声明 read-only。
- image_urls 只接受 http(s) URL(来自 A 的 batch);工具 description 明说"图片先用 /upload 页或
  POST /api/uploads/images 拿到 URL,再传这里,不接受 base64"。

### B.3 挂载(重加 fastmcp)

- `app/mcp_facade.py`:`mcp = FastMCP("nbdpsy")` + 注册 7 工具(每个薄转发);导出 `mcp`。
- `requirements.txt` 重加 `fastmcp>=3.4,<4`。
- `app/server.py`:`mcp_app = mcp.http_app(path="/", host_origin_protection=False)`;
  `FastAPI(lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan))`;`app.mount("/mcp", mcp_app)`。
  `/mcp` **不进白名单**(靠 apikey 中间件鉴权)。
- 运维硬约束(写进 DEPLOY):Anthropic 出口 160.79.104.0/21 仅 IPv4(域名别只给 AAAA);
  注册 URL 不能 3xx 跳别的 host(丢 Authorization);工具超时 300s。

### B.4 服务名 / manifest

- REST 的 `GET /api/manifest` 不变(REST agent 仍用它);facade 是给 claude.ai 的**另一入口**,
  不影响 REST。README/DEPLOY 补一节"claude.ai 网页/App 接入(自定义连接器 → /mcp + static_headers apikey)"。

## 数据流

**发图片**:运营在 `/upload` 页填 apikey + 拖 6 图 → `POST /api/uploads/images` → 落盘 + 返回
`{batch_id, urls[6]}` → 运营复制 urls(或页面直接给)。
**发布**(claude.ai App):运营说"用这批图发到 X 号" → 模型调 `publish_note(account_id, title,
content, image_urls=urls)` → facade httpx → `POST /api/publish-jobs`(images=urls)→ 202 {job_id}
→ 模型轮询 `get_publish_status(job_id)` 到 published → 回报 note_url。发布 runner 里
materialize_images 把 urls httpx 下载落盘再 set_input_files——**零改发布链**。

## 错误处理

- A:非图片文件(Pillow verify 失败)→ 400 该项报错(整批拒,不落半批);张数越界 400;单张超限 400;
  落盘失败 → 500 不留半批(失败清理已写的文件)。GET 不存在/过期 → 404。
- B:apikey 取不到 → 工具返回未认证错误;REST 非 2xx → 原样带回错误文案;httpx 超时/连不上本机
  REST → 工具返回"服务内部错误"(不泄栈)。facade 绝不吞掉 REST 的 403/404 语义。

## 测试策略

- **A upload_service**:临时 DATA_DIR + 临时 DB;save_images(真 PNG bytes,Pillow 造小图)→ 验落盘
  文件名页序、返回 urls、插行、expires_at;非图片 bytes → 拒;>18 张 400;>10MB 400;sweep_expired
  删过期目录+行;list_batches 只列自己未过期。
- **A 端点**:httpx ASGITransport multipart 上传(rest_helpers 造 operator)→ 200 urls;无 key 401;
  越权无关(上传归属自己);GET /uploads/{batch}/01.png 免 key 200 + content-type;过期/不存在 404;
  GET /upload 返 HTML 200 免 key。
- **B facade**:monkeypatch facade 的 httpx 客户端(或用 respx)→ 验每个工具把 apikey 头透传、
  转发到对的 REST 路径、原样回 JSON;REST 返 403 → 工具带回错误;check_cookie 轮询逻辑
  (monkeypatch 返 checking→valid);publish_note 把 image_urls 放进 images。
- **B 挂载回归**:create_app 后 `/mcp` 挂上(mcp_app.lifespan 组合正确,task group 初始化);
  `POST /mcp/` 无 key → 401(中间件);带 key initialize → 不 421(host_origin_protection=False)。
- **防漂移**:facade 工具集与 REST 端点的映射有一个"每个工具的目标 REST 路径都真实存在"的测试。

## 明确不做(YAGNI)

- 不做 C publish_blog_post(P2 跨服务,B 之后)、不做 OAuth2.1(赌 static_headers beta)、不碰视频链。
- 不做长期图床/CDN、不做图片压缩/改格式(原样存,发布器负责)、不做上传断点续传。
- facade 不加任何业务逻辑/缓存/重试(纯转发);不暴露 admin 工具(运营侧不需要建号/授权)。
- 不改现有 REST 端点(facade 只转发,不重构 publish 编排)。
