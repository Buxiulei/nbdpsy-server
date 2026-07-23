"""一致性生图能力(自薯营家 2026-07-23 停机迁移,契约见 NBDpsy 仓协同记录)。

- ``openai_image``:gpt-image-2 锚点法 provider(P1 锚点跨篇一致性)
- ``reraster`` + ``postprocess``:去水印工作流(Chromium 截图重栅格化扰动像素级
  水印 + 剥 C2PA/EXIF 元数据,失败退回 PIL 像素重存兜底,绝不阻断出图)
- ``playwright_guard``:一次性截图链路的有界护栏(自主仓 #379 验证过的公共件)
"""
