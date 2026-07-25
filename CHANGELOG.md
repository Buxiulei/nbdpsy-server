# Changelog

## 0.8.0 (2026-07-26)

去水印口径收严:**返回给 API 的图必须都是去过水印的**。

- **只认 reraster 主路**（`0d0ee71`）：原三级链的 ② PIL 像素重存**只剥 C2PA 元数据、
  一个像素不动**，像素级耐久水印与原图完全一样——在"会不会被识别成 AI 生成"上和交原图
  没区别，留着只会让人误以为已处理过；③ 直接交原图更是明着带水印出图。两级兜底全部删除，
  `dewatermark()` 失败返回 `None`。保留 reraster 的诚实声明：重采样是**扰动**而非保证清除，
  能否规避平台检测以平台真实行为为准。
- **去水印失败 = 该页失败**：`urls[i]=""` + `errors[i]` 写明原因，绝不拿带水印的图冒充交付
  （宁可这页没图，运营用 `--pages` 重出）。
- **原图另开提取通道**：原图无论去水印成败都归档成 `NN.orig.ext`，结果新增 `orig_urls`
  （与 prompts/urls/errors 等长同序）；`/uploads` 白名单只多放行 `.orig` 一种形态
  （免鉴权路由，白名单即访问控制）。`urls`/`errors` 既有语义零变更。
- **单页 rename 兜底**：改名失败只塌该位，不冒泡把整批（含已成功、已付费的页）判崩——
  与 `openai_image._edit_one`「保证该下标位不塌陷」同款纪律。
- **修 0.7.0 引入的回归**：`egress_guard` 原实现 `start()` 瞬间即探测，每次 worker 重启
  白起一个 camoufox，并在测试里真拉起 playwright 进程打红 supervisor 用例；改为首检延迟 60s。

**BREAKING**：去水印失败的页从「交带水印图」变为「该页失败」；`result` 新增 `orig_urls`（附加字段）。


## 0.7.0 (2026-07-26)

发布链路两处**上传后掉登录**根治 + 生图吞吐 5-7 倍 + 三条自动化补齐（数据采集 / 草稿清理 / 出口自检）。

### 发布链路

- **根治「上传后编辑器消失」**（`0f033b4`）：编辑器一打开，创作页就去问千帆商家后台
  `ark.xiaohongshu.com` 的带货权限；**曾绑过千帆的号**（NBDpsy聊心理 / NBDpsy 官号）收到 401，
  而创作页把**任意 401 当整体登录失效**，0.6~2s 内跳 `login?redirectReason=401` —— 编辑器连同
  已上传的图一并消失，只在草稿箱留一篇。修法是恢复 Firefox PAC 把 ark 单域打进死代理，
  让它变成**网络错误而非 401**。07-21 弃用 PAC 的两条依据今天均被实测推翻：①「PAC 会崩 driver」
  的崩溃（pageerror）已被全局错误吞噬根治；②「sing-box 直连后 ark 返 200」不成立——实证
  camoufox 27/27 走 direct-out、出口北京联通，ark 仍稳定 401。
  逐项证伪：图片体积/张数/格式、上传方式（`set_input_files` 与真点按钮 + `expect_file_chooser`
  同样被踢）、cookie 里的 access-token-*、profile 残留、page/context.route 拦截（都拦不到 ark，
  故必须用 PAC 这个浏览器全局代理层）。**矩阵验证 5 账号 × 3 次（每次清空 profile 全新浏览器）
  15 轮 0 次被踢**。
- **账号封禁即时捕捉**（`fecb74d`）：点发布后 0.2s 弹的 `d-new-toast`「因违反社区规范禁止发笔记」
  ~7.8s 即消失，旧逻辑 12s 轮询跑完才抓 forensic 必然扑空 → 干等 30s 报「未检测到成功标志」。
  改为密集轮询/等待循环/超时兜底三处内嵌即时扫描，命中封禁类置 `account_restricted=True`
  → 直接 failed 不重试（重发是更强封号信号）。实测 ~1s 明确收口。
- **发布/导出/删除 fast-path**（`19097c6` `b08fb6d` `43bc415`）：cookie 双域已登录 creator，
  直接 goto 目标页秒进，砍掉 explore 重导航 / 弹窗白等 / 开新窗口 / SSO 预热重试；
  固定 sleep 全面改条件等待。发布 71s→39s、检测 ~13s→6.6s、拉数据 ~46s→26s。
- **出口链路自检 `egress_guard`**（`568cf54`）：sing-box 里 camoufox→direct-out 的直连规则是
  磁盘配置，代理软件更新/重装会覆盖；规则一丢 camoufox 出国 → 风控 401 踢登录，**症状与
  ark-401 一模一样极易误判**。两级自检（读配置 + 起一次性空 cookie camoufox 实测出口地区），
  默认 6h，告警文案直接写明「勿误判为 PAC 失效」+ 修复步骤。

### 一致性生图

- **批量并发化**（`97730b7` `51a86b4`）：锚点法各页互不依赖（`images.edit` 无状态，一致性来自
  每张重传同一张 P1，与顺序无关），逐张串行改 `asyncio.Semaphore` 有界并发。一篇 9 页
  **7.5 分钟 → 约 50 秒**。硬约束：结果与 prompts **严格按下标对齐、失败位留空位占位**
  （下游按下标落 P01…PNN）。
- **429 指数退避**（`97730b7`）：`_is_rate_limit_error` 双层判定（结构化 + 中转拍扁后的纯文案），
  3 次 / 基数 2s / 抖动，moderation 明确不重试；撞限额不再直接判失败让运营手工 `--pages` 重出。
  另落 usage 日志（成本可核算）与 429 时的 `x-ratelimit-*` 响应头（真实限额的权威来源）。
- **进程级并发闸**（`687fbaa`）：页级 × 篇级是相乘的（10 路 × 10 篇 = 100 在飞），只靠调用方
  守约维持；本闸不依赖守约，超出排队不拒绝。闸只包住**真正在飞的请求**、不包退避 sleep；
  获取顺序恒为「页级 → 全局」故无死锁。上限 100 ≈ 120 张/分，占 gpt-image-2 Tier 5
  (250 IPM，已核实) 的 48%。

### 数据与内容

- **内容资产库**（`dcfd81a`）：发布成功自动归档（文案/标签/图片独立副本），跨账号复用 REST，
  取详情即刷新最后使用时间，90 天滑动 TTL。
- **笔记数据定时采集**（`717c1b6` `8c28a68`）：每号每天最多自动采 3 次、失败等比退避（1h→2h），
  覆盖全部已接入 cookie 的账号；新号 cookie 转 valid 即采一次作首次基底。
- **趋势分析包端点**（`92dd13f`）：`GET /api/accounts/{id}/note-trends` 一次返回账号级日汇总
  （含相邻快照增量与 `days_between`，防把断档增量当日均）+ 每篇最新态/率值/逐日序列，
  并随包给出口径说明，数分 agent 免二次组装。
- **导出 no_data 竞态修复**（`2c2e5d3`）：内容分析页表格是异步加载，单次数行会撞上未渲染窗口
  → 有 28 篇数据的账号被误报空表。改条件轮询，行一出现即通过。
- **草稿箱周清理**（`f253563`）：XHS 草稿存浏览器本地，发布编辑器被自动化打开即自动存空草稿
  持续累积；每号每 7 天清空全部四类 tab，防误删纪律与删笔记同款（确认弹窗必须含「删除」）。
- **账号名跟随昵称**（`041204d`）：cookie 检测写回时同步 `name`，运营在小红书改名后无需手工改。


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
