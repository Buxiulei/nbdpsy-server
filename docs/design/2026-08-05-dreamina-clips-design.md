# 即梦（Dreamina）视频生成服务化设计

日期：2026-08-05 ｜ 状态：按 skill 侧需求契约实施（`NBDpsy/文档/2026-08-05-server需求-即梦视频生成服务化.md`）｜ 消费方：nbdpsy-skills `nbdpsy-text-to-video/scripts/jimeng_gen.py`（v1.60.0 双后端客户端已发版，端点一上线即自动切换）

## 0. 目标与边界

把 skill 侧「笔记 → 成片十步」里**唯一碰即梦的第 5–6 步**（submit / fetch）搬进 server：dreamina CLI 的持有、登录态、提交、轮询、取片、积分查询全部服务端化，产物 MP4 落盘给**免鉴权公网直链**（形态同 YouTube 搬运 `products.video_url`）。

**不搬**：分镜（智力工作）、TTS、时长写回、BGM、manifest、ffmpeg 合成、审查——继续在 skill 侧。

搬的理由按痛感排序：① 即梦登录走**本地回调流**（callback 指 `127.0.0.1:<port>`，凭据落 `~/.dreamina_cli/`），是全产线唯一发不出去的凭据，每台运营电脑都要人扫码；② 生成异步、单账号串行，高峰排队近 2 小时，本地跑要运营挂着会话，submit_id 丢了就是积分白烧；③ 积分是公司资产却散在各人机器上不可观测。

登录态落地取需求第五节**方案 A（文件迁移）**：管理员在任意电脑扫码后把 `~/.dreamina_cli/` 整目录放到服务账号家目录；server 只负责把「登录失效」暴露在 `/api/dreamina-status`。B（SSH 隧道）不做——每次要从日志里捞端口和 secret，运维复杂度换不来什么。

## 1. 贯穿全局的一条事实：submit 即占队列位、success 即扣积分

这条产线上「重复提交」不是浪费时间而是**烧钱**，且排队中的任务 CLI **无法取消**。所有重试语义都是从这一条推出来的，而不是偏好：

| 场景 | 处置 | 为什么不那么做 |
|---|---|---|
| 任务卡 querying 数小时 | 不动，如实下发 `queued_seconds` | 自动重提 = 双倍扣分；换档重发是运营的决策（旧 submit_id 保留，谁先出用谁） |
| submit 超时 / 被信号打断 / rc=0 但没 submit_id | 判 `error`，error 文案写明「任务是否已到即梦队列**未知**，不自动重提防双扣」 | 重排回 queued = 赌它没入队，赌输就是双倍扣分 |
| 进程崩在 submit 中途留下 `submitting` 行 | 启动自愈判 `error`（同上话术） | 同上。这是崩溃恢复的唯一正确处置 |
| `query_result` 瞬时失败 / 非 JSON | 只写 `last_poll_error`，**status 不动** | 写成终态会让运营误判任务已死而重发 |
| 同 `client_ref` 重发 | 回已有 clip_id，零新建 | 网络抖动重发、agent 重试都不该烧第二份积分 |
| 同 ref 命中一条**从没跑过 submit CLI** 的 error 行 | 原地**复活**（重物化 + 回 `queued`，同 clip_id） | 图源一次瞬时故障就把这个幂等键永久烧死，那一镜再也生不出来 |
| 同 ref 命中**跑过 CLI** 的 error 行（有 submit_id / 认领过） | 原样返回，绝不复活 | 资金状态未知或已扣分，复活 = 可能双倍扣分 |
| 认领后久久不落终态的 `submitting` 行 | 每轮 sweep 判 `error`（阈值 3×submit 超时） | 只靠启动自愈的话，运行期产生的残留要卡到下次重启 |
| 余额低于粗估 | `warning` 但照常入队 | 扣费 success 才结算、排队中还有变数，凭估算拒绝会误杀真能跑成的提交 |
| 余额 < 25（最便宜一镜） | 409 拦 | 这时提交必然失败，入队只是制造垃圾行 |

## 2. 数据面：`video_clips` 单表

一条任务一行（`app/models/video_clip.py`，迁移 `e2c47f9b3a15`，down_revision `a1f6d2e83b90` 单头）。三个不显然的决定：

- **`clip_id = vc_ + token_hex(5)`，形态钉死**。`secrets.token_hex(8)` / `uuid4().hex[:16]` 这些默认写法恰好撞上本机 dreamina CLI `submit_id` 的形态（16 位纯小写 hex），而 skill 侧 auto 模式靠**形态**判断「这个 id 该问 server 还是问本机 CLI」——撞车会把 server 的 clip 派到本机 CLI 去查一个不存在的任务、空转到超时。需求第三节第 1 条 / 验收第 8 条为此专门立了款，测试里也有独立断言。
- **幂等键是 `UNIQUE(created_by, client_ref)` 而不是 `UNIQUE(client_ref)`**。单列唯一会让 B 运营用 `ref="shot-1"` 时拿到 A 的任务——既是抢注也是他人任务状态泄漏。SQLite 里 NULL 不参与唯一比较，故不带 ref 的任务可任意多条。这个约束同时是并发双发的最后一道闸：后到者撞 IntegrityError → 回滚重查拿同一条，绝不新建第二条。
- **状态机六态，对外五态**。`submitting` 是内部态（CLI 调用在途），GET 时映射回 `queued`。它存在的唯一理由是给「原子认领」一个落点：`UPDATE ... WHERE status='queued'`，rowcount==1 才算占到，杜绝两轮扫描对同一 clip 二次 submit。**认领时同时写 `submitted_at`**（提交成功再刷成真正的提交时刻，`queued_seconds` 语义不受影响），它兼任两个结构化判据：sweep 据此算认领时长；幂等复活据此判「这行有没有被 CLI 碰过」。用时间戳而不是解析 error 文案——文案会截断、会带 CLI 原文、会改。

`error` 与 `last_poll_error` 分列，理由见 §1 表格第四行——需求第四节第 4 条明确要求界面能区分「在排队（正常）」与「查询接口连续失败（异常）」。

## 3. 执行面：`DreaminaScheduler`（worker 进程内）

没有沿用 `VideoScheduler` 那套 stage 框架 / 心跳泵 / 僵死回收——本调度不跑长任务，每次 CLI 调用都有硬超时（submit 120s / query 300s），「僵死」在这里的等价物就是 `submitting` 残留，由启动自愈一次性处置。剩下的是两个阶段的循环（每 `CLIP_POLL_SECONDS=20`）：

- **submit**：按 id 序扫 `queued` → 原子认领 → 组 CLI 参数（`--poll=0` 纯提交；`image2video` **不带 `--ratio`**；一律 `--video_resolution=720p`，Seedance 家族只有这一档）→ 按结局落 `submitted` / `error`。
- **poll**：`submitted|querying` 且距上次查询 ≥ `CLIP_QUERY_INTERVAL=60s` → `query_result --download_dir=<clip 工作目录>` → `success` 落产物 / `failed` 落 error / 其余保持排队语义。上次查询时刻放**内存 dict** 不加列：重启后立即全量 poll 一轮无害（query 是只读、不占队列、不扣分）。

**所有 CLI 调用经两层锁串行**：单账号 + CLI 自带本地 tasks.db，两个 CLI 同时跑会互相踩。并发在这条链路上没有收益（即梦侧本来就是单账号串行排队）。两层缺一不可：进程内 `asyncio.Lock` 管本进程协程；**跨进程文件锁**（`DATA_DIR/dreamina-cli.lock` 上的 `flock`）管 api / worker 两个 systemd 单元——API 侧 `/api/video-credits`、提交前的登录闸都会跑 `user_credit`，与 worker 侧的 submit/query 是两个 OS 进程，`asyncio.Lock` 对它们完全不生效。等锁上限 10s，等不到回 `_RC_LOCK_BUSY`（**CLI 没被调起**，与「超时=歧义结局」相反）：submit 复位回 `queued` 下轮再提，poll 本轮跳过，`user_credit` 退回上次缓存的结论且不刷新缓存（绝不因为「CLI 忙」把登录态判成失效——那会缓存 60s，期间提交全 503）。

**非阻塞红线**：一律 `asyncio.create_subprocess_exec`，禁 `subprocess.run`——worker 单事件循环上还有 supervisor 扫描、视频调度心跳泵，同步阻塞会把整个循环冻住。

产物落 `DATA_DIR/uploads/clips/{clip_id}-{hmac16}/clip.mp4`：HMAC 由 `SECRET_KEY` 派生（手法抄 `app/video/paths.py`，那边一个字没动），不可猜的目录名即免鉴权直链的访问控制。CLI 下载下来的原名 `{submit_id}_video_N.mp4` 会被改名成 `clip.mp4` / `clip_2.mp4`…——文件名白名单只放行这个形态，顺带把即梦任务号挡在 URL 之外。

`ClipReaper` 按 `CLIP_TTL_DAYS=14` 收三类，都只删盘不删行：① `done` 且产物过期 → 删目录 + 清 `video_url`（**status 保持 done、`credit_count` 保留**，积分对账要用，删行等于毁账）；② `error` 终态超 TTL → 只删工作目录（error 文案是复盘依据，行必须留）；③ **无主孤儿目录** → `uploads/clips/` 下没有对应 DB 行、mtime 又超 TTL 的目录，来自「先建目录物化参考图、再插行」中途失败留下的半截。③ 卡 TTL 而不是「没行就删」，是为了不误杀那几秒里正在物化、行还没插进去的目录。

**产物过期不写 `error`**：那一格只装「任务失败原因」，掺进「产物已清理」会让运营和 skill 侧判 error 的分支把一条成功的片当成失败。改由 GET 视图算一个 `expired` 布尔键下发（`video_url` 已清 或 `expires_at` 已过），语义正交。

## 4. 接口面：6 个 `/api/*` + 1 条免鉴权直链

端点、字段名逐字对齐 skill 侧 `jimeng_gen.py` 顶部的 `EP_*` / `K_*` 常量块（那边已发版，改名就是断链）：

```
POST /api/video-clips            → 202 {clip_id, warning?}
GET  /api/video-clips/{clip_id}  → 单镜视图（含 queued_seconds / expires_at）
POST /api/video-clip-batches     → 202 {batch_id, clip_ids[]}   clip_ids 与 shots 等长同序
GET  /api/video-clip-batches/{batch_id} → 逐镜汇总 + summary
GET  /api/video-credits          → {credit, low_threshold_hit}
GET  /api/dreamina-status        → {logged_in, credit, compliance_confirmed_models}
GET  /uploads/clips/{token_dir}/{name}   免鉴权直链（token 目录即访问控制）
```

三处值得记的取舍：

- **参考图在建行之前同步物化**（仿 note-components `add_images`：坏图当场 4xx）。`/uploads` 来源**复制**而非引用——图床 7 天懒清理，clip 的 TTL 是独立的，引用会让重查的镜找不到图。只收图床直链或 `/uploads` 路径，**不收 base64 大包**（524 教训）。**判来源看 scheme 不看 path 形状**：`http(s)://` 一律走远程下载（本服务公网域名的完整直链就长这样，经 Cloudflare 解析到公网 IP，回环下载正常），裸 `/uploads/...` 才查本地图床；改前「URL 的 path 以 `/uploads` 开头就当本服务图床」会把任意主机的 `https://evil.example/uploads/x.png` 误认成自家的图。远程下载三道闸：**SSRF**（解析出的每个 A/AAAA 都不得是 loopback/RFC1918/link-local/169.254 等，且**重定向的每一跳都过闸**——只查初始 URL 挡不住跳板）、**总时长 30s**（`asyncio.wait_for` 包整段；httpx 的 timeout 是每次操作各自计时，慢速滴流能拖到无限长）、**15MB 上限**。
- **`batch_id` 对纯重放批返回 `null`**（或命中镜共享的原批次号），不现编一个 DB 里一行都没有的号——那种号拿去 GET batch 必 404，比 `null` 更难排查。clip 定位一律以 `clip_ids` 为准；GET batch 另加 `vcb_` 形态闸，把 `"null"` / `"None"` 这类字面量当批次号查直接 404。
- **批量的「逐镜不连坐」有个两难**：物化失败时 clip 行还没建，无法「置该镜 error」。定稿是——Pydantic 结构校验整批（结构错 = 调用方 bug，整批 422 可接受），物化/建行逐镜独立：物化失败的镜**照样建行但直接 `status=error`**，错误写物化原因。这样 `clip_ids` 与 `shots` 保持等长同序（skill 按下标映射 shot-NN），一镜坏不影响其余入队。
- **合规授权错误原文透传** + 附「需人到 Dreamina 网页端做一次性授权」提示。这是账号级一次性动作，服务端重试无意义（需求第四节第 6 条）。
- `compliance_confirmed_models` 是**观测近似**：CLI 没有「查某模型是否已授权」的接口，能确证的只有「这个模型真出过片」，故取 DB 里有 done 记录的 distinct model。manifest notes 里写明了这层含义，避免消费方把「不在列表里」当成「未授权」。

## 5. 接线与配置

- `app/http/__init__.py`：`dreamina_rest` 进 ALL_ROUTERS + ALL_MANIFEST_ENTRIES（防漂移测试 `tests/test_manifest.py` 钉死）。
- `app/worker.py` Supervisor：新开关 `include_dreamina`（与 `include_video` 同级），worker 进程入口传 True 起 `DreaminaScheduler` + `ClipReaper`。`app/server.py` 的 `role=all` 回滚位**不接**——它本来也没起 VideoScheduler，保持「后台消费只在 worker 进程」的既有形态。
- Settings 新增 10 个字段全带默认值（生产 `.env` 零改动可跑），`.env.example` 同步分区。`DREAMINA_BIN` 必须是**绝对路径**：systemd 进程 PATH 不含 `~/.local/bin`，交互 shell 能跑不代表服务能跑。
- **上线记得 API 与 worker 两个 systemd 单元都 restart**（08-04 生图竖版事故同型：只重启 API，参数看着传进去了就是不生效）。真正跑 dreamina 的是 worker 单元。

## 6. 验收对照（需求第七节）

| 条款 | 落点 |
|---|---|
| 1 单镜 text2video 到 done、直链可下、credit_count 对得上 | 调度器 poll success 分支；上线后真号联调 |
| 2 image2video 传 ratio → 422 | `CreateClipRequest._check_media_matrix`，测试 `test_validation_matrix` |
| 3 批量 8 镜逐镜独立、一镜 error 不连坐 | `test_batch_ids_align_with_shots_and_no_collateral_damage` |
| 4 同 client_ref 重发回原 clip_id | `test_client_ref_replay_returns_same_clip` |
| 5 断网重连后 GET 仍能取到 done 直链 | 状态全落库，无内存态；GET 纯读 |
| 6 拔登录态 → logged_in=false + 提交明确报错 | `_require_login` 503；`test_logged_out_blocks_submission_and_reports_status` |
| 7 批量重放零新增任务 | `test_batch_replay_creates_nothing`（DB 行数断言） |
| 8 clip_id 不是 16 位纯小写 hex | `test_clip_id_never_collides_with_cli_submit_id_shape`（200 个采样） |

## 7. 已知缺口（回执要写给 skill 侧）

- **本机参考图上传通道**：需求追记第 4 条问的「运营本机的 storyboard 参考图怎么上去」——现成通道就是 `POST /api/uploads/images`（multipart，图床直链 7 天），拿到直链再作为 `image` 提交即可，本次不新增端点。
- **CLI 版本号未进 manifest**：需求第六节要求「安装路径、版本落进 `/api/manifest`」。路径已进（`/api/dreamina-status` 的 notes），版本要另跑一次 CLI 才能拿到，为一个自检字段在每次 manifest 请求上挂一次子进程不划算，暂缺。
