"""一致性生图能力(自薯营家 2026-07-23 停机迁移,契约见 NBDpsy 仓协同记录)。

- ``openai_image``:gpt-image-2 锚点法 provider(P1 锚点跨篇一致性)
- ``postprocess``:去水印工作流(非整数缩小到 0.855 + 存 PNG 无损:重新插值打乱扩散
  模型像素网格生成指纹,PNG 重编码天然丢弃 C2PA/EXIF/XMP 元数据;失败 fail-closed 返
  None,绝不拿未处理图冒充交付)
"""
