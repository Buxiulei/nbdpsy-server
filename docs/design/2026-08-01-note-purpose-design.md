# 笔记核心目的(note_purpose)+ 手工笔记内容回填 设计

日期:2026-08-01
状态:待实现

## 一、需求

1. 每篇笔记要有一个**核心目的**字段,说明这篇笔记是干什么的
   ——推介咨询师 / 解读心理学概念 / 剖析场景案例 / 从心理学视角分析社会热点 等;
2. 这个字段**是给调用笔记的 agent 读的**,让它知道这篇笔记的意图,才能正确操作;
3. **手工发布的笔记**(本系统没发过的),在每日笔记数据更新任务里发现库中没有时,
   要**主动去抓正文**,把核心目的补上,方便以后 agent 对这批笔记也能操作。

## 二、现状与缺口

`published_notes` 台账已存 note_id / 标题 / 时间 / 可见性 / 互动快照 / 关联关系,
但**没有任何内容语义信息**。

更关键的是:**手工发布的笔记本地一个字正文都没有**。台账数据来自创作中心列表接口
(`posted`),该接口只给元数据,不给正文。实测规模:

| 账号 | 台账篇数 | orphan(手工发,无正文) |
|---|---|---|
| NBDpsy-我们都有病 | 39 | 28 |
| NBDpsy聊心理 | 30 | 25 |
| NBDpsy-亲密关系 | 18 | 0 |

`linked` 的那批(本系统发的)正文在 `content_archive` 里有副本;
orphan 那批**从来没有过**。

## 三、设计

### 3.1 字段

`published_notes` 新增:

- `note_purpose` TEXT NULL —— 核心目的
- `purpose_source` TEXT NULL —— 这个值怎么来的:`declared`(发布时调用方声明)/
  `inferred`(从正文推断)/ NULL(未知)
- `content_text` TEXT NULL —— 笔记正文(orphan 抓回来的;linked 的可留空,正文在归档里)
- `content_fetched_at` DATETIME NULL —— 正文抓取时刻

`purpose_source` 必须有:agent 需要知道这个目的是**人声明的**还是**机器猜的**,
两者可信度不同。声明的可直接信,推断的要留余地。

### 3.2 推荐取值(受控词表,但不强制)

```
推介咨询师      介绍某位咨询师,引导预约
概念解读        解释一个心理学概念
案例剖析        拆解一个具体场景或来访情境
热点分析        从心理学视角分析社会热点
互动引导        引导关注/收藏/私信一类的功能性笔记
个人记录        转型前的个人生活内容(大量存量属于此类)
其他            以上都不是
```

**不强制枚举**:用户原话是"等等",说明会扩。存字符串,文档里给推荐词表,
agent 按已知值匹配,遇到新值不报错。

### 3.3 两条填充路径

**路径 A — 发布时声明(权威)**

`POST /api/publish-jobs` 请求体加 `note_purpose`。T0 发布当场随
`generated_at` / `operator_id` / `related_counselor` 一起写进台账,
`purpose_source='declared'`。

**路径 B — 手工笔记回填(推断)**

台账同步(T2)发现**新的 orphan 行**(平台上有、台账里没有、且无 `source_publish_job_id`)时,
登记一条回填任务:

1. 用 note_id 深链进编辑页 `publish/update?id={note_id}&noteType=normal`
   ——**这是已验证的只读路径**,写测试期间多次进出未提交,创作中心全程未触发验证墙;
2. 只读读取正文(tiptap ProseMirror 的 `textContent`),连同标题一起落
   `content_text` / `content_fetched_at`;
3. 用 LLM 按受控词表分类,写 `note_purpose`,`purpose_source='inferred'`;
4. **绝不点发布、绝不改任何内容**。

### 3.4 为什么走编辑页而不是笔记详情页

- 编辑页在 `creator.xiaohongshu.com`,今天多次验证**创作中心从不触发验证墙**;
  而笔记详情页要经 `xiaohongshu.com/user/profile/`,今天已有两个账号栽在那条路上;
- 编辑页能拿到**结构化的正文**(话题是独立节点),详情页只能抓渲染后的文本;
- 只读进出已被写测试反复验证安全。

### 3.5 LLM 分类

用现有 `LLM_API_KEY` / `LLM_MODEL`(DashScope qwen3.6-flash,自愈已在用)。

**约束**:

- 只做**分类**,不生成内容(与"评论文案不做 LLM 生成"的既定纪律不冲突——那条针对的是
  对外发布的文案,分类是内部标注);
- 输出必须落在受控词表内,**拿不准就填「其他」,不许自造新类别**;
- 分类失败 / LLM 不可达 → `note_purpose` 留 NULL,**不阻断同步**,下轮重试;
- 正文为空(如纯图笔记)→ 只用标题分类,并在 `purpose_source` 里体现证据不足。

### 3.6 节流(重要)

存量 orphan 有 53 篇(28+25),每篇要开一次编辑页。**绝不能一次性全抓**:

- 每轮同步**最多回填 N 篇**(建议 3-5),N 可配置;
- 优先回填**最近发布的**(旧的个人记录价值低);
- 已有 `note_purpose` 的不再抓;
- 与 `browser_slot` 闸共用并发上限,不额外起并发。

理由:今天已实测,同一账号一小时内起 5 次会话会把它从"扫码验证"打成"请求太频繁",
两个账号因此被弹墙,其中一个靠人工扫码才解开。

## 四、REST

`published_notes` 的列表/单条响应加 `note_purpose` / `purpose_source` /
`content_text`(单条才给,列表不给以免响应过大)/ `content_fetched_at`。

`POST /api/publish-jobs` 加 `note_purpose`。

另开手工触发回填的端点,便于运营对指定笔记补录:

```
POST /api/accounts/{id}/note-purpose-backfills → 202 {job_id}
       note_id 可选(不传则按策略自动挑几篇)
GET  /api/note-purpose-backfills/{job_id}
```

## 五、待确认

- 受控词表是否够用,要不要增删;
- 存量 53 篇 orphan 里有大量转型前的个人生活内容(如「海马体，打钱！」「宝宝出去玩吗」),
  这批分类成「个人记录」即可,是否值得花浏览器会话去抓正文,由业务侧定
  ——建议**只回填公开的**,私密的跳过(读者看不到,agent 也不会去操作它)。
