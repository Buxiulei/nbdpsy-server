# 视频管线迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Spec: `docs/design/2026-07-22-video-pipeline-migration-design.md`（要求唯一出处）。

**Goal:** 视频 transport/remake/revise 全量迁入 nbdpsy-server（方案 C 独立 asyncio worker），双 e2e 验收后切换。

## Global Constraints

- python 用 nbdpsy-server 自己的运行环境（查 `scripts/run.sh`/systemd 确定解释器与 venv；若无独立 venv 则按宿主实际方式跑 pytest）
- **源代码是逻辑基准**：`/home/roots/小红书运营工具/backend/app/services/video_transport/`（含 remake/）与 `video_transport_tasks.py`、`tests/test_video_transport/`（456 项）——纯逻辑模块逐行保真平移，仅换 import 面（AI 收口→providers、paths→新 paths、config→宿主 Settings、celery→scheduler）
- 宿主惯例强约束：services 收调用方传入的 AsyncSession；http 模块导出 router+MANIFEST_ENTRIES 并接线 ALL_ROUTERS（tests/test_manifest.py 防漂移）；模型 Mapped/mapped_column 一表一文件注册 models/__init__；中文详注；`.env.example` 与 Settings 同步（tests/test_env_example.py 防漂移）；pytest asyncio_mode=auto、slow 标记
- 关键语义逐字保真：心跳泵 300s/僵死 15min/恢复扫描先 touch；阶段预算表；HMAC token 目录；零重叠/吸附栅格/互斥/收束句末位等全部 validate 不变量；revision 不可变基底幂等
- commit 遵宿主 Conventional Commits 中文体；禁 git add -A
- 每 track commit 前跑宿主全量 `pytest -m "not slow"` 确认零回归

## 并行执行结构

- **Track M1（providers+models）**：providers.py 薄 AI 层（§3 五能力+重试）+ video_job 模型 + alembic 迁移 + paths.py + Settings/.env.example 扩展。测试：providers mock 契约、模型、paths HMAC。
- **Track M2（scheduler+worker）**：VideoScheduler（阶段自链/原子占用/心跳泵/恢复扫描/预算）+ worker.py 入口 + systemd unit 文件。测试：调度语义全套（占用防双发/心跳周期/僵死恢复/按阶段续跑/预算透传），mock handler。
- M1 ∥ M2 文件零交集，各自 worktree 分支（feat/video-mig-providers、feat/video-mig-scheduler），合流到 feat/video-pipeline-migration。
- **Track M3（pipeline 平移，合流后）**：pipeline/ 全模块 + stages.py handler 层 + 456 项测试平移适配。这是最大任务，可按「transport 链 / remake 链 / revision」拆 3 个连续子任务各自审查。
- **Track M4（集成+部署+验收）**：video_rest.py + manifest + uploads video 路由 + requirements/DEPLOY/systemd/README + 最小 CI + 集成测试；然后 e2e（EMDR remake + revise）+ 切换步骤（nbdpsy-skill 文档、运营工具 410）。

## 接口契约（跨 track 绑定）

- providers（M1 产，M3 消费）：`async asr_transcribe(audio_url:str)->list[dict{start,end,text}]` / `async mt_translate(texts:list[str],*,term_sheet)->list[str]`（内部构造 translation_options 直传） / `async llm_chat(messages:list[dict],*,temperature=0.3)->str` / `async vl_describe(image_path:str,prompt:str)->str` / `async tts_synthesize(text:str,*,voice:str,out_path:str)->float`（返回时长秒）。全部失败抛原异常，重试由调用方按源语义。
- scheduler（M2 产，M3/M4 消费）：`STAGE_ORDER/REMAKE_STAGE_ORDER` 与源一致；handler 注册表 `STAGE_HANDLERS: dict[str, async (job, session, ctx)->stats]`；`ctx={"deadline": monotonic秒}`；`enqueue(job_id)` 即置 queued（worker 轮询取）；`create_job/create_revision_job/fail_job/finish_job/update_stage/first_incomplete_stage` 语义与源 job_store 一致（改 async + AsyncSession）。
- paths（M1 产）：`raw_dir/tts_dir/out_dir(job_id)->Path`（根 `DATA_DIR/uploads/video/{id}-{hmac16}/`）、`to_public_url(path)->"/uploads/video/..."`。

## 验收（M4 内）

设计 §5 全部四步；e2e QC 清单沿用 job16 关口（transcript 补漏数 / storyboard 三不变量 / 成片抽帧+声道+帧率）。

## Self-Review

- 设计 §1-§5 全部映射到 M1-M4；契约签名全计划唯一；并行安全（M1/M2 零交集，M3 依赖契约在本文件冻结）。
