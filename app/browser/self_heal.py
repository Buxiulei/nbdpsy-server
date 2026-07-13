"""选择器自愈定位(SelfHealLocator)。

硬编码 CSS 选择器全部失效时的兜底:抓页面可交互元素的精简文本快照 → 同步调
Qwen(DashScope,openai 兼容接口)指认目标元素 → 派生稳定 CSS 选择器 → 安全校验
→ 返回 (handle, selector)。selector 可为 None,表示走坐标点击、无稳定选择器可学。

设计原则:
- 纯定位,不碰持久化(持久化由挂载点调 registry 完成,两者解耦)。
- 绝不抛异常:snapshot/LLM/解析/校验/取 handle 任一步失败 → 返回 None,全程 try/except
  兜底,仅 logger 记录,不打断发布主流程。
"""

import re

from openai import OpenAI  # 模块级导入,便于测试 monkeypatch self_heal.OpenAI
from loguru import logger

from app.core.config import settings


# 输入类意图:目标元素必须是真正可输入的控件,防 LLM 指错元素。
_INPUT_INTENTS = {"title_input", "content_input", "upload_image_input", "editor_ready"}

# 收集可交互元素的选择器集(移植老仓精简版)。
_INTERACTIVE_SELECTORS = (
    "a,button,input,select,textarea,"
    "[role=button],[role=link],[role=tab],[role=menuitem],"
    "[onclick],[contenteditable],[tabindex]"
)

# 浏览器端快照 JS:收集 ≤100 个视口内可见的可交互元素,做属性/文本截断。
_SNAPSHOT_JS = """
(sel) => {
  const nodes = document.querySelectorAll(sel);
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  let ref = 0;
  for (const node of nodes) {
    if (out.length >= 100) break;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden'
        || parseFloat(style.opacity) === 0) continue;
    const r = node.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    // 视口内(部分可见即可)
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    ref += 1;
    const attrNames = ['id','class','name','type','placeholder','aria-label','data-testid'];
    const attrs = {};
    for (const a of attrNames) {
      const v = node.getAttribute(a);
      if (v) attrs[a] = v.slice(0, 80);
    }
    const raw = node.getAttribute('aria-label') || node.innerText
      || node.getAttribute('placeholder') || node.getAttribute('title')
      || node.value || '';
    const text = (raw || '').trim().slice(0, 50);
    out.push({
      ref: ref,
      tag: node.tagName.toLowerCase(),
      role: node.getAttribute('role') || '',
      text: text,
      attrs: attrs,
      bbox: {x: Math.round(r.x), y: Math.round(r.y),
             width: Math.round(r.width), height: Math.round(r.height)},
      visible: true
    });
  }
  return out;
}
"""


def snapshot_interactive(page) -> list[dict]:
    """同步 page.evaluate 跑 JS,收集 ≤100 个视口内可见的可交互元素。

    每元素:{ref(1..N), tag, role, text(≤50), attrs(id/class/name/type/placeholder/
    aria-label/data-testid 各≤80), bbox{x,y,width,height}, visible}。异常 → 返回 []。
    """
    try:
        elements = page.evaluate(_SNAPSHOT_JS, _INTERACTIVE_SELECTORS)
        return elements or []
    except Exception as exc:  # 页面已跳转/关闭/JS 异常 → 空快照,不打断主流程
        logger.warning(f"[self_heal] 快照失败:{exc}")
        return []


def build_snapshot_text(elements: list[dict]) -> str:
    """把快照转成给 LLM 的精简文本,每行 `[3] button "发布" id=xxx type=submit (x,y wxh)`。

    仅展示存在的属性(id/type/placeholder/name/data-testid/aria-label),目标 ≤2000 token。
    """
    try:
        lines: list[str] = []
        for el in elements or []:
            parts = [f"[{el.get('ref')}]", str(el.get("tag") or "")]
            text = el.get("text") or ""
            if text:
                parts.append(f'"{text}"')
            attrs = el.get("attrs") or {}
            for key in ("id", "type", "placeholder", "name", "data-testid", "aria-label"):
                val = attrs.get(key)
                if val:
                    parts.append(f"{key}={val}")
            bbox = el.get("bbox") or {}
            parts.append(
                f"({bbox.get('x', 0)},{bbox.get('y', 0)} "
                f"{bbox.get('width', 0)}x{bbox.get('height', 0)})"
            )
            lines.append(" ".join(parts))
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"[self_heal] 快照文本构建失败:{exc}")
        return ""


def llm_locate(snapshot_text: str, intent: str) -> int | None:
    """同步调 Qwen(openai 兼容接口)指认目标元素编号。

    prompt = 页面快照 + 用户意图 + "只返回元素编号(纯数字)"。temperature=0,超时
    LLM_TIMEOUT。取回复里第一个数字为 ref;无数字/异常/超时 → None。
    """
    try:
        client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        prompt = (
            "你是网页元素定位助手。\n"
            "## 页面快照\n"
            f"{snapshot_text}\n"
            "## 用户意图\n"
            f"{intent}\n"
            "只返回最匹配的元素编号(纯数字),例如:3"
        )
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=settings.LLM_TIMEOUT,
        )
        content = resp.choices[0].message.content or ""
        matches = re.findall(r"\d+", content)
        if not matches:
            logger.info(f"[self_heal] LLM 未返回元素编号:{content!r}")
            return None
        return int(matches[0])
    except Exception as exc:
        logger.warning(f"[self_heal] LLM 定位失败:{exc}")
        return None


def element_to_selector(el: dict) -> str | None:
    """从选中元素派生稳定 CSS 选择器。

    优先级:id > data-testid > aria-label > name > tag.首个class > tag。
    无可用锚点(连 tag 都没有)→ None(退回坐标点击)。
    """
    try:
        attrs = el.get("attrs") or {}
        tag = (el.get("tag") or "").strip().lower()

        el_id = attrs.get("id")
        if el_id:
            return f"#{el_id}"
        data_testid = attrs.get("data-testid")
        if data_testid:
            return f'[data-testid="{data_testid}"]'
        aria_label = attrs.get("aria-label")
        if aria_label:
            return f'[aria-label="{aria_label}"]'
        name = attrs.get("name")
        if name and tag:
            return f'{tag}[name="{name}"]'
        cls = attrs.get("class")
        if cls and tag:
            first = next((c for c in cls.split() if c), None)
            if first:
                return f"{tag}.{first}"
        if tag:
            return tag
        return None
    except Exception as exc:
        logger.warning(f"[self_heal] 选择器派生失败:{exc}")
        return None


def passes_safety(el: dict, intent_key: str) -> bool:
    """安全校验:防 LLM 在发布链指错元素造成误点/误填。

    - publish_button(危险动作):text 或 aria-label 含"发布"/"publish" 且 tag∈{button,a}
      或 role∈{button,link};不满足 → False(宁可失败不误发)。
    - 输入类(title_input/content_input/upload_image_input/editor_ready):tag∈{input,textarea}
      或有 contenteditable;不满足 → False。
    - 其它意图:无额外约束 → True。
    """
    try:
        tag = (el.get("tag") or "").strip().lower()
        role = (el.get("role") or "").strip().lower()
        attrs = el.get("attrs") or {}
        text = el.get("text") or ""
        aria = attrs.get("aria-label") or ""

        if intent_key == "publish_button":
            blob = f"{text} {aria}".lower()
            has_keyword = ("发布" in blob) or ("publish" in blob)
            ok_role = tag in {"button", "a"} or role in {"button", "link"}
            return bool(has_keyword and ok_role)

        if intent_key in _INPUT_INTENTS:
            has_contenteditable = "contenteditable" in attrs
            return tag in {"input", "textarea"} or has_contenteditable

        return True
    except Exception as exc:
        logger.warning(f"[self_heal] 安全校验异常:{exc}")
        return False


class SelfHealLocator:
    """选择器自愈定位收口:快照 → LLM 定位 → 安全校验 → 派生选择器 → 取 handle。"""

    def locate(self, page, intent_key: str, intent_desc: str) -> tuple | None:
        """返回 (handle, selector|None);任一步失败 → None(不抛异常)。

        selector 为 None 表示走的坐标点击(elementFromPoint),无稳定选择器可学。
        """
        try:
            elements = snapshot_interactive(page)
            if not elements:
                return None

            snapshot_text = build_snapshot_text(elements)
            ref = llm_locate(snapshot_text, intent_desc)
            if ref is None:
                return None

            el = next((e for e in elements if e.get("ref") == ref), None)
            if el is None:
                logger.warning(f"[self_heal] LLM 返回的编号 {ref} 不在快照中")
                return None

            if not passes_safety(el, intent_key):
                logger.warning(
                    f"[self_heal] 安全校验未通过:intent={intent_key} "
                    f"tag={el.get('tag')} text={el.get('text')!r}"
                )
                return None

            selector = element_to_selector(el)
            handle = None
            selector_out = None

            # 优先用派生选择器取 handle;命中才把它当"可学"的稳定选择器返回。
            if selector:
                try:
                    handle = page.query_selector(selector)
                except Exception:
                    handle = None
                if handle is not None:
                    selector_out = selector

            # 选择器取不到 → 用 bbox 中心坐标 elementFromPoint 兜底,此路无稳定选择器可学。
            if handle is None:
                bbox = el.get("bbox") or {}
                cx = bbox.get("x", 0) + bbox.get("width", 0) / 2
                cy = bbox.get("y", 0) + bbox.get("height", 0) / 2
                try:
                    js_handle = page.evaluate_handle(
                        "(p)=>document.elementFromPoint(p.x,p.y)", {"x": cx, "y": cy}
                    )
                    handle = js_handle.as_element()
                except Exception:
                    handle = None
                selector_out = None

            if handle is None:
                return None

            logger.info(
                f"[self_heal] 定位成功:intent={intent_key} ref={ref} "
                f"selector={selector_out}"
            )
            return (handle, selector_out)
        except Exception as exc:
            logger.warning(f"[self_heal] 定位异常:{exc}")
            return None
