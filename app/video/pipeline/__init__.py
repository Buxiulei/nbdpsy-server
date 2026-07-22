"""视频管线阶段实现包（transport 搬运链 · 逐行平移自小红书运营工具 video_transport）。

各模块是纯逻辑阶段体，只换 import 面（AI 收口→app.video.providers、paths→app.video.paths、
术语表→app.models.psych_glossary、config→宿主 settings）：

- ``downloader``：yt-dlp 下载 + 字幕抓取（人工优先，缺则自动字幕）。
- ``transcript``：转录来源链（人工字幕 > 自动字幕 > ASR）+ 字幕空窗 ASR 补漏（remake 才开）。
- ``resegment``：碎片字幕 → 语义整句（LLM 重断句，校验不过降级保原）。
- ``glossary``：psych_glossary 术语表匹配 + 自动回写（AsyncSession）。
- ``translator``：信雅达三步（抽术语 → 术语对齐 → 分批翻译 + 时长审校）。
- ``dubber``：逐句 TTS + 全片统一语速同步（二分决策）+ pydub 拼轨。
- ``muxer``：ffmpeg 音轨替换 / ASS 字幕烧录 / logo 水印 / 音频分层混音。
- ``deliver``：产物组装（final.mp4 + 双 SRT + 双语 md + meta.json）。

阶段 handler（``async (job, session, ctx) -> stats``）在 ``app/video/stages.py``，注册进
``scheduler.STAGE_HANDLERS``；非阻塞红线（同步段 to_thread / 子进程 create_subprocess_exec）见
``scheduler.py`` STAGE_HANDLERS 契约注释块。
"""
