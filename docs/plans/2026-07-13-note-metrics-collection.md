# 账号笔记数据采集实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps 用 checkbox。

**Goal:** 采集账号自己已发布笔记的创作中心数据(13 指标),落库为最新快照 + 每日趋势两表,经异步 REST 端点触发/轮询/读取。

**Architecture:** creator_export.py(同步导航创作中心 → 下载 Excel → openpyxl 解析 → list[dict])+ note_metrics_service(两表 upsert/读)+ note_export.py(ephemeral 台账 + 后台线程 + account_locks,照 cookie_check)+ notes_rest.py(3 端点)。复用 SyncClient/account_locks/SelfHealLocator/camoufox。

**Spec:** `docs/design/2026-07-13-note-metrics-collection-design.md`(字段/表结构/端点/错误契约以 spec 为准)

**移植源:** `/home/roots/小红书运营工具/backend/app/services/creator_center_exporter.py`(导航 + 下载 + openpyxl 解析逻辑照抄,适配 server 的同步 SyncClient page)

## Global Constraints

- 解释器 `/home/roots/nbdpsy-server/.venv/bin/python`;测试 `source .../activate && python -m pytest tests/ -q`(cwd = worktree 根)。**worktree 共用主仓 venv**;openpyxl 需先装进主仓 venv(Task 2 说明)。
- 注释/docstring/commit 全中文;禁 emoji;commit `type(scope): 描述`;**禁 git add -A,显式列文件**。
- 模型:`from app.core.db import Base`;SQLAlchemy 2.0 `Mapped`/`mapped_column`;`from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint`。新模型必须在 `app/models/__init__.py` import + __all__ 注册(否则 create_all/alembic 感知不到)。
- 端点:`operator = current_operator()`;`async with get_session() as session`;`await assert_account_access(operator, account_id, session)`;账号/资源不存在抛 `app.core.errors.NotFoundError`(→404);越权 AccessDenied(→403);其余入参非法裸 ValueError(→400)。**端点内不手动 model_validate 当业务校验**(pydantic ValidationError 是 ValueError 子类会被 400 吞)。
- 导出任务绝不崩后台 loop:任何异常收成 error 台账 + reason,不写半截数据。
- cookie 解密:`from app.core.security import decrypt_cookies`;账号 cookie = `decrypt_cookies(account.login_cookies)` → json.loads(参照 `app/http/cookies_rest.py::_decrypt_account_cookies`)。
- 时间注入:导出器/服务不自取 `datetime.now()` 做业务值(snapshot_date、文件名时间戳由调用方传入),便于测试;updated_at 用 `datetime.utcnow`(与既有模型一致,沿用不改风格)。

---

### Task 1: DB 两表 + 注册 + 迁移 + note_metrics_service(foundation,可与 Task 2 并行)

**Files:**
- Create: `app/models/note_metric.py`、`app/services/note_metrics_service.py`、`tests/test_note_metrics_service.py`、`alembic/versions/<新>_note_metrics.py`
- Modify: `app/models/__init__.py`(注册 2 模型)

**Interfaces(Produces):**
```python
# app/models/note_metric.py
class NoteMetric(Base):        # __tablename__="note_metrics"; UNIQUE(account_id,title,publish_time)
class NoteMetricDaily(Base):   # __tablename__="note_metrics_daily"; UNIQUE(account_id,title,publish_time,snapshot_date)
# 两表列:id PK / account_id FK xhs_accounts.id / title str / publish_time str
#   / likes,collects,comments,danmu,shares,reposts,follows,exposure,views: int(默认0)
#   / cover_ctr,avg_view_duration: float(默认0.0)
#   NoteMetric 额外 updated_at: datetime;NoteMetricDaily 额外 snapshot_date: str

# app/services/note_metrics_service.py
async def upsert_notes(session, account_id: int, rows: list[dict], snapshot_date: str, now: datetime) -> int
    # 对每行:upsert NoteMetric(按唯一键,更新 13 指标+updated_at=now)+ upsert NoteMetricDaily
    #  (按唯一键含 snapshot_date,更新 13 指标)。返回处理条数。
async def list_notes(session, operator, account_id: int) -> list[dict]   # RBAC 收窄后读最新快照
async def note_trend(session, operator, account_id: int, title: str, publish_time: str) -> list[dict]  # daily 升序
```

- [ ] **Step 1: 写失败测试 `tests/test_note_metrics_service.py`**

参照 `tests/test_selector_registry.py` / 现有 service 测试的 db fixture 用法(conftest 的 `db` fixture 给 AsyncSession)。用例:
```python
# 完整用例(每个都要写):
# 1. test_upsert_inserts_snapshot_and_daily:upsert 1 行 → NoteMetric 1 行值对、NoteMetricDaily 1 行含 snapshot_date
# 2. test_upsert_same_note_updates_snapshot:同 (account_id,title,publish_time) 二次 upsert(likes 变)→ NoteMetric 仍 1 行且 likes 更新;updated_at 用传入 now
# 3. test_daily_same_date_overwrites_diff_date_appends:同 snapshot_date 覆盖(1 行)、不同 snapshot_date 加行(2 行)
# 4. test_list_notes_rbac:operator 只见被授权号的快照(无授权→空/403 按 list 语义);admin 全见
# 5. test_note_trend_ascending:某笔记多天 daily → note_trend 按 snapshot_date 升序返回
```
（RBAC 用例参照 test_accounts_rest 造 operator+grant 的 helper,或 rest_helpers;list_notes/note_trend 的 RBAC 收窄用 `visible_account_ids`/`assert_account_access` 与 account_service 同款。）

- [ ] **Step 2: 确认失败** `pytest tests/test_note_metrics_service.py -q` → FAIL(模块不存在)。

- [ ] **Step 3: 实现**

3a. `app/models/note_metric.py`:两个模型(参照 `app/models/publish_job.py` 的 Base/列风格);`__table_args__ = (UniqueConstraint(...),)`。13 指标列带默认值(int 默认 0、float 默认 0.0)。

3b. `app/models/__init__.py`:import `NoteMetric, NoteMetricDaily` + 加进 __all__。

3c. `app/services/note_metrics_service.py`:upsert 用"先 select 唯一键、有则 update 无则 insert"(SQLite 兼容,不用 dialect-specific upsert);list_notes/note_trend 用 select + RBAC(admin 全见 / operator 经 assert_account_access)。

3d. alembic 迁移:在**本 task 的 worktree 根**跑 `source /home/roots/nbdpsy-server/.venv/bin/activate && alembic revision --autogenerate -m "note_metrics 两表"`(alembic.ini 在仓库根,env.py 已 import app.models,会感知新表)。**检查生成的迁移**含 create_table note_metrics + note_metrics_daily + 两个 UNIQUE 约束(autogenerate 有时漏 UNIQUE,漏了手工补 op.create_unique_constraint);`alembic upgrade head` 跑通验证。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_note_metrics_service.py -q` + 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/models/note_metric.py app/models/__init__.py app/services/note_metrics_service.py \
  tests/test_note_metrics_service.py alembic/versions/<新文件>
git commit -m "feat(notes): note_metrics 两表(快照+日趋势)+ upsert/读服务 + 迁移"
```

---

### Task 2: creator_export.py 导出器(可与 Task 1 并行,新文件)

**Files:**
- Create: `app/browser/creator_export.py`、`tests/test_creator_export.py`
- Modify: `requirements.txt`(加 openpyxl>=3.1)

**Interfaces(Produces):**
```python
COLUMN_MAPPING: dict[str,str]  # 见 spec:笔记标题→title 等 13 项
class CreatorExportError(Exception): ...   # 导出失败(含 reason)
def export_notes(page, account_id: int, download_dir: str, ts: str) -> list[dict]
    # 导航创作中心 → expect_download 存 <download_dir>/export_<account_id>_<ts>.xlsx → openpyxl 解析
    # → list[dict](每行 13 字段 + account_id);任一步失败抛 CreatorExportError
def parse_export_xlsx(path: str, account_id: int) -> list[dict]   # 解析纯逻辑,单独可测
```

**Interfaces(Consumes):** `settings.SELFHEAL_ENABLED/LLM_API_KEY`;`app.browser.self_heal.SelfHealLocator`(自愈复用,可选 fallback)。

- [ ] **Step 1: 写失败测试 `tests/test_creator_export.py`**

**只测 `parse_export_xlsx` 纯逻辑**(export_notes 依赖真 page,留集成)。用 openpyxl 造 fixture .xlsx:
```python
# 用例:
# 1. test_parse_maps_13_fields:写表头(13 中文列)+ 1 行 → 返回 dict 13 字段 + account_id 注入
# 2. test_parse_cover_ctr_percentage:cover_ctr 单元格 0.12 → 返回 12.0;已是 12.0 → 保持
# 3. test_parse_int_and_float_columns:整数列 int()、时长 float
# 4. test_parse_missing_column_tolerant:缺某列 → 该字段给默认(0/0.0)不崩
# 5. test_parse_empty_sheet:仅表头无数据行 → []
```
（用 openpyxl.Workbook() 在 tmp_path 写 .xlsx 喂给 parse_export_xlsx。）

- [ ] **Step 2: 确认失败** —— 先 `pip install`:`source /home/roots/nbdpsy-server/.venv/bin/activate && pip install 'openpyxl>=3.1' -q`;然后 `pytest tests/test_creator_export.py -q` → FAIL(模块不存在)。

- [ ] **Step 3: 实现 `app/browser/creator_export.py`**

照抄 `/home/roots/小红书运营工具/backend/app/services/creator_center_exporter.py` 的 `_navigate_to_creator_center` + `_download_export` + Excel 解析,适配为独立函数(吃传入的 sync `page`,不建自己的浏览器):
- 导航:goto 主站预热 → creator warm-up(publish_url→home_url,等 `.d-sub-menu:has-text("数据看板")` 可见,≤3 轮,仍无 → `CreatorExportError("need_manual_login")`)→ 点数据看板 → 点内容分析 → `with page.expect_download(30000): 点导出数据` → save_as。
- 自愈复用:菜单/按钮点击封装 `_find_creator_element(page, selectors, intent_key, desc)`——先试硬编码 `page.wait_for_selector`/locator,失败且 `settings.SELFHEAL_ENABLED and settings.LLM_API_KEY` 时 `SelfHealLocator().locate(page, intent_key, desc)` 取 handle 点击;默认关时纯硬编码。intent_key:`creator_data_dashboard_menu`/`creator_content_analysis_menu`/`creator_export_button`。
- `parse_export_xlsx`:openpyxl load → 首行定位列索引 → 逐行按 COLUMN_MAPPING 取值 + 类型转换(int/float、cover_ctr 百分比)+ 注入 account_id → list[dict]。
- `requirements.txt` 加 `openpyxl>=3.1`。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_creator_export.py -q` + 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/browser/creator_export.py tests/test_creator_export.py requirements.txt
git commit -m "feat(notes): creator_export 导出器——导航创作中心下载 Excel + openpyxl 解析(复用自愈)"
```

---

### Task 3: note_export.py 导出任务(串行,Task 1+2 合并后)

**Files:**
- Create: `app/services/note_export.py`、`tests/test_note_export.py`

**Interfaces:**
- Consumes:Task 1 `note_metrics_service.upsert_notes`;Task 2 `creator_export.export_notes`/`CreatorExportError`;`app.browser.sync_client.SyncClient`;`app.browser.account_locks.account_locks`;`app.core.db.get_session`
- Produces:
```python
def start_export(account_id: int, cookies: list[dict]) -> str   # 起后台任务,立即返 export_id
def get_export(export_id: str) -> dict | None                    # {status: running/done/error, account_id, note_count, reason?}
```

- [ ] **Step 1: 写失败测试 `tests/test_note_export.py`**

照 `tests/test_cookie_check.py` 模式(monkeypatch 浏览器边界)。用例:
```python
# 1. test_start_returns_export_id_and_running:start_export → get_export 初始 running
# 2. test_success_flow_stores_and_done:monkeypatch export_notes 返假行 + upsert_notes → 轮询到 done + note_count
# 3. test_export_error_lands_error_entry:monkeypatch export_notes 抛 CreatorExportError → done 台账 error + reason,不崩
# 4. test_stale_terminal_evicted:终态条目超 TTL 驱逐(照 cookie_check)
```
（monkeypatch 点:`note_export` 内部调的 SyncClient.start / export_notes / upsert_notes,不真起浏览器;用 `_await` 小超时等后台任务,同 cookie_check 测试。）

- [ ] **Step 2: 确认失败** `pytest tests/test_note_export.py -q` → FAIL。

- [ ] **Step 3: 实现 `app/services/note_export.py`**

照抄 `app/services/cookie_check.py` 的骨架(进程级 `_registry` dict + `_tasks` 强引用 + TTL 驱逐 + `_run` 用 `asyncio.to_thread` + `async with account_locks.get(account_id)`):
- `start_export`:生成 export_id(uuid),登记 running,`asyncio.create_task(_run_export(...))`,返回 id。
- `_run_export`:`async with account_locks.get(account_id)` → `rows = await asyncio.to_thread(_export_sync, account_id, cookies)`(内部 `SyncClient(account_id, cookies).start()` 建 page + `export_notes(page, ...)` + `client.stop()` 收尾)→ `async with get_session() as s: await upsert_notes(s, account_id, rows, snapshot_date, now)` → 台账 done + note_count。`CreatorExportError`/任何异常 → 台账 error + reason,**不抛出**。snapshot_date/now/ts 用 `datetime.now(timezone.utc)` 在 `_run_export` 里生成(service 层可用真实时间,导出器/service 纯函数才注入)。
- `get_export`:读台账(读写各驱逐一次终态超龄条目)。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_note_export.py tests/test_note_metrics_service.py -q` + 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/services/note_export.py tests/test_note_export.py
git commit -m "feat(notes): note_export 导出任务——ephemeral 台账 + account_locks 串行 + 落库"
```

---

### Task 4: notes_rest.py 端点(串行,Task 1+3 合并后)

**Files:**
- Create: `app/http/notes_rest.py`、`tests/test_notes_rest.py`
- Modify: `app/http/__init__.py`(接线 3 行)

**Interfaces:**
- Consumes:Task 1 `note_metrics_service.list_notes/note_trend`;Task 3 `note_export.start_export/get_export`;`_decrypt_account_cookies`(可从 cookies_rest 复用或本地实现)
- Produces:3 端点 + MANIFEST_ENTRIES

- [ ] **Step 1: 写失败测试 `tests/test_notes_rest.py`**

用 `tests/rest_helpers.py`(rest_client/bearer/seed_account/make_operator)。用例:
```python
# 1. test_start_export_202:授权号 POST /api/accounts/{id}/note-exports → 202 {export_id,status:"running"}(monkeypatch start_export)
# 2. test_start_export_denied_403 / unknown_account_404
# 3. test_get_export_poll:GET /api/note-exports/{id}(monkeypatch get_export 返 done)→ 200;不存在 404;跨 operator 403
# 4. test_list_notes:seed 笔记数据(直接 upsert)→ GET /api/accounts/{id}/notes 读到;越权 403
# 5. test_notes_trend:?title=&publish_time=&trend=daily → 返回 daily 序列
# 6. test_manifest_covers_new_routes:防漂移测试仍绿(3 条 entries 双向全等)
```

- [ ] **Step 2: 确认失败** `pytest tests/test_notes_rest.py -q` → FAIL。

- [ ] **Step 3: 实现 `app/http/notes_rest.py`**

- `POST /api/accounts/{account_id}/note-exports` → status_code=202:current_operator → assert_account_access → 取账号解密 cookie(`_decrypt_account_cookies`)→ `note_export.start_export(account_id, cookies)` → `{export_id, status:"running"}`;账号不存在 NotFoundError。
- `GET /api/note-exports/{export_id}`:`entry = note_export.get_export(export_id)`;None → NotFoundError(404);`assert_account_access(operator, entry["account_id"], session)` 防越权;返回 `{status, note_count?, reason?}`。
- `GET /api/accounts/{account_id}/notes`:query `title`/`publish_time`/`trend`;有 `trend=daily`+title+publish_time → `note_trend(...)` 返 `{trend:[...]}`;否则 `list_notes(...)` 返 `{notes:[...]}`。current_operator + service 内 RBAC。
- MANIFEST_ENTRIES 3 条(键齐,admin_only:False);notes 写清异步契约 + 无 note_id/封面 + (account_id,标题,发布时间) 主键 + 需 creator 登录态。
- `app/http/__init__.py`:import notes_rest、ALL_ROUTERS 加、ALL_MANIFEST_ENTRIES 拼。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_notes_rest.py tests/test_manifest.py -q` + 全量不回归。

- [ ] **Step 5: 提交**
```bash
git add app/http/notes_rest.py app/http/__init__.py tests/test_notes_rest.py
git commit -m "feat(notes): notes REST 3 端点——触发导出/轮询/读快照与日趋势"
```

---

## 合并与验证(lead)

1. Task 1、2 从 main 并行实施(互不相扰:1=models/service/迁移,2=exporter/requirements)→ 各自 review → merge(alembic 迁移文件与 requirements 无冲突)。
2. Task 3 从新 main 串行 → review → merge。
3. Task 4 从新 main 串行 → review → merge(__init__.py 一行式,无并发冲突)。
4. 全并后:主仓 `alembic upgrade head`(建两表)+ 全量绿;`pip install -r requirements.txt`(openpyxl)。端点默认可用(不依赖自愈开关)。
5. 真机验证(需真号 creator 登录态):`POST /api/accounts/{id}/note-exports` → 轮询 done → `GET notes` 看真实笔记数据(含手工发布历史笔记)落库。
6. 部署:merge 到 main 涉及新表 → 生产需 `alembic upgrade head` + `pip install` + restart(有 DB schema 变更,不同于自愈的默认关零重启)。
