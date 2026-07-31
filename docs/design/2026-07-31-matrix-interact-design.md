# 矩阵互动(点赞 / 收藏 / 评论)设计

日期:2026-07-31
状态:已实验验证,待实现

## 一、背景与实验结论

需求:某个账号发布笔记后,矩阵内其余账号在 10 分钟内的随机时刻前往该笔记点赞、收藏、评论。

实施前先做了真号可行性实验(headed 真屏 `DISPLAY=:0`,全程 `SyncHumanActions`),结论如下。

### 1.1 路径可行,已跑通

用 NBDpsy-好好生活 的 cookie 打开 NBDpsy-我们都有病 的主页
`https://www.xiaohongshu.com/user/profile/{user_id}`:昵称、10 张笔记卡片正常渲染,
无登录墙。点进第一篇笔记后 **URL 自动带上 xsec_token**
(`?xsec_token=AB4ix-...&xsec_source=pc_user`)。拟人化点击点赞按钮后图标
`#like → #liked`、赞数 `1 → 2`;换 NBDpsy 这个号重开同一篇笔记复核赞数确为 2,
证明落库成功而非前端假象。

### 1.2 三个关键发现(实现必须遵守)

**发现一:库里没有任何真实笔记链接。** `publish_jobs.note_url` 存的是 creator
发布成功页(`https://creator.xiaohongshu.com/publish/success?...`),`note_id` 全部为
空字符串,`note_metrics` 只有标题没有 id。因此**互动任务不能依赖预存的笔记 URL**,
必须走"主页路径"现场定位。xsec_token 由当前会话生成,主页路径自洽,无需预存。

**发现二:旧仓的已点赞判定是错的。** 旧仓
`小红书运营工具/backend/app/services/xhs_playwright_client.py:1541` 用
`already_liked = "like-active" in class` 判断。实测 `like-active` 这个 class
**点赞前后常驻**——两个不同账号的会话打开同一篇未赞笔记,class 均带 `like-active`
而图标为 `#like`。真实状态在 `use[xlink:href]`:

| 图标 href | 含义 |
|---|---|
| `#like` | 未点赞 |
| `#liked` | 已点赞 |
| `#collect` | 未收藏 |
| `#collected` | 已收藏(待实现时复核) |

照搬旧仓判定会 100% 误判为"已点赞",导致要么全部跳过、要么把点赞记成取消点赞。

**发现三:`.not-active.inner-when-not-active` 不是遮罩层,是未激活的评论入口。**
旧仓 `comment_note` 用 JS 把它 `display:none` 隐藏,是把入口当障碍物拆了。实测该元素
可见、`pointerEvents: auto`、文案"说点什么...",尺寸 167x40。未激活态下
`#content-textarea` 中心点被一个 SPAN 覆盖(`elementFromPoint` 命中 SPAN 而非输入框),
发送按钮为 `button.btn.submit.gray`(gray = 禁用)。

## 二、目标与非目标

**目标**:发布成功后,矩阵内其余账号在 10 分钟窗口内的随机时刻,对该笔记执行
点赞 + 收藏 + 评论。

**非目标**:
- 不做转发(用户已明确去掉;PC 端 share 需二次选渠道,且矩阵互相分享无真实分发价值)
- 不做评论文案生成。评论文案是**入参**,后续承载营销钩子话术,由调用方/配置提供;
  本期只保留评论能力与接口,不内嵌 LLM 生成。

## 三、风险声明(已向用户披露,用户选择按原方案执行)

本方案的执行策略存在以下风险,实现方需知悉,不得自行弱化:

1. **同机同出口 IP**。6 个号全在同一台机器跑 camoufox,per-account 指纹隔离解决浏览器
   指纹,解决不了出口 IP。全员互动会把风控图上原本孤立的 6 个点连成完全图,并随每次
   发布重复。
2. **100% 到场率**。真实用户到达稀疏、随机、有缺席;每篇必到一个不落是易检出特征。
3. **收益有限**。早期流量池看互动率与完播而非绝对数;被判定为矩阵互赞后互动不计权。
4. **新号敏感**。NBDpsy-聊创伤 与 NBDpsy-亲密关系 正在申请数据看板权限,属新号阶段。

已建议但用户未采纳的保守版:稀疏抽样 1-2 个号、窗口拉长至 30 分钟-6 小时、只点赞、
新号不参与。若后续要切保守版,本设计的动作层与调度层可直接复用,只需改选号与排期策略。

## 四、设计

### 4.1 矩阵定义

库中无 matrix / group 表。`operator_account_access` 显示 op3(管理员) 与 op6(佰亿) 各持
5 个号,而 NBDpsy-好好生活 仅归 op7(woo)——按 operator 划分会把它永久排除,且 op4(苏澜)
名下仅 1 个号则自身无矩阵可言。operator 是权限维度而非矩阵维度。

**定义:矩阵 = 全部 `cookie_status='valid'` 的账号,排除发布者本人。**

### 4.2 触发与调度

发布成功钩子有两处,与 `archive_published_job` 同址挂载:

- `app/account_worker.py:198`(账号级进程隔离 worker,生产主路径)
- `app/publish/scheduler.py:355`(all 模式回滚位)

钩子内为每个矩阵账号登记一条 `browser_jobs`(`kind='matrix_interact'`),各自分配
窗口内的随机执行时刻。

**硬约束:延时必须落库排期,不得靠进程内 `asyncio.sleep` / `time.sleep` 等待。**
任务领取后干等会占死全局浏览器闸(`browser_slot`),5 个号最多干等 10 分钟将阻塞
cookie_check / note_export / 发布等所有浏览器任务。

`browser_jobs` 现无 `scheduled_at` 列。实现方案二选一,择简:

- 加 `scheduled_at` 列(alembic 迁移)+ 派发侧过滤 `scheduled_at <= now`;
- payload 内放 `not_before`,派发侧用 `json_extract` 过滤。

选前者需注意:api 单元有 `ExecStartPre=alembic upgrade head`,迁移会自动生效;
迁移文件必须与代码同一次提交进 main(参见幽灵迁移事故)。

`matrix_interact` **非幂等**(重复执行会取消已点的赞),不得加入
`_IDEMPOTENT_KINDS`,僵死后置 error 不自动重跑。

### 4.3 笔记定位:标题匹配,匹配不到就放弃

发布成功时 `note_id` 为空,故互动方需现场定位目标笔记:

1. 拟人导航至发布者主页 `xiaohongshu.com/user/profile/{publisher_user_id}`
2. 读取笔记卡片列表(`section.note-item`)的标题
3. **按 `publish_jobs.title` 匹配**目标卡片,匹配不到则返回 error 放弃

不得默认取第一篇。窗口内发布者可能发了多篇,取第一篇会点错笔记。

### 4.4 互动动作(全程拟人化,零 JS 注入)

所有交互经 `SyncHumanActions`,禁止 `element.click()` / `page.evaluate` 触发点击 /
JS 设值 / `keyboard.type` 直灌。`page.evaluate` 仅可用于**只读取证**(读 class、
读图标 href、读文本),与 `creator_export` 读表格行数同性质。

互动栏容器 `.interactions.engage-bar`,三个按钮 `.like-wrapper` / `.collect-wrapper` /
`.chat-wrapper`。

**点赞**:读 `.like-wrapper use[xlink:href]`,为 `#liked` 则已赞、跳过(记 skipped 非
error);为 `#like` 则拟人点击,点击后复核变为 `#liked` 方算成功。

**收藏**:同构,`.collect-wrapper`,`#collect` → 点击 → 复核 `#collected`。

**评论**(文案由 payload 传入,为空则跳过评论):

1. 拟人点击 `.not-active.inner-when-not-active`(或 `.engage-bar .inner`)激活输入区
2. 轮询等待 `#content-textarea` 真正可交互(`elementFromPoint` 命中输入框而非 SPAN)
3. 拟人点击 `#content-textarea` 聚焦
4. `human.type_text(el, text)` 逐字输入(自带节奏与偶发退格)
5. 轮询等待 `button.btn.submit` 去掉 `gray` class
6. 拟人点击发送,复核评论已出现在列表

任一动作失败不阻断其余动作;整体结果按动作粒度汇总返回。

### 4.5 拟人化补强

进入笔记后不得秒进秒赞。先 `human.scroll` 浏览正文、停留随机时长,再执行互动;
多个动作之间插入随机间隔。

## 五、验收

1. 单元测试覆盖:矩阵选号(排除发布者/排除失效 cookie)、标题匹配定位、
   已赞已藏的跳过分支、评论文案为空时跳过评论。
2. 真号 e2e:触发一次发布,确认矩阵账号在窗口内完成互动,`browser_jobs` 落 done,
   笔记赞数/收藏数/评论数实际增长。
3. 全程 headed 真屏,日志可见 `SyncHuman` 动作轨迹。
4. `matrix_interact` 未进 `_IDEMPOTENT_KINDS`。
5. 无任何 JS 注入式点击/输入。
