#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""幂等修补 Playwright(1.60.0)Firefox 驱动的 pageError 崩溃 bug。

根因(两层):XHS 页面在某些交互(点发布)时抛出未捕获错误,其 ``location`` 缺字段。
驱动的 PageError 处理器把它塞进协议事件 ``location:{url,line,column}`` 时:
  第一层:直接读 ``pageError.location.url`` —— location 为空 → TypeError 崩 node;
  第二层:即便 location 存在,``location.url`` 可能是 undefined,而**协议校验器**要求
          url 必须是 string(``ValidationError: location.url: expected string, got undefined``)
          → 同样崩 node,把浏览器/发布流程一起带走。
上游 coreBundle.js 未对这些空值设防。

本脚本把三字段改成"可选链 + 兜底默认值"(url→"" / line,column→0),既避免读空崩,
又让协议校验过关。幂等:已是最终态则跳过;能识别原始态与半修补(仅 ``?.``)态并归一。
venv 重建后由 systemd ExecStartPre 重新执行,保证生产始终带补丁。异常只告警不阻断启动。
"""
import sys
from pathlib import Path

DRIVER = Path(__file__).resolve().parent.parent / (
    ".venv/lib/python3.12/site-packages/playwright/driver/package/lib/coreBundle.js"
)

# 最终正确形态(可选链 + 兜底默认值)
FINAL = """            location: {
              url: pageError.location?.url ?? "",
              line: pageError.location?.lineNumber ?? 0,
              column: pageError.location?.columnNumber ?? 0
            }"""

# 需要被替换成 FINAL 的历史形态:原始态、半修补(仅 ?.)态
STALE_FORMS = [
    # 原始上游形态
    """            location: {
              url: pageError.location.url,
              line: pageError.location.lineNumber,
              column: pageError.location.columnNumber
            }""",
    # 半修补态(仅加了可选链,未加兜底 → 撞协议校验)
    """            location: {
              url: pageError.location?.url,
              line: pageError.location?.lineNumber,
              column: pageError.location?.columnNumber
            }""",
]


def main() -> int:
    if not DRIVER.exists():
        print(f"[patch_playwright_driver] 驱动文件不存在,跳过: {DRIVER}")
        return 0
    text = DRIVER.read_text(encoding="utf-8")
    total = 0
    for stale in STALE_FORMS:
        c = text.count(stale)
        if c:
            text = text.replace(stale, FINAL)
            total += c
    if total == 0:
        print("[patch_playwright_driver] 已是最终修补态或无匹配,跳过")
        return 0
    DRIVER.write_text(text, encoding="utf-8")
    print(f"[patch_playwright_driver] 已修补 {total} 处 pageError.location 空值崩溃")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 补丁失败绝不阻断服务启动
        print(f"[patch_playwright_driver] 修补异常(忽略,不阻断启动): {e}")
        sys.exit(0)
