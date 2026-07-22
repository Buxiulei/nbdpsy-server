# nbdpsy-api 上线 Checklist

配合 `README.md` 的部署叙述使用。本文件是**可勾选的操作清单 + 首跑验证 + 红线**。逐项打钩再上线。

## 0. 前置环境

- [ ] 机器有 **Xvfb**（`which Xvfb`；发布/检测用 Camoufox 走 `:99` 虚拟屏）。
- [ ] Python 3.11+；建好项目 venv：`python3 -m venv .venv && .venv/bin/pip install -U pip`。
- [ ] `.venv/bin/pip install -r requirements.txt`（含 `camoufox==0.4.11`；视频能力另含 `dashscope`/`yt-dlp`/`numpy`/`pydub`）。
- [ ] **拉 Camoufox 浏览器体**：`.venv/bin/python -m camoufox fetch`（首次必须，单测不需要但真发布需要）。
- [ ] **视频能力需系统 `ffmpeg`/`ffprobe`**（`which ffmpeg ffprobe`）：下载转码/配音拼轨/烧字幕/成片编码全依赖；不开视频 worker 可不装。
- [ ] **视频 still_image 渲染复用宿主 Playwright chromium**（`playwright==1.60.0` 已在 requirements）：首次 `.venv/bin/python -m playwright install chromium`（remake 的标题/文字卡截图用；不开 remake 可不装）。
- [ ] 反向代理已就绪，能把 `PUBLIC_BASE_URL` 代到本机 `API_PORT`。

## 1. 配置 `.env`（照 `.env.example` 全部字段，现 44 项）

- [ ] **`SECRET_KEY`** 设为**非默认**的高熵值。**这是硬闸**：`DEBUG=False` 且 `SECRET_KEY` 仍是默认值 `change-me-...` 时进程启动即 fail-fast 退出（防止用源码公开的 key 加密全量 cookie）。**一旦设定不可再改**——换 key 会让存量 cookie 全部解不出且静默返空。
- [ ] **`ROOT_ADMIN_APIKEY`** 设好（引导管理员的 apikey）。留空则启动时随机生成并在日志打印一次——生产建议显式设，别漏看日志。
- [ ] **`PUBLIC_BASE_URL`** = 对外访问地址（插件下载 URL、`GET /api/extension` 都用它）。
- [ ] `DATABASE_URL`（默认 SQLite `./data/nbdpsy.db`）、`DATA_DIR`、`UPLOAD_DIR`、`API_HOST/API_PORT`、`XVFB_DISPLAY=:99`。
- [ ] `PUBLISH_CONCURRENCY`（默认 2）、`PUBLISH_RETRY_SCHEDULE`（120,600,1800）、`PUBLISH_JOB_TIMEOUT`（600s）。
- [ ] `COOKIE_CHECK_INTERVAL`：默认 `0`=关闭周期巡检（按需 on-demand `POST /api/accounts/{id}/cookie-checks`）；设正整数秒才起后台巡检。
- [ ] `DEBUG=False`（生产）。

## 2. 起服务

- [ ] `bash scripts/xvfb.sh start`（幂等；`status` 可查）。
- [ ] `bash scripts/run.sh`（内部：`alembic upgrade head` → `pack_extension.sh` 生成 `DATA_DIR/extension.zip` → `uvicorn app.server:create_app --factory`）。用 systemd 托管（`exec uvicorn` 让信号/退出码直通）。
- [ ] 探活：`curl -s $PUBLIC_BASE_URL/healthz` 返回 `{"ok":true}`。
- [ ] 迁移单头：`.venv/bin/alembic heads` 只有一个 head（现为 `cb468a963422` psych_glossary）。

## 2b. 视频 worker（可选：开启视频搬运/再制作能力）

视频管线（transport/remake/revise）走**独立 asyncio worker 进程**，与 API 进程（8848）隔离——
API 重启不杀正在跑的长任务（方案 C）。**不需要视频能力可整节跳过**（`/api/video/*` 端点仍会
建 job 落 queued，但无 worker 消费即一直排队）。

- [ ] **DB 迁移已就绪**：视频表（`video_jobs` / `psych_glossary`）随 `alembic upgrade head` 建（§2 的
  `run.sh`/`nbdpsy-server.service` 的 `ExecStartPre=alembic upgrade head` 已覆盖）。worker 进程本身
  启动也会 `init_db` 兜底建表，但**生产以 alembic 为准**——先确保 API 侧迁移跑到 head 再起 worker。
- [ ] **系统依赖**：`ffmpeg`/`ffprobe` 已装（§0）；`dashscope`/`yt-dlp`/`numpy`/`pydub` 已随
  `requirements.txt` 装入 venv。remake 的 still_image 截图另需 Playwright chromium（§0）。
- [ ] **装 systemd 单元**：`deploy/systemd/nbdpsy-video-worker.service`（已随仓库提供）拷到
  `/etc/systemd/system/` → `systemctl daemon-reload` → `systemctl enable --now nbdpsy-video-worker`。
  单元要点：`After=nbdpsy-server.service`（DB/迁移先就绪）、`EnvironmentFile=-/…/.env`（与 API **同源**
  环境：`DATABASE_URL`/`DATA_DIR`/AI 凭据一致，撕裂会让 worker 读错库或缺凭据）、
  `ExecStart=.venv/bin/python -m app.video.worker`、`Environment=DISPLAY=:99`（still_image 截图用）。
- [ ] **AI 凭据齐**：`.env` 里 `DASHSCOPE_API_KEY`（ASR/翻译/LLM/VL 四能力共用）与
  `DOUBAO_TTS_APPID`/`DOUBAO_TTS_TOKEN`（配音）已填；缺则对应阶段 job 落 failed（error 可见）。
- [ ] **验活**：`journalctl -u nbdpsy-video-worker -f` 见 `VideoScheduler 已启动（concurrency=1）`；
  建一条 transport 任务 `POST /api/video/jobs`（YouTube 链接）→ 轮询 `GET /api/video/jobs/{id}` 从
  queued 转 running 即通链。产物在 `DATA_DIR/uploads/video/{id}-{hmac}/out/`，经 `/uploads/video/…` 直链。
- [ ] **并发**：`VIDEO_WORKER_CONCURRENCY` 默认 1（单机 CPU 编码 1 足够，排队语义与源一致）；
  阶段内心跳 300s（`VIDEO_HEARTBEAT_INTERVAL`）、僵死判定 900s（`VIDEO_STALE_TIMEOUT`）超阈从
  首个未完成阶段续跑。**改视频 config 后重启 worker**（`systemctl restart nbdpsy-video-worker`）。

## 3. 首跑验证（**本 build 未对真 XHS 账号跑过，务必走一遍**）

- [ ] **远程 agent 连通**：用某 operator 的 apikey（`Authorization: Bearer <key>`）探 manifest:
  `curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" $PUBLIC_BASE_URL/api/manifest`
  期望 `200`,响应体 `endpoints` 应有 36 条(含 6 个 `/api/video/*`);`GET /healthz`/`GET /api/whoami` 通。
- [ ] **建 operator + 授权**：admin apikey 调 `POST /api/operators`（记下一次性明文 apikey）→ `POST /api/operators/{id}/grants`。
- [ ] **装插件登录**：`GET /api/extension` → 下载 → chrome://extensions 开发者模式加载已解压目录 → 填 `serverUrl`(=PUBLIC_BASE_URL) 与 operator apikey → **勾选"在无痕模式下启用"**（MV3 限制，manifest 声明不了）→ 隐身窗口人工完成登录+验证 → cookie 自动推回。
- [ ] **验 cookie**：`POST /api/accounts/{id}/cookie-checks` **异步**——立即返 `check_id`，随后用 `GET /api/cookie-checks/{check_id}` 轮询到终态 `valid`/`invalid`/`captcha`/`error`。`valid` 附回填资料；`error` 是浏览器启动失败等基础设施故障（**不写回、保留原状态**，不等于 cookie 真失效——首跑盯一下）。
- [ ] **试发一条**：`POST /api/publish-jobs` → 轮询 `GET /api/publish-jobs/{job_id}` 到 `published`。**盯发布确认**：成功页只停 ~3s，代码已做"确认即立即收口"防重复发帖——首跑确认不出现重复发帖。
- [ ] **权限隔离**：换一个无该号 access 的 operator，确认 `POST /api/publish-jobs`/`GET /api/accounts/{id}/cookies` 对该号抛 403。

## 4. 运维红线 / 已知边界

- [ ] **同账号禁并发发布**（Camoufox/Firefox 单写锁）——队列已做 per-account 互斥，别绕过直接并发同号。
- [ ] **发布走服务器 IP**：账号在服务器 IP + 固定指纹上被操作；对新 IP 敏感的号首登可能触发验证（插件在住宅浏览器登录，操作在服务端，IP 不一致是既有现实）。
- [ ] **改代码/prompt 后必须重启** uvicorn（进程启动时加载模块）。
- [ ] **改 `.env`/config 后必须重启**（pydantic 启动锁定字段）。
- [ ] **Camoufox profile 锁**：崩溃残留 `lock`/`.parentlock` 会让下次启动死等——`profile_guard` 已在每次启动前清锁 + 杀孤儿（argv 精确匹配，不误杀兄弟号）。
- [ ] **备份**：`SECRET_KEY` 与 `DATABASE_URL` 指向的库一起备份（cookie 加密绑定 SECRET_KEY）。
- [ ] **不做的事**：本服务**不含**任何"去 AI 水印 / 洗图 / 绕过 AI 检测"能力，图片字节原样上传。这是设计红线，勿自行加装。

## 5. 回滚

- [ ] 保留上一个可用 commit tag；回滚 = `git checkout <tag>` + `alembic downgrade`（如涉及迁移）+ 重启。SQLite 库文件单独快照。
