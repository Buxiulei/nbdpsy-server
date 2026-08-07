# 已发布笔记「移出合集」能力（P0）+ 合集成员名单（P1）+ 批量移出（P2）

- 日期：2026-08-07
- 需求：`/home/roots/NBDpsy/docs/2026-08-07-移出合集能力-需求.md`（6 合集 100 篇成员，其中 52 篇是误挂的科普笔记）
- 事实来源：用户 2026-08-07 编辑页四张实拍（当事实用）
- 前置：`feature/video-publish`（本分支基于它——两者都改 `app/browser/note_components.py`）

---

## 0. 一句话

`_set_collection`（加入）有了对称的 `_remove_collection`（移出），并在既有「绝不点 `.close-icon`」红线上开一个**受控例外**：只在显式请求移出的那一步、且以 `.collection-plugin-choose` 为容器 scope 的选择器上点它，默认路径纪律一字不改。

---

## 1. P0：移出合集

### 1.1 契约

`POST /api/accounts/{id}/note-components` 新增 `remove_collection_id` / `remove_collection_name`。

- 与 `collection_id` **同时传 → 422**：「换合集」的页面交互（点 chip 主体是否重新弹下拉）未取证，语义歧义不该由服务端静默替调用方选一个；
- `remove_collection_name` 单独出现不构成组件请求（与 `collection_name` 同款）；
- 回执 `applied.collection_remove` 三态 true/false/null，`components.collection_remove.status` = done/skipped/error。

### 1.2 幂等矩阵（`_remove_collection`）

| 当前态 | 目标 C | 结果 |
|---|---|---|
| 空态（「选择合集」） | 移出 C | `skipped`，**零点击零悬停** |
| 在 C 里 | 移出 C | 悬停 chip → 点 × → 回读空态 → `done` |
| 在 D 里（D≠C，名字已比对） | 移出 C | `skipped`，reason 带出实际所在合集名 |
| 已选但名字比对不了 | 移出 C | `error`，**绝不点 ×** |

### 1.3 四道闸（都写进函数 docstring）

1. **名字比对不过绝不动手**——`remove_collection_name` 是主路径（已选态开不了弹层，拿不到 id→名映射）；
2. **选择器容器 scope**——`.collection-plugin-choose .close-icon`，裸 `.close-icon` 页面上不唯一；
3. **未验证弹窗即停**——点 × 之后**新**出现的可见 `.d-modal`（动手前先取基线，页面上本来就有的不算）会抛 `NoteComponentsError` 中止**整单**（不提交）。这与设计初稿「文案含确认/移出就点确认」不同，见 §5 偏离 1；
4. **点完当场回读**——chip 必须回到空态或至少不再含目标名。

### 1.4 幂等零点击不提交

`collection_remove` 落 `skipped` 且这次请求只有它算数时，**一次发布都不点**，`applied.collection_remove=true` 直接取编辑器内回读。理由：提交是全量覆盖语义，存量清理会对上百篇非目标笔记跑这条路，每篇白提交一次就是上百次真发布。

### 1.5 导航

只走 `open_update_page` 深链（三次独立验证过），**完全绕开笔记管理列表的悬停图标组**——铅笔与垃圾桶相邻，调研期间真的弹出过删除确认框。方案乙（合集管理页）未启用，取证未跑到。

---

## 2. P1：合集成员名单（落地形态与初稿不同）

调研已确证 `note/collection/pc/list_v2` 响应体只有 `id/name/desc/note_num`，**没有成员列表**；合集详情页的入口/URL/是否存在分页成员接口，取证轮未跑到。在拿到实证前编一个页面路径出来点，是本仓最忌讳的事。

故 P1 以**单会话逐篇扫描**落地（`dry_run=true`）：一次会话里逐篇 `open_update_page` + `read_collection_label`，判这篇在不在目标合集。只用两个已验证的既有能力，**零新选择器、零点击、零提交**。等成员接口取证到手，换掉这条路的数据源即可，对外契约不变。

---

## 3. P2：批量（`note_collection_batch`）

一个 kind 两条路（`dry_run` 分），`POST /api/accounts/{id}/collection-batches` + `GET /api/collection-batches/{job_id}`。

- **一轮一会话**：整轮持有 `browser_slot`，N 篇共用一个 camoufox（会话频次才是被弹墙的直接原因）；
- **单轮上限**：移出 `NOTE_COLLECTION_REMOVE_ROUND_LIMIT`（默认 5，每篇一次真提交）/ 扫描 `NOTE_COLLECTION_SCAN_ROUND_LIMIT`（默认 60，只读）；调用方 `limit` **只能往小压**；
- **单轮预算** `ROUND_BUDGET_SECONDS=1200`，没轮到的进 `remaining`，**不算失败**；
- **撞墙即停**：剩余一篇不碰、撞墙那篇不记账、已完成的部分照常记账不回滚，该号置 `restricted` 并落 `risk_events`（`source` 用本 kind，不复用互动补量的，否则事后归因查错方向）；
- **非幂等**，不进 `_IDEMPOTENT_KINDS`。

**没做日上限**：日上限需要一张按篇计数的台账（`interaction_backfill` 靠 `note_interactions`），本能力没有对应表，为它新开一张表在一次性清理任务上不划算。现有两道闸已够——单轮上限 + `worker` 的「同号一小时浏览器会话总闸」（`ACCOUNT_HOURLY_SESSION_CAP`，默认 4）。

---

## 4. 改动文件

- `app/browser/note_components.py` — `_visible_modal_texts` / `_remove_collection` 新函数、`apply_components` 新 step（排第 0）、`set_note_components` 透传 + 零点击不提交分支、`_verify_after_submit` 新分支、`_skipped_components` 新键
- `app/services/note_components.py` — `COMPONENT_FIELDS` + `start_components` / `_apply_sync` 透传
- `app/http/note_components_rest.py` — 两字段 + 互斥校验 + manifest 三处文案（含 `collection_id` 防呆化改写）
- `app/services/note_collection_batch.py`、`app/http/note_collection_batch_rest.py` — 新模块（P1/P2）
- `app/core/config.py`、`.env.example` — 两个单轮上限
- `app/http/__init__.py`、`app/account_worker.py` — 路由与 kind 接线
- `tests/test_note_components.py`、`tests/test_note_components_rest.py`、`tests/test_note_components_wire.py`、`tests/test_note_collection_batch.py`
- skill 侧 `nbdpsy-xiaohongshu-creator/scripts/note_ops.py`（另仓）—— `--remove-collection-id` / `--remove-collection-name` + 加入侧文案防呆化

无新表新列，不需要 alembic revision。

---

## 5. 与初稿的偏离（都往更保守偏）

1. **确认弹窗一律不点**（初稿：文案含「确认/移出」就点确认）。取证轮的 DOM dump 没跑到，弹窗的形态/文案/按钮全无实证；猜错按钮的代价从「没移出」到「删了别的东西」都有可能。现在见到任何可见弹窗就带原文抛硬错、整单不提交。**这条要在真号 e2e 里第一时间验掉**。
2. **P1 不是 `GET /collections/{cid}/notes` 同步端点**，而是批量 kind 的 `dry_run` 路（理由见 §2）。同步端点在 O(N) 篇的扫描下必然超时，而能让它变快的成员接口尚未取证。
3. **移出的幂等 skipped 不提交**（初稿默认走提交）。理由见 §1.4。
4. **没做日上限**，理由见 §3。

---

## 6. 未验证点（真号 e2e 要一并验掉）

| # | 未验证点 | 观察方式 |
|---|---|---|
| 1 | hover 后 × 的真实 DOM 是否即 `.collection-plugin-choose .close-icon` | 失败会报 `close_icon_not_found_after_hover`，reason 带当时 chip 文案 |
| 2 | 点 × 后有无确认弹窗 | 有则报 `collection_remove_unknown_modal` 并带弹窗原文，整单不提交 |
| 3 | 点 × 是立即生效还是要提交才落地 | 实现按最保守假设一律提交；若是立即生效，多提交一次只是空更新 |
| 4 | 提交后重进页面是否稳定呈现空态 | `applied.collection_remove` 三态如实上报；判据两档（空态强证据 / 目标名不在弱底线） |
| 5 | 合集详情页入口与成员分页接口 | 未取证，P1 因此走扫描路 |
