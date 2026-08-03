"""已发布笔记更新页的**图片增删**(浏览器交互层,纯同步,吃已打开更新页的 page)。

设计 `docs/design/2026-08-03-note-editing-design.md` 4.2 / 4.3 / 4.4 + 附录 B(T1 只读
采集)/ 附录 C(T2 受控写)。编排(什么时候调、失败怎么翻译成 `aborted_before_submit`)
不在这里,归 `note_components.set_note_components`(T6)。

**为什么图片单独一个文件**:①并行分工——`note_editing.py`(文本步)同轮在改;②图片是这
条产品线里最险的一块(删除不可逆),独立文件让它的红线与防线集中可读,与设计"文件不失控"
同旨。纯逻辑(下标降序计划、图数等式)从 `note_editing` import 复用,**不重写也不改动它**。

## 这里的四道防线(每一道都有事故/实证背书)

1. **图数不可确认就不许任何图片点击**(`count_images` → None → `image_gate` error)。
   同"权限读不出就不提交"的世界观:全量覆盖提交下,认知不确定时零动作是唯一安全解。
2. **`expected_image_count` 闸**:台账认知与页面实数对不上 → 一次 hover 都不做就退出。
   这是删除类操作的第一道防呆(设计 3.1 / 4.3)。
3. **删除按钮是唯一受控例外,四前提缺一不可**(设计 4.3):夹具背书的选择器 + 闸已过 +
   序号身份复核 + `elementFromPoint` 落点复核。原红线三项(合集 `.close-icon`、
   「取消关联」、笔记删除)**一个都没解禁**,本模块从头到尾不碰它们。
4. **一切不确定当场 error,绝不猜**:重试封顶后停手不删下一张;点完冒出没见过的弹窗
   一律如实上报交人工,**绝不猜弹窗按钮乱点**(E2 的确认弹窗情形未在真号闭环)。

安全底座是附录 C 的 E4/E7 实证:**编辑器内删图/加图是纯前端态,未提交离开即恢复原状**。
所以本模块任何一步失败,只要 T6 走弃提交路径,笔记就是原样未动的。

## 尚未真号验证的部分(读代码的人必须知道)

- **E3 灌入行为**(`set_input_files` 灌进更新页后到底会不会上传、追加在哪)从未实证;
- **E2 的「-1 计数」路径**只在 6 图回放夹具里模拟过(真号那次牺牲品只有 1 张图,删到 0
  被平台「至少 1 张」拦下)。

两者的第一次真号验证在 T7。因此这两条路径的代码一律 **fail-safe**:渲染/计数不达预期
就是 error,绝不"大概成了"。

## 硬约束(与 `note_components` 模块 docstring 同一套)

- 全程 `SyncHumanActions`;`page.evaluate` **只用于只读取证**,不做 JS 点击/设值/
  scrollIntoView;等待用 `page.wait_for_timeout` / `human.wait`,**不用 `time.sleep`**
  (同步 API 下 `time.sleep` 期间 response 监听器一个都不会触发);
- **不 import `note_components`**(那边要 import 本模块的编排点,成环)。需要的范式
  ——`elementFromPoint` 落点复核、"每跳重新找元素不持旧句柄"、窄按钮静默失效的同张
  重试——在本文件各写一份同款实现,出处见各函数 docstring。两处将来要一起改。
"""

import time
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.note_editing import plan_image_removal
from app.browser.sync_human_actions import SyncHumanActions

# ---------------- 选择器(全部有 T1 只读夹具背书,见 tests/test_update_editor_replay.py)----

# 图片区与单图容器:命中数即图数,容器文案就是它的序号 "1".."N"(附录 B / E1,6==6 实拍)
_IMG_AREA = ".img-upload-area"
_IMG_CONTAINER = ".img-upload-area .img-container"
# 单图删除按钮:与容器 1:1、各 20x20、靠 hoverShow 类 hover 才显形(附录 B / E2 只读半)。
# **只在目标容器内部查**——全页查再按下标取,等于把"第 k 个按钮对应第 k 张图"这个
# 同序假设又搬回来一次(引用弹窗就是这么栽的),这里坚持"从卡片往下找"。
_CLOSE_BTN = ".close-btn"
# 页面上共 3 个 file input(附录 B / E3):两个 accept=".jpg,.jpeg,.png,.webp"(一个带
# multiple 一个不带),一个 accept 是 pdf/doc/ppt。这里从**全部** file input 里按谓词筛,
# 不是"取第一个":取首个会撞上同 accept 无 multiple 的单张替换通道(多图一次灌入就丢图),
# 而 pdf 那个灌进去等于往笔记里塞附件。
_FILE_INPUT = "input[type='file']"
_IMG_ACCEPT_MARK = ".jpg"
_DOC_ACCEPT_MARK = ".pdf"
# 意外弹窗探测:本仓已知的弹窗根(`.d-modal` 是引用弹窗那一族)+ 通用 role。
# 探测用 query_selector 而非 evaluate —— 只读、且能被回放夹具驱动。
_DIALOG_SELECTORS = (".d-modal", "[role='dialog']")

# ---------------- 时序/次数(与既有模块同水位)----

# 窄按钮静默失效的同张重试封顶:活动「关联」28x68 都会首点静默失效(设计 2.7④ 真号实测),
# close-btn 只有 20x20,同族风险更高。**只重试同一张**,绝不"跳过去删下一张"。
_REMOVE_ATTEMPTS = 3
# 删一张后等前端重排的窗口。纯前端态(E4 实证:零网络请求),给足余量即可
_REMOVE_SETTLE_TIMEOUT_S = 8.0
# 灌图后等真上传渲染:走网络传图,给到分钟级(atomic_tasks.step2 实测 ~16s 够用,
# 但那是发布页单次;这里图可能更大,宁可等也不要误判失败)
_UPLOAD_RENDER_TIMEOUT_S = 90.0
_POLL_MS = 500
# 滚进视口的拟人滚动轮次上限(同 click_publish 与 note_editing._SCROLL_TRIES,三处同为 3)
_SCROLL_ATTEMPTS = 3


# ---------------- 图数清点与校验闸 ----------------


def count_images(page) -> Optional[int]:
    """更新页当前图数,**双判据一致才作数**;任一读不出或两者不一致 → None。

    - 判据 A:``.img-upload-area .img-container`` **命中数**(附录 B / E1 实拍 6==6);
    - 判据 B:``.img-upload-area`` 自身的**序号拼接文案**("1 2 3 4 5 6",同为 T1 实拍),
      要求它恰好是 1..N 的连续序列,取 N。

    返回 None 的语义是"**图数不可确认**",调用方(`image_gate`)据此零点击退出。理由同
    "权限读不出就不提交":删图不可逆,而"数错一张"直接等于"删错一张"。宁可整单拒绝。

    **判据 B 的取舍(裁决记录,别当没写)**:设计 4 节流程②原本写的是右侧预览列
    ``.image-preview`` 的「k/N」页码。那个元素**确实存在**(8-03 泛化探针见过 cls
    ``image-preview``、文案含「1/6」),但它没进 T1 采集清单、不在正式夹具里 —— 没有夹具
    背书的选择器不能当判据(否则 CI 里永远证伪不了它,正是这套回放测试要根治的病)。故本
    期改用同样实拍、且现在就能被夹具证伪的序号拼接串。

    **它的局限也写明**:A 与 B 同源(都来自 ``.img-upload-area`` 这棵子树),防不住整片
    图片区渲染异常,只防得住"渲染半截 / 序号错乱"。**真正独立的第二判据 ``.image-preview``
    (右侧预览列,与左侧编辑区是不同组件,交叉价值更高)待 T7 正式采证后升级** —— 到那时
    这里改成 A vs 预览页码,序号连续性作为第三条留着即可。
    """
    by_container = _container_count(page)
    by_ordinal = _ordinal_sequence_total(page)
    if by_container is None or by_ordinal is None:
        logger.warning(
            f"[note_editing_images] 图数不可确认:判据A(.img-container 计数)={by_container} "
            f"判据B(图片区序号拼接)={by_ordinal} —— 任一读不出即拒绝图片操作"
        )
        return None
    if by_container != by_ordinal:
        logger.warning(
            f"[note_editing_images] 图数双判据不一致:容器数={by_container} "
            f"序号拼接={by_ordinal} —— 页面可能渲染半截,拒绝图片操作"
        )
        return None
    return by_container


def image_gate(page, expected_image_count: int) -> Dict[str, Any]:
    """删除/追加动手前的**第一道防呆**:页面实数必须等于调用方声明的 ``expected``。

    必须在**任何** hover/点击之前调用(设计 4.3 前提②)。台账认知过期(别处改过这篇、
    或调用方拿的是旧快照)时,下标语义整个失效——第 3 张已经不是它以为的那张了。此时
    唯一安全的动作是**零点击退出**。

    Returns:
        ``{"status": "ok", "count": n}`` 或 ``{"status": "error", "reason": ..., "count": ...}``
        (不可确认时 ``count`` 为 None)。
    """
    actual = count_images(page)
    if actual is None:
        return {
            "status": "error",
            "count": None,
            "reason": "image_count_unconfirmable: 图数双判据未取到一致值"
                      f"(判据A .img-container 计数 / 判据B {_IMG_AREA} 序号拼接),"
                      f"图数不可确认,零点击退出(expected={expected_image_count})",
        }
    if actual != expected_image_count:
        return {
            "status": "error",
            "count": actual,
            "reason": f"image_count_mismatch: 页面实数 {actual} ≠ expected "
                      f"{expected_image_count},台账认知过期,零点击退出",
        }
    return {"status": "ok", "count": actual}


def _container_count(page) -> Optional[int]:
    """判据 A:图片容器命中数。图片区本身都没有 → None(不是 0:页面没渲染 ≠ 零张图)。"""
    try:
        if page.query_selector(_IMG_AREA) is None:
            return None
        return len(page.query_selector_all(_IMG_CONTAINER))
    except Exception as exc:  # noqa: BLE001 — 读不出就是不可确认,不猜
        logger.warning(f"[note_editing_images] 判据A 读取失败: {exc}")
        return None


def _ordinal_sequence_total(page) -> Optional[int]:
    """判据 B:图片区自身文案是不是 1..N 的连续序号拼接;是则取 N,否则 None。

    实拍(附录 B / E1):``.img-upload-area`` 的 ``innerText`` 就是 ``"1 2 3 4 5 6"`` ——
    各卡序号顺次拼接。它与判据 A **不是同一个读法**:A 数节点个数,B 读父节点渲染出来的
    文本,所以"节点在但序号没重绘""序号跳号/重号"这类半截态会被 B 抓住。

    只用 ``query_selector`` + ``inner_text``(不走 evaluate):同样是只读,但这条路能被
    回放夹具驱动,页面改版夹具一跑就红。
    """
    try:
        area = page.query_selector(_IMG_AREA)
        if area is None:
            return None
        tokens = (area.inner_text() or "").split()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_editing_images] 判据B 读取失败: {exc}")
        return None
    if not tokens or tokens != [str(i) for i in range(1, len(tokens) + 1)]:
        return None
    return len(tokens)


def _ordinals(page) -> List[str]:
    """当前各图容器的序号文案(附录 B / E1:容器文案就是 "1".."N")。"""
    out = []
    for card in page.query_selector_all(_IMG_CONTAINER):
        try:
            out.append((card.inner_text() or "").strip())
        except Exception:  # noqa: BLE001 — 单张读失败按空串计,下面的重排校验自然会红
            out.append("")
    return out


def _ordinals_are_sequential(page, expected_count: int) -> bool:
    """容器数 == expected 且序号文案恰好重排成 1..expected。

    删/加之后**光数数量不够**:数对了但序号错乱说明前端只重绘了一半,此时下一张的身份
    复核就会认错卡。这条是"当场回读"纪律在图片上的落点。
    """
    return _ordinals(page) == [str(i) for i in range(1, expected_count + 1)]


# ---------------- 删除(最高危:不可逆)----------------


def remove_images(page, human: SyncHumanActions, indexes: List[int]) -> Dict[str, Any]:
    """按发布态图序删除指定图片(1-based 下标),**降序逐张删、逐张当场核验**。

    删除顺序来自 ``note_editing.plan_image_removal``(去重 + 降序):删掉第 k 张之后原来
    第 k+1..n 张全部左移 1,按原下标接着删就删错图,而误删不可逆。

    单张流程(设计 4.3 的受控例外四前提,缺一不可):**重新滚进视口 → 按序号文案身份复核
    → 拟人 hover 显形 → `elementFromPoint` 落点复核 → 拟人点 → 当场核验 -1 + 序号重排**。

    任何一环不达预期:同一张重试封顶 ``_REMOVE_ATTEMPTS`` 次,仍不成 → **立刻停手,不再
    删下一张**,整单 error(残缺态交编排层走弃提交,笔记原样未动)。

    Returns:
        ``{"status": "done"|"error", "reason"?, "removed": 实删数, "count_before", "count_after"}``。
        **部分删成后失败也是 ``error``** —— 已经删掉的那几张只存在于编辑器前端态,
        不提交就不落库(附录 C / E4 实证),所以"删了 2 张卡在第 3 张"的正确结论是整单失败。
    """
    order = plan_image_removal(indexes)
    before = count_images(page)
    if before is None:
        return {
            "status": "error",
            "reason": "image_count_unconfirmable: 删除前图数不可确认,一次点击都不发",
            "removed": 0, "count_before": None, "count_after": None,
        }
    if not order:
        # 没有要删的:不是失败,但也别让调用方以为删过。0 张删成,计数原样。
        return {"status": "done", "removed": 0,
                "count_before": before, "count_after": before}

    logger.info(
        f"[note_editing_images] 准备删图:现有 {before} 张,按降序删 {order}"
        f"(原始下标 {list(indexes)})"
    )
    current = before
    removed = 0
    for ordinal in order:
        outcome = _remove_one(page, human, ordinal, expected_after=current - 1)
        if outcome["status"] != "done":
            return {
                "status": "error",
                "reason": outcome["reason"],
                "removed": removed,
                "count_before": before,
                # 停手时页面上到底剩几张,交编排层如实上报(不可确认就是 None)
                "count_after": count_images(page),
            }
        current -= 1
        removed += 1
        logger.info(f"[note_editing_images] ✓ 已删第 {ordinal} 张,现剩 {current} 张")

    return {"status": "done", "removed": removed,
            "count_before": before, "count_after": current}


def _remove_one(
    page, human: SyncHumanActions, ordinal: int, *, expected_after: int
) -> Dict[str, Any]:
    """删单张:四前提复核 → 点 → 核验 -1;不达预期就同张重试,封顶后 error。

    重试范式抄 ``note_components._set_activity``(窄按钮首点静默失效:无 toast、零网络
    请求、状态不翻转,同样的点法再点一次就成)。**每次尝试都从头重新定位**:滚动位、
    DOM 句柄、坐标全部重取——附录 B / E8 实证弹层会把图片区顶到 y=-815,复用上一轮的
    坐标就是往顶栏上点(与 ``_wait_activity_flip`` 每跳重新找元素同理)。
    """
    last_reason = ""
    for attempt in range(1, _REMOVE_ATTEMPTS + 1):
        dialogs_before = _visible_dialog_texts(page)

        located = _locate_close_button(page, human, ordinal)
        if located["status"] != "ok":
            # 定位/复核类失败**不重试**:序号对不上、按钮找不着、落点不是它 —— 这些不是
            # "点了没生效",是"根本不知道该点哪",再来一次只会再猜一次。
            return {"status": "error", "reason": located["reason"]}

        x, y = located["point"]
        human.click((x, y), reason=f"删除第 {ordinal} 张图(第 {attempt} 次,已复核落点)")

        if _wait_removed(page, expected_after):
            return {"status": "done"}

        # 数没变:先看是不是冒出了没见过的弹窗(E2 的确认弹窗情形从未在真号闭环)
        new_dialogs = _new_dialogs(dialogs_before, _visible_dialog_texts(page))
        if new_dialogs:
            return {
                "status": "error",
                "reason": "unexpected_dialog_after_close_click: 点删除第 "
                          f"{ordinal} 张后页面弹出未知弹窗且图数未变,弹窗文案="
                          f"{new_dialogs!r};**不猜弹窗按钮**,交人工确认后再改代码",
            }
        last_reason = (
            f"image_remove_not_applied: 点了 {attempt} 次第 {ordinal} 张的删除按钮,"
            f"容器数始终没降到 {expected_after}(序号也没重排),疑似窄按钮静默失效"
        )
        logger.warning(f"[note_editing_images] {last_reason},重试同一张")

    return {"status": "error", "reason": last_reason}


def _locate_close_button(
    page, human: SyncHumanActions, ordinal: int
) -> Dict[str, Any]:
    """受控例外的前提 ①②③:滚进视口 → 序号身份复核 → hover 显形 → 落点复核。

    返回 ``{"status": "ok", "point": (x, y)}`` 或 ``{"status": "error", "reason": ...}``。
    **返回 error 时一次点击都没发生**。
    """
    # ① 重新定位并滚进视口(附录 B / E8 硬要求)。参照 click_publish 的滚动循环,但**双向**
    # ——E8 实拍是图片区被顶到视口**上方**(y=-815),只会往下滚的循环在这种情形永远滚不回来。
    scrolled = _scroll_card_into_view(page, human, ordinal)
    if scrolled["status"] != "ok":
        return scrolled

    # ② 序号身份复核:按容器文案认卡(附录 B / E1:文案就是 "1".."N")。
    # 找不到或多于一张都拒绝——多命中意味着序号不唯一,此时"第 ordinal 张"这个概念本身
    # 已经不成立,删哪张都可能是错的。
    card = scrolled["card"]

    # ③ 拟人 hover 让 .close-btn.hoverShow 显形(附录 B / E2 实证:常驻 DOM、hover 才显形)
    human.hover(card, reason=f"悬停第 {ordinal} 张图(让删除按钮显形)")
    human.wait(0.3, 0.7, context="等删除按钮显形")

    btn = card.query_selector(_CLOSE_BTN)
    if btn is None:
        return {"status": "error",
                "reason": f"close_btn_not_found: 第 {ordinal} 张图的容器里没有 "
                          f"{_CLOSE_BTN}(hover 后仍没有),拒绝点击"}
    try:
        box = btn.bounding_box()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error",
                "reason": f"close_btn_rect_unreadable: 第 {ordinal} 张图的删除按钮矩形"
                          f"读不出({exc}),拒绝点击"}
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return {"status": "error",
                "reason": f"close_btn_rect_unreadable: 第 {ordinal} 张图的删除按钮矩形"
                          f"为 {box!r}(未显形/尺寸 0),拒绝点击"}
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2

    # ④ 落点复核:只读 JS 确认这个坐标上最上层的元素**确实是目标那张图容器内的删除控件**。
    # 同款纪律出处 note_components.click_publish(质心复核不过一律不点)。这一带周围就是
    # 相邻图片和上传按钮,点空了轻则无效、重则删错图——而删错不可逆。
    hit = _hit_test(page, x, y)
    if hit is None:
        return {"status": "error",
                "reason": f"close_point_unverifiable: 第 {ordinal} 张图删除按钮落点 "
                          f"({x:.0f},{y:.0f}) 的 elementFromPoint 读不出,拒绝点击"}
    if not hit.get("on_close_btn") or (hit.get("card_text") or "").strip() != str(ordinal):
        return {"status": "error",
                "reason": f"close_point_mismatch: 落点 ({x:.0f},{y:.0f}) 上是 "
                          f"tag={hit.get('tag')!r} class={hit.get('cls')!r} "
                          f"所属图容器文案={hit.get('card_text')!r},"
                          f"不是第 {ordinal} 张图的删除按钮,拒绝点击"}
    return {"status": "ok", "point": (x, y)}


def _scroll_card_into_view(
    page, human: SyncHumanActions, ordinal: int
) -> Dict[str, Any]:
    """把第 ordinal 张图滚进视口,并返回**重新查到的**那个容器句柄。

    每轮都重新 ``query_selector_all`` 认卡:滚动会触发重渲染,持旧句柄读到的是已经
    detach 的元素(``_wait_activity_flip`` 同款教训)。滚动用拟人 ``human.scroll``,
    **不用 JS scrollIntoView**(evaluate 只读)。
    """
    viewport_h = _viewport_height(page)
    if viewport_h is None:
        return {"status": "error",
                "reason": "viewport_unreadable: 读不出视口高度,无法确认目标图在视口内,"
                          "拒绝点击(不确定就不点)"}

    last_box = None
    for _ in range(_SCROLL_ATTEMPTS):
        picked = _pick_card(page, ordinal)
        if picked["status"] != "ok":
            return picked
        card = picked["card"]
        try:
            box = card.bounding_box()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error",
                    "reason": f"image_card_rect_unreadable: 第 {ordinal} 张图矩形读不出({exc})"}
        if not box or box.get("height", 0) <= 0:
            return {"status": "error",
                    "reason": f"image_card_rect_unreadable: 第 {ordinal} 张图矩形为 {box!r}"}
        last_box = box
        # 删除按钮压在图的**上边缘之上**(附录 B 实拍:btn.y < card.y),所以视口判定要把
        # 按钮那一圈也算进去,不然会出现"图在视口内、按钮在视口外"的半截态。
        top = box["y"] - box["height"] * 0.5
        bottom = box["y"] + box["height"]
        if top >= 0 and bottom <= viewport_h:
            return {"status": "ok", "card": card}
        # 双向滚:目标在上方(E8 弹层顶出去的情形)就往上滚,在下方就往下滚
        human.scroll("up" if top < 0 else "down")
        human.wait(0.3, 0.7, context=f"把第 {ordinal} 张图滚进视口")

    return {"status": "error",
            "reason": f"image_card_not_in_viewport: 滚了 {_SCROLL_ATTEMPTS} 轮,第 "
                      f"{ordinal} 张图仍不在视口内(最后矩形 {last_box!r},视口高 "
                      f"{viewport_h}),拒绝盲点"}


def _pick_card(page, ordinal: int) -> Dict[str, Any]:
    """按序号文案唯一命中一张 ``.img-container``;零命中/多命中都 error。"""
    hits = [c for c in page.query_selector_all(_IMG_CONTAINER)
            if _safe_text(c) == str(ordinal)]
    if not hits:
        return {"status": "error",
                "reason": f"image_ordinal_not_found: 页面上没有文案为 {ordinal!r} 的图容器"
                          f"(现有序号 {_ordinals(page)}),拒绝按位置猜"}
    if len(hits) > 1:
        return {"status": "error",
                "reason": f"image_ordinal_ambiguous: 文案为 {ordinal!r} 的图容器命中 "
                          f"{len(hits)} 个,身份不唯一,拒绝点击"}
    return {"status": "ok", "card": hits[0]}


def _wait_removed(page, expected_after: int) -> bool:
    """轮询等"容器数 == expected_after **且** 序号重排成 1..expected_after"。

    两个条件都要:光看数量会把"删掉一张但序号没重绘"的半截态当成功,而下一张的身份复核
    正是靠序号文案。超时返回 False(交调用方重试/停手,**不抛异常**)。
    """
    deadline = time.monotonic() + _REMOVE_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        page.wait_for_timeout(_POLL_MS)
        if _ordinals_are_sequential(page, expected_after):
            return True
    return False


# ---------------- 追加 ----------------


def add_images(page, human: SyncHumanActions, local_paths: List[str]) -> Dict[str, Any]:
    """把本地图片追加到图序末尾:``set_input_files`` 直传隐藏 input,**不点上传按钮**。

    不点按钮是实测坑(``atomic_tasks.step2_upload_images``):真桌面上点「上传图片」会弹
    **原生 GTK 文件框**(Playwright 的 file_chooser 拦不住),模态卡死整条流程。直接把文件
    灌进隐藏 ``<input type=file>`` 触发 change 事件,平台上传逻辑照常接管。

    通道选择是本函数最要紧的一步:页面上 3 个 file input 长得很像但用途完全不同,必须
    **唯一命中**"accept 含 .jpg 且带 multiple"那个。不唯一就 error,**绝不回退到裸
    ``input[type=file]`` 取首个**(会撞同 accept 无 multiple 的单张替换通道 → 多图丢图),
    **更不碰 accept 含 .pdf 的那个**(灌进去等于往笔记里塞附件)。

    ``human`` 只用于灌入后的拟人等待:这条路径没有可点的元素(不点按钮就是全部要点)。

    Returns:
        ``{"status": "done"|"error", "reason"?, "added": 实增数, "count_before", "count_after"}``。

    **E3 未实证**:灌入后到底会不会上传、图追加在末尾还是别处,从未在真号上验证过
    (第一次真验证在 T7)。所以这里 fail-safe:渲染不达预期(数不对/序号不是拼在末尾)
    一律 error,不做"大概成了"。
    """
    if not local_paths:
        return {"status": "error",
                "reason": "add_images_empty: 没有要追加的图片路径(调用方不该走到这里)",
                "added": 0, "count_before": None, "count_after": None}

    before = count_images(page)
    if before is None:
        return {"status": "error",
                "reason": "image_count_unconfirmable: 追加前图数不可确认,不灌文件",
                "added": 0, "count_before": None, "count_after": None}

    picked = _pick_image_input(page)
    if picked["status"] != "ok":
        return {"status": "error", "reason": picked["reason"],
                "added": 0, "count_before": before, "count_after": before}

    expected_after = before + len(local_paths)
    logger.info(
        f"[note_editing_images] set_input_files 直传 {len(local_paths)} 张"
        f"(现有 {before} 张,期望 {expected_after} 张),不点上传按钮"
    )
    try:
        picked["input"].set_input_files(list(local_paths))
    except Exception as exc:  # noqa: BLE001 — 灌不进去就是灌不进去,不猜别的通道
        return {"status": "error",
                "reason": f"set_input_files_failed: 往图片通道灌 {len(local_paths)} 张失败({exc})",
                "added": 0, "count_before": before, "count_after": count_images(page)}

    human.wait(0.5, 1.2, context="等图片上传")
    if _wait_added(page, expected_after):
        logger.info(f"[note_editing_images] ✓ 追加 {len(local_paths)} 张已渲染,现 {expected_after} 张")
        return {"status": "done", "added": len(local_paths),
                "count_before": before, "count_after": expected_after}

    now = _container_count(page)
    return {
        "status": "error",
        "reason": f"image_add_not_rendered: 灌入 {len(local_paths)} 张后 "
                  f"{_UPLOAD_RENDER_TIMEOUT_S:.0f}s 内容器数/序号没变成 1..{expected_after}"
                  f"(现容器数={now},序号={_ordinals(page)}),不当成功",
        # 实增数以**回读**为准:灌了几张不等于成了几张(E3 未实证,更不能拿请求数当结果)
        "added": max(0, (now or 0) - before),
        "count_before": before,
        "count_after": count_images(page),
    }


def _pick_image_input(page) -> Dict[str, Any]:
    """从**全部** file input 里筛出图片批量通道,唯一命中才返回。

    这里刻意不用 ``input[type='file'][accept*='.jpg'][multiple]`` 这条复合 CSS 直查,
    而是枚举 + 同语义谓词筛:①谓词与那条 CSS 完全等价(accept 含 .jpg 且有 multiple);
    ②枚举出来才能顺带断言"pdf 通道被排除""同 accept 无 multiple 的那个确实另有其人",
    也才能被真号夹具回放证伪(回放层按采集时用的选择器回答,复合选择器离线恒零命中,
    等于这道最关键的锚点在 CI 里测不到)。**唯一命中是硬要求,不做任何"取第一个"回退。**
    """
    try:
        inputs = page.query_selector_all(_FILE_INPUT)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error",
                "reason": f"image_input_unreadable: 枚举 file input 失败({exc})"}

    picks = []
    for el in inputs:
        accept = el.get_attribute("accept") or ""
        if _DOC_ACCEPT_MARK in accept:
            continue  # 文档通道:**绝不碰**,灌进去等于往笔记里塞附件
        if _IMG_ACCEPT_MARK not in accept:
            continue
        if el.get_attribute("multiple") is None:
            continue  # 同 accept 无 multiple = 疑似单张替换通道,多图会丢图
        picks.append(el)

    if not picks:
        return {"status": "error",
                "reason": f"image_input_not_found: {len(inputs)} 个 file input 里没有"
                          f"「accept 含 {_IMG_ACCEPT_MARK} 且带 multiple」的图片批量通道,"
                          f"拒绝回退到裸 {_FILE_INPUT}(会撞单张替换/文档通道)"}
    if len(picks) > 1:
        return {"status": "error",
                "reason": f"image_input_ambiguous: 图片批量通道命中 {len(picks)} 个"
                          f"(要求恰好 1 个),锚点已失准,拒绝盲灌"}
    return {"status": "ok", "input": picks[0]}


def _wait_added(page, expected_after: int) -> bool:
    """轮询等"容器数 == expected_after 且序号是 1..expected_after"(新图拼在末尾)。"""
    deadline = time.monotonic() + _UPLOAD_RENDER_TIMEOUT_S
    while time.monotonic() < deadline:
        page.wait_for_timeout(_POLL_MS)
        if _ordinals_are_sequential(page, expected_after):
            return True
    return False


# ---------------- 只读取证小工具 ----------------


# 落点复核(只读):返回该坐标上最上层元素的 tag/class,以及它是不是某张图容器内的
# 删除控件、所属容器的序号文案。`closest` 一次问清"是不是那个按钮"和"是不是那张图",
# 比只比 tagName 严 —— 相邻两张图的删除按钮 tag/class 一模一样,只有所属容器能区分。
_HIT_TEST_JS = r"""
([x, y]) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const btn = el.closest ? el.closest('.close-btn') : null;
    const card = el.closest ? el.closest('.img-container') : null;
    return {
        tag: el.tagName || null,
        cls: el.getAttribute ? (el.getAttribute('class') || '') : '',
        on_close_btn: !!btn,
        card_text: card ? (card.innerText || '').trim() : null,
    };
}
"""


def _hit_test(page, x: float, y: float) -> Optional[Dict[str, Any]]:
    """只读回落点上的元素身份;读不出返回 None(调用方据此拒绝点击)。"""
    try:
        return page.evaluate(_HIT_TEST_JS, [x, y])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_editing_images] elementFromPoint 复核失败: {exc}")
        return None


def _viewport_height(page) -> Optional[float]:
    """只读回视口高度(判"目标在不在视口内"用);读不出返回 None。"""
    try:
        return page.evaluate("() => window.innerHeight")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_editing_images] 视口高度读取失败: {exc}")
        return None


def _visible_dialog_texts(page) -> List[str]:
    """当前可见的弹窗文案列表(只读,用于"点完是不是冒出了确认弹窗")。

    用 ``query_selector`` 而非 evaluate:同样只读,但这条路离线可回放。
    """
    texts = []
    for sel in _DIALOG_SELECTORS:
        try:
            elements = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                if el.is_visible():
                    texts.append(_safe_text(el))
            except Exception:  # noqa: BLE001
                continue
    return texts


def _new_dialogs(before: List[str], after: List[str]) -> List[str]:
    """点击前后可见弹窗的差集(按出现次数),空列表表示没有新弹窗。

    比"点后有没有弹窗"更准:页面本来就挂着的弹层(引用弹窗等)不该被算成"删除确认框"。
    """
    remaining = list(before)
    fresh = []
    for text in after:
        if text in remaining:
            remaining.remove(text)
        else:
            fresh.append(text)
    return fresh


def _safe_text(el) -> str:
    """读元素文案,失败按空串(单个元素读不出不该炸掉整个清点)。"""
    try:
        return (el.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
