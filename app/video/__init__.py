"""视频管线（transport 搬运 / remake 分镜级再制作 / revision 成片修订）迁入宿主的原生能力包。

M1 已落：providers（薄 AI 直连层）、paths（HMAC token 产物目录）、video_job 模型。
M2 落 scheduler/worker，M3 落 pipeline 平移，M4 落 http/部署/验收（见
docs/plans/2026-07-22-video-pipeline-migration.md）。
"""
