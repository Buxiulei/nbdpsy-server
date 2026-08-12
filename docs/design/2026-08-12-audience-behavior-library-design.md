# 受众行为库设计（互动者身份 · 纵向轨迹 · 群体切片 · 潜客漏斗）

- 日期：2026-08-12
- 状态：设计定稿，待实现
- 取证依据：`scripts/probe_notification_scene.py`（f9c12e3）+ 快照 `data/scene_captures/notification_probe_account_1_20260812T082020Z.json`（号1 实采 922 条互动 + 98 条关注，两年真触底）

## 0. 目标与合规边界（先读这一节）

老板需求：把「谁在跟我们矩阵互动」沉淀成一个**受众公开行为库**，支撑三件事——

1. **互动者身份**：认得出跟矩阵互动的每个人（站内公开身份）；
2. **纵向轨迹**：同一个人跨时间、跨自家号的互动序列，看得出关系怎么演变；
3. **群体切片 + 潜客漏斗**：把受众按行为分层，识别「高互动意愿但还没转化」的高潜人群，供运营做私域导流与选题决策。

### 合规硬边界（不可协商，写进代码注释与 REST manifest）

这是一个**有隐私维度**的系统，边界由内容运营划定、老板认可，实现必须守死：

- **只存平台在通知流里已经公开给我们的字段**：`/you/likes`、`/you/connections` 返回的 userid / 昵称 / 头像 / 关系状态 / 互动的公开笔记。不采集互动者主页、不进任何陌生人主页抓额外信息、不做人脸识别或去匿名化。
- **绝不建立 `actor_userid → 来访者真实身份（姓名 / 手机 / 预约记录 / 咨询关系）` 的任何关联**。库里不设、不留、不预留这类字段或外键。这是「受众公开行为分析」，不是「个人档案追踪」。
- **自家号排除**：分析查询默认排除本矩阵 10 个号的 `user_id`（从 `xhs_accounts.user_id` 现查，不硬编码）。自家号互刷产生的互动照常入库（它也是数据），但**默认从受众分析里剔除**。
- **头像 / 昵称按采集时快照存**，会变不追溯，不做历史画像比对。

## 1. 数据来源（取证结论，实现按此解析）

三个 endpoint，均在 `edith.xiaohongshu.com`，cursor 游标分页，响应 `data.message_list[]` + `data.cursor` + `data.has_more`：

| endpoint | 内容 | 号1 实采 |
|---|---|---|
| `GET /api/sns/web/v1/you/likes?num=20&cursor=<上页 data.cursor>` | 赞和收藏 | 922 条 / 47 页 |
| `GET /api/sns/web/v1/you/connections?num=20&cursor=…` | 新增关注 | 98 条 / 5 页 |
| `GET /api/sns/web/v1/you/mentions` | 评论和@ | 本版**不采**（取证未深挖，评论体系另说） |

**采集方式：UI 驱动被动监听**，与取证脚本同法——起号 → goto `/notification` → 拟人切 tab → 拟人滚动触发懒加载 → `page.on("response")` 被动截 `message_list`。**不逆向小红书签名头（x-s/x-t）直调 API**：那是脆弱且高风险的逆向，违背本仓拟人化红线。增量场景滚动量小（滚到已知最新事件即停）。

### 每条 likes 事件字段（脱敏样例，`liked/item`）

```json
{
  "id": "76xxxxxxxxxxxxxxxxx",          // 平台事件 id，去重键
  "time": 1786518603,                    // epoch 秒，精确时间
  "type": "liked/item",                  // 四态之一，见下
  "title": "赞了你的笔记",
  "user_info": {
    "userid": "5xxxxxxxxxxxxxxxxxxxxxxx", // 互动者稳定唯一键（24 位 hex）
    "nickname": "…", "image": "https://sns-avatar-qc.xhscdn.com/…",
    "fstatus": "both",                    // 关系：both 互关 / fans 他关注我 / follows 我关注他 / none 陌生人
    "indicator": "你的好友"               // fstatus 的中文标签，462/922 有值，不可当必填
  },
  "item_info": { "type": "note_info", "id": "6a7c…", "content": "笔记标题" }
}
```

### `type` 四态 + note_id 取法分叉（**解析器必踩坑**）

| `type` | 规范化 event_type | 目标笔记 id / 标题位置 | 号1 计数 |
|---|---|---|---|
| `liked/item`（`item_info.type=note_info`） | `like_note` | `item_info.id` / `item_info.content` | 373 |
| `liked/item`（`item_info.type=avatar`） | `like_avatar` | 无笔记（赞头像），`target_note_id=NULL` | 混在上面，需按 `item_info.type` 甄别 |
| `faved/item` | `fav_note` | **`item_info.attach_item_info.id` / `.content`**（`item_info` 本体是收藏夹 `board_info`） | 251 |
| `liked/comment` | `like_comment` | `item_info.id` / `item_info.content`（评论所在笔记） | 294 |
| `liked/share/item` | `like_share` | `item_info.id` / `item_info.content` | 4 |

connections 事件：`type="follow/you"` → `follow`；字段较薄：`user.userid` / `nickname` / **`images`（复数，与 likes 的 `image` 不同名）** / `fstatus`；顶层 `time` / `id`。**注意 connections 是历史事件流（含后来取关的），不是当前粉丝快照**——98 条 vs 主页 93 粉丝，口径不能混。

关键去重语义：**同一 `userid` 会多次互动**（赞了 A 又收藏了 B，或先赞后取消再赞），每次是不同平台事件 id。用平台事件 id 去重，一条互动一行。

## 2. 数据模型

模型套 `app/models/note_interaction.py` 惯例：`from app.core.db import Base`，SQLAlchemy 2.0 `Mapped`/`mapped_column`，模块 docstring 讲设计理由。

### 2.1 `audience_events`（原子事件流，追加为主）

```
id                int   pk
account_id        int   FK xhs_accounts.id  —— 哪个自家号收到这条互动
platform_event_id str   平台事件 id（去重键）
actor_userid      str   indexed —— 互动者稳定唯一键
actor_nickname    str   采集时快照
actor_image       str   头像 URL 快照，nullable
event_type        str   like_note / fav_note / like_comment / like_share / like_avatar / follow
target_note_id    str   nullable（follow / like_avatar 无）
target_note_title str   nullable
fstatus           str   both / fans / follows / none —— 采集时关系快照，nullable
event_time        int   epoch 秒
raw_json          text  原始 message 留档（一行一事件，便于回溯解析口径）
created_at        datetime  default utcnow
UNIQUE(account_id, platform_event_id)  —— 跨号事件 id 可能不唯一，带 account_id 保险
INDEX(actor_userid)         —— 纵向轨迹按 userid 聚合
INDEX(account_id, event_time)  —— 增量采集游标 & 时间范围查询
```

`target_note_id` **不做外键**（同 note_interactions 的理由）：被互动的笔记不一定在 `published_notes`（可能是已删的、台账没同步的），缺行不该挡记账。

### 2.2 `audience_sync_state`（每号每 channel 增量游标）

```
account_id     int   } 复合 pk
channel        str   } likes / connections
last_event_time int  上次采到的最新 event_time；增量滚到 <= 此值即停
last_full_sync_at datetime nullable —— 首次/周期全量回采时刻
updated_at     datetime
```

增量策略：拉第一页起，遇到 `event_time <= last_event_time` 停（新事件在最前）；首次（`last_event_time` 为空）或强制全量则翻到 `has_more=false`。采完把本轮最大 `event_time` 写回 `last_event_time`。

## 3. 采集层

### 3.1 调度器 `AudienceSyncScheduler`

**照抄 `app/services/interaction_backfill_scheduler.py` 模板**（读它，三条纪律照搬）：

- 活在 supervisor 进程，**只往 `browser_jobs` 插 `queued` 行，绝不 spawn_inline**（浏览器只在 account_worker 子进程起）；
- **单 kind 在飞不叠**：`audience_sync` 有 queued/running 就跳过本轮；
- 每 `AUDIENCE_SYNC_INTERVAL` 秒扫一次，为「到期未采」的代管号 enqueue 一条增量采集单；挑不出就跳过不空转。
- 「到期」判定：`audience_sync_state.updated_at` 早于 `now - AUDIENCE_SYNC_INTERVAL` 的代管号，一次挑一个（与 backfill 同样一轮一单，避免堆队列被平台看出特征）。
- 只采 `managed=1` 的号。

新 browser job kind：`audience_sync`。payload：`{account_id, full: bool}`。

### 3.2 浏览器采集 `app/browser/audience_collect.py`

**结构参照 `scripts/capture_page_scene.py` 的被动监听 + 拟人滚动**，但落库不落夹具。执行流：

1. `SyncClient` 起号 → `page.goto("https://www.xiaohongshu.com/notification")` → 验证登录态（未登录 → 该 job error，不入任何数据）。
2. 对 `likes`、`connections` 两个 channel 各做：拟人点 tab 切过去 → **先 `human.hover` 到通知列表行再滚**（取证血泪坑①：`mouse.wheel` 打鼠标当前位置，初始在顶栏滚不动）→ 被动监听 response 收 `message_list` → 增量停止条件：本页出现 `event_time <= last_event_time` 或 `has_more=false` 或滚满封顶（full 模式封顶 40 轮，增量封顶 5 轮）。
3. 停滞判据必须含 `document.scrollTop`（取证血泪坑②：通知页滚的是 document 不是内层容器，漏了它会把「还在滚、懒加载没触发」误判到底）。
4. 解析 `message_list` → 按 §1 的 type 分叉规范化 → 批量 upsert 进 `audience_events`（`INSERT ... ON CONFLICT(account_id, platform_event_id) DO NOTHING`，重采不叠）→ 更新 `audience_sync_state`。
5. 全程拟人化 `SyncHumanActions`，只读导航 + 滚动，**无任何点赞/关注/进陌生人主页/提交类点击**。
6. 跨进程门禁：采集单由 supervisor 派子进程，与其它 browser job 同路串行（account 级），无需脚本层门禁。

### 3.3 解析器 `app/services/audience_events.py`

- `normalize_like_event(msg, account_id) -> dict | None`：按 type 分叉取 note_id/title；未知 type 返回 None（记 warning 不抛）。
- `normalize_connection_event(msg, account_id) -> dict`。
- `upsert_events(session, rows)`：批量幂等入库。
- **纯函数可单测**：喂取证快照里的真实 message 断言解析出的 event_type/target_note_id 正确（尤其 fav_note 走 attach、like_avatar 无 note_id、like_comment 取评论所在笔记）。取证快照可作为测试夹具来源。

## 4. 分析层

### 4.1 潜客打分（启发式 v1，权重可配，显式标注待校准）

**明确约束**：转化回流数据（谁最终进了私域）当前不存在，所以权重是**运营直觉的启发式初版，不是科学模型**。做成 `AUDIENCE_SCORE_WEIGHTS` 可配 dict，代码注释与 manifest 均标注「v1 启发式，待真实转化数据校准」。绝不假装有数据支撑。

潜客分刻画「高互动意愿但未转化」。按 userid 聚合后的维度（各维度候选集内 min-max 归一化再加权，套 retention_scheduler 的归一化手法）：

| 维度 | 信号 | 默认权重 |
|---|---|---|
| `frequency` | 互动事件总数 | 0.30 |
| `cross_account` | 互动过的不同自家号数（跨号=对矩阵而非单篇感兴趣） | 0.20 |
| `recency` | 最近一次互动距今（越近越高） | 0.20 |
| `depth` | 收藏权重 > 赞笔记 > 赞评论/分享（收藏是更强意愿信号） | 0.15 |
| `relation` | `fans`（关注了我还没私域）最高潜；`none`/`follows` 次之；`both` 已是自己人**降权** | 0.15 |

输出每个 userid 一个 `potential_score` + 各维度分项明细（可解释，不是黑盒）。

### 4.2 REST 端点 `app/http/audience_rest.py`

套 `app/http/self_interactions_rest.py` 惯例（注册、manifest、口径注释）。所有端点**默认排除自家 10 号 userid**。

| 端点 | 用途 |
|---|---|
| `GET /api/audience/overview` | 汇总卡：总互动者数、按 fstatus 分布、近 7/30 天新增互动者、高潜人数 |
| `GET /api/audience/actors` | 互动者列表，支持 `sort`（互动次数/最近互动/潜客分）、`filter`（fstatus / event_type / 是否已关注） |
| `GET /api/audience/actors/{userid}` | 单个互动者完整纵向轨迹：事件时间线 + 跨自家号分布 + 关系演变 + 潜客分明细 |
| `GET /api/audience/funnel` | 潜客漏斗：分层人数 + 各层代表人物（陌生高频 → 已关注未深互动 → 互关活跃 …），层定义写死在服务层并注释 |
| `GET /api/audience/segments` | 群体切片：按关系分布 / 互动内容偏好（聚合目标笔记）/ 活跃度分档聚合 |

漏斗与切片的「层 / 档」定义是运营决策，写在服务层常量并注释理由，不散落在 SQL。

## 5. 配置项（`app/core/config.py`，全部带默认值）

```
AUDIENCE_SYNC_ENABLED: bool = True        # 采集调度总开关（kill switch）
AUDIENCE_SYNC_INTERVAL: int = 3600        # 采集扫描间隔秒（每号约每小时增量一次）
AUDIENCE_SCORE_WEIGHTS: str/dict          # 潜客打分权重，可配；默认见 §4.1
AUDIENCE_SELF_EXCLUDE: bool = True        # 分析是否排除自家号（默认排除）
```

装配：`AudienceSyncScheduler` 在 server 启动处 start（参照 `InteractionBackfillScheduler` 的装配点）；`audience_sync` job kind 在 account_worker 的 dispatch 表注册对应 handler。

## 6. 迁移

新迁移（单 head，接当前 head），照 `f2b8d41c7e09` 惯例：

- 建 `audience_events` 表（含两个索引 + UNIQUE 约束）。
- 建 `audience_sync_state` 表。
- **无 seed**（历史随时可回采，首次由采集单自然全量）。
- 新表无「给存量行加 NOT NULL 列」问题，但 `created_at` 等默认值仍按惯例设 server_default / default。

## 7. 测试要求（TDD）

- **解析器单测**（最关键，防「照一条样例写解析器」的老坑）：用取证快照里的**真实** message 断言四态 type 各自解析正确，尤其 `fav_note` 走 `attach_item_info`、`like_avatar` 无 note_id、connections 的 `images` 复数字段。
- **打分单测**：构造已知事件集，断言各维度归一化 + 加权 + 关系降权符合预期；断言权重可配。
- **采集增量单测**：mock message_list，断言遇 `event_time <= last_event_time` 停、upsert 幂等（重采不产生重复行）。
- **REST 单测**：断言自家号被默认排除；轨迹端点按 userid 正确聚合；漏斗分层计数正确。
- **调度器单测**：套 backfill scheduler 测法，断言在飞不叠、挑不出跳过。
- 测试自清理（建的库记录 teardown 删）。

## 8. 验收标准

1. 迁移 upgrade/downgrade 干净；`alembic heads` 单 head。
2. 全套 pytest 绿（含新增单测，显式取退出码，不链式 grep 吞失败）。
3. 采集单在真号（挑空闲代管号，走跨进程门禁）实采一轮，`audience_events` 真实落库、可复采幂等。
4. 五个 REST 端点返回结构正确、自家号已排除、轨迹/漏斗/切片口径符合本文。
5. 合规边界自查：全库无任何 PHI 关联字段/表；采集全程只读无破坏性点击。

## 9. 部署（迁移优先，照本仓铁律）

空窗 → `alembic upgrade head` → 起 api → healthz 200 → 串行重启 worker。`check_no_inflight.sh` 确认无在飞再重启 worker。CHANGELOG + 版本号（Feature=Minor → 0.24.0）。

## 10. 明确不做（YAGNI）

- 不采 `mentions`（评论和@）——评论体系另账，本版聚焦赞/收藏/关注。
- 不采集互动者主页任何信息（合规 + YAGNI）。
- 不做实时推送/告警——受众库是分析资产，日增量足够。
- 不建 actor 独立维度表——纵向轨迹是 `audience_events` 按 userid 的聚合查询/视图，不冗余存一份会漂移的 actor 快照。
- 打分不接机器学习——启发式加权 v1，待真实转化数据再谈。
