# 部署手册:API/Worker 双单元架构

> 配套设计:`docs/design/2026-07-24-api-worker-split-design.md`。
> 首跑上线的逐项 checklist(环境/`.env`/首跑验证/红线)见 `docs/DEPLOY.md`,本文不重复;
> 本文承载**双单元架构、日常部署 SOP、故障排查与回滚**。

## 一、架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│ nbdpsy-api.service        uvicorn app.server(NBDPSY_ROLE=api)   │
│   端口 8848。只做 REST 收发/鉴权/台账写入(enqueue)/台账读取(poll)│
│   绝不起浏览器、绝不跑长任务 → restart 亚秒级,随时可部署。      │
│   ExecStartPre 三件套(alembic / pack_extension / patch_driver)  │
│   只在本单元跑。                                                │
└───────────────┬─────────────────────────────────────────────────┘
                │ 共享 SQLite(WAL,data/nbdpsy.db)——无 Redis、无 RPC
┌───────────────┴─────────────────────────────────────────────────┐
│ nbdpsy-worker.service     python -m app.worker(NBDPSY_ROLE=worker)│
│   调度中枢(supervisor):5s 周期扫描 publish_jobs + browser_jobs │
│   → 按账号派生 account_worker 子进程 + op_images 执行 + reaper。│
│   自身不起账号浏览器。After=nbdpsy-api(迁移先就绪)。           │
│     └─ account_worker 子进程(python -m app.account_worker)      │
│        每账号独立 OS 进程 + 独立 camoufox 真屏 headed 会话,      │
│        本批任务串行执行完即退出;同账号同一时刻至多 1 个。       │
└─────────────────────────────────────────────────────────────────┘

另有 nbdpsy-video-worker.service(视频管线,独立 asyncio 进程)不在本次拆分范围,照旧。
nbdpsy-server.service 保留在仓库里作单进程回滚位(NBDPSY_ROLE=all),常态不启用。
```

角色开关是环境变量 `NBDPSY_ROLE`(默认 `all`):`api` 只挂路由;`worker` 只跑消费;
`all` 兼跑(开发/测试/单进程小部署,行为与拆分前逐字节兼容)。

## 二、单元安装(一次性)

```bash
sudo cp deploy/systemd/nbdpsy-api.service deploy/systemd/nbdpsy-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
# 切换时先停旧的单进程单元,再起双单元
sudo systemctl disable --now nbdpsy-server
sudo systemctl enable --now nbdpsy-api nbdpsy-worker
```

验活:

```bash
curl -sf http://127.0.0.1:8848/healthz          # {"ok":true}
systemctl is-active nbdpsy-api nbdpsy-worker     # 两个 active
```

## 三、日常部署 SOP

**核心纪律:改什么就只重启什么。api 随时可 restart;restart worker 前必须先跑
`scripts/check_no_inflight.sh` 确认没有在跑/排队任务。**

### 1. 只改了 API 侧代码(路由/鉴权/enqueue-poll 实现/schema)

```bash
git -C /home/roots/nbdpsy-server pull
sudo systemctl restart nbdpsy-api
curl -sf http://127.0.0.1:8848/healthz
```

api 无浏览器负载,restart 亚秒级,in-flight 浏览器任务全在 worker 侧不受影响。
这是**部署常态**——绝大多数改动走这条路。

### 2. 改了 worker 侧代码(supervisor/account_worker/浏览器执行链)

```bash
git -C /home/roots/nbdpsy-server pull
bash scripts/check_no_inflight.sh        # 退出码 0 才继续;非 0 会打印未完成任务明细
sudo systemctl restart nbdpsy-worker
journalctl -u nbdpsy-worker -n 20 --no-pager   # 确认 supervisor 起来、开始扫描
```

`check_no_inflight.sh` 非 0 时:等任务自然跑完再查,或确认可以牺牲后强行 restart——
被杀任务由僵死恢复兜底(语义见 §五),**不会静默丢失**,但非幂等类会落 error 需人工核对。

### 3. api 和 worker 都改了

先按 §2 流程处理 worker,再 restart api(顺序无强依赖,但 worker 的浅探测依赖 api 在线,
先 worker 后 api 可避免探测窗口期空转重试)。

### 4. 改了 `.env` / config

pydantic 启动锁定字段——**两个单元都要 restart**(还有 nbdpsy-video-worker,若视频相关
字段也动了)。worker 侧照样先跑 `check_no_inflight.sh`。

### 5. 带数据库迁移的发布

alembic 只由 api 的 `ExecStartPre` 跑(worker 单元不跑,避免双迁移竞争)。
顺序:`check_no_inflight.sh` → stop worker → restart api(迁移随启动执行)→ start worker
(worker 的浅探测 `curl -sf :8848/healthz` 保证迁移完成后才起)。

## 四、回滚

```bash
sudo systemctl disable --now nbdpsy-api nbdpsy-worker
sudo systemctl enable --now nbdpsy-server        # NBDPSY_ROLE 默认 all,单进程兼跑全部角色
```

`nbdpsy-server.service` 与拆分前逐项一致(8848/ExecStartPre 三件套/真屏环境),
`browser_jobs` 表向后兼容、无破坏迁移,回滚不需要动数据库。
代码级回滚(checkout 旧 tag + alembic downgrade)见 `docs/DEPLOY.md` §5。

## 五、故障排查

### 常用命令

```bash
journalctl -u nbdpsy-api -f                      # API 实时日志
journalctl -u nbdpsy-worker -f                   # worker/supervisor 实时日志
journalctl -u nbdpsy-worker --since "10 min ago" --no-pager
systemctl status nbdpsy-api nbdpsy-worker        # 进程态 + 最近日志摘要
bash scripts/check_no_inflight.sh                # 当前未完成任务明细
ps -ef | grep -E "app.worker|app.account_worker|camoufox" | grep -v grep   # 子进程树
```

### 症状 → 排查路径

| 症状 | 先看 | 说明 |
|---|---|---|
| 提交任务后一直 queued 不动 | `systemctl is-active nbdpsy-worker` | worker 没跑则任务只入库不消费;起 worker 即自动消化 |
| worker 反复重启 | `journalctl -u nbdpsy-worker -n 50` | 浅探测 `curl :8848/healthz` 失败=api 没起,先修 api;systemd Restart 每 3s 重试属预期 |
| 任务卡 running 很久 | `check_no_inflight.sh` 明细 + worker 日志 | 心跳 300s 周期 touch;超 900s 无心跳判僵死,按下表语义自动处置 |
| 结果里 status=error 且 reason 含 unknown | 任务明细 result 字段 | 非幂等任务被杀后的保守终态,需人工核对(见下) |
| 发布 job 悬挂 publishing | worker 日志 + publish_jobs 行 | account_worker 入口 finally 归位 pending+next_retry;若进程被 SIGKILL,由僵死恢复兜底 |

### 僵死恢复语义(重启/强杀后任务去向)

worker 启动时 + 周期扫描把 `running` 且心跳超 900s 的 browser_jobs 行按 kind 处置:

| kind | 处置 | 理由 |
|---|---|---|
| cookie_check | 重置 `queued` 自动重跑 | 幂等,重跑无副作用 |
| note_export | 重置 `queued` 自动重跑 | 幂等,重跑无副作用 |
| note_delete | 置 `error`,reason 含 `unknown` + 人工核对指引 | 非幂等:删除可能已执行了一半,自动重跑会误删 |
| op_images | 置 `error`,reason 含 `unknown` + 人工核对指引 | 非幂等:生图配额已可能消耗,自动重跑重复扣费 |

publish_jobs 沿用既有规则:`need_manual_login`/`account_restricted` 直接 failed 不重试;
普通失败按 `PUBLISH_RETRY_SCHEDULE` 排 `next_retry_at`;进程被杀的 publishing 悬挂由
入口 finally / 僵死恢复归位 pending 续跑。

### 优雅停机语义(restart worker 时发生什么)

SIGTERM → supervisor 置停止旗(不再认领/扫描)→ 给 in-flight 浏览器 10s 收尾机会 →
强杀残余 camoufox → 退出。`TimeoutStopSec=15` + `KillMode=mixed` 兜底:15s 内退不掉
就 SIGKILL 全进程组(连带清 camoufox 子进程树)。被杀任务走上表僵死恢复,不静默丢失。
