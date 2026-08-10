# 小红书代管账号计划 —— 设计

日期:2026-08-10
状态:已实现(0.22.0)

## 一、需求原文(运营侧,一字不折)

1. 加入代管的账号统一管理;发笔记时**除非特指账号,默认所有代管账号一起发**;
2. 非代管账号 = 水军号(现有互动任务行为不变,只是语义标记);
3. 每个代管账号可设笔记数量上限,默认 100 篇;每天拉取数据后发现超了,把(浏览 / 点赞 /
   收藏 / 评论 / 增粉)**加权平均最低**的几篇删掉,维持上限;
4. 全部后台处理,通过 API 给前端控制。

## 二、为什么不新造执行域

代管计划落在三件既有件之上,一行执行链路都不新增:

| 需求 | 复用的既有件 |
|---|---|
| 每日数据拉取 | `NoteMetricsScheduler` + `note_metrics` / `note_metrics_daily` 两表 |
| 笔记库存 | `published_notes` 永久台账(note_id + title + platform_published_at) |
| 删除动作 | `browser_jobs` 的 `note_delete` kind(真号已验的删除链) |
| 调度器骨架 | `CookieChecker` / `NoteMetricsScheduler` / `DraftCleanScheduler` 同一模板 |

新增的只有:两个账号列、一张审计表、一个调度器、四个 REST 端点,以及 publish 入口的
account_id 可选化。

## 三、D1 数据模型

### 3.1 xhs_accounts 两列

```
managed   BOOL NOT NULL DEFAULT 0   -- 1=代管账号(内容号),0=水军号
note_cap  INT  NOT NULL DEFAULT 100 -- 该号笔记数量上限
```

迁移**不 seed 任何账号**:全部留在 `managed=0`,谁进代管由 `PUT /api/accounts/{id}/managed`
显式开启 ——「加入代管」本来就是个显式动作。

> 初版设计写过一条 seed:「凡 `published_notes` 里有过记录的账号一律 managed=1」,依据是
> "发过内容的就是内容号",打算一次性把 1/2/5/6/7/8 带进代管、让 9-12 那批纯互动号自然留在 0。
> **实查生产库后推翻**:9-12 名下同样有 `published_notes` 行 —— 那是台账**全量同步**捞回来的
> **他人个人笔记** orphan 行,不是我们发的内容。照这条 seed 上线,水军号会被静默标成代管号,
> 之后不带 `account_id` 的发布会广播到它们,而且它们会进入淘汰作用域开始删笔记。
> 教训:**「表里有行」不等于「这些行是我们的」**。

`managed=0` **不改变任何既有行为** —— 互动补量、矩阵互动、cookie 巡检、数据采集全部照旧
覆盖全部账号。这一列只是给"广播发布"与"笔记淘汰"两个新功能划定作用域的语义标记。

### 3.2 retention_runs 审计表

```
id / account_id / run_date(UTC 日) / platform_note_count / cap /
eligible_count / deleted_count / dry_run / details_json / created_at
```

**为什么必须有**:淘汰是不可逆删除。没有这张表,"今天删了谁、它当时几个赞、凭什么是它"
在删除动作发生后就永远查不回来了 —— 而这正是运营第一时间会问的三个问题。
`details_json` 存**全量**得分明细(不只是被删的那几篇):每篇的五指标、归一化后的加权
得分、是否入淘汰名单、真建了删除 job 的话 job_id 是多少。

`platform_note_count` 这个列名是历史称呼,**它存的是台账计数**(见第七节的漂移说明)。

## 四、D2 广播发布

`POST /api/publish-jobs` 的 `account_id` 转可选:

- **省略 / null → 广播**:给每个 `managed=1` 且调用方有权限的账号各建一条发布任务,
  同一份 payload(images/video/audio 都是服务器侧路径或 URL,天然可共享),响应
  `{broadcast: true, jobs: [{account_id, job_id}, ...]}`;
- **传了 → 一字不变**(这就是需求里的"特指账号")。

校验顺序刻意保持:**先鉴权、再跑既有的三选一 / 图片张数校验,最后才展开广播**。
广播时每个账号**各自**算日上限顺延与引用推导 —— 一个号今天到量顺延到次日窗口,不能
牵连别的号;引用推导本就是"只引本账号自己的咨询师推介笔记",逐号算才对。

零代管账号 → 422 明说(不静默发 0 条);有代管账号但调用方一个都没授权 → 403。

## 五、D3 每日淘汰(RetentionScheduler)

结构套 `NoteMetricsScheduler` 模板:`start / _run_loop / scan_once / stop`,
`RETENTION_CHECK_INTERVAL > 0` 才注册,注册点与它完全同款(worker 进程的
`Supervisor._start_components`)。

### 5.1 每 tick 的两道前置

1. **本 UTC 日已跑过就跳过**(retention_runs 有当日行);
2. **要求当日 note_metrics 快照已存在** —— 需求原话是"每天拉取数据后发现超了",淘汰
   必须挂在数据拉取**之后**。没有当日快照就本 tick 跳过等下轮,绝不拿隔夜数据杀笔记。

### 5.2 逐号流程

0. **先收敛**:把上几轮建的删除任务对一遍 `browser_jobs` —— 终态 `done` 且 `deleted ≥ 1`
   的,给对应台账行落 `deleted_at`;还在 `queued`/`running` 的,记下来本轮不重复选。
   没有这一步就是幽灵重删:删掉的笔记仍在库存里计数,天天被重新选中建删不到东西的任务;
1. 库存 = `published_notes` 该号 `deleted_at` 为空的行(我们的台账真值);`count <= note_cap`
   → 记一条 run(deleted_count=0)完事;`note_cap` 缺失或 ≤0 → **回退默认 100** 并在
   details 记一条告警(绝不当 0 用,那等于按日封顶把号一路删空);
2. 超限 → 候选 = 库存里**发布超过 `RETENTION_GRACE_DAYS`(默认 7 天)**的笔记;
   **标题在该号台账里不唯一的,那几篇整批排除**(删除按标题定位卡片,同名会删错人),
   删除任务在途的那几篇也排除;
3. 每篇 join 最新 `note_metrics`:`(account_id, title 精确, publish_time 的日期 ==
   platform_published_at 换算到北京时间的日期)`。**join 不上的不进淘汰名单**,
   进 details 记「无指标跳过」;
4. 评分 = 五指标(views/likes/collects/comments/follows)各自在**候选集内**
   min-max 归一化,再按 `RETENTION_WEIGHTS` 加权平均;
5. 淘汰数 = `min(count - cap - 在途删除数, RETENTION_DAILY_DELETE_MAX)`,取分最低的 N 篇,
   逐篇建一条 `note_delete` browser job。在途数要扣掉:那几篇的删除已经承诺出去了,
   不扣就会在没跑完的删除之上再叠一批,最后删过头;
6. **先落审计行再建删除 job**。

### 5.3 三条安全轨(它们是这个功能的主体,不是附加项)

| 轨 | 配置 | 挡住的是什么 |
|---|---|---|
| ① 宽限期 | `RETENTION_GRACE_DAYS=7` | 新笔记指标未成型,不设宽限期会天天误杀刚发的内容 |
| ② 无指标不杀 | 无配置(硬规则) | join 不上 = 我们对这篇一无所知,删它等于抽签 |
| ③ 单日单号删除封顶 | `RETENTION_DAILY_DELETE_MAX=5` | 首次启用时库存可能远超上限,防"第一天大屠杀" |

外加一个 kill switch:`RETENTION_ENABLED=0` → **照常算、照常落审计,只是不建删除 job**
(审计行 `dry_run=1`)。要观察这套评分选谁,就把它关着跑几天看 retention_runs。

### 5.4 时区口径(容易错的地方)

- `published_notes.platform_published_at` 是 **naive UTC**(由平台 `visible_time`
  unix 秒转,见 `note_ledger.platform_published_at_of`);
- `note_metrics.publish_time` 是创作中心 Excel 导出的**北京时间原文串**
  (「2026年05月22日10时59分14秒」)。

所以 join 前必须把 UTC 时刻 +8 小时再取日期,否则北京时间 00:00-08:00 发布的笔记
全部 join 不上(会被安全轨②默默排除,表现为"这些笔记永远不参与淘汰")。

## 六、D4 管理 API

| 端点 | 作用 |
|---|---|
| `GET /api/managed-accounts` | 全账号(按权限收窄)的 managed / note_cap / 笔记数 / 最近一次淘汰 |
| `PUT /api/accounts/{id}/managed` | 改 managed 与 note_cap(1-1000) |
| `GET /api/retention-runs` | 审计流水,details_json 解开返回 |
| `POST /api/retention-runs` | 手动触发一次,**dry_run 默认 true** |

`POST` 的 `dry_run` 默认 true 是前端控制面的安全默认:先看"将删名单 + 每篇得分",
确认无误再 `dry_run=false` 真删。

**`dry_run=false` 要过三道闸**(任一不过整批 409、一条任务都不建):①`RETENTION_ENABLED=0`
时不许真删(kill switch 对手动触发也生效,否则这个开关等于没有);②该号当天已有真删轮次
(自动或手动)不许再叠;③当天已建的 `note_delete` 数已达 `RETENTION_DAILY_DELETE_MAX` 不许
再建,本次实际额度 = 封顶 − 当天已建。**封顶按当天累计算,不是按每次运行算** —— 否则连点
三次就是 3×封顶,手动与自动跑同一套选篇代码却受不同限速,那不是"手动更灵活",那是绕闸。
预演不受这三道限制(没有副作用)。

**手动 dry-run 不落审计行**(真删才落)—— 否则一次预演就会给当天留下 retention_runs
行,把调度器当天的真实轮次吃掉,变成"点了一下预览,今天就不淘汰了"的静默失效。

鉴权与既有端点同门:指定 account_id 走 `assert_account_access`,不指定则按
`visible_account_ids` 收窄(admin 全见)。

## 七、已知边界:台账计数 ≠ 平台真实笔记数

`published_notes` 是**我们的**台账,它与平台真实笔记数会双向漂移:

- **台账多于平台**:运营在小红书 App / 网页端手工删了笔记,台账不会自动少一行
  (`note_ledger` 同步补的是新增与字段,没有"平台上没了就删台账行"的逻辑)。
  后果:cap 被**虚高**的计数触发,可能删掉本不该删的一篇;
- **台账少于平台**:手工发布的笔记要靠台账同步(T2 全量列表同步)才会以 orphan 行
  落库;同步没跑或跑失败的窗口期内,库存被**低估**,该淘汰的没淘汰。

**本版不做对账**(需要给每号起一次浏览器会话拉全量列表,与淘汰同轮跑会把会话预算打满)。
代之以三条护栏把漂移的后果压到可接受:①单日单号删除封顶 5 篇,漂移只会让淘汰慢几天,
不会一次删错一批;②无指标不杀 —— 平台上已经不存在的笔记不会有新的指标快照,过一阵子
它的 `note_metrics` 就是陈旧行,而真正被 join 上的都是采集当天还在平台上的笔记;
③全量审计,事后能查"当时按什么数删的"。

代码里在三处显式标注了这条漂移:`RetentionRun.platform_note_count` 列注释、
`plan_account_retention` 读库存那一段、以及 guide 的 `KNOWN_LIMITATIONS`。

另一条继承自删除链的边界:`note_delete` 是**按标题删除**的(平台导出无 note_id,删除
只能在笔记管理页按标题定位卡片)。同一账号存在同名笔记时,删除的可能是同名里的另一篇 ——
所以淘汰侧的处置是**同名整批不删**(见 5.2 第 2 步):那几篇一律不进候选,审计里记
「同名歧义跳过」。代价是它们永远不参与淘汰,换掉的是删错人的可能;要让它们能被淘汰,
只能先把标题改成不重名的。

第三条与收敛有关:删除任务落 `error`(含 server 重启导致的 `unknown`)时那篇会重新进候选
下轮再试 —— 删除确实没生效时重试是对的。若是 `unknown` 而平台上其实已删,下一轮会因找不到
同题卡片再落一次 `error`,表现为审计里反复出现同一篇;跑一次台账同步让库存落回真实值即可。

## 八、配置

| 键 | 默认 | 语义 |
|---|---|---|
| `RETENTION_ENABLED` | 1 | 总开关;0 = 只审计不删(kill switch) |
| `RETENTION_GRACE_DAYS` | 7 | 发布不足这么多天的笔记不参与淘汰 |
| `RETENTION_DAILY_DELETE_MAX` | 5 | 单日单号删除封顶 |
| `RETENTION_WEIGHTS` | `{"views":1,"likes":2,"collects":3,"comments":3,"follows":5}` | 五指标权重(JSON 串;解析失败回退默认并告警) |
| `RETENTION_CHECK_INTERVAL` | 3600 | 扫描间隔秒,0 = 关闭(不注册调度器) |
