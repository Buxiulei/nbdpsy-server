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

from app.browser.note_components import open_update_page
from app.browser.sync_human_actions import SyncHumanActions

# 永久取图域(实测唯一可用的无签名域)
_PERMANENT_HOST = "https://sns-img-qc.xhscdn.com"
# 笔记图的路径段白名单:实测见过这三种;新段出现时**如实记下并放行**(宁可多存一条
# 待核,也不要静默丢掉一张图),但会打告警日志提醒来核。
_KNOWN_SEGMENTS = ("notes_pre_post", "spectrum", "notes_uhdr")
# file_id 形态:平台的长串标识(实测 1040g... 40 位上下的字母数字)
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
# 签名 hash(32 位 hex):出现在倒数第二段时说明这条 URL **没有**路径段
_SIGN_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
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
    if not parts:
        return None
    # 末段是 file_id(可能带 !变体 后缀)。带签名形态 /{ts}/{sign}/{seg}/{file_id} 与
    # 无签名形态 /{seg}/{file_id} 取末两段结果相同 —— 归一化能同时吃下两者的原因。
    file_id = parts[-1].split("!")[0]
    if not _FILE_ID_RE.match(file_id):
        return None
    segment = parts[-2] if len(parts) >= 2 else None
    # **老笔记没有路径段**(2026-08-05 实测:2025 年的笔记形如
    # ``sns-na-i2.xhscdn.com/{file_id}?sign=..``,永久形态就是 ``sns-img-qc/{file_id}``,
    # 硬套路径段反而 404)。此时倒数第二段其实是签发时间戳或签名 hash,不是路径段 ——
    # 按形态识破它们:纯数字 = 时间戳,32 位 hex = 签名。首轮 24 篇图文笔记被判成"空清单"
    # 就是漏了这一支(要求两段才处理)。
    if segment and (segment.isdigit() or _SIGN_HASH_RE.match(segment)):
        segment = None
    if segment and segment not in _KNOWN_SEGMENTS:
        logger.warning(
            f"[note_media] 未见过的路径段 {segment!r}(file_id={file_id});"
            "照常收录,请人工核一次归一化是否仍成立"
        )
    return {
        "file_id": file_id,
        "segment": segment or "",
        "url": (
            f"{_PERMANENT_HOST}/{segment}/{file_id}" if segment
            else f"{_PERMANENT_HOST}/{file_id}"
        ),
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


# 编辑页图片区:**容器精确 + 一次拿全**(2026-08-05 取证定案,见下方 fetch_note_media)
_EDITOR_IMG_JS = r"""
() => [...document.querySelectorAll('.img-upload-area .img-container img')]
    .map(i => i.currentSrc || i.src)
    .filter(s => s && s.includes('xhscdn.com'))
"""


def fetch_note_media(page, account_id: int, notes: List[Dict[str, str]]) -> Dict[str, dict]:
    """同一会话内逐篇打开**编辑页**,只读取媒体清单;返回 ``{note_id: {...}}``。

    **为什么用编辑页而不是详情页**(2026-08-05 两轮取证的结论,改前必读):

    - 详情页那条路**两个硬伤**:①右侧推荐流也是 ``img.xhscdn``,一篇 10 图的笔记页面上
      有 92 张图、其中 43 张是别人笔记的封面(实测账号 9 首轮抓出 444 项垃圾就是这么来的);
      ②笔记图在 swiper 轮播里**懒加载**,只渲染可见的 2-3 张,不滑动拿不全;
    - 编辑页 ``.img-upload-area .img-container img`` **容器精确且一次渲染全部**
      (12 图夹具为证),URL 形如 ``sns-na-i4.xhscdn.com/{段}/{file_id}?sign=..&t=..``,
      归一化后与详情页同一张原图(实测两者取回体积逐字节相同 375254B)。

    ``notes`` 每项只需 ``{note_id}``(编辑页深链不要 xsec_token)。单篇失败(笔记被删)
    只记该篇 ``error``,**不影响其余篇** —— 与 ``note_purpose.fetch_note_contents`` 同纪律。

    纯只读:进页面 + 读 DOM,不点任何按钮、不提交、不改任何状态。
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
            open_update_page(page, account_id, note_id)
            human.wait(*_MEDIA_WAIT_S, context="编辑页图片区渲染")
            raw = page.evaluate(_EDITOR_IMG_JS) or []
            media = collect_media(raw)
            results[note_id] = {"media": media, "raw_count": len(raw)}
            logger.info(
                f"[note_media] 账号{account_id} {note_id}: 收 {len(media)} 项"
                f"(图片区 img {len(raw)} 个)"
            )
        except Exception as exc:  # noqa: BLE001 — 单篇失败不拖垮整批
            logger.warning(f"[note_media] 账号{account_id} {note_id} 抓取失败: {exc}")
            results[note_id] = {"error": f"media_fetch_failed: {exc}"}
    return results
