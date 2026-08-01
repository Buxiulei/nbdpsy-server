"""手工笔记正文只读抓取(纯同步,吃已登录 page):深链进编辑页,只看不碰。

设计 docs/design/2026-08-01-note-purpose-design.md 第 3.3 路径 B / 3.4 节。

台账里手工发布的那批笔记(``sync_status='orphan'``)**本地一个字正文都没有** —— 台账
数据来自创作中心列表接口,只给元数据。这里把正文读回来,给 LLM 分类核心目的用。

**只读纪律(本模块最硬的约束)**:

- 全程只 ``open_update_page`` + 读标题 + 读正文,**绝不点发布、绝不改任何内容**;
  编辑页未提交的状态不落库,进出即恢复原状。
- 复用 ``app.browser.note_components`` 的三个只读函数,不另抄一套选择器 —— 那三个是
  三组件那条链路真号验证过的,选择器变了两处一起改比两份各自漂移强。
- 走编辑页而不是笔记详情页(设计 3.4):编辑页在 creator 域,实测**从不触发验证墙**;
  详情页要经 ``xiaohongshu.com/user/profile/``,已有两个账号栽在那条路上。

**节流在服务层**(``app.services.note_purpose`` 按 ``NOTE_PURPOSE_BACKFILL_LIMIT`` 挑
几篇再进来),本模块只负责在**同一个会话内**按拟人节奏逐篇读完:一次会话读几篇,比
一篇一次会话安全得多 —— 被风控盯上的是会话起停频次,不是页面跳转次数。
"""

from typing import Dict, List

from loguru import logger

from app.browser.note_components import (
    NoteComponentsError,
    open_update_page,
    read_body_text,
    read_note_title,
)
from app.browser.sync_human_actions import SyncHumanActions


def fetch_note_contents(
    page, account_id: int, note_ids: List[str]
) -> Dict[str, dict]:
    """在同一个已登录会话里逐篇只读取回正文;返回 ``{note_id: {...}}``。

    Args:
        page: 已建好登录态的同步 Playwright Page(``SyncClient.start`` 之后)。
        account_id: 账号 id(日志用)。
        note_ids: 要抓的平台笔记 id 列表(顺序即抓取顺序,调用方已按策略排好序并截断)。

    Returns:
        每篇一项:成功为 ``{"title": str, "content_text": str}``(正文为空的纯图笔记
        给空串,**不是缺项** —— 空串也是"看过了"的事实);失败为 ``{"error": reason}``,
        **单篇失败不影响其余篇**(一篇进不去多半是这篇被删了,不该拖垮整批)。
    """
    human = SyncHumanActions(page)
    results: Dict[str, dict] = {}
    for index, note_id in enumerate(note_ids):
        if index:
            # 两篇之间的停顿:同一会话内连续跳编辑页也要有人味,别做成机器节律
            human.wait(2.0, 5.0, context="读完一篇,歇一下再看下一篇")
        try:
            open_update_page(page, account_id, note_id)
            human.wait(0.8, 1.8, context="编辑页浏览")
            results[note_id] = {
                # 平台**当前**标题(比台账 title 权威:运营可能在 App 里改过)
                "title": read_note_title(page),
                "content_text": read_body_text(page),
            }
            logger.info(
                f"[note_purpose] 账号{account_id} note_id={note_id} 正文已读回"
                f"({len(results[note_id]['content_text'])} 字)"
            )
        except NoteComponentsError as exc:
            logger.warning(
                f"[note_purpose] 账号{account_id} note_id={note_id} 编辑页进不去: {exc.reason}"
            )
            results[note_id] = {"error": exc.reason}
        except Exception as exc:  # noqa: BLE001 — 单篇异常不拖垮整批
            logger.warning(f"[note_purpose] 账号{account_id} note_id={note_id} 读取异常: {exc}")
            results[note_id] = {"error": f"content_read_exception: {exc}"}
    return results
