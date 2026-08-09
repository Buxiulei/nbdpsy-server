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
预览面板左缘 ~1145;图文页 ~1580 / ~1645)。所以"浮层与正文框的 x 区间相交"既能放行真
下拉,又能一刀切掉预览面板,且不依赖任何 class 名。判据一度收得更紧("水平**中心**落在
区间内"),把跟着光标右移、探出栏缘的真下拉也拒了 —— 详见 ``is_anchored_to_editor``。

**结构判据**(多数子项长得像话题选项:``#`` 开头或带浏览量统计文案)作两用:拿不到正文框
几何时兜底放行,以及给失败分类定性。

**class 黑名单只是兜底**,平台改个 class 就失效,绝不当主判据。

## fail-loud

"抓错容器"和"词不存在"是两种完全不同的处置,旧代码都糊成 ``no_exact_match``:

- ``topic_dropdown_not_shown``:**话题联想浮层没弹出来**。两种形态都算:页面上压根没有
  候选浮层(``layers`` 为空),以及有通过判据的浮层容器但它**一个选项都没有**(``items``
  全空 —— 平台的下拉外壳先挂上、内容异步填,内容没到就是这副样子;真号回执样本
  ``layer_class="suffix" item_count=0 layers_seen=14``)。这是**时序/输入问题**,调用方该
  反馈我们修,**绝不能换词**;
- ``topic_dropdown_not_found``:有浮层,但没有一个通过判据 —— 我们**没找到真下拉**(抓到的
  多半是右侧预览面板的镜像容器),同属"别拿'这词平台没有'糊弄自己",也**不该换词**;
- ``no_exact_match``:**浮层弹了、里面有选项、真没这个词** —— 只有这一个是真·平台没这词,
  调用方据此换词才有意义。空壳浮层曾被算进这一类(取 ``accepted[0]`` 时不看它有没有选项),
  于是"浮层没弹"被报成"平台没这词"、调用方白烧会话换词(RCA 2026-08-09 真号复验)。
"""

import re
from typing import Any, Dict, List, Optional

# 一层浮层最多回传多少个子项 / 一页最多回传多少层:回执要塞进 job 台账,不能无界
MAX_LAYERS = 24
MAX_ITEMS_PER_LAYER = 60
# 取证里带回多少候选文案 / 多少个被拒层的 class / 多少个"被拒但带选项"的层
MAX_CANDIDATES = 10
MAX_REJECTED_CLASSES = 5
MAX_REJECTED_WITH_ITEMS = 3

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
    // 光标矩形:联想浮层挂在光标上,所以"光标在哪"是判据的地面真值。取不到留 null,
    // 绝不让取证把整次采集带崩(异常会让这一 tick 什么层都拿不到)。
    let caret = null;
    try {
        const sel = document.getSelection();
        if (sel && sel.rangeCount > 0) {
            const cr = sel.getRangeAt(0).getBoundingClientRect();
            caret = {x: Math.round(cr.x), y: Math.round(cr.y)};
        }
    } catch (e) {
        caret = null;
    }
    return {layers: layers.slice(0, MAX_LAYERS), caret: caret};
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
    """主判据:浮层与正文框那一列**水平相交**。

    判别性质自始至终是同一条:下拉挂在正文框的光标上,必然压在正文栏这一列;右侧手机预览
    面板与正文栏**水平不相交**(视频页正文栏右缘 ~1096 / 预览面板左缘 ~1145;图文页
    ~1580 / ~1645,两种页型生产截图双实证)。

    早先用"浮层水平中心在栏内"代理这条性质,它比性质本身**紧**:浮层跟着光标走,正文里
    话题 chip 一多、光标被顶到行尾,浮层就半个身子探出正文栏右缘 —— 中心出栏了,与栏
    仍相交,判据却把这个真下拉拒了(RCA 2026-08-09 真号三单:追加词全报
    ``topic_dropdown_not_shown``,poll_timeline 里 layers_seen 稳定 14-19 层 / 8 秒恒定,
    浮层不是晚到;号6 播客同一单前 2 个词成功、第 3 个起连败 = 位置败不是词败)。改判相交
    后紧回性质本身:预览面板与正文栏零相交,照拒不误。

    拿不到正文框几何时返回 False,由调用方退回结构判据。
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
    # 区间相交(边缘相切不算相交:贴着栏缘的层属于旁边那一列)
    return lx < ex + ew and lx + lw > ex


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
    # 被拒的层里**带话题选项**的那些:判据拒错时,真下拉就藏在这里。``rejected_classes``
    # 只记前 5 个类名、也不说那层有没有选项,真凶被它挡住过一整轮(RCA 2026-08-09)。
    # **临时诊断字段**,锚定判据的候选一坐实/排除就该撤。
    rejected_with_items = [
        {
            "cls": str(r.get("cls") or "")[:40],
            "items": topic_option_count(r),
            "x": (r.get("rect") or {}).get("x"),
            "y": (r.get("rect") or {}).get("y"),
            "w": (r.get("rect") or {}).get("width"),
        }
        for r in rejected if topic_option_count(r) > 0
    ][:MAX_REJECTED_WITH_ITEMS]
    return {
        "candidates": seen,
        "item_count": len((layer or {}).get("items") or []),
        "layer_class": str((layer or {}).get("cls") or "")[:80],
        "layers_seen": len(layers),
        "rejected_classes": [str(r.get("cls") or "")[:40] for r in rejected[:MAX_REJECTED_CLASSES]],
        "rejected_with_items": rejected_with_items,
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

    # 带选项的候选层:``no_exact_match`` 只有它们才配得上——一个 0 选项的空壳容器
    # 不构成"平台没这词"的证据(RCA 2026-08-09)。
    with_items = [ly for ly in accepted if (ly.get("items") or [])]

    if not layers:
        # 浮层根本没弹:是时序/输入问题不是"词不存在",调用方绝不能据此换词
        reason, focus = "topic_dropdown_not_shown", None
    elif not accepted:
        # 有浮层但没一个是下拉:抓错容器,和"词不存在"必须分开记
        reason, focus = "topic_dropdown_not_found", (rejected[0] if rejected else None)
    elif not with_items:
        # 通过判据的层全是 0 选项的空壳(平台下拉外壳先挂、内容异步填,内容还没到)——
        # 这仍然是"浮层没弹",和"平台没这词"天差地别。focus 取 accepted[0],让
        # layer_class / item_count=0 照样进取证,下一次一眼看得出抓到的是哪个空壳。
        reason, focus = "topic_dropdown_not_shown", accepted[0]
    else:
        # focus 必须取**第一个带选项**的层:accepted[0] 可能是个空壳,拿它取证
        # candidates 就是空的,回执里等于什么都没说
        reason, focus = "no_exact_match", with_items[0]

    out: Dict[str, Any] = {"success": False, "reason": reason}
    out.update(_forensics(focus, layers, rejected))
    # 光标矩形原样透传(采集 JS 取不到时是 None)。判据只看水平,垂直是靠推断的 ——
    # 有了 光标 + 浮层 rect + 正文栏 rect 三者,下一次真号回执一测就知道是水平错锚
    # 还是垂直方向的事。**临时诊断字段**,同 rejected_with_items 的保质期纪律。
    out["caret_rect"] = (payload or {}).get("caret")
    return out
