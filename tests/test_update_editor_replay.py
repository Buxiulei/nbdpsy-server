"""用**真号实拍夹具**回放已发布笔记的更新页:钉死图片编辑那几条选择器假设。

与 ``test_quote_modal_replay.py`` 同一套路数、同一个理由:更新页的图片增删全靠 UI 驱动,
代码里的"第 i 张卡就是第 i 张图""close-btn 常驻""file input 只有一个"这类假设,
手写假页面永远测不出来 —— 手写时人自然照着假设写。这份夹具的数量、序号、坐标、属性
来自 ``scripts/capture_page_scene.py`` 2026-08-03 在账号 8 的笔记 6a6f18c4 上**只读**采集
(零破坏性点击),不是想象出来的。

本文件只覆盖已闭环的三条证据(设计文档附录 B):

- **E1** 图数清点与单图身份 —— ``.img-upload-area .img-container`` 命中数即图数,
  卡内文案就是序号;
- **E2 只读半 / E3 入口半** —— 删除按钮与图片上传通道的**定位锚点**(点击/灌入这两个
  写动作属 T2 受控写测试,这里一概不碰);
- **E8** 弹层把图片区顶出视口 —— 这条是回归钉,见对应用例的 docstring。

夹具是证据不是设计:选择器对不上只能重新采集,**不许改夹具去迁就代码**。
"""

import pytest

from app.browser import note_components as nc
from tests.page_replay import ReplayPage, load_scene

_SCENE = "update_editor_images"

# ---- 待验的选择器假设(与 scripts/capture_page_scene.py 的采集清单同口径)----
_IMG_AREA = ".img-upload-area"
_IMG_CONTAINER = ".img-upload-area .img-container"
_CLOSE_BTN = ".img-container .close-btn"
_FILE_INPUT = "input[type='file']"
_TITLE_INPUT = "input[placeholder*='标题']"
_BODY_EDITOR = "div[contenteditable='true']"

# 图片通道的锚点谓词:accept 含 .jpg **且**带 multiple。
# ReplayPage 按夹具里存的选择器原样回答,复合属性选择器不在夹具键里,
# 因此这里对实拍 attrs 做同一份筛选 —— 验的是"这个谓词在真页面上唯一命中"。
_IMG_ACCEPT_MARK = ".jpg"


@pytest.fixture
def scene():
    return load_scene(_SCENE)


@pytest.fixture
def page(scene):
    return ReplayPage(scene)


def test_image_containers_count_and_ordinal_identity(page):
    """E1:``.img-container`` 命中数 == 图数,且每张卡的文案就是它的序号。

    图数清点(删第 k 张前要知道共几张)和身份复核(删完重查确认少了正确那张)
    两件事都压在这一条上:序号即文案意味着可以**不靠下标**、直接按文案认卡。
    """
    cards = page.query_selector_all(_IMG_CONTAINER)

    assert len(cards) == 6, f"这篇笔记实拍是 6 张图,回放得到 {len(cards)} 张"
    assert [c.inner_text() for c in cards] == ["1", "2", "3", "4", "5", "6"], (
        "卡内文案必须恰好是 1..6 的序号 —— 一旦不是,按文案认卡的做法立刻失效"
    )
    # 图片区容器本身也在,其文案是各卡序号的拼接(可用来快速核对总数)
    area = page.query_selector(_IMG_AREA)
    assert area is not None and area.inner_text() == "1 2 3 4 5 6"


def test_close_btn_is_one_per_container_and_visible_after_hover(scene, page):
    """E2 只读半:删除按钮与图片容器 1:1,hover 后可见,尺寸 20x20。

    这是"删第 k 张图"的定位依据 —— 第 k 张卡的删除按钮就是 ``.close-btn`` 的第 k 个。
    **点击行为不在这里验**(属 T2 受控写),这条只钉住"数得上、找得着、点得中"。
    """
    cards = page.query_selector_all(_IMG_CONTAINER)
    buttons = page.query_selector_all(_CLOSE_BTN)

    assert len(buttons) == len(cards), (
        f"删除按钮 {len(buttons)} 个 vs 图片 {len(cards)} 张 —— 不是 1:1 就没法按序号删"
    )

    # hover 之后的实拍状态(采集时 hover 了首图):按钮显形且尺寸稳定。
    # extra 不在 dom 下,按它的采集选择器挂回去,仍走 ReplayPage 这条通路
    hovered = ReplayPage({
        **scene,
        "dom": {**scene["dom"], _CLOSE_BTN: scene["extra"]["close_btn_after_hover"]},
    })
    hovered_buttons = hovered.query_selector_all(_CLOSE_BTN)

    assert len(hovered_buttons) == len(cards), "hover 不该改变按钮数量(它们常驻 DOM)"
    for i, btn in enumerate(hovered_buttons):
        assert btn.is_visible(), f"第 {i + 1} 个删除按钮 hover 后仍不可见"
        box = btn.bounding_box()
        assert (box["width"], box["height"]) == (20.0, 20.0), (
            f"第 {i + 1} 个删除按钮实拍是 20x20,现为 {box['width']}x{box['height']}"
        )
        assert "hoverShow" in (btn.get_attribute("class") or ""), (
            "close-btn 靠 hoverShow 类控制显隐 —— 这个类没了说明页面改版,要重采"
        )

    # 第 k 个按钮确实压在第 k 张图上(左上角落在该卡的横向跨度内)——
    # 这条把「按序号配对」从"看着像"变成"量得出",顺序错一格就红
    for i, (card, btn) in enumerate(zip(cards, hovered_buttons)):
        card_box, btn_box = card.bounding_box(), btn.bounding_box()
        assert card_box["x"] <= btn_box["x"] <= card_box["x"] + card_box["width"], (
            f"第 {i + 1} 个删除按钮(x={btn_box['x']})不在第 {i + 1} 张图的横向跨度内"
        )
        assert btn_box["y"] <= card_box["y"], "删除按钮应在图片顶边之上(右上角)"


def test_image_upload_input_is_the_only_jpg_multiple_channel(page):
    """E3 入口半:图片批量通道 = accept 含 .jpg **且**带 multiple 的那个 file input。

    页面上有 3 个 file input,长得很像但用途完全不同,靠 ``input[type='file']`` 取首个
    会撞上另一个同 accept 但**无 multiple** 的(疑似单张替换),多图一次灌入就会丢图。
    这条钉住"复合谓词在真页面上唯一命中"。灌文件的动作属 T2 受控写,这里不做。
    """
    inputs = page.query_selector_all(_FILE_INPUT)
    assert len(inputs) == 3, f"实拍是 3 个 file input,回放得到 {len(inputs)} 个"

    jpg_multiple = [
        el for el in inputs
        if _IMG_ACCEPT_MARK in (el.get_attribute("accept") or "")
        and el.get_attribute("multiple") is not None
    ]
    assert len(jpg_multiple) == 1, (
        f"accept 含 .jpg 且带 multiple 的应恰好 1 个,实得 {len(jpg_multiple)} 个 —— "
        "不唯一就说明这个锚点选不准,得重新采集找新锚点"
    )
    assert jpg_multiple[0].get_attribute("accept") == ".jpg,.jpeg,.png,.webp"

    # 同 accept 但无 multiple 的那个确实存在 —— 它正是"取首个"会撞上的坑
    jpg_single = [
        el for el in inputs
        if _IMG_ACCEPT_MARK in (el.get_attribute("accept") or "")
        and el.get_attribute("multiple") is None
    ]
    assert len(jpg_single) == 1, "同 accept 无 multiple 的那个不见了,说明页面改版要重采"

    # 第三个 accept 是 pdf/doc/ppt —— 属「选择文件」组件,与图片编辑毫无关系。
    # **绝不碰项**:任何图片路径都不许 set_input_files 到它,灌进去等于往笔记里塞附件。
    doc_inputs = [el for el in inputs if ".pdf" in (el.get_attribute("accept") or "")]
    assert len(doc_inputs) == 1
    assert _IMG_ACCEPT_MARK not in (doc_inputs[0].get_attribute("accept") or ""), (
        "文档 input 不该接受图片 —— 它与图片通道必须泾渭分明"
    )


def test_open_modal_pushes_image_area_out_of_viewport(scene):
    """E8 回归钉:弹窗一开,图片区被顶到视口外(rect.y = -815 < 0)。

    **这就是为什么每个编辑步前必须重新滚进视口。** 弹窗(引用/话题等)会改变页面滚动位,
    上一步拿到的坐标在下一步已经失效 —— 此时按旧坐标点击,落点是顶栏或页面空白处,
    动作静默失败而代码看起来一切正常。文字版发布"丢话题/丢活动"就是同款陷阱:
    超长竖图把目标顶出视口,点击落在顶栏(见 02b44f6)。

    所以编排层的纪律是:每个破坏性编辑步(删图/加图/写文本)动手前**重新定位 + 滚进视口**,
    绝不复用上一步的元素句柄或坐标。
    """
    # extra 不在 dom 下:把弹窗开着时的两份实拍按各自的采集选择器挂回去,
    # 得到"弹窗态"的页面(采集侧见 scripts/capture_page_scene.py 的 modal_overlap)
    overlap = scene["extra"]["modal_overlap"]
    during_modal = ReplayPage({
        **scene,
        "dom": {_IMG_AREA: overlap["img_area"], nc._QUOTE_MODAL: overlap["modal"]},
    })

    img_area = during_modal.query_selector(_IMG_AREA)
    assert img_area is not None, "遮挡快照里应有图片区的实拍矩形"
    y = img_area.bounding_box()["y"]
    assert y < 0, (
        f"弹窗开着时图片区 rect.y 应为负(被滚出视口上方),实得 {y} —— "
        "这条绿了反而说明陷阱没复现,要重新采集"
    )

    # 弹窗本身在视口内:一上一下的对比才说明"是滚动位变了",不是整页没渲染
    modal_box = during_modal.query_selector(nc._QUOTE_MODAL).bounding_box()
    assert modal_box["y"] >= 0 and modal_box["height"] > 0, "弹窗应正常显示在视口内"

    # 同一个选择器、同一份页面,基态时图片区在视口内 —— 坐标是**会变的**,不能跨步复用
    base_y = ReplayPage(scene).query_selector(_IMG_AREA).bounding_box()["y"]
    assert base_y >= 0, "基态图片区本应在视口内,否则这条对比不成立"
    assert base_y != y, "基态与弹窗态的纵坐标必须不同,这正是「重新滚进视口」的理由"


def test_title_and_body_selectors_hit_exactly_one_each(page):
    """标题框 / 正文框在这份夹具上各唯一命中 —— 文本修改的定位依据。

    唯一性是重点:多命中就得引入下标或额外限定,那正是引用弹窗踩过的坑。
    """
    titles = page.query_selector_all(_TITLE_INPUT)
    bodies = page.query_selector_all(_BODY_EDITOR)

    assert len(titles) == 1, f"标题框命中 {len(titles)} 个(要求恰好 1 个)"
    assert len(bodies) == 1, f"正文框命中 {len(bodies)} 个(要求恰好 1 个)"

    # 标题的现值在 value 属性上(不是 innerText),正文的现值在 innerText 上 ——
    # 两者读法不同,读错就会把"没改动"误判成"改动了"
    assert titles[0].get_attribute("value"), "标题框应带已发布笔记的现有标题"
    assert titles[0].get_attribute("placeholder") == "填写标题会有更多赞哦"
    assert bodies[0].inner_text(), "正文框应带已发布笔记的现有正文"
    assert "ProseMirror" in (bodies[0].get_attribute("class") or ""), (
        "正文是 tiptap/ProseMirror 富文本 —— 不是 textarea,写入方式受此约束"
    )
