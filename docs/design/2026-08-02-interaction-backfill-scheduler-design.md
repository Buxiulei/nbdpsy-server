# 补量自动续跑调度器设计

日期:2026-08-02
状态:已实现

## 一、为什么要有它(这是缺口,不是增强)

`2026-08-02-interaction-backfill-design.md` 把补量能力做齐了:三种 scope 的 REST 手工触发、
台账同步发现手工新增笔记后自动派单、四层节流、撞墙即停。**但没有任何东西负责"把存量补完"。**

存量规模(2026-08-02 实测):

```
公开笔记 158 篇 × 有效号 7 个,排除给自己点赞
应互动组合   948
已完成        44
待做         904
日上限 20/号 → 7×20 = 140 篇/天 → 约 6.5 天
```

也就是说:**没有本组件,这个功能只有在有人连续六天、每天手动 POST 一次
`/api/interaction-backfills` 的前提下才跑得完** —— 它交付了却完不成自己的活。
原设计第六节写明"全量补完需要约 6 天,这是设计意图不是性能问题",但没人负责把这六天走完。

## 二、它做什么(只有一件事)

每 `INTERACTION_BACKFILL_INTERVAL` 秒醒一次,问一句"现在还有得补吗",有就往
`browser_jobs` 放一条 `interaction_backfill` 的 `queued` 行。**选谁去补、补哪几篇、
日上限、单轮上限、冷却、优先级——一条都不在这里判**,原样交给 `plan_round`。

这条边界是刻意的:那些闸是补量的**核心风控**,复制一份到调度器里就等于埋下两套口径迟早
对不上的雷。调小扫描间隔只会让它更频繁地问,答案照样受 `plan_round` 的闸约束;
**真正决定"多久补完"的是日上限,不是这个间隔值。**

## 三、三条与同族调度器一致的纪律

### 3.1 直插台账,不走 `start_backfill`

`start_backfill` 里有 `spawn_inline`,会在**当前进程**把任务跑掉。本组件活在 supervisor
进程里,那等于让 supervisor 自己起浏览器 —— 违背 API/Worker 拆分里
"supervisor 只派发、浏览器只在 account_worker 子进程"的规矩。

插一条 `queued` 行,supervisor 照常派子进程,与手工触发**完全同路**。
`list_dispatchable` 只要求 `status='queued'` 且 `payload.not_before` 未来时刻不挡,
直插行天然满足(`DraftCleanScheduler` 是同款先例)。

### 3.2 在途就不叠

一轮补量要十几分钟(篇间刻意停 60-240 秒),扫描间隔比它短是常态。不挡这一下,
队列里就会堆一串同号补量任务 —— **而"队列里堆着一串补量任务"正是被平台看出补量特征的
样子**。判据:存在 `kind='interaction_backfill'` 且 `status in (queued, running)` 的行即跳过。

终态(`done` / `error`)**不算在途**,否则补完第一轮就再也不会有第二轮。

### 3.3 挑不出来就不登记

`plan_round` 挑不出 actor(都到日上限 / 没得可补了)时直接跳过 —— 开一个注定空转的浏览器
任务毫无意义,还白占号锁与浏览器闸。

日配额归零靠的是 `plan_round` 内部的 UTC 日界,本组件**不需要知道"今天"是哪天**:
配额吃完后它每轮都挑不出 actor,自然空转到次日零点,不必额外写跨日唤醒逻辑。

## 四、只登记 `scope=all`

存量补量就是"所有号的公开笔记互相补齐"。`account` 与 `newcomer` 是**运营带着意图**手工
发起的(某个号要冲、某个新号要融进矩阵),由 REST 触发 —— 自动续跑不替运营做这种决定。

## 五、间隔取值

默认 `INTERACTION_BACKFILL_INTERVAL=1800`(30 分钟)。

一轮约 15 分钟,配上在途去重,实际节奏是"跑一轮 → 空一段 → 再跑一轮",约 2 轮/小时
= 10 篇/小时。矩阵日产能 140 篇需要约 14 小时,**活动自然摊在一天里而不是集中在几小时**
—— 这正是补量要的形状。调小它不会更快(日上限才是瓶颈),只会让活动更集中。

## 六、验收

单测 `tests/test_interaction_backfill_scheduler.py`:

1. 有存量 → 登记一条 `queued`,`account_id` = plan_round 挑出的 actor,payload 与
   `start_backfill` 同构且 `limit=None`(执行时再挑一次篇,拿登记那刻的快照去做等于绕过日上限);
2. 在途(`queued` / `running`)→ 一条都不加;
3. 终态历史任务不挡下一轮;
4. `plan_round` 返回空 → 不登记(直接 patch `plan_round`,不构造数据 —— 构造数据等于在测
   `plan_round`,那是另一个文件的事);
5. 别的 kind 在途不该挡补量。

## 七、部署注意

本组件活在 supervisor 进程里,**新增 config 字段 + 改 `app/worker.py`,必须重启
`nbdpsy-worker`**(与只改动作层的取证不同 —— 那个靠 account_worker 每任务新起子进程,
合并即生效)。

`interaction_backfill` **非幂等**,不在 `_IDEMPOTENT_KINDS` 里:重启会把在途那轮判 `error`
且不重跑。所以**重启要挑没有在途补量任务的空档**,否则白烧那一轮的当日配额。
