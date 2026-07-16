# Changelog

## 0.6.1 (2026-07-16)

修插件「打开隐私窗口登录」采集新号后**永久卡死**的生产 bug（登录检测成功、窗口进主页后不关窗 /
不入库 / popup 无结果，服务端零 `/api/cookies/import`）。

- **根因·后半程异常被静默吞掉**：`startRemoteLogin` 登录检测成功后先 `cleanup()` 清掉 interval，
  再串行跑「进主页→采 userInfo→采 cookies→推送→关窗→resolve」。这段任一步抛异常（`chrome.tabs.update`
  在 tab 被用户动过时抛、`pushCookies` 的 `fetch` 网络错等）只被外层 `console.warn` 吞掉——此时 interval
  已清、`loginDetected=true`，promise 永不 resolve、窗口永不关、`finishRemoteLogin` 永不写 storage。
  修复：把整个后半程包进 try/catch，catch 里**必然**关窗 + 摘 webRequest listener + `resolve({success:false})`，
  调用方拿到终态写 storage，popup 稳定显示「采集失败: 采集中断: ...」。
- **`pushCookies` 网络异常不再抛**：`fetch` + 响应处理包进 try/catch，网络层异常返回
  `{success:false, error:'推送后台失败(网络): ...'}`。函数契约收敛为「永不 throw，总返回 {success,...}」。
- **apikey 未保存快速失败**：`startRemoteLogin` 开窗前预检 storage apikey（无则 3 秒内返回指引），
  popup 侧 `remoteLogin` 改判 `savedApikey`（已存 key）而非输入框裸值，杜绝「填了没点保存」白走全流程。
- 插件版本 `2.1.0 → 2.1.1`（bugfix）。

## 0.6.0 (2026-07-15)

两个功能:发布计划原地修改(定时发布收口)+ chrome 插件交互精简为账号管理器。

- **待发定时任务原地修改**:`PATCH /api/publish-jobs/{job_id}`——**仅 `pending`** 任务可原地改
  `schedule_time` / `title` / `content` / `images` / `topics`,不必"取消再重建"。定时发布确定用
  服务端定时(job 压库到点发,可随时改计划),不做小红书原生定时按钮。语义:PATCH 部分更新
  (`model_fields_set`,省略字段不改);`schedule_time` 显式 `null`=清空转立即发并 submit;
  条件更新 `WHERE status='pending'` 原子防与调度器 scan 抢占(rowcount=0 返 `{ok:false,status}`,
  绝不改到正在发的任务);非 pending 返 `{ok:false,status:<当前态>}`;显式 `title/content=null`→400
  (非 500);`account_id` 不可改。补 manifest 条目,agent/claude.ai/插件均可发现调用。
- **chrome 插件账号管理器化(v2.1.0)**:插件从"当前标签页 cookie 采集器"精简为"我的账号管理器"。
  **移除**:同步当前页 cookie、当前标签页状态指示 + 用户信息区、打开小红书普通标签。**保留五条**:
  录 apikey(唯一必填,server-url 折叠进高级设置默认 mcp.nbdpsy.com)、看归属账号列表、点卡注入
  cookie 开无痕窗、无痕登录采集 cookies(加/换号)、per-card 验活。所有小红书会话统一走无痕窗。
  六项权限全保留(webRequest 被无痕采 httpOnly cookie 依赖,不误删)。337 行删减,消息协议双侧一致。
- **部署**:`systemctl restart nbdpsy-server` 加载 PATCH 端点 + ExecStartPre 重打包插件 zip(供运营
  下载更新到 2.1.0);**无新迁移**(PATCH 复用 PublishJob 现有字段)。插件更新后运营需 load-unpacked
  或重新下载 zip 走查。

## 0.5.0 (2026-07-15)

浏览器并发硬化:补上并发缺口 + 空闲释放防内存泄露,支撑 20+ 运营同时发起浏览器操作。
此前 publish 有 PublishQueue(2) 硬闸,但 cookie 检测 / 笔记导出 / 周期巡检**无全局闸**——
20 个运营齐发可能同时起 20 个 camoufox 打爆内存。三块 + 一处收口:

- **全局浏览器并发闸**:`app/browser/browser_gate.py` 进程级信号量 `BROWSER_CONCURRENCY=6`,
  `browser_slot()` 套住**全部 4 个** camoufox 启动入口(发布 / cookie 检测 / 笔记导出 / 周期巡检)。
  超出上限的操作排队等名额(不拒绝、不崩),总 camoufox 数恒 ≤6。与 PublishQueue(2) 共存:
  publish 在闸下最多占 2 名额不自卡。camoufox 瘦身:`block_webgl=True` 恒开、只读操作
  (cookie 检测 / 导出)`block_images=True`,**发布不 block_images 保发布页渲染保真**。
- **孤儿 camoufox 周期回收 reaper**:`app/browser/browser_reaper.py` 周期(默认 300s)扫 /proc,
  杀"账号锁未持有(无在跑操作)+ 存活超 `BROWSER_REAP_AGE=900s`"的残留 camoufox,兜住崩溃/
  超时打断留下的孤儿进程,防内存泄露。三条件缺一不杀,锁持有的在跑浏览器绝不误杀;复用
  `profile_guard.browser_profiles_root()` / `iter_camoufox_procs()`,路径约定与 /proc 枚举单一真相源。
  `BROWSER_REAP_INTERVAL=0` 可关。
- **SQLite WAL + busy_timeout**:`app/core/db.py` 仅当 DATABASE_URL 是 sqlite 时启用
  `journal_mode=WAL` + `busy_timeout`(`SQLITE_BUSY_TIMEOUT=30s`),并发写从"database is locked"
  报错变排队等待。非 sqlite(Postgres)自动跳过,不传 sqlite-only 参数。
- **周期巡检补账号锁**:`_check_account` 补 `account_locks`,与另三入口锁序一致——让 reaper 视其
  浏览器"有主"不误杀,并关掉同号巡检 × publish/手动检测之间 pre-existing 的 kill_orphans 互杀窗口。
- **部署**:走 `systemctl restart nbdpsy-server`(**本特性无新建表 / 无新迁移**,ExecStartPre 的
  `alembic upgrade head` 为 no-op)。新增可选 `.env` 字段(均有默认值,不配也能跑):
  `BROWSER_CONCURRENCY` / `BROWSER_REAP_INTERVAL` / `BROWSER_REAP_AGE` / `SQLITE_BUSY_TIMEOUT`。

## 0.4.0 (2026-07-15)

claude.ai 网页/手机 App 接入:图片上传端点 + 薄 MCP facade。让不能装 Claude Desktop 的运营
在 claude.ai 聊天里也能发小红书(claude.ai 沙箱够不到 API、web_fetch 不能带 header,MCP 连接器是唯一官方通道)。

- **图片上传**:`POST /api/uploads/images`(apikey,multipart,1–18 张,Pillow 真解验证)→ 落盘
  `data/uploads/{batch_id}/` + 返回图片 URL(顺序即页序);`GET /uploads/{batch}/{n}`(免鉴权取图,
  随机 batch_id + fullmatch 白名单 + resolve 前缀双层防穿越);`/upload` 拖拽上传页(页内填 apikey);
  `upload_batches` 表 + 7 天懒清理。解决"base64 塞不进 MCP 工具参数"——图变 URL 后复用发布链零改。
- **薄 MCP facade**:`/mcp`(Streamable HTTP,host_origin_protection=False,combine_lifespans)7 工具
  (whoami/list_accounts/publish_note/get_publish_status/list_publish_jobs/check_cookie/get_extension_info)
  httpx 自转发本机 REST,apikey 从 MCP 请求头透传(static_headers 鉴权),facade 零业务逻辑、REST 是唯一真源。
  publish_note 只收 image_urls 绝不收 base64。新增依赖 `fastmcp`。
- **部署**:走 `systemctl restart`(ExecStartPre 自动 `alembic upgrade head` 建 upload_batches,先于 uvicorn,
  规避 create_all 抢建表)。claude.ai 侧需 static_headers 连接器 beta(向 mcp-review@anthropic.com 申请)。

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
