# 内容资产库(content_archive)设计

> 2026-07-25。发布内容归档 + 跨账号复用 + 基于"最后使用时间"的 90 天滑动 TTL。
> 用户批准要点:全部发布成功自动归档;取内容(取详情)即刷新使用时间(不做手动标记);
> 内容库全局共享(打破账号壁垒是复用的目的)。

## 目标

- 每篇**发布成功**的笔记(文案/标签/图片/视频)自动归档,每条内容独立存储一份副本,
  供任意账号/运营复用发布、借鉴、复盘。
- 归档媒体脱离 uploads 的 7 天清理,由本库 90 天独立管。
- **取内容即续命**:取某条完整内容(不管为复用还是借鉴)刷新其 `last_used_at`;
  距最后使用超 90 天的归档自动删除(行 + 媒体目录)。

## 数据模型:`content_archive`

| 列 | 说明 |
|---|---|
| id INTEGER PK | 对外 archive_id |
| title / content TEXT | 文案 |
| topics_json TEXT | 标签(#tag)列表 JSON |
| media_json TEXT | 归档目录内媒体清单 JSON:`[{"type":"image"|"video","name":"01.jpg"}]` |
| kind TEXT | image_note(当前)/ video_note(视频发布接入后) |
| source_account_id INTEGER FK | 源发布账号 |
| source_note_url TEXT | 源笔记链接(小红书) |
| source_operator_id INTEGER | 源发布运营(publish_jobs.created_by,无 FK 与其一致) |
| source_publish_job_id INTEGER UNIQUE | 源发布任务 id;**唯一约束**保证同一发布只归档一条(幂等) |
| created_at DATETIME | 归档时刻 |
| last_used_at DATETIME | 初始=created_at;取详情即刷新 |
| use_count INTEGER | 被取用次数(取详情 +1) |

## 媒体独立存储

归档时把该发布用的图片(publish_jobs.images_json,可能是 /uploads 相对路径 / http / b64)
经 `materialize_images` 落成本地文件,再按页序改名存 `DATA_DIR/uploads/archive_{id}/{NN}.{ext}`
(NN 两位数字,复用现有 `/uploads/{batch}/{name}` **免鉴权直链**路由,`_NAME_RE`
本就放行 png/jpg/webp)。`archive_{id}` 目录无 `UploadBatch` 行 → 不被 uploads 7 天懒清理
覆盖 → 仅由 ArchiveReaper 按 90 天管。视频(video_note 接入后)存 archive 目录,届时
扩 `_NAME_RE` 放行 mp4;当前发布均为图文,不为不存在的视频发布路径写代码(YAGNI)。

## 自动归档钩子

抽公共 `archive_service.archive_published_job(db_path, job_id)`(sync,自读 job 行):
- 幂等:`source_publish_job_id` 已存在则跳过;
- 从 publish_jobs 读 title/content/topics_json/images_json/note_url/account_id/created_by;
- materialize + 改名媒体到 archive 目录;insert content_archive 行。

接入两条发布终态路径的 **published 分支**(同源,均生产路径):
- `app/account_worker.py` `_apply_publish_decision` published 分支(子进程,生产主路)——
  写终态后 sync 直调;
- `app/publish/scheduler.py` `finish` published 分支(all 模式回滚位)——`asyncio.to_thread` 调 sync。

**绝不阻断发布**:归档整段 try/except,失败只 `logger.warning`,发布终态已先行落库。

## REST(全局共享;鉴权 operator 皆可读,DELETE 限 admin)

- `GET /api/content-archive` —— 列库摘要(id/title/topics/kind/source_account_id/
  last_used_at/use_count/media 张数),支持 `?q=` 模糊搜标题+标签、`?limit=`。**不刷新**。
- `GET /api/content-archive/{id}` —— 取单条完整内容(title/content/topics/media 免鉴权
  直链 URL/source_note_url)。**取即刷新** `last_used_at=now`、`use_count+1`。404 不存在。
- `DELETE /api/content-archive/{id}` —— 删归档(行 + 媒体目录),admin only。204/404。
- 发布端点 `POST /api/publish-jobs` 增可选 `from_archive_id`(溯源标注,复用前已 GET
  详情刷新,此参数仅记录来源关系,不再重复刷新)。

## 90 天滑动 TTL:`ArchiveReaper`

套 `PlaceholderReaper` 模板:纯函数 `reap_archive_once(session_factory)` —— cutoff=
`utcnow - ARCHIVE_TTL_DAYS(90)`,SELECT `last_used_at < cutoff` 候选 → 删媒体目录
(`shutil.rmtree`)+ DELETE 行(内重申判据防并发)→ commit;后台类 `ArchiveReaper`
(先睡后扫 + interval==0 不启 + 优雅 stop),注册 `Supervisor._start_components`
(`ARCHIVE_REAP_INTERVAL>0` 才起,默认 21600s=6h)。

## 配置

`ARCHIVE_TTL_DAYS: int = 90`、`ARCHIVE_REAP_INTERVAL: int = 21600`。

## 测试

归档幂等(同 job 二次归档不新增)、媒体独立副本落地+改名、取详情刷新 last_used_at/
use_count、列表不刷新、reap 删过期(行+目录)、RBAC(DELETE 非 admin 403)、发布钩子
不阻断(归档抛错发布仍 published)。

## 非目标

- 不做内容编辑/版本管理(归档是只读快照)。
- 不做视频发布路径(当前无);media_json 结构预留 video 类型。
- 不占 operator 配额(归档是自动副作用,非任务)。
