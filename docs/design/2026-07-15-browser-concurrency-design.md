# 浏览器并发闸 + 空闲释放 + SQLite 并发写 设计

**日期**:2026-07-15
**决策**:补上浏览器并发缺口,支撑 20+ 运营。三块:①全局浏览器并发闸(publish/cookie-check/
note-export 统一,超出排队不崩)②保证释放 + 孤儿回收(防内存泄露)③SQLite WAL+busy_timeout
(并发写从报错变排队)。附 camoufox 瘦身选项减小单浏览器占用。

## 背景与关键前提(已核实)

- **浏览器是"每操作新建、用完 stop"**:`SyncClient.start()` 建 per-account 持久化 context
  (`NewBrowser(from_options=launch_options(...))`)→ 操作 → `stop()`(page→context 逐层关)。
  每操作必须加载**特定账号的 profile+cookies**(kill_orphans 按 profile argv 精确杀)。
- **camoufox 无法跨账号复用/池化**(暖浏览器服务不了别的账号)→ 不搞暖池;"释放"= 保证 stop
  + 回收崩溃残留的孤儿进程。camoufox server 模式对我们不适用。
- 三个浏览器入口(都经 `asyncio.to_thread` 把同步 SyncClient 下沉线程):
  publish(`PublishScheduler._publish_runner`,已有 PublishQueue 2 worker 硬闸)、
  cookie-check(`cookie_check._run_check`,**无全局闸**)、note-export(`note_export._run_export`,**无全局闸**)。
- camoufox `launch_options` 支持 `block_images` / `block_webgl` / `headless` / `humanize` /
  `virtual_display`;我们现传 `headless` + `block_webrtc=True`。
- SQLite:`create_async_engine(DATABASE_URL)` 无 connect_args;生产库 `journal_mode=delete`。
- 硬件 62GB/32 核;单 uvicorn worker。
- 用户定:`BROWSER_CONCURRENCY=6`;**publish 不 block_images**,cookie-check/note-export block_images。

## ① 全局浏览器并发闸(核心)

新 `app/browser/browser_gate.py`:进程级信号量,封顶同时运行的 camoufox 数。

```python
# 懒初始化、绑定运行中事件循环的进程级信号量(单 uvicorn worker = 单 loop)。
def _get_semaphore() -> asyncio.Semaphore   # 首次调用按 settings.BROWSER_CONCURRENCY 建,之后复用
@asynccontextmanager
async def browser_slot():
    """acquire 一个浏览器名额;满了就 await 排队(不拒绝)。用法:
       async with browser_slot():
           await asyncio.to_thread(<起 camoufox 的同步活>)
    出作用域(含异常)自动 release。"""
```

- **三处入口统一套 `async with browser_slot():`** 包住各自 `await asyncio.to_thread(...)` 的浏览器段:
  `_publish_runner` / `_run_check` / `_run_export`。总 camoufox 数 ≤ `BROWSER_CONCURRENCY`。
- **超出排队**:信号量 acquire 阻塞等待——三者都是异步(publish 队列 worker / cookie-check、
  export 后台任务),调用方已 202/立即返回,排队期间状态停在 checking/running,对调用方不可见。
- **与 PublishQueue 共存**:PublishQueue(2) 保留(publish 自身并行度),publish worker 在闸之下
  最多占 2 个名额(2<6,publish 不会被闸卡);闸的职责是给此前无闸的 cookie-check/export 加闸 +
  封顶总数。信号量非锁、每操作 acquire→release,无死锁。
- 配置 `app/core/config.py` 加 `BROWSER_CONCURRENCY: int = 6`。

## ② 保证释放 + 孤儿回收

**保证 stop**:审计三处入口,SyncClient 的 start→操作→stop **必须在 try/finally** 里(stop 在
finally,即便操作抛也关);`browser_slot()` 的 release 由 async with 保证。(现状:note-export 的
`_export_sync` 已 try/finally stop;审计 publish/cookie-check 补齐。)

**孤儿回收 reaper** `app/browser/browser_reaper.py`:周期后台任务(lifespan 启,类比 CookieChecker),
扫描所有 camoufox 进程,对每个:从其 `--profile .../account_{id}` argv 提取 account_id →
若该号的 `account_locks` **未被持有**(无在跑操作)**且**进程存活 > `BROWSER_REAP_AGE` → 杀掉
(复用 profile_guard 的 argv 精确匹配逻辑)。兜住崩溃操作残留的孤儿进程。配置
`BROWSER_REAP_INTERVAL: int = 300`(秒,0=关)、`BROWSER_REAP_AGE: int = 900`(秒,超此龄的无主
camoufox 才杀,给最长操作留余量)。lifespan 里 `if BROWSER_REAP_INTERVAL>0` 才起,测试不受影响。

## ③ camoufox 瘦身(减小单浏览器占用,同内存塞更多)

`SyncClient.__init__` 加 `block_images: bool = False` 参数;`start()` 的 `launch_options(...)`:
- `block_webgl=True`(全操作,无头 GPU 省内存,安全)。
- `block_images=self.block_images`(cookie-check/note-export 构造 SyncClient 时传 `True`;
  **publish 传 `False`**——保留发布页完整渲染,避免图元素缺失影响上传/发布按钮定位)。
- headless 维持现状(服务经 DISPLAY=:99 + headless);不改 virtual 模式(避免动既有稳定路径)。

## ④ SQLite WAL + busy_timeout

`app/core/db.py`:仅当 `DATABASE_URL` 是 sqlite 时(Postgres 不套):
- `create_async_engine(DATABASE_URL, connect_args={"timeout": settings.SQLITE_BUSY_TIMEOUT})`
  (aiosqlite 的 timeout = busy_timeout,并发写等待而非立即报 database is locked)。
- 首次连接设 `PRAGMA journal_mode=WAL`(SQLAlchemy `connect` 事件监听器里执行;WAL 让读写、
  多写者并发度更好)。配置 `SQLITE_BUSY_TIMEOUT: int = 30`(秒)。
- Postgres 迁移路径不在本设计实现范围(切 `DATABASE_URL` 即可,pragma 分支自动跳过)。

## 数据流(闸 + 排队)

20 运营同时触发操作 → 各自异步入口拿到请求(202/入队立即返回)→ 后台段 `async with
browser_slot()`:前 6 个 acquire 成功起 camoufox,其余 await 排队 → 每个操作 stop() 释放 +
出 slot → 排队的下一个 acquire 到名额起浏览器。总 camoufox ≤6,零 OOM;reaper 周期清崩溃孤儿。

## 错误处理

- 操作抛异常:`browser_slot()` async with 保证 release 名额不泄漏;SyncClient try/finally 保证
  stop 不泄漏进程。二者独立,任一异常都不占用名额/不留浏览器。
- reaper 本身异常:不崩后台 loop(整体 try/except,记 log 继续);杀进程失败(权限/已退)忽略。
- SQLite busy_timeout 到仍锁:抛原生 OperationalError(极端持续高写才触发),不吞;WAL 已大幅降概率。

## 测试策略

- **browser_gate**:`BROWSER_CONCURRENCY=2`,起 5 个 `async with browser_slot()` 并发任务(内部
  sleep + 计数当前在闸内数)→ 断言峰值 ≤2、5 个最终全跑完(排队不丢);异常任务出闸后名额归还
  (再 acquire 不阻塞)。
- **三入口套闸**:monkeypatch to_thread 的浏览器活为假 + 计数,验 publish/cookie-check/export
  都经 browser_slot(峰值 ≤ 上限)。
- **reaper**:造假进程列表(monkeypatch 进程枚举 + kill),验超龄无主 camoufox 被杀、有主(锁持有)
  的不杀、未超龄不杀;reaper 异常不崩。
- **block_images 传递**:cookie-check/export 构造 SyncClient 传 block_images=True、publish 传 False
  (查 launch_options 收到的参数);block_webgl 恒 True。
- **SQLite WAL**:临时 sqlite engine 起后 `PRAGMA journal_mode` == wal;busy_timeout 生效
  (connect_args timeout 传入);Postgres URL 时不套 pragma(URL 判断分支)。
- 全量不回归(现有 publish/cookie/scheduler/发布链)。

## 明确不做(YAGNI)

- 不搞浏览器暖池/跨账号复用(per-account profile 隔离决定不可行)。
- 不改 headless→virtual(动既有稳定路径,收益不明)。
- 不实现 Postgres 迁移(只留 pragma 分支跳过,切库是运维动作)。
- 不给 publish block_images(保发布保真)。
- reaper 不做"空闲暖浏览器回收"(没有暖浏览器);只清崩溃孤儿。
