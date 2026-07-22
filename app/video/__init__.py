"""视频搬运/再制作后台能力包（方案 C：独立 asyncio worker）。

- ``scheduler``：VideoScheduler（DB 状态机 + 原子占用 + 阶段自链 + 心跳泵 + 僵死恢复）
  与 async 化的 job_store 函数族（收 AsyncSession）。
- ``worker``：进程入口（``python -m app.video.worker``），与 API 进程（8848）隔离——
  API 重启不杀长任务。

阶段 handler 注册表 ``scheduler.STAGE_HANDLERS`` 由 Track M3（pipeline 平移）填充；
本包只承载调度骨架，不含具体 pipeline 逻辑。
"""
