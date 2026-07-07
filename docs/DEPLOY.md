# nbdpsy-mcp 上线 Checklist

配合 `README.md` 的部署叙述使用。本文件是**可勾选的操作清单 + 首跑验证 + 红线**。逐项打钩再上线。

## 0. 前置环境

- [ ] 机器有 **Xvfb**（`which Xvfb`；发布/检测用 Camoufox 走 `:99` 虚拟屏）。
- [ ] Python 3.11+；建好项目 venv：`python3 -m venv .venv && .venv/bin/pip install -U pip`。
- [ ] `.venv/bin/pip install -r requirements.txt`（含 `camoufox==0.4.11`）。
- [ ] **拉 Camoufox 浏览器体**：`.venv/bin/python -m camoufox fetch`（首次必须，单测不需要但真发布需要）。
- [ ] 反向代理已就绪，能把 `PUBLIC_BASE_URL` 代到本机 `API_PORT`。

## 1. 配置 `.env`（照 `.env.example` 全 18 字段）

- [ ] **`SECRET_KEY`** 设为**非默认**的高熵值。**这是硬闸**：`DEBUG=False` 且 `SECRET_KEY` 仍是默认值 `change-me-...` 时进程启动即 fail-fast 退出（防止用源码公开的 key 加密全量 cookie）。**一旦设定不可再改**——换 key 会让存量 cookie 全部解不出且静默返空。
- [ ] **`ROOT_ADMIN_APIKEY`** 设好（引导管理员的 apikey）。留空则启动时随机生成并在日志打印一次——生产建议显式设，别漏看日志。
- [ ] **`PUBLIC_BASE_URL`** = 对外访问地址（插件下载 URL、`get_extension_download` 都用它）。
- [ ] `DATABASE_URL`（默认 SQLite `./data/nbdpsy.db`）、`DATA_DIR`、`UPLOAD_DIR`、`API_HOST/API_PORT`、`XVFB_DISPLAY=:99`。
- [ ] `PUBLISH_CONCURRENCY`（默认 2）、`PUBLISH_RETRY_SCHEDULE`（120,600,1800）、`PUBLISH_JOB_TIMEOUT`（600s）。
- [ ] `COOKIE_CHECK_INTERVAL`：默认 `0`=关闭周期巡检（按需 on-demand `check_cookies`）；设正整数秒才起后台巡检。
- [ ] `DEBUG=False`（生产）。

## 2. 起服务

- [ ] `bash scripts/xvfb.sh start`（幂等；`status` 可查）。
- [ ] `bash scripts/run.sh`（内部：`alembic upgrade head` → `pack_extension.sh` 生成 `DATA_DIR/extension.zip` → `uvicorn app.server:create_app --factory`）。用 systemd 托管（`exec uvicorn` 让信号/退出码直通）。
- [ ] 探活：`curl -s $PUBLIC_BASE_URL/healthz` 返回 `{"ok":true}`。
- [ ] 迁移单头：`.venv/bin/alembic heads` 只有一个 head。

## 3. 首跑验证（**本 build 未对真 XHS 账号跑过，务必走一遍**）

- [ ] **远程 agent 连通**：用某 operator 的 apikey（`Authorization: Bearer <key>`）连 `PUBLIC_BASE_URL/mcp/`（注意结尾斜杠），`initialize` + `tools/list` 应见 22 工具；`health`/`whoami` 通。
- [ ] **建 operator + 授权**：admin apikey 调 `create_operator`（记下一次性明文 apikey）→ `grant_account_access`。
- [ ] **装插件登录**：`get_extension_download` → 下载 → chrome://extensions 开发者模式加载已解压目录 → 填 `serverUrl`(=PUBLIC_BASE_URL) 与 operator apikey → **勾选"在无痕模式下启用"**（MV3 限制，manifest 声明不了）→ 隐身窗口人工完成登录+验证 → cookie 自动推回。
- [ ] **验 cookie**：`check_cookies(account_id)` 返 `valid` + 回填资料。（注意：浏览器启动失败也会保守归 `invalid`，不等于 cookie 真失效——首跑盯一下。）
- [ ] **试发一条**：`publish_note(...)` → 轮询 `get_publish_status(job_id)` 到 `published`。**盯发布确认**：成功页只停 ~3s，代码已做"确认即立即收口"防重复发帖——首跑确认不出现重复发帖。
- [ ] **权限隔离**：换一个无该号 access 的 operator，确认 `publish_note`/`get_cookies` 对该号抛 403。

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
