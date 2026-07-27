# Changelog

## 0.14.0 (2026-07-27)

**publish 任务时间字段读回带显式 `+00:00`**——消除"裸 UTC 串被当本地时间"的歧义。

库里存的是 naive UTC(与调度器 utcnow 基准一致),但 REST 读回 `.isoformat()` 吐的是裸串
(如 `2026-07-27T10:00:00`),运营/agent 自然当成北京时间,实际是 UTC 10:00 = 北京 18:00。
排查定时任务时正是被这个读回格式误导过。系统**接收时认时区偏移、吐出时却丢偏移**,亲手制造误读。

- `_job_view` 的 `schedule_time` / `next_retry_at` / `created_at` 三字段读回补 `.replace(tzinfo=utc)`,
  产出 `2026-07-27T10:00:00+00:00`。**数值一位不变**,只把 naive datetime 本就代表的 UTC 显式标注。
- **不转 +08:00**:那会把 Beijing-only 假设焊进通用 REST 契约、让钟点数字漂移(10:00→18:00)、
  与存储/调度器锚定 UTC 的语义分裂,制造二次歧义。要看本地时间由消费方自行 astimezone。
- 存储层、调度器(worker/publish scheduler 的 utcnow 比对)、入口 `_parse_schedule_time` **一律未动**。
- skill 侧对这些字段全是 dict 透传/字符串相等、不做 fromisoformat 解析,**运行时零破坏**。
- manifest 补一条读回口径说明。范围只治报告点出的事故字段(publish-jobs);其余 8 处 created_at/
  last_login 等信息展示字段暂不动,避免"有的带偏移有的不带"的新不一致——若要全仓统一应作一次性批量决策。
## 0.13.0 (2026-07-27)

**发布口 fail-closed 去水印闸** —— 发的图必须是去过水印的,不再靠"建任务时塞进来的图恰好干净"。

事故背景:生图侧的去水印(reraster 主路)**只在生图那一刻跑**;发布任务把图片以 base64 快照
存进 `publish_jobs.images_json`,到点后 materialize 只是原样落盘上传,**整条发布链路零校验**。
用像素级判据(纯 JPEG 重编码 ≤1.03x vs 真 reraster 1.41x)实测确认:job 14/15/16(已发)
7/7 张全是未去水印的 provider 原图,job 23/24 各 1/8 张。

- **闸设在发布口**(`app/account_worker.py` 活路径 + `app/publish/scheduler.py` 停用回滚位):
  materialize 之后、交浏览器之前,对每张统一跑 `dewatermark_all`。发布时手上只有字节快照、
  **判断不了某张是否已清洗**(像素判据只看得出"有没有被重采样",看不出对应原图是谁),故不猜、统一重做。
- **fail-closed**:任一张去水印失败 → 抛异常整任务失败(落 error 排重试),**绝不降级"用原图发"**——
  那正是事故形态。闸在 materialize 之后 = **图片来源无关**,从资产库复用再发也照样过闸。
- 归档保留原始母版(不改):复用链路仍过闸,且从母版做一次 reraster 比"已 reraster 再 reraster"质量更好。

**同批热修:reraster 不认相对路径(线上事故)**

- `reraster_image` 内部 `Path(path).as_uri()` 对相对路径直接抛
  "relative path can't be expressed as a file URI"。生图侧一直传绝对路径所以从未暴露,
  而发布口的 workdir 是 `settings.UPLOAD_DIR` 下的**相对路径** → 07-27 09:00 那批定时发布
  被闸全数拦下(**fail-closed 生效,没发出带水印的图,但也没发出去**)。改为 `.resolve().as_uri()`。
- 教训:最初"拿真实待发图跑一遍闸"的验证用了 `tempfile.mkdtemp()` 的**绝对路径**,没还原生产的
  相对路径形态,放过了这个 bug。已补相对路径回归测试 + 变异验证(去掉 resolve 立刻红)。
- 生产验证:两条被拦任务复位重试后 published(图经去水印);18:00 job28 准点发布成功。

## 0.12.0 (2026-07-27)

skill 侧第三批 + 一次安全告警的核查。**两条真缺陷是对抗验证抓出来的,不是实现阶段发现的。**

**安全告警核查:`admin-default` 权限失效——不成立**

消费方实测 \"operator key 打 `PUT /api/style-profile/admin-default` 返 422 而非 403\",
判定为越权。实测证伪:他们为避免真写入而**故意不带 `profile`**,而 FastAPI 先验 body 再进 handler,
缺字段必然在权限检查之前 422 ——**这个测法在构造上就够不到权限关**。带合法 body 的 operator key
拿到 403,默认档案纹丝未动。全仓 admin 守卫都是 handler 内首行 `require_admin(current_operator())`
(见 admin_rest.py),不存在漏挂,故**不改守卫写法**(只改一个反而制造真的不一致)。

**采纳其系统性建议**:新增 `tests/test_admin_only_sweep.py`,拿 operator key 遍历 manifest 全部
10 个 `admin_only` 端点断言 **只接受 403**(写成 `in (403,422)` 就等于把判别力扔掉),
外加**完备性断言**——新增 admin_only 端点却没配合法 body 会直接变红。变异实证:
把 `require_admin` 打成空操作后 10 个里 8 个变 200/404,证明这条防线不是恒真断言。

**新增 `GET /api/style-profile/admin-default`**

管理员默认档案此前**只写不读**:它不进版本历史、不可回退,manifest 却写着\"改前请自行留底\"——
而没有任何端点能读回它,留底实际做不到。更隐蔽的是 `GET /api/style-profile` 在运营**尚未建档时
恰好**能读到默认配置,一旦他建了档同一条命令悄悄变成读自己那份、照样 200 不报错。
按消费方理由**不设 admin 门**(读没有敏感性,未建档者本来就能从另一条路读到同样内容,挡了反而不一致)。

**「最后一个管理员」硬保护(三条等价路径)**

0 个管理员 = 管理面永久瘫痪(`POST/GET/PATCH/DELETE /api/operators` 全是 admin_only)。
`update_operator` 的降级(role→operator)与停用(enabled→false)、`delete_operator` 的删除,
三条路径共用 `_ensure_admin_remains`:**先应用改动再在同一事务内统计**,为 0 则回滚 + 409。

- **对抗验证抓到的 blocker**:delete 起初用 `if was_effective_admin` 当门,而该判断取自
  **写事务之外的陈旧读**。三条普通并发 HTTP 请求即可绕过——DELETE 读到 bob 还是 operator →
  另一请求把 bob 提成 admin → 第三个请求降级 root(此刻统计看到 bob 是 admin,放行)→
  DELETE 才拿到写锁,删掉 bob 且**跳过判定** → 0 管理员(时序网格 35/120 命中,最差配置 75%)。
  已改为**删到行就无条件判定**;真实 HTTP 并发 40 轮(5 组时序)复打该路径,0 次击穿。
- 回归钉用确定性性质而非并发时序(并发测试必 flaky):无管理员的库里删**普通运营者**须 409 ——
  谁把那道门加回来,这条立刻红(已变异实证)。

**`/notes` 的 `field_meta` 收窄 + `field_notes["排序"]` 停止撒谎**

- `field_meta` 原声明 19 字段却只下发 11 个平台原生列(6 个派生率只在 `/note-trends` 有),
  数分 agent 照口径去找会当成数据缺失。收窄的筛子复用 `_METRIC_FIELDS` 这个真源,不是手抄清单。
- **对抗验证抓到的 major**:同一 meta 块里 `field_notes["排序"]` 仍写着\"notes 按最新 views 降序\",
  而 `list_notes` 实际是 `order_by(NoteMetric.id)`(入库序)、且 `/notes` 根本不下发 series。
  真实数据实证:账号 7 的 views 序列 `[528,396,375,1664,...]` 非降序,**真 Top1 排在第 4 位**——
  agent 取 `notes[:5]` 当 Top5 就会拿错。根因是端点相关的断言放进了端点无关的共用常量,
  同一份 meta 挂两个端点必然对其中一个撒谎。现 `ordering` 改为**必填关键字参数**,三个调用点各说各的真话;
  测试同时盯\"实现的真实排序\"与\"meta 的说明\",三次变异(改实现/改文案/改第三个调用点)均能致红。

**已知残留(不修,记录在案)**

- 硬保护的并发正确性依赖 SQLite 写事务串行。`app/core/db.py` 保留着 Postgres 迁移路径——
  切到 PG 默认 READ COMMITTED 后两个并发事务更新不同行、互不可见,可能双双放行。**迁移那天必须重验此处。**
- 并发 PATCH 撞上正被删除的同一行 → `StaleDataError` → 500(既有形态,非本次引入)。

## 0.11.0 (2026-07-26)

skill 侧实测撞出来的第二批(数据口径 3 条 + 风格档案 4 条)。

**数据口径**

- **`follow_rate` 从「只告知」变成「有出路」**:新增同窗口径 `follow_rate_t1`
  = follows(T-1) ÷ **上一快照日**的 views,分子分母都对齐到昨日。
  先证伪了一条更省事的捷径:`cover_ctr` 官方定义是「截至昨日的封面点击÷封面曝光」,
  本想用 `exposure × cover_ctr` 在同一行里凑出 T-1 观看量(不必跨日 join),
  **实测不成立**——比值不收敛反随笔记变老降到 0.30,说明「观看」含大量非封面点击入口
  (主页/搜索/分享链接)而「封面曝光」只算信息流。故仍走跨快照。
- **偏差用生产数据量化,不再是一句「这数不准」**:07-25/07-26 两连续快照日、78 条笔记实测,
  30 天+ 中位偏低 0%(最大 4.8%)、14-29 天中位 0%(最大 5.9%)、3-6 天中位 4.8%(最大 18.2%)。
  desc 里**连样本量一起写**(0-2 天 n=1、3-6 天 n=4,证据弱照实标),并点明
  **发布当天/次日的值不是「偏低」而是「无意义」**(follows 是 T-1,还没覆盖发布日,必然趋近 0)。
  `follow_rate_t1` 的 desc 同样写明它**轻微偏高**(昨日快照只截到当时,follows 覆盖到 24:00),
  以及快照断档时偏高幅度按间隔放大。
- **口径随数据下发补齐最后一个缺口**:`field_meta` 原先只挂在 note-trends,
  而 `GET /api/accounts/{id}/notes` 下发 15 个指标字段却**零口径信息**——
  数分 agent 调它只能整份按「口径未知」保守处理。现两个端点共用 `field_meta_block()`。
- 顺带结案 `engage_rate`:已核实四项同为实时 T,不存在跨窗问题,desc 里写死免得再被问。

**风格档案**

- **`PUT` 响应带 `dropped_keys`**(顶层与二级,整段消失只报顶层名,无丢弃给 `[]` 不省略)。
  实测形态是 agent 只想改配色却只带了 palette + density 五字段 → 200 OK 而人物卡被清空,
  它恰恰绕过了 skill 侧唯一那道「density 缺失才告警」的防线。**不拦截不报错只告知**:
  这道比对只有 server 做得了——server 手上有上一版,skill 侧手上只有 agent 记得带的那份。
- 新增 `PUT /api/style-profile/admin-default`(require_admin):管理员默认档案此前**没有维护入口**,
  而它影响所有还没建档的运营。明确语义:未建档者**每次 GET 实时读最新默认档案**(非建档快照),
  故管理员一改沿用者立刻跟着变,`admin_default_version`/`updated_at` 供 skill 侧察觉变更;
  已建档者各自独立不受影响。该行**不进版本历史表、不可回退**(版本表 operator_id 是 NOT NULL
  外键,塞 NULL 要改表结构,不值当),已写进 manifest 免得误以为能 rollback。
- `GET /versions` 加分页 `limit`(默认 50,上限 200,超出钳制不报错)/`offset`,返回加
  `total`/`limit`/`offset`/`has_more`;`versions` 键结构不动。
- `GET /api/style-profile` 两种情形都给 `base_version`(无档案给 0),
  让「下一次 PUT 该传什么」有唯一真源,不再两端各推一次。

## 0.10.0 (2026-07-26)

**每用户风格档案**(style profile):视觉调性从写死在 skill 里的全局常量,变成按 apikey
认人的个人资产 —— 现有莫兰迪三色 + 固定人物卡降级为「管理员默认档案」,每个运营可有
完全自主的一套(配色 / 人物形象 / 版式 / 语气 / 信息密度)。

- 两表:`style_profiles`(当前态,operator 唯一)+ `style_profile_versions`
  (append-only 完整快照,`(operator_id, version)` 唯一)。存快照而非 diff,回退一步到位。
- 5 端点:`GET /api/style-profile`(带 **`exists`** 区分「有个人档案」与「没有、这是
  管理员的」——skill 安装引导据此决定说哪句话)/ `PUT`(乐观锁)/ `GET /versions`(轻,
  不含全文)/ `GET /versions/{v}`(预览)/ `POST /rollback`。
- **回退 = 以旧版内容造新版本,不是拨版本指针**:回退 v3 产生 v8,v4–v7 仍在历史里,
  「回退后又后悔」还能回去。历史长期保存不清理,只在删除运营账号时级联清空
  (SQLite 不开 `PRAGMA foreign_keys` 就不真级联,故清空在 `delete_operator` 应用层显式做)。
- **乐观锁补 TOCTOU 兜底**:前置校验「读 current_version → 比对 base_version」与写入
  不是原子的,两个会话同时带同版本时**双方都能通过校验**,最后只有唯一约束拦得住 ——
  但它抛 `IntegrityError` 会冒成 **500**,而契约承诺 409。现撞唯一键转正牌
  `VersionConflict`(带 `current_version` 供 skill 提示重读)。变异验证:摘掉该分支
  新增的竞态回归测试立刻变红。
- `profile` **原样存取**、不校验语义、不做 key 规范化:`density` 那五个中文 key
  (信息密度档位 / 每页文字量 / 每页信息点 / 版式档 / 运营原话)是 skill 侧 v1.37.0
  定死的跨端契约,改写成英文即断链。仅校大小(64 KB 上限,超限报错**不静默截断**)。

## 0.9.0 (2026-07-26)

笔记数据**逐字段口径随数据下发**(`meta.field_meta`),防 LLM 数分 agent 望文生义。

- 原 `field_notes` 是散文、只覆盖 3 个字段;现给 **18 个字段**(11 平台指标 + 7 派生)
  各配 `label` / `desc` / `window`(T 实时 / T-1 每天更新 / unknown)/ `unit` / `source`。
- **核实纪律:desc 与 window 全部抄自创作中心表头悬停 ⓘ 的官方原文,一个没猜。**
  首轮「分享 / 人均观看时长 / 弹幕」因在表格横向滚动区外抓不到,横向滚过去补抄成功。
  唯一 `unknown` 是 `reposts`——当前导出表根本没有这列。
  - 实时 **T**:观看 / 点赞 / 评论 / 收藏 / 分享 / 弹幕
  - 每天更新 **T-1**:曝光 / 封面点击率 / 涨粉 / **人均观看时长**
- **查实一个此前无人察觉的跨口径陷阱**:`follow_rate` = follows(T-1) ÷ views(T),
  分子分母不同期,与 `cover_ctr` 同构——系统性偏低且笔记越新偏得越多。已显式标注风险
  (daily 表无 T-1 的 views 快照,目前没有干净修法)。`engage_rate` 经核实**安全**
  (点赞/评论/收藏与 views 同为实时 T)。
- 修时间依赖 flaky:`test_daily_attempts_capped_at_three` 把记录造在 600 分钟前,
  凌晨跑会落到昨天被今日窗口过滤而变红。


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
