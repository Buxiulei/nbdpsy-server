"""笔记媒体清单:详情页只读抓媒体 URL → **归一化成永久形态**存台账,不落文件。

2026-08-05 实测定下的平台事实(每条都有 HTTP 实证,别凭印象改):

- 平台在页面上给的是**带签名的展示图** URL::

      https://sns-webpic-qc.xhscdn.com/{签发时间戳}/{签名hash}/{路径段}/{file_id}!{变体}

  它**会过期**:2025-11 存的 403、2026-07-18 存的(18 天前)也 403,当场签发的才 200。
  所以**照原样存链接毫无意义**,过一阵全是死链;
- 但 ``file_id`` 是永久的。剥掉时间戳与签名、把域换成 ``sns-img-qc``::

      https://sns-img-qc.xhscdn.com/{路径段}/{file_id}

  9 个月前的 file_id 今天照样 200,**且是原图**(实测 424KB vs 签名展示图 56KB,
  三种路径段 notes_pre_post / spectrum / notes_uhdr 全部成立);
- 两个坑:①路径段必须跟原 URL 一致,混用 404(spectrum 的 id 拿去 notes_pre_post 段
  取不到);②域必须换 ``sns-img-qc``,``sns-webpic-qc`` 去掉签名同样 403。

于是台账只存**归一化后的永久 URL + file_id**(几百字节/篇),要原图时按需下载即可 ——
这比把 1500 张图落盘省几十 GB,拿到的还更清晰。

头像域 ``sns-avatar-qc`` 一律排除:那是作者头像,不是笔记内容。
"""

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from loguru import logger

from app.browser.sync_human_actions import SyncHumanActions

# 永久取图域(实测唯一可用的无签名域)
_PERMANENT_HOST = "https://sns-img-qc.xhscdn.com"
# 笔记图的路径段白名单:实测见过这三种;新段出现时**如实记下并放行**(宁可多存一条
# 待核,也不要静默丢掉一张图),但会打告警日志提醒来核。
_KNOWN_SEGMENTS = ("notes_pre_post", "spectrum", "notes_uhdr")
# file_id 形态:平台的长串标识(实测 1040g... 40 位上下的字母数字)
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
# 头像/表情等非笔记内容的域或段
_EXCLUDE_HOST_MARKS = ("sns-avatar", "picasso-static", "fe-video-qc")

_MEDIA_WAIT_S = (2.0, 3.5)


def normalize_media_url(url: str) -> Optional[Dict[str, str]]:
    """把平台给的带签名图片 URL 归一化成永久形态。

    Returns:
        ``{"file_id", "segment", "url"}``;不是笔记图(头像/静态资源/形态不认)→ None。
        **返回 None 是"这不是笔记图"的判断,不是"抓取失败"** —— 调用方据此静默跳过。
    """
    if not url or "xhscdn.com" not in url:
        return None
    if any(mark in url for mark in _EXCLUDE_HOST_MARKS):
        return None
    path = urlsplit(url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    # 末段是 file_id(可能带 !变体 后缀),倒数第二段是路径段;
    # 带签名的形态是 /{ts}/{sign}/{seg}/{file_id},无签名的是 /{seg}/{file_id},
    # 两者取末两段的结果相同 —— 这正是归一化能同时吃下两种形态的原因。
    file_id = parts[-1].split("!")[0]
    segment = parts[-2]
    if not _FILE_ID_RE.match(file_id):
        return None
    if segment not in _KNOWN_SEGMENTS:
        logger.warning(
            f"[note_media] 未见过的路径段 {segment!r}(file_id={file_id});"
            "照常收录,请人工核一次归一化是否仍成立"
        )
    return {
        "file_id": file_id,
        "segment": segment,
        "url": f"{_PERMANENT_HOST}/{segment}/{file_id}",
    }


def collect_media(raw_urls: List[str]) -> List[Dict[str, Any]]:
    """一篇笔记的原始 URL 列表 → 去重、保序的媒体清单(带 1-based 序号)。

    **保序**:页面 DOM 顺序即图序,这是调用方"第几张图"的唯一依据;去重按 file_id
    (同一张图页面上常有多个尺寸变体的 img 节点)。
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in raw_urls or []:
        item = normalize_media_url(raw)
        if item is None or item["file_id"] in seen:
            continue
        seen.add(item["file_id"])
        out.append({
            "ordinal": len(out) + 1,
            "kind": "image",
            "file_id": item["file_id"],
            "segment": item["segment"],
            "url": item["url"],
        })
    return out


# 详情页只读取图:取笔记正文区的 img(排除头像等),按 DOM 顺序
_COLLECT_JS = r"""
() => [...document.querySelectorAll('img')]
    .map(i => i.currentSrc || i.src)
    .filter(s => s && s.includes('xhscdn.com'))
"""


def fetch_note_media(page, account_id: int, notes: List[Dict[str, str]]) -> Dict[str, dict]:
    """同一会话内逐篇打开详情页,只读取媒体清单;返回 ``{note_id: {...}}``。

    ``notes`` 每项 ``{note_id, xsec_token, xsec_source}``(深链要 token,台账里有)。
    单篇失败(笔记被删/token 过期)只记该篇的 ``error``,**不影响其余篇** —— 与
    ``note_purpose.fetch_note_contents`` 同一纪律。

    纯只读:导航 + 读 DOM,不点任何按钮、不改任何状态。
    """
    human = SyncHumanActions(page)
    results: Dict[str, dict] = {}
    for index, note in enumerate(notes):
        note_id = str(note.get("note_id") or "")
        if not note_id:
            continue
        if index:
            human.wait(2.0, 5.0, context="看完一篇,歇一下再看下一篇")
        try:
            token = note.get("xsec_token") or ""
            source = note.get("xsec_source") or "pc_creatormng"
            url = (
                f"https://www.xiaohongshu.com/explore/{note_id}"
                f"?xsec_token={token}&xsec_source={source}"
            )
            human.navigate(url)
            human.wait(*_MEDIA_WAIT_S, context="详情页渲染")
            raw = page.evaluate(_COLLECT_JS) or []
            media = collect_media(raw)
            results[note_id] = {"media": media, "raw_count": len(raw)}
            logger.info(
                f"[note_media] 账号{account_id} {note_id}: 收 {len(media)} 项"
                f"(页面 xhscdn img {len(raw)} 个)"
            )
        except Exception as exc:  # noqa: BLE001 — 单篇失败不拖垮整批
            logger.warning(f"[note_media] 账号{account_id} {note_id} 抓取失败: {exc}")
            results[note_id] = {"error": f"media_fetch_failed: {exc}"}
    return results
