# 发布笔记永久台账(published_notes)设计

日期:2026-07-31
状态:调研+真号实验已完成,待实现

## 一、需求

"我们发布过什么笔记,都应该在后台数据库里存储":笔记 id、标题、正文、媒体文件
(图片和视频)、发布时间、生成时间、生成用户。

## 二、现状与缺口(三路并行调研 + 真号实验结论)

### 2.1 已经有的

`content_archive` 表(66 行)已存:标题、正文全文、话题、媒体**独立副本**
(`data/uploads/archive_{id}/NN.ext` + 免鉴权直链 `/uploads/archive_{id}/NN.ext`)、
发布账号、生成者 operator、归档时间、来源发布任务 id。归档在发布成功后同步触发
(`app/account_worker.py:198` / `app/publish/scheduler.py:358`),幂等键
`source_publish_job_id`。

### 2.2 缺的

1. **笔记 id 与真实链接**:`publish_jobs.note_id` 68 条 published 全是空串,
   `note_url` 全是同一条 creator 通用成功页 URL;`content_archive.source_note_url` 同。
   `note_metrics` 只有标题没有 id(创作中心"数据导出"的 xlsx 表头本就无 ID/链接列)。
2. **发布完成时间**:`publish_jobs` 无 `published_at` 列。现有最近似代理是
   `content_archive.created_at`(实测距 `started_at` 160~284s,均值 243s,
   这段差值是浏览器发布本身的耗时)。
3. **笔记体裁**:`content_archive.kind` 是 INSERT 语句里的**硬编码字面量**
   `"image_note"`(`content_archive.py:74`),66/66 全部 image_note,与实际无关。

### 2.3 为什么以前抓不到 note_id(推翻"没抓"的误判)

代码**抓了而且有兜底**,只是两条路都用错方法:

- `_extract_note_id_from_url`(`atomic_tasks.py:1995`)正则匹配 URL 里的
  `/explore/([a-f0-9]+)`,而生产成功页 URL 不含 id → 必然失败;
- 兜底 `_fetch_latest_note_id_from_creator`(`atomic_tasks.py:2007`)去创作中心,
  但对 `page.content()` 跑 `"noteId":"([a-f0-9]{24})"` **纯文本正则** → 生产 100% 失败
  (近 7 天 8 次发布,每次都打 "无法从创作中心提取 note_id")。

**真号实验(2026-07-31,账号 NBDpsy)**:
- 笔记管理页 `/new/note-manager` 的 DOM **不暴露** note_id——属性里含 24 位 hex 的元素
  0 个、笔记链接 0 个。这条路封死。
- 全仓**从未拦截过任何网络响应**(无 `page.on("response")`)。拦截后定位到笔记列表接口。

### 2.4 存量回填:靠库内数据不可能

任何表都没采集过 note_id。标题匹配也不可靠:全库 note_metrics 168 条里空标题 7 条、
同账号标题完全重复 2 组 4 条;64 条真正 published 的归档与 note_metrics 按
(account_id, title) 精确匹配,只有 55 条唯一命中、2 条落进重复组无法区分、7 条查无此标题。
**必须重新抓取。**

## 三、核心机制:拦截创作中心笔记列表接口

```
GET https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=0&page=0
→ {code, success, msg, data: {notes: [...], tags, page}}
```

`notes[i]` 实测字段(账号 NBDpsy,2026-07-31):

| 字段 | 样例 | 用途 |
|---|---|---|
| `id` | `68d50838000000000e00c3b6` | **真实 note_id** |
| `xsec_token` | `YBdYh3yPUGXDnUtPAa3OB6QKNkFxNM1336_ZdOW3h_mqA=` | 拼完整可访问链接 |
| `xsec_source` | `pc_creatormng` | 同上 |
| `display_title` | `""`(空标题笔记也有 id) | 标题 |
| `time` | `2025-09-25 17:15` | 发布时间(字符串) |
| `visible_time` | `1758791784` | 发布时间(unix 秒,**优先用这个**) |
| `type` | `normal` | 笔记体裁 |
| `images_list` | 列表 | 封面/图片 |
| `likes` / `collected_count` / `comments_count` / `shared_count` / `view_count` | 数值 | 互动数据 |
| `sticky` / `tab_status` / `permission_code` | — | 置顶/状态/权限 |

**同一机制服务两个用途**:分页遍历 = 存量回填 + 定期对账;发布后单次拉取 = 增量捕获。

## 四、设计

### 4.1 新表 `published_notes`(永久台账)

**为什么不复用 `content_archive`**:两点硬冲突。

1. `content_archive` 有 `ArchiveReaper` 90 天滑动 TTL,**会删行 + 删媒体目录**
   (`ARCHIVE_TTL_DAYS`,`content_archive.py:225-268`)。而需求是"发布过的都要存",
   永久台账不能建在会被清理的表上。
2. `content_archive` 只覆盖**本系统发出去的**笔记。实测 NBDpsy-夕夕 在 `note_metrics`
   里有 26 篇,`publish_jobs` 里**一条记录都没有**(非本系统发布),这些笔记
   `content_archive` 永远不会有。

两者生命周期本就不同:台账是小行、永久;归档是大内容+媒体文件、可 TTL。台账通过
可空外键指向归档,不重复存正文与媒体。

字段:

- `id` 主键
- `account_id` + `note_id` **联合唯一**(幂等键)
- `note_id` / `xsec_token` / `xsec_source` / `note_url`(拼好的完整链接)
- `title`(`display_title`,可空串)
- `note_type`(`type` 原样落,**不做映射**——真实取值集合尚未穷举)
- `published_at`(由 `visible_time` unix 秒转,权威发布时间)
- `source_publish_job_id`(可空 FK → publish_jobs,非本系统发布的为 NULL)
- `content_archive_id`(可空 FK → content_archive,正文与媒体在那边)
- `first_seen_at` / `last_synced_at`
- 互动快照 `likes` / `collects` / `comments` / `shares` / `views`(对账用,非权威指标源)

### 4.2 抓取层 `app/browser/creator_note_list.py`

拦截 `page.on("response")` 捞 `creator/note/user/posted`,分页遍历直到返回空
(`page` 参数递增),返回全部 notes 原始 dict 列表。

**约束**:纯只读抓取,不点击不修改;沿用 `_goto_creator` 的 SSO 预热;
翻页用拟人化滚动或改 URL 参数导航,不得裸调接口(不构造请求,只被动读响应)。

### 4.3 服务层 `app/services/note_ledger.py`

- `execute(account_id, payload)` 契约:照抄 `note_export` 的收敛纪律——持
  `account_locks` 号锁、过 `browser_slot` 闸、异常收敛成 `{"error": reason}` 绝不上抛。
- `upsert_notes(...)`:按 (account_id, note_id) upsert;已存在则刷新
  `last_synced_at` / 互动快照 / title / url,不动 `first_seen_at`。
- **回连**:按 (account_id, title, published_at 邻近) 尽力把台账行关联到
  `publish_jobs` / `content_archive`;**关联不上就留 NULL,绝不猜**
  (标题重复/为空的那几条本就无法靠标题区分,宁可空着)。
- 同时**回填** `publish_jobs.note_id` / `published_at`(仅当唯一匹配)。

`browser_jobs` 新 kind `note_ledger_sync`。**幂等**(纯只读抓取 + upsert),
可加入 `_IDEMPOTENT_KINDS`。

### 4.4 触发

1. **发布成功后**:与 `archive_published_job` 同址(`account_worker.py:198` /
   `publish/scheduler.py:358`)登记一条 `note_ledger_sync`。注意发布后笔记入列表
   可能有延迟,失败不重试不阻断(下次定时同步会兜住)。
2. **定时对账**:复用 `note_metrics_scheduler` 的模式,每账号周期性全量同步一次。
3. **存量回填**:上线后手工对每个可用账号触发一次。

### 4.5 时间语义(明确回答需求)

| 需求里的说法 | 落到哪 | 说明 |
|---|---|---|
| 发布时间 | `published_notes.published_at` | 由接口 `visible_time` 转,权威 |
| 生成时间 | `publish_jobs.created_at` | **代理值**:发布任务提交时刻 |
| 生成用户 | `publish_jobs.created_by` → `content_archive.source_operator_id` | 语义是"谁的 apikey 提交了发布",不区分内容是人写还是 AI 代写 |

**必须如实告知用户的语义偏差**:nbdpsy-server 是纯发布 API 服务,内容生成发生在调用方
(外部 agent/skill)内部,本仓库看到的最早时间戳就是任务提交时刻。真正的"内容生成时刻"
只能由调用方传入,现阶段用提交时刻做代理。

## 五、本期不做(需用户单独拍板)

1. **视频笔记发布链路**。`PublishJob` 只有 `images_json`,无任何 video 字段;
   `PublishNoteRequest` 只收 `images`;`materialize_images` 只认 6 种图片扩展名,
   未知扩展名**静默兜底成 `.jpg`**。`app/video/` 是独立的 YouTube 搬运/再制作管线,
   与 `publish_jobs` 零关联。也就是说**小红书视频笔记发布这个能力整体不存在**,
   不是"归档漏了视频"。台账的 `note_type` 会如实记录接口返回的体裁,但要真发视频笔记
   是另一个项目。
2. **`content_archive` 的 90 天 TTL 是否调整**。本设计用新台账绕开了这个冲突
   (台账永久、归档照旧 TTL)。若希望正文与媒体也永久保留,需单独决策(涉及磁盘增长)。
3. **NBDpsy-夕夕 的 26 篇存量内容补齐**。它们非本系统发布,台账能拿到 id/标题/时间,
   但正文与媒体本地完全没有,补齐是"从零采集"另一个量级的工作。

## 六、风险与已知问题

1. **账号可用性**。NBDpsy-聊创伤 已被小红书挂**扫码验证墙**(2026-07-31 11:38 实测:
   访问他人主页被重定向到 `website-login/captcha`,提示"请使用已登录该账号的小红书APP
   扫码验证身份"),需运营用手机扫码恢复,恢复前不参与同步。
2. **cookie 健康检查有盲区**。该号 `cookie_status` 仍是 `valid`,因为登录检测只看首页
   有没有"我"导航栏,验证墙是访问他人主页时才弹的。**风控事件不落库**,`browser_jobs`
   里全文检索"验证/风控/captcha"零命中。这个盲区值得单独修。
3. **`xsec_token` 时效未知**。接口返回的 token 能否长期复用没实测。台账存下来,
   但使用方必须容忍失效——**不得假设存下的链接永远可打开**。
4. **2 条归档来自 failed/canceled 的 job**(archive id=2、4,对应 job status
   failed/canceled 且 `started_at` 为 NULL),说明归档触发条件不严格绑 published,
   这两篇从未真正发布,回填时应排除。
5. **4 条 published 无归档**(job id=7,8,10,11,账号 5/6 同秒批量发布),内容仍在
   `publish_jobs` 里没丢,但没走归档副本路径。

## 七、验收

1. 单元测试:分页遍历终止条件、upsert 幂等(同一 note_id 跑两次不产生重复行)、
   关联不上时留 NULL 不猜、`visible_time` → `published_at` 转换。
2. 真号 e2e:对一个健康账号(NBDpsy 或 NBDpsy-我们都有病)跑一次全量同步,
   核对台账行数与创作中心页面显示的笔记总数一致(实测 NBDpsy 显示"全部 61")。
3. 回填后核对:`publish_jobs.note_id` 不再全空;台账里空标题与重复标题的笔记
   都有各自独立的 note_id。
4. 全程只读抓取,无任何 JS 注入式点击/输入。
