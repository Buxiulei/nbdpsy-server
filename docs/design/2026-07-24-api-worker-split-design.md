# 架构升级设计:API/Worker 进程拆分 + 任务台账全落库 + 多账号并行调度

> 2026-07-24 深夜事故驱动(用户裁决:停清全部任务后整体改造;多账号并行操作是核心功能)。
> 事故复盘:单进程架构下,部署 restart 被 in-flight 浏览器线程挡住优雅退出(systemd 90s
> 强杀)→ 每次部署 ≈ 2 分钟停机,skill 侧发布重试连撞 ~20 次 502/530;且四个内存台账
> (cookie_check/note_export/note_delete/op_images)重启即丢。目标:20+ 账号、多运营并发
> 的生产可用性。

## 一、目标与非目标

**目标**
1. API 永远秒级响应、部署零(亚秒级)停机——浏览器负载与 API 彻底隔离。
2. 任务台账全落库:任何进程重启不丢任务、不丢终态,轮询 id 永不因重启 404。
3. 多账号并行为一等公民:同账号严格串行(风控),跨账号并行,账号间公平调度
   (单账号大批量不得饿死其他账号),全局浏览器并发有闸。
4. 运营级公平:每 operator 未完成任务配额,超额 429 明确报错。
5. worker 优雅停机 ≤15s:停止领新任务,在跑任务被杀后自动重派,不留半截状态。

**非目标(本轮不做)**
- 不换存储(SQLite WAL 够 20+ 账号的写入量;Redis/PG 是伪需求)。
- 不做多机水平扩展(单机 4090 是浏览器指纹与 headed 屏幕的物理绑定)。
- 不改任何对外 REST 契约(skill 侧零改动;台账落库只会让轮询更可靠)。

## 二、进程模型

```
nbdpsy-api.service      uvicorn(app.server, NBDPSY_ROLE=api)
  └─ 只做:REST 收发 / 鉴权 / 参数校验 / 台账写入(enqueue)/ 台账读取(poll)
     绝不起浏览器、绝不跑长任务。restart 亚秒级,随时可部署。

nbdpsy-worker.service   python -m app.worker(NBDPSY_ROLE=worker)
  └─ 唯一浏览器宿主:发布调度器 + browser_jobs 消费循环 + 视频 worker
     + cookie 巡检/各类 reaper。SIGTERM → 停领新任务 → ≤15s 退出。

通信:共享 SQLite(WAL)。API 写任务行,worker 扫描认领;结果写回行,API 读行。
无 Redis、无 RPC——沿用发布调度器/视频 worker 已验证的 DB 扫描模式(5s 周期)。
```

`NBDPSY_ROLE`(env,默认 `all`):`api` 只挂路由;`worker` 只跑消费;`all` 兼跑
(开发/测试/单进程小部署零回归)。测试套件继续用 `all`,行为逐字节兼容。

## 三、任务台账统一落库:`browser_jobs` 表

四个内存台账(cookie_check / note_export / note_delete¹ / op_images)统一收敛:

```
browser_jobs
  id           TEXT PK        —— 对外轮询 id(uuid hex,兼容各端点现有 id 语义)
  kind         TEXT           —— cookie_check | note_export | note_delete | op_images
  account_id   INTEGER NULL   —— 账号绑定类任务必填(op_images 为 NULL)
  operator_id  INTEGER        —— 提交者(配额与 RBAC 依据)
  payload      TEXT(JSON)     —— 任务入参(prompts/title/count/...)
  status       TEXT           —— queued | running | done | error
  result       TEXT(JSON)     —— 终态结果(note_count/urls/errors/deleted/...)
  claimed_by   TEXT NULL      —— worker 实例标识(乐观认领)
  heartbeat_at DATETIME NULL  —— 在跑心跳(300s 周期 touch;僵死判定 900s)
  created_at / updated_at
```

- **认领**:`UPDATE ... SET status='running', claimed_by=?, heartbeat_at=now
  WHERE id=? AND status='queued'`——rowcount=1 才算领到(单 worker 也保持该纪律,
  为未来多 worker 留正确性,不留架构债)。
- **僵死恢复**:worker 启动 + 周期扫描把 `running` 且心跳超 900s 的行按 kind 语义处置:
  幂等类(cookie_check/note_export)重置 queued 自动重跑;非幂等类(note_delete/
  op_images)置 `error/unknown` + reason 指引(沿用删除台账已落地的 unknown 语义)。
- 各 REST 端点仅改内部实现(enqueue/poll 走 DB),路径、请求/响应结构逐字段不变;
  note_deletions¹ 现有表保留读兼容一个版本,新写走 browser_jobs。
- publish_jobs / video_jobs 本就落库,不动;发布"立即投递"的进程内 nudge 改为
  worker 5s 扫描兜底(最坏多等 5s,换来进程解耦)。

## 四、多账号并行调度(核心)

worker 内单一 `AccountLaneScheduler` 统筹全部浏览器任务(发布 + browser_jobs):

1. **同账号严格串行**:沿用进程级 `account_locks`(kill_orphans 互杀与风控节律的
   既有保障,不变)。
2. **跨账号并行**:全局 `browser_slot` 信号量封顶(BROWSER_CONCURRENCY,默认 6,
   实测 headed 4K 屏 + 32G 内存可承载;账号数 >并发数时排队)。
3. **账号间公平**:候选任务按 (account_id 轮转, created_at) 排序——每轮扫描先给
   "有任务且不在跑"的账号各派 1 单,再回头派同账号第 2 单;单账号灌 50 单不会
   饿死其他 19 个账号。
4. **账号冷却/日上限**:发布专属的 cooldown/daily-cap 门不变(风控保障)。

## 五、运营配额与限流

- enqueue 时数该 operator 未终态任务(browser_jobs + publish_jobs):
  超 `OPERATOR_PENDING_QUOTA`(默认 30)→ 429 `{"error": "配额已满:未完成任务 N/30,请等待完成后再提交"}`。
- admin 不受限。无速率桶、无滑动窗——数据库计数即够,不增实体。

## 六、优雅停机与部署

- worker:SIGTERM → 置停止旗(不再认领/扫描)→ 给 in-flight 浏览器 10s 收尾机会 →
  强杀残余 camoufox → 退出。TimeoutStopSec=15 兜底(已上线)。被杀任务由僵死恢复
  按 kind 语义处置,绝无静默丢失。
- api:无浏览器负载,restart 亚秒级;**部署常态 = 只重启 api**,worker 仅在
  worker 代码变更时重启,且先看 `browser_jobs/publish_jobs` 在跑数(部署纪律已入库)。
- systemd:`nbdpsy-api.service`(顶替现 nbdpsy-server.service 的 8848 端口)+
  `nbdpsy-worker.service`(After=nbdpsy-api,共享 venv 与 .env)。

## 七、实施阶段(每阶段测试全绿再进下一阶段)

1. **P1 台账落库**:browser_jobs 模型 + 迁移 + 四服务改 DB enqueue/poll(REST 契约
   不变);单测:重启存活/僵死恢复/RBAC/配额。
2. **P2 worker 进程**:`app/worker.py` 入口 + AccountLaneScheduler + NBDPSY_ROLE
   接缝(lifespan 按角色挂载);`all` 模式全量回归。
3. **P3 systemd 拆分**:两 unit 上线 + 部署脚本;拆分后契约冒烟(manifest/健康/
   enqueue-poll 闭环)。
4. **P4 公平与配额**:轮转排序 + 429;并发公平单测(账号 A 灌 10 单不阻塞 B)。
5. **P5 e2e 验收**:双账号并行真发 1 次(录屏)+ 任务中途杀 worker 的恢复演练 +
   api 部署期间提交零失败演练。

## 八、风险与回滚

- 最大风险:P2 接缝漏挂某后台组件(reaper/巡检)→ `all` 模式回归 + lifespan 组件
  清单测试锁死。
- 回滚:`NBDPSY_ROLE=all` 单 unit 即回到现架构;browser_jobs 表向后兼容,无破坏迁移。
