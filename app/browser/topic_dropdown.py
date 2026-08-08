"""话题下拉浮层的**定位判据**(纯逻辑,不碰浏览器)。

## 为什么单独拆一个模块

原实现把"枚举浮层 + 挑一个 + 匹配话题"整坨写在 ``page.evaluate`` 的 JS 里,挑法是
``candidates.sort((a, b) => a.area - b.area)`` —— **取面积最小的那一层**。这条启发式没有
任何正向判据,它赌的是"页面上含该话题文案、又小又浮的东西只可能是下拉"。

2026-08-07 生产取证证伪了这个赌注:视频笔记发布 job273-280 连续 8 条、每条 6/6 话题全败,
回执里 ``layer_class`` 是 ``base-info``、候选是「昵称 / 关注 / 展开 / 编辑于 刚刚·公开可见」——
抓到的是**右侧手机预览面板里的作者信息区**。它之所以能进候选,是因为预览面板会镜像正文
内容(正文里刚打进去的 ``#话题`` 也一并镜像了),于是"含话题文案"成立;它又比真下拉小,
于是"面积最小"把它排到了第一。图文页同样不可靠,只是碰巧多数时候真下拉更小(94% 侥幸)。

## 现在的判据

**主判据是几何锚定**:下拉是挂在正文框光标上的,必然落在正文栏那一列;而预览面板在页面
右侧,与正文栏**水平不相交**。两种页型的生产截图都实证了这一点(视频页正文栏右缘 ~1096 /
预览面板左缘 ~1145;图文页 ~1580 / ~1645)。所以"浮层水平中心落在正文框的 x 区间内"既能
放行真下拉,又能一刀切掉预览面板,且不依赖任何 class 名。

**结构判据**(多数子项长得像话题选项:``#`` 开头或带浏览量统计文案)作两用:拿不到正文框
几何时兜底放行,以及给失败分类定性。

**class 黑名单只是兜底**,平台改个 class 就失效,绝不当主判据。

## fail-loud

"抓错容器"和"词不存在"是两种完全不同的处置,旧代码都糊成 ``no_exact_match``:

- ``topic_dropdown_not_shown``:页面上压根没有候选浮层(``candidates`` 为空)—— 追加场景
  最常见的失败,``#`` 紧贴前一个话题实体粘连、编辑器没弹联想浮层(RCA 2026-08-09)。这是
  **定位/输入问题**,调用方该反馈我们修,**绝不能换词**;
- ``topic_dropdown_not_found``:有浮层,但没有一个通过判据 —— 我们**没找到真下拉**(抓到的
  多半是右侧预览面板的镜像容器),同属"别拿'这词平台没有'糊弄自己",也**不该换词**;
- ``no_exact_match``:真下拉在(``candidates`` 非空),里面确实没有这个词 —— **这一个才是
  真·平台没这词**,调用方据此换词才有意义。
"""

import re
from typing import Any, Dict, List, Optional

# 一层浮层最多回传多少个子项 / 一页最多回传多少层:回执要塞进 job 台账,不能无界
MAX_LAYERS = 24
MAX_ITEMS_PER_LAYER = 60
# 取证里带回多少候选文案 / 多少个被拒层的 class
MAX_CANDIDATES = 10
MAX_REJECTED_CLASSES = 5

# 单条选项文案长于它就不是话题行(和旧实现同阈值,别动)
MAX_ITEM_TEXT = 50

# 话题选项的统计文案:「1.2万次浏览」「3563人参与讨论」之类
_TOPIC_STAT_RE = re.compile(r"\d[\d.]*\s*[万亿]?\s*(?:次|人)?\s*(?:浏览|参与|讨论|阅读|观看)")

# 兜底黑名单:实证抓错过的镜像容器(右侧预览面板的作者信息区 / 昵称条)。
# **只是兜底** —— 主判据是几何锚定,平台改 class 不影响正确性。
MIRROR_CLASS_HINTS = ("base-info", "top-nickname", "nickname", "preview")

# 浮层采集 JS:**只枚举,不判断**。判断全在 Python 里,才测得动。
# 过滤条件沿用旧实现(absolute/fixed、可见、不在 contenteditable 里、尺寸区间),
# 唯一的变化是不再在 JS 里挑一个 —— 候选整体交给 select_topic_option。
COLLECT_LAYERS_JS = r"""
(tagName) => {
    const layers = [];
    for (const el of document.querySelectorAll('*')) {
        const style = window.getComputedStyle(el);
        const pos = style.position;
        if (pos !== 'absolute' && pos !== 'fixed') continue;
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if (el.closest('[contenteditable]')) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width > 800 || rect.height > 600) continue;
        if (rect.width < 10 || rect.height < 10) continue;
        const items = [];
        for (const item of el.querySelectorAll('div, li, a, span, p')) {
            const t = (item.innerText || '').trim().replace(/\s+/g, ' ');
            if (!t || t.length > 60) continue;   // 60 是取证的余量,匹配判据仍是 ≤50
            const r = item.getBoundingClientRect();
            // 可点的行:太扁太窄的是装饰,太高的是包裹容器(旧实现 okRect 同阈值)
            if (r.width <= 5 || r.height <= 5 || r.height >= 80) continue;
            items.push({text: t, x: r.x + r.width / 2, y: r.y + r.height / 2});
            if (items.length >= MAX_ITEMS) break;
        }
        layers.push({
            cls: String(el.className || '').slice(0, 80),
            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            has_tag: (el.innerText || '').includes(tagName),
            items: items,
        });
    }
    // 超量时先保含话题文案的、再按面积小的:截断不能把真下拉截掉
    layers.sort((a, b) => (b.has_tag - a.has_tag)
        || (a.rect.width * a.rect.height - b.rect.width * b.rect.height));
    return {layers: layers.slice(0, MAX_LAYERS)};
}
""".replace("MAX_ITEMS", str(MAX_ITEMS_PER_LAYER)).replace("MAX_LAYERS", str(MAX_LAYERS))


def looks_like_topic_option(text: str) -> bool:
    """这条文案像不像话题下拉里的一行:``#`` 开头,或带浏览量/参与人数统计文案。"""
    t = (text or "").strip()
    if not t or len(t) > MAX_ITEM_TEXT:
        return False
    return t.startswith("#") or bool(_TOPIC_STAT_RE.search(t))


def topic_option_count(layer: Dict[str, Any]) -> int:
    """这一层里有多少子项长得像话题选项(结构判据的强度)。"""
    return sum(1 for it in (layer.get("items") or []) if looks_like_topic_option(it.get("text", "")))


def is_dropdown_like(layer: Dict[str, Any]) -> bool:
    """结构判据:多数下拉至少有两行话题选项(单行结果也常带统计文案)。"""
    return topic_option_count(layer) >= 2


def is_mirror_layer(layer: Dict[str, Any]) -> bool:
    """兜底黑名单:实证抓错过的镜像容器。**不是主判据**。"""
    cls = str(layer.get("cls") or "").lower()
    return any(hint in cls for hint in MIRROR_CLASS_HINTS)


def is_anchored_to_editor(layer: Dict[str, Any], editor_rect: Optional[Dict[str, Any]]) -> bool:
    """主判据:浮层水平中心落在正文框那一列里。

    下拉挂在正文框的光标上,必然与正文栏同列;右侧手机预览面板与正文栏水平不相交
    (视频页 / 图文页生产截图双实证)。拿不到正文框几何时返回 False,由调用方退回结构判据。
    """
    if not editor_rect:
        return False
    ex = editor_rect.get("x")
    ew = editor_rect.get("width")
    if ex is None or not ew:
        return False
    rect = layer.get("rect") or {}
    lx, lw = rect.get("x"), rect.get("width")
    if lx is None or lw is None:
        return False
    center = lx + lw / 2.0
    return ex <= center <= ex + ew


def _match_in_layer(layer: Dict[str, Any], tag_name: str, exact: bool) -> Optional[Dict[str, Any]]:
    """在一层里找话题行。匹配规则原样沿用主仓 RCA 2026-05-18 的两轮判据:

    第一轮精确相等;第二轮以完整 tagName 开头且**剩余不是汉字**(剩余应是统计文案),
    禁止残缺前缀误配(「心理」不能配上「心理咨询师」)。
    """
    for item in layer.get("items") or []:
        text = (item.get("text") or "").strip()
        if not text or len(text) > MAX_ITEM_TEXT:
            continue
        clean = text[1:].strip() if text.startswith("#") else text
        if exact:
            hit = clean == tag_name
        else:
            rest = clean[len(tag_name):] if clean.startswith(tag_name) else ""
            hit = bool(rest) and not ("一" <= rest[0] <= "龥")
        if hit:
            return {"success": True, "x": item.get("x"), "y": item.get("y"), "matched": text}
    return None


def _forensics(layer: Optional[Dict[str, Any]], layers: List[Dict[str, Any]],
               rejected: List[Dict[str, Any]]) -> Dict[str, Any]:
    """失败回执带的当场证据:实际枚举到的候选文案 / 条数 / 容器 class / 层数 / 被拒层。"""
    seen: List[str] = []
    for item in (layer or {}).get("items") or []:
        t = (item.get("text") or "").strip()
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= MAX_CANDIDATES:
            break
    return {
        "candidates": seen,
        "item_count": len((layer or {}).get("items") or []),
        "layer_class": str((layer or {}).get("cls") or "")[:80],
        "layers_seen": len(layers),
        "rejected_classes": [str(r.get("cls") or "")[:40] for r in rejected[:MAX_REJECTED_CLASSES]],
    }


def select_topic_option(payload: Optional[Dict[str, Any]], tag_name: str,
                        editor_rect: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从采集到的浮层里定位话题选项坐标,定不到就**明确说定不到**。

    成功返回 ``{"success": True, "x", "y", "matched"}``;失败返回 ``success=False`` +
    三选一的 reason(见模块 docstring)+ 取证字段。
    """
    layers = list((payload or {}).get("layers") or [])
    accepted, rejected = [], []
    for layer in layers:
        if is_mirror_layer(layer):
            rejected.append(layer)
            continue
        ok = is_anchored_to_editor(layer, editor_rect) if editor_rect else is_dropdown_like(layer)
        (accepted if ok else rejected).append(layer)

    # 像下拉的排前面,同强度取面积小的(嵌套容器里内层更贴近真实行)
    accepted.sort(key=lambda ly: (
        -topic_option_count(ly),
        (ly.get("rect") or {}).get("width", 0) * (ly.get("rect") or {}).get("height", 0),
    ))

    # 精确相等在**所有**候选层里优先于前缀匹配 —— 不再"挑一层然后一条路走到黑"
    for exact in (True, False):
        for layer in accepted:
            hit = _match_in_layer(layer, tag_name, exact)
            if hit:
                return hit

    if not layers:
        # 浮层根本没弹(candidates 空):追加场景 # 粘连前一个话题实体的典型征状,
        # 是定位/输入问题不是"词不存在",调用方绝不能据此换词(RCA 2026-08-09)。
        reason, focus = "topic_dropdown_not_shown", None
    elif not accepted:
        # 有浮层但没一个是下拉:抓错容器,和"词不存在"必须分开记
        reason, focus = "topic_dropdown_not_found", (rejected[0] if rejected else None)
    else:
        reason, focus = "no_exact_match", accepted[0]

    out: Dict[str, Any] = {"success": False, "reason": reason}
    out.update(_forensics(focus, layers, rejected))
    return out
