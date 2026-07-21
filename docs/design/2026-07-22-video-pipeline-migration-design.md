# 视频搬运/再制作管线迁入 nbdpsy-server 设计

日期：2026-07-22 ｜ 状态：用户授权 lead 决策定案（方案 C）｜ 源：小红书运营工具 `backend/app/services/video_transport/`（含 remake/revision，456 项测试语义，生产已验收 job16/17）

## 0. 目标与边界

把视频搬运（transport）与分镜级再制作（remake）+ 成片修订（revise）**整体迁入 nbdpsy-server**，成为其原生能力：走宿主 apikey 鉴权、`mcp.nbdpsy.com` 对外、宿主代码惯例（async services 收 AsyncSession、manifest 接线、中文详注）。迁移完成双 e2e 验收后，运营工具侧 API 标记 deprecated（410 指引），一个版本周期后删码。

**不做**：demucs/torch（transport BGM 层自动优雅降级，muxer 已有降级路径）；AIScheduler/registry/batch 大机器不搬（薄 provider 直连）；GPU/NVENC 不在本项目（同机，重启修复后自然可用）。

## 1. 后台任务体系（方案 C：独立 asyncio worker 进程）

- 新 systemd unit `nbdpsy-video-worker.service`：独立进程跑 `python -m app.video.worker`，与 API 进程（8848）隔离——API 重启不杀长任务。
- **VideoScheduler**（`app/video/scheduler.py`）：把运营工具 celery 版已验证的全部语义 1:1 翻译成 asyncio：
  - DB 状态机：`video_jobs` 表（mode/stage/stages_json/options/products/parent_job_id/heartbeat_at/retry_count/error，与源表同构）；
  - 轮询主循环：扫 `status=queued` 或到期恢复任务 → 原子占用（`UPDATE ... WHERE status='queued'` 防双发，扩展宿主 publish 范式）→ 逐阶段执行自链；
  - **阶段内心跳泵**（300s 周期 touch，独立协程）+ 恢复扫描（heartbeat 超 15min 判僵死，从 first_incomplete_stage 续跑，重排前先 touch）；
  - 阶段预算表（monotonic deadline 透传 handler）；并发上限 `VIDEO_WORKER_CONCURRENCY=1`（单机 CPU 编码，1 足够，可调）；
  - 阶段间大数据落盘 raw/*.json、stats 只存路径+计数（源铁律照搬）。
- worker 进程崩溃恢复 = 重启后恢复扫描按阶段续跑（源语义）。

## 2. 代码迁移面

```
app/video/
  worker.py            # 进程入口：起 VideoScheduler 主循环
  scheduler.py         # 方案 C 调度器（阶段自链/心跳/恢复/预算）
  stages.py            # 阶段 handler（源 video_transport_tasks 的 handler 层，去 celery 化）
  providers.py         # 薄 AI 直连层（见 §3）
  paths.py             # HMAC token 产物目录（源逻辑，根在 DATA_DIR/uploads/video/）
  pipeline/            # 源 video_transport 服务层平移：
    downloader.py transcript.py resegment.py translator.py glossary.py
    dubber.py muxer.py deliver.py job_store 语义并入 scheduler/models
    remake/ (style timeline storyboard analyzer rewriter tones composer inherit revision renderers/ templates/)
app/models/video_job.py
app/http/video_rest.py  # 6 端点(jobs CRUD/retry/revise) + MANIFEST_ENTRIES，apikey 中间件天然覆盖
```

- 平移原则：**battle-tested 逻辑逐行保真**（muxer/timeline/storyboard/analyzer/renderers/tones/revision 等纯逻辑模块原样搬，仅换 import 面：AI 收口 → providers、paths → 新 paths、config → 宿主 Settings）；测试全家平移（456 项语义），按宿主 tests/ 扁平布局与 `asyncio_mode=auto` 适配。
- 产物服务：仿 `uploads_rest.py` 新增 `GET /uploads/video/{token_dir}/{sub}/{name}` 只读路由（正则防穿越，免鉴权直链——HMAC token 即访问控制，与源一致）；URL 基于 `PUBLIC_BASE_URL`。

## 3. 薄 AI provider 层（providers.py，直连不搬调度器）

| 能力 | 实现 | 配置（Settings 新增，.env.example 同步） |
|---|---|---|
| ASR | DashScope paraformer-v2（源 dashscope_asr 平移） | `DASHSCOPE_API_KEY` |
| 翻译 | qwen-mt-plus（源 translator 的调用面平移，translation_options 直传） | 同上 |
| LLM（重写/解析/本地化） | DashScope openai 兼容 `qwen3.7-plus`（复用宿主已有 openai 客户端模式，async） | `VIDEO_LLM_MODEL` 等 |
| VL | DashScope qwen-vl-max（openai 兼容 multimodal 或 dashscope SDK） | 同上 |
| TTS | 豆包声音复刻 v3（源 doubao_tts 平移） | `DOUBAO_TTS_APPID/TOKEN/VOICE`(默认 S_hoiqVFN72) |

统一薄接口：`async asr_transcribe(url)->segments` / `async llm_chat(messages,**kw)->str` / `async vl_describe(image_path,prompt)->str` / `async mt_translate(...)` / `async tts_synthesize(text,voice,out)->duration`。重试语义随源（dubber._retry 模式）。

## 4. 依赖与部署

- requirements 增：`yt-dlp pydub numpy dashscope`；系统依赖 ffmpeg（DEPLOY.md + systemd 文档注明）；Playwright 复用宿主 1.60（still_image 截图；chromium 复用宿主安装）。
- systemd：新增 `nbdpsy-video-worker.service`（After=nbdpsy-server，EnvironmentFile 同源）；`deploy/systemd/` 落文件。
- 最小 CI：`.github/workflows/ci.yml`（pytest -m "not slow"，ubuntu-latest + ffmpeg apt）——宿主此前无 CI，本项目顺手补。

## 5. 验收与切换

1. 单测/集成全绿（平移 456 项 + 新调度器测试）；
2. 双 e2e：EMDR remake 全链（对 job16 基线逐项 QC 清单：补漏 76/球停提问/零跳变栅格不变量/居中声明/锐利球/120fps/立体声）+ revise 增量（缓存命中≥95%）；
3. nbdpsy-skill 对接文档改指向 `https://mcp.nbdpsy.com/api/video/*`（apikey 凭据）；
4. 运营工具 `/api/video-transport/*` 返回 410 + 新地址提示（一个版本周期后删码）。

## 6. 风险与已知取舍

- transport 模式无 demucs → BGM 层降级纯配音替换（源 muxer 优雅降级路径，remake 不受影响）；
- 单 worker 并发 1：同视频链路 CPU 密集，排队语义与源一致；
- nbdpsy-server 本地 main 领先 origin 29 commits（仓主自身状态）——迁移分支基于本地 main，push/PR 流程由仓主惯例决定（无 CI 时合并门=本地全绿+lead 终审）。
