# 待发定时任务原地修改 设计

**日期**:2026-07-15
**决策**:发布流程「定时发布」用现有服务端定时(`PublishJob.schedule_time` + `scan_once` 到点发),
**不做**小红书原生定时按钮。现有已支持 发/列/查/取消,唯一缺口是**原地修改待发任务**——补一个
PATCH 端点,让 `pending` 任务可改时间与内容,不必"取消再重建"。

## 背景(已核实现状)

- `POST /api/publish-jobs`:建 job(可带 `schedule_time`);无 schedule_time 立即 `submit` 入队,
  有则压库等 `scan_once` 到期自取。
- `GET /api/publish-jobs/{id}` 轮询;`GET /api/publish-jobs` 列表(按 account/status 筛);
  `POST /api/publish-jobs/{id}/cancel` 取消(仅 pending → canceled)。
- `PublishJob` 字段:`account_id/title/content/images_json/topics_json/schedule_time/status/...`;
  status 生命周期 `pending → publishing → published/failed/canceled`。
- 复用件:`_parse_schedule_time`(ISO8601 带时区 → naive UTC)、`_job_view`、`_MAX_IMAGES=18`、
  `assert_account_access`、`get_active_scheduler().submit`。

## 端点

`PATCH /api/publish-jobs/{job_id}`(REST;与 `.../cancel` 同风格,不进 MCP facade——与 cancel 一致)。

**请求体**(全部可选,PATCH 部分更新):
```python
class PublishJobPatchRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    images: list | None = None
    topics: list[str] | None = None
    schedule_time: str | None = None
```

## 语义

1. **仅 `pending` 可改**。非 pending → `{"ok": false, "status": <当前态>}`(镜像 cancel 契约,不报错)。
2. **原子防竞态**:pending job 随时可能被 `scan_once` 翻 publishing。用**条件更新
   `UPDATE ... WHERE id=:id AND status='pending'`** 落库,查 rowcount:0 行 = 已被调度器抢走 →
   返回 `{"ok": false, "status": <重读的当前态>}`,**绝不改到正在发的 job**。
   (现有 `cancel` 有同款 pre-existing TOCTOU 竞态,本次不顺手改它——surgical。)
3. **PATCH 部分更新语义**:用 Pydantic v2 `model_fields_set` 区分"未传"与"显式传":
   - 字段未在请求体出现 → 保持原值不变。
   - `schedule_time` 显式传 `null` → **清空定时 = 转立即发**;落库后 `scheduler.submit(job_id)`
     立即入队(不等下个 scan 周期),对齐 create 的立即发路径。
   - `schedule_time` 传新 ISO8601 串 → `_parse_schedule_time` 解析后更新。
   - `title/content` 传 → 更新对应列;`images/topics` 传 → 重新 `json.dumps` 落 images_json/topics_json。
4. **`account_id` 不可改**(改账号会绕过 access 校验;请求体不含该字段)。
5. **校验复用**:`images` 若传,沿用张数校验(空 → 400"至少 1 张";>18 → 400);topics 存法同 create。

## 返回 / 错误

- 成功:`{"ok": true, "job": <_job_view(job)>}`(回带更新后视图,调用方直接看到新计划)。
- 非 pending:`{"ok": false, "status": <当前态>}`。
- 404:job 不存在;403:无该账号 access;400:images 越界。

## 鉴权

`current_operator()` + `assert_account_access(operator, job.account_id, session)`——与 cancel/get 一致,
越权 403。

## 收口

- `MANIFEST_ENTRIES` 补一条 PATCH 描述(agent/claude.ai/插件可发现)。
- `_JOB_STATUSES` / `_MAX_IMAGES` / `_parse_schedule_time` / `_job_view` 全部复用,不重复定义。

## 测试

- pending 改 `schedule_time` → 持久化 + 视图反映。
- pending 改 `title/content/images/topics` → 持久化。
- `schedule_time` 显式 null(清空)→ 转立即发且 `submit(job_id)` 被调用。
- 未传的字段保持不变(model_fields_set 语义)。
- 非 pending(publishing/published/failed/canceled)改 → `{ok:false, status}` 且 DB 无变化。
- 条件更新:status 非 pending 时 rowcount=0 → ok:false(模拟并发抢占)。
- `images` 空 / >18 → 400。
- 404 job 不存在;403 越权。
- 现有 create/list/cancel/get 路径回归不变。

## 明确不做(YAGNI)

- 不做小红书原生定时按钮(改用服务端定时,可原地改计划,已定)。
- 不改 `account_id`(安全)。
- 不修 cancel 的 pre-existing TOCTOU 竞态(surgical,与本需求无关)。
- 不做 chrome 插件里的"编辑"UI 按钮(本端点先给能力;插件 UI 是独立前端需求)。
- MCP facade 不加此工具(与 cancel 保持一致,REST-only)。
