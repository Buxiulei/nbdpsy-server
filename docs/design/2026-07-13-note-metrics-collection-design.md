# 账号笔记数据采集(note metrics collection)设计

**日期**:2026-07-13
**决策**:给 nbdpsy-server 加回"采集账号自己已发布笔记的数据看板"能力——移植老仓
`creator_center_exporter`(同步 Camoufox 登录创作中心 → 数据看板 → 内容分析 → 导出 Excel →
openpyxl 解析),拿每条笔记 13 项指标,落库为**最新快照 + 每日趋势**两张表,经异步 REST 端点
触发/轮询/读取。这是当初精简时砍掉的采集能力,按用户要求单独加回(只做自己账号,不含竞品搜集/
评论/热点雷达)。

## 背景与前提(调研已确认)

- 老仓 exporter **纯浏览器 UI 点击 + 下载 Excel,零 API 调用、零 x-s/x-t 签名**——移植最难的
  签名逆向问题不存在。同步 + Camoufox,正好贴 server 的发布链技术栈。
- server 现有两种异步任务模式:**发布**=持久化表 + 调度器(durable);**cookie 巡检**=进程内存
  台账 + 后台 asyncio 任务 → to_thread 跑同步浏览器 + 共享 `account_locks`(ephemeral)。
  本功能的**导出任务**照 cookie 巡检的 ephemeral 模式(结果落表,任务本身重启可丢重导);
  **笔记数据**必须落持久表。
- 用户定:**快照 + 历史趋势**(两张表)。
- creator 域 cookie:插件目前推的是主站 cookie。v1 走老仓兜底路径——**主站 cookie 注入 creator
  子域 + SSO warm-up**(不动插件;偶尔要重扫时报为账号需重登)。
- Excel **无 note_id、无封面 URL**;按 `(account_id, 标题, 发布时间)` 做主键(发布时间消歧同名)。

## 架构

### 1. 导出器 `app/browser/creator_export.py`(纯同步,吃已登录 page)

```python
# 13 指标 + 标题 + 发布时间。Excel 中文列名 → 字段映射(COLUMN_MAPPING)。
COLUMN_MAPPING = {
    "笔记标题": "title", "首次发布时间": "publish_time",
    "点赞": "likes", "收藏": "collects", "评论": "comments", "弹幕": "danmu",
    "分享": "shares", "转载": "reposts", "涨粉": "follows",
    "封面点击率": "cover_ctr", "曝光": "exposure", "观看量": "views",
    "人均观看时长": "avg_view_duration",
}

def export_notes(page, account_id: int, download_dir: str) -> list[dict]:
    """导航创作中心 → 导出 Excel → openpyxl 解析 → list[dict]。全程兜底,失败抛
    CreatorExportError(见错误处理),不静默返回半截数据。"""
```

导航(复刻老仓 `_navigate_to_creator_center` + `_download_export`):
1. `page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)` 预热建 session。
2. creator warm-up(最多 3 轮):`goto https://creator.xiaohongshu.com/publish/publish?source=official`
   → `goto https://creator.xiaohongshu.com/creator/home` → 等 `.d-sub-menu:has-text("数据看板")`
   可见(12s)。仍不可见 → `CreatorExportError("need_manual_login")`(账号需重扫 creator 域)。
3. 点「数据看板」→ 点「内容分析」→ `with page.expect_download(30000): 点「导出数据」`
   → `download.save_as(download_dir/export_<account_id>_<注入时间戳>.xlsx)`。
4. openpyxl 打开:读表头行定位列 → 逐行按 COLUMN_MAPPING 取值;整数列 `int()`、
   `cover_ctr` 若 0<x<1 则 ×100 转百分比、`avg_view_duration` float;每行注入
   `account_id`。返回 list[dict]。

**自愈复用**(用户明确要):第 3 步的中文 `:has-text` 菜单/按钮选择器接进已有 `SelfHealLocator`——
封装小 helper `_find_creator_element(page, selectors, intent_key, desc)`:先试硬编码 locator,
失败且 `settings.SELFHEAL_ENABLED and settings.LLM_API_KEY` 时 fallback
`SelfHealLocator().locate(page, intent_key, desc)` 取 handle 点击(intent_key:
`creator_data_dashboard_menu` / `creator_content_analysis_menu` / `creator_export_button`)。
默认关时行为与纯硬编码一致。

**v1 决定:creator-center 自愈为"恢复即用",不 learn 持久化**。发布链的自愈会把学到的选择器
写回 registry(高频场景值得自维护);creator-center 导出是手动低频操作,v1 只做"硬编码失效时
LLM 兜底定位一次"(仍能扛改版),不接 learned-prepend + learn 全套,避免为低频路径引入额外
状态。若后续高频化再补持久化。

**定位与 expect_download 计时**:`creator_export_button` 的定位必须在 `with page.expect_download()`
**之外**完成(expect_download 计时从 `__enter__` 起算,把可能吃满 30s + 自愈的定位放进 with 体内
会先耗尽 download waiter),with 体内只做 click。

时间戳注入:`export_notes` 的文件名时间戳与"注入时间"由调用方(service 层)传入,导出器不自取
`datetime.now()`(便于测试)。

### 2. 导出任务 `app/services/note_export.py`(ephemeral 台账,照 cookie_check)

```python
# export_id -> {"status","account_id","note_count","error","created_at"} 进程级内存台账。
def start_export(account_id: int, cookies: list[dict]) -> str          # 返回 export_id,起后台任务立即返回
def get_export(export_id: str) -> dict | None                          # 轮询;终态 done/error
```

后台任务:`asyncio.to_thread` 里 `with account_locks.get(account_id):`(与发布/cookie 检测同号
串行)→ `SyncClient(account_id, cookies).start()`(主站 cookie,SSO warm-up 建 creator session)
→ `export_notes(page, account_id, download_dir)` → `note_metrics_service.upsert_notes(...)` 落库
→ 台账标 `done` + note_count。任何异常 → 台账标 `error` + reason(不写库、不崩)。终态 TTL 驱逐
(同 cookie_check)。`_tasks` 强引用防 GC。

### 3. 存储:两张持久表 `app/models/note_metric.py` + alembic 迁移

```python
class NoteMetric(Base):            # 最新快照
    __tablename__ = "note_metrics"
    id: int PK
    account_id: int  FK xhs_accounts.id
    title: str
    publish_time: str             # Excel 原文 "2026年05月22日10时59分14秒"(不强解析,原样存)
    likes/collects/comments/danmu/shares/reposts/follows/exposure/views: int
    cover_ctr/avg_view_duration: float
    updated_at: datetime
    # UNIQUE(account_id, title, publish_time)  → 每次导出 upsert 覆盖成最新

class NoteMetricDaily(Base):       # 每日趋势
    __tablename__ = "note_metrics_daily"
    id: int PK
    account_id: int  FK
    title: str
    publish_time: str
    snapshot_date: str            # "YYYY-MM-DD"(调用方注入的导出日)
    <同上 13 指标列>
    # UNIQUE(account_id, title, publish_time, snapshot_date) → 每天一行,当天重导覆盖、跨天加行
```

服务 `app/services/note_metrics_service.py`:
- `upsert_notes(session, account_id, rows: list[dict], snapshot_date: str)`:对每行 upsert
  NoteMetric(按唯一键)+ upsert NoteMetricDaily(按唯一键含 snapshot_date)。
- `list_notes(session, operator, account_id) -> list[dict]`:RBAC 收窄后读该号最新快照(NoteMetric)。
- `note_trend(session, operator, account_id, title, publish_time) -> list[dict]`:读某条笔记的
  daily 序列(按 snapshot_date 升序)。

### 4. REST 端点 `app/http/notes_rest.py`(照 publish/cookie-check 异步 + RBAC + manifest)

```
POST /api/accounts/{account_id}/note-exports   → 202 {export_id, status:"running"}
     assert_account_access → 解密该号 cookie → note_export.start_export → 回 export_id
GET  /api/note-exports/{export_id}             → {status: running/done/error, note_count, reason?}
     鉴权用台账 account_id 防越权(同 get_cookie_check)
GET  /api/accounts/{account_id}/notes          → {notes: [...最新快照...]};
     可选 ?title=&publish_time=&trend=daily → {trend: [...daily 序列...]}
```

MANIFEST_ENTRIES 三条,notes 写清:异步契约(202+轮询)、数据来自创作中心 Excel 导出、
**无 note_id/封面 URL**、按 (account_id,标题,发布时间) 存、13 指标含义、需该号 creator 登录态。
接进 `app/http/__init__.py` 注册表(防漂移测试自动覆盖)。

### 5. 依赖与配置

- `requirements.txt` 加 `openpyxl>=3.1`(装进 venv)。
- 复用 `SyncClient` / `account_locks` / `SelfHealLocator` / camoufox,无新浏览器依赖。
- 导出文件目录 `settings.DATA_DIR/creator_exports/<account_id>/`(沿用 DATA_DIR,不新增配置)。

## 数据流

`POST note-exports` → assert_account_access + 解密 cookie → `start_export` 起后台 → 台账 running →
(后台线程)account_lock → SyncClient.start(主站 cookie + SSO warm-up)→ export_notes(导航+下载+
解析)→ upsert_notes(NoteMetric 最新 + NoteMetricDaily 当天)→ 台账 done。
调用方轮询 `GET note-exports/{id}` 到 done → `GET accounts/{id}/notes` 读快照 / 带 trend 读日序列。

## 错误处理

- creator warm-up 三轮仍无「数据看板」→ `CreatorExportError("need_manual_login")` → 台账 error
  + reason(不代表 cookie 全失效,是 creator 域需重扫)。
- 下载超时 / 选择器失效(自愈也没救回)/ openpyxl 解析失败 → CreatorExportError → 台账 error + reason。
- 导出 0 条(新号无笔记)→ done,note_count=0(非错误)。
- 后台任务任何异常都被 service 收成 error 台账,**绝不崩后台 loop**;不写半截数据(解析失败即整批弃)。
- 端点层:账号不存在 → NotFoundError(404);越权 → AccessDenied(403);export_id 不存在 → 404。

## 测试策略

- **openpyxl 解析纯逻辑**:造一个 fixture .xlsx(openpyxl 写入表头 + 2 行)→ 验 13 字段映射、
  cover_ctr 百分比转换(0.12 → 12.0)、整数列解析、account_id 注入、缺列/空行容错。
- **落库 upsert**:临时 DB,`upsert_notes` 两次(同键)→ NoteMetric 只 1 行且为最新值;
  NoteMetricDaily 同 snapshot_date 覆盖、不同 snapshot_date 加行;`note_trend` 按日升序。
- **端点 RBAC + 异步**:monkeypatch `note_export.start_export` 返假 export_id、`export_notes`
  返假行 → POST 202、GET 轮询 done、GET notes 读到假数据;越权号 403;不存在 404。
- **导出台账**:start→running→(monkeypatch 导出)→done/error;error 携 reason;跨 operator 查 403。
- **创作中心导航**(依赖真 page)→ 无单测,真机验证:配一个真号,POST note-exports → 轮询 done →
  GET notes 看真实笔记数据落库(含手工发布的历史笔记)。
- 防漂移测试:3 个新端点自动纳入 manifest 一致性校验。

## 明确不做(YAGNI)

- 不做竞品/关键词搜集、评论采集、热点雷达、note_sync 详情同步(用户只要自己账号数据)。
- 不做定时自动导出(server 无 celery;手动触发即可,需要定时后续再加)。
- 不做 note_id 逆向/详情页补采封面 URL(Excel 没有就不强求;按 title+publish_time 存)。
- 不改 chrome 插件采 creator 域 cookie(v1 靠主站 cookie + SSO warm-up;不够用再议)。
- 导出任务不做持久化表/重试队列(ephemeral 台账;结果已落库,失败重导即可)。
