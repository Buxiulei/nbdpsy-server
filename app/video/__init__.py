"""视频管线（transport 搬运 / remake 分镜级再制作 / revision 成片修订）迁入宿主的原生能力包。

方案 C：独立 asyncio worker，与 API 进程（8848）隔离——API 重启不杀长任务。

- ``providers``：薄 AI 直连层（ASR/qwen-mt/LLM/VL/豆包 TTS，五契约签名见迁移 plan）。
- ``paths``：HMAC token 产物目录（根 ``DATA_DIR/uploads/video/``）。
- ``scheduler``：VideoScheduler（DB 状态机 + 原子占用 + 阶段自链 + 心跳泵 + 僵死恢复）
  与 async 化的 job_store 函数族（收 AsyncSession）。
- ``worker``：进程入口（``python -m app.video.worker``）。

阶段 handler 注册表 ``scheduler.STAGE_HANDLERS`` 由 Track M3（pipeline 平移）填充；
模型在 ``app/models/video_job.py``。总纲见 docs/plans/2026-07-22-video-pipeline-migration.md。
"""
