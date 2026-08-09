"""话题下拉浮层定位判据单测(纯逻辑,不起浏览器)。

夹具照生产实拍的两种页型摆:正文栏在左、手机预览面板在右(视频页正文栏右缘 ~1096 /
预览面板左缘 ~1145;图文页 ~1580 / ~1645,见 job277 与 job229 的 05_10 截图)。

牙口:把判据改回"取面积最小的浮层",本文件的 test_video_preview_layer_* 与
test_real_dropdown_wins_over_smaller_preview_layer 必红 —— 假浮层在两份夹具里都**更小**,
正是它当初能骗过旧启发式的原因。
"""

import pytest

from app.browser.topic_dropdown import (
    is_anchored_to_editor,
    is_dropdown_like,
    looks_like_topic_option,
    select_topic_option,
)


# ── 夹具:照生产截图的几何摆位 ──

# 视频页正文框(job277 实拍:正文栏 x≈418..1096)
VIDEO_EDITOR_RECT = {"x": 418.0, "y": 470.0, "width": 678.0, "height": 260.0}
# 图文页正文框(job229 实拍:正文栏 x≈717..1580)
IMAGE_EDITOR_RECT = {"x": 717.0, "y": 260.0, "width": 863.0, "height": 700.0}


def _item(text, x, y):
    return {"text": text, "x": x, "y": y}


def video_preview_layer(tag="投射性认同"):
    """右侧手机预览面板的作者信息区 —— job277 回执里**实际**抓到的那一层。

    候选文案逐字来自生产:昵称 / 关注 / 展开 / 编辑于 刚刚·公开可见;它含话题文案是因为
    预览面板镜像了正文(正文里刚打进去 ``#话题``),面积又比真下拉小。
    """
    return {
        "cls": "base-info",
        "rect": {"x": 1145.0, "y": 490.0, "width": 225.0, "height": 55.0},
        "has_tag": True,
        "items": [
            _item("NBDpsy-亲密关系 关注", 1250.0, 500.0),
            _item("NBDpsy-亲密关系", 1230.0, 500.0),
            _item("关注", 1340.0, 500.0),
            _item(f"越用力要爱 他越冷漠 | {tag} 发条消息过去…", 1250.0, 525.0),
            _item("展开", 1360.0, 540.0),
            _item("编辑于 刚刚·公开可见", 1240.0, 552.0),
            _item("编辑于", 1200.0, 552.0),
            _item("刚刚", 1240.0, 552.0),
            _item("·", 1260.0, 552.0),
            _item("公开可见", 1290.0, 552.0),
        ],
    }


def real_dropdown_layer(tag="投射性认同", editor_rect=VIDEO_EDITOR_RECT):
    """真话题下拉:挂在正文框光标下方,行里是话题名 + 浏览量统计文案。"""
    x = editor_rect["x"] + 60.0
    y = editor_rect["y"] + editor_rect["height"] + 10.0
    return {
        "cls": "topic-container",
        "rect": {"x": x, "y": y, "width": 320.0, "height": 240.0},
        "has_tag": True,
        "items": [
            _item(f"#{tag}", x + 100, y + 20),
            _item(f"#{tag} 1.2万次浏览", x + 150, y + 20),
            _item("#投射性认同的怪圈 3563人参与讨论", x + 150, y + 60),
            _item("#亲密关系 8.7万次浏览", x + 150, y + 100),
        ],
    }


# ── 主场景:视频页那种"抓到 base-info 假浮层" ──

def test_video_preview_layer_is_not_taken_as_dropdown():
    """预览面板的作者信息区**不是**话题下拉:必须判定位失败,而不是往上点一下。"""
    out = select_topic_option(
        {"layers": [video_preview_layer()]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["success"] is False
    assert out["reason"] == "topic_dropdown_not_found", out


def test_video_preview_layer_failure_is_distinct_from_word_absent():
    """fail-loud:"抓错容器"绝不能再糊成 no_exact_match —— 两者处置完全不同。"""
    out = select_topic_option(
        {"layers": [video_preview_layer()]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["reason"] != "no_exact_match"


def test_video_preview_rejected_without_class_blacklist():
    """平台把 ``base-info`` 改个名也照样拒 —— 黑名单只是兜底,主判据是几何锚定。"""
    layer = video_preview_layer()
    layer["cls"] = "note-author-8f3a2c"      # 改名后黑名单命不中

    out = select_topic_option(
        {"layers": [layer]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["reason"] == "topic_dropdown_not_found", out


def test_mirrored_tag_text_in_preview_is_never_clicked():
    """预览面板镜像出**一模一样**的 ``#话题`` 文本时,也绝不能拿它的坐标去点。

    这是"面积最小"最凶的下场:点在右侧预览面板上,话题没加成,还乱点了页面。
    """
    layer = video_preview_layer()
    layer["items"].append(_item("#投射性认同", 1250.0, 530.0))   # 镜像出的精确同名文本

    out = select_topic_option(
        {"layers": [layer]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["success"] is False, "镜像文本被当成下拉选项点了"


def test_video_forensics_still_reports_the_layer_we_saw():
    """取证字段一个不少:正是它们(候选/条数/class)把真因指了出来。"""
    out = select_topic_option(
        {"layers": [video_preview_layer()]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["layer_class"] == "base-info"
    assert out["item_count"] == 10
    assert "关注" in out["candidates"]
    assert out["layers_seen"] == 1
    assert out["rejected_classes"] == ["base-info"]


# ── 真下拉要命中 ──

def test_real_dropdown_is_matched_exactly():
    """真下拉里的精确同名行:命中并交出该行坐标。"""
    layer = real_dropdown_layer()
    out = select_topic_option({"layers": [layer]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT)

    assert out["success"] is True
    assert out["matched"] == "#投射性认同"
    assert (out["x"], out["y"]) == (layer["items"][0]["x"], layer["items"][0]["y"])


def test_real_dropdown_wins_over_smaller_preview_layer():
    """两层同时在:预览面板更小(旧启发式的赢家),但必须选真下拉。"""
    preview = video_preview_layer()
    dropdown = real_dropdown_layer()
    preview_area = preview["rect"]["width"] * preview["rect"]["height"]
    dropdown_area = dropdown["rect"]["width"] * dropdown["rect"]["height"]
    assert preview_area < dropdown_area, "夹具没还原'假浮层更小'这个前提"

    out = select_topic_option(
        {"layers": [preview, dropdown]}, "投射性认同", editor_rect=VIDEO_EDITOR_RECT
    )

    assert out["success"] is True
    assert out["x"] == dropdown["items"][0]["x"], "点到预览面板上去了"


def test_prefix_match_with_stats_suffix_still_works():
    """第二轮判据(话题名 + 统计文案)原样保留 —— 图文 171 次成功里就靠它。"""
    layer = real_dropdown_layer(editor_rect=IMAGE_EDITOR_RECT)
    layer["items"] = [_item("#心理科普 2.3万次浏览", 800.0, 1000.0)]

    out = select_topic_option({"layers": [layer]}, "心理科普", editor_rect=IMAGE_EDITOR_RECT)

    assert out["success"] is True and out["matched"] == "#心理科普 2.3万次浏览"


def test_partial_prefix_of_a_longer_topic_is_not_matched():
    """禁止残缺前缀误配:搜「心理」不能选中「心理咨询师」。"""
    layer = real_dropdown_layer(editor_rect=IMAGE_EDITOR_RECT)
    layer["items"] = [_item("#心理咨询师 5万次浏览", 800.0, 1000.0),
                      _item("#心理学 1万次浏览", 800.0, 1040.0)]

    out = select_topic_option({"layers": [layer]}, "心理", editor_rect=IMAGE_EDITOR_RECT)

    assert out["success"] is False and out["reason"] == "no_exact_match"


def test_exact_match_beats_prefix_match_across_layers():
    """精确相等在**所有**候选层里优先 —— 不再"挑一层然后一条路走到黑"。"""
    stats_layer = real_dropdown_layer(editor_rect=IMAGE_EDITOR_RECT)
    stats_layer["items"] = [_item("#心理科普 2.3万次浏览", 800.0, 1000.0)]
    exact_layer = real_dropdown_layer(editor_rect=IMAGE_EDITOR_RECT)
    exact_layer["cls"] = "topic-list"
    exact_layer["items"] = [_item("#心理科普", 810.0, 1200.0),
                            _item("#心理科普日常 1万次浏览", 810.0, 1240.0)]

    out = select_topic_option(
        {"layers": [stats_layer, exact_layer]}, "心理科普", editor_rect=IMAGE_EDITOR_RECT
    )

    assert out["success"] is True and out["matched"] == "#心理科普"


# ── 图文既有路径不回归 ──

def test_image_note_dropdown_matches_under_image_page_geometry():
    """图文页几何(正文栏 x≈717..1580)下,真下拉照样命中。"""
    layer = real_dropdown_layer("我的家庭简史", editor_rect=IMAGE_EDITOR_RECT)

    out = select_topic_option({"layers": [layer]}, "我的家庭简史", editor_rect=IMAGE_EDITOR_RECT)

    assert out["success"] is True


def test_image_note_preview_panel_is_rejected_too():
    """图文页的预览面板在更右侧(x≈1645+),同样不能被当成下拉。"""
    layer = video_preview_layer("我的家庭简史")
    layer["rect"] = {"x": 1645.0, "y": 300.0, "width": 300.0, "height": 60.0}

    out = select_topic_option(
        {"layers": [layer]}, "我的家庭简史", editor_rect=IMAGE_EDITOR_RECT
    )

    assert out["reason"] == "topic_dropdown_not_found"


def test_dropdown_present_but_word_absent_is_no_exact_match():
    """下拉在、词不在 —— 这才是 no_exact_match,和"没找到下拉"分得干干净净。"""
    layer = real_dropdown_layer("别的话题", editor_rect=IMAGE_EDITOR_RECT)

    out = select_topic_option({"layers": [layer]}, "查无此词", editor_rect=IMAGE_EDITOR_RECT)

    assert out["success"] is False
    assert out["reason"] == "no_exact_match"
    assert out["candidates"], "下拉里看到什么必须带出来"


def test_reason_split_keys_on_candidates_emptiness():
    """缺陷3 核心不变量:candidates 空 → topic_dropdown_not_shown(浮层没弹,别换词);
    candidates 非空无匹配 → no_exact_match(真没这词,才换词)。两条断言有牙,互为对照。"""
    # 空浮层:candidates 必空,reason 必是"没弹"
    shown = select_topic_option({"layers": []}, "失眠", editor_rect=IMAGE_EDITOR_RECT)
    assert shown["reason"] == "topic_dropdown_not_shown"
    assert not shown["candidates"]

    # 真下拉在但没这词:candidates 必非空,reason 必是"换词"
    layer = real_dropdown_layer("别的话题", editor_rect=IMAGE_EDITOR_RECT)
    absent = select_topic_option({"layers": [layer]}, "失眠", editor_rect=IMAGE_EDITOR_RECT)
    assert absent["reason"] == "no_exact_match"
    assert absent["candidates"]


def test_no_layers_at_all_is_topic_dropdown_not_shown():
    """页面上压根没有浮层(candidates 空)→ topic_dropdown_not_shown(定位/输入问题,别换词)。

    改名自 no_floating_layer(2026-08-09 缺陷3):调用方按 candidates 空/非空区分
    "浮层没弹(反馈)"vs"真没这词(换词)",这条正是"浮层没弹"的判据锚点。
    """
    out = select_topic_option({"layers": []}, "心理科普", editor_rect=IMAGE_EDITOR_RECT)

    assert out["reason"] == "topic_dropdown_not_shown"
    assert out["candidates"] == [] and out["item_count"] == 0


# ── 空壳浮层:通过判据但一个选项都没有 ──

def empty_shell_layer(cls="suffix", editor_rect=IMAGE_EDITOR_RECT, size=(120.0, 40.0)):
    """通过几何锚定、但**一个子项都没有**的空壳浮层 —— 真号回执实拍的那一层。

    2026-08-09 补话题回执样本:``reason=no_exact_match candidates=[] item_count=0
    layer_class="suffix" layers_seen=14``。平台的下拉外壳先挂进 DOM、选项内容异步填,
    内容还没到就是这副样子;它被判据放行(几何上确实在正文栏那一列),旧代码于是把它
    当 ``accepted[0]`` 收下,报出一个"平台没这词"—— 实际是**浮层还没弹**。
    """
    return {
        "cls": cls,
        "rect": {"x": editor_rect["x"] + 40.0, "y": editor_rect["y"] + 300.0,
                 "width": size[0], "height": size[1]},
        "has_tag": False,
        "items": [],
    }


def test_empty_accepted_layer_is_dropdown_not_shown():
    """候选层全是 0 选项的空壳 → topic_dropdown_not_shown(浮层没弹),**绝不是**没这词。

    牙口:把这条新分支拿掉,reason 立刻退回 no_exact_match —— 调用方会据此换词,
    而真正该做的是反馈"浮层没弹"让我们修。
    """
    out = select_topic_option(
        {"layers": [empty_shell_layer()]}, "失眠", editor_rect=IMAGE_EDITOR_RECT
    )

    assert out["success"] is False
    assert out["reason"] == "topic_dropdown_not_shown"
    assert out["candidates"] == [] and out["item_count"] == 0


def test_empty_accepted_layer_still_reports_which_shell_we_saw():
    """空壳照样进取证:layer_class / layers_seen 得带回来,否则下次仍是黑箱。

    生产样本正是靠 ``layer_class="suffix"`` 才认出"抓到的是个空外壳"。
    """
    out = select_topic_option(
        {"layers": [empty_shell_layer(), empty_shell_layer(cls="suffix-2", size=(300.0, 90.0))]},
        "失眠", editor_rect=IMAGE_EDITOR_RECT,
    )

    assert out["reason"] == "topic_dropdown_not_shown"
    assert out["layer_class"] == "suffix"       # 排最前那个空壳
    assert out["layers_seen"] == 2


def test_forensics_come_from_the_layer_that_has_options():
    """空壳与带选项的层并存、词又没匹配上 → no_exact_match,取证必须取**带选项**那层。

    夹具刻意让空壳排在候选序列**前面**(两层的话题行数都是 0 时按面积排,空壳更小),
    这正是生产那次 candidates 空的成因:取 ``accepted[0]`` 抓到空壳,回执等于什么都没说。
    """
    shell = empty_shell_layer()
    options = {
        "cls": "topic-list",
        "rect": {"x": IMAGE_EDITOR_RECT["x"] + 40.0, "y": 900.0, "width": 320.0, "height": 240.0},
        "has_tag": False,
        "items": [_item("创建话题", 800.0, 940.0), _item("试试其他关键词", 800.0, 980.0)],
    }
    assert (shell["rect"]["width"] * shell["rect"]["height"]
            < options["rect"]["width"] * options["rect"]["height"]), "夹具没还原'空壳排更前'"

    out = select_topic_option(
        {"layers": [shell, options]}, "查无此词", editor_rect=IMAGE_EDITOR_RECT
    )

    assert out["reason"] == "no_exact_match", "有选项在,这才是真·没这词"
    assert out["layer_class"] == "topic-list"
    assert out["item_count"] == 2
    assert "创建话题" in out["candidates"], "取证取到空壳上去了,回执里什么都没说"


def test_missing_payload_does_not_blow_up():
    """采集 JS 没回东西时,判据交明确失败,不抛异常打断整条发布。"""
    assert select_topic_option(None, "心理科普")["reason"] == "topic_dropdown_not_shown"


# ── 拿不到正文框几何时的兜底 ──

def test_without_editor_rect_structure_judgement_takes_over():
    """正文框几何拿不到(bounding_box 返 None)时退回结构判据:像下拉的才放行。"""
    out = select_topic_option({"layers": [real_dropdown_layer()]}, "投射性认同", editor_rect=None)

    assert out["success"] is True


def test_without_editor_rect_preview_layer_still_rejected():
    """兜底路径下预览面板照样拒(它的子项没一条长得像话题选项)。"""
    layer = video_preview_layer()
    layer["cls"] = "note-author-8f3a2c"      # 连黑名单也不给它

    out = select_topic_option({"layers": [layer]}, "投射性认同", editor_rect=None)

    assert out["reason"] == "topic_dropdown_not_found"


# ── 判据零件 ──

@pytest.mark.parametrize("text, expected", [
    ("#投射性认同", True),
    ("投射性认同 1.2万次浏览", True),
    ("投射性认同 3563人参与讨论", True),
    ("NBDpsy-亲密关系 关注", False),
    ("编辑于 刚刚·公开可见", False),
    ("展开", False),
    ("", False),
    ("#" + "长" * 60, False),          # 超长文案不是话题行
])
def test_looks_like_topic_option(text, expected):
    assert looks_like_topic_option(text) is expected


def test_is_dropdown_like_needs_two_topic_rows():
    assert is_dropdown_like(real_dropdown_layer()) is True
    assert is_dropdown_like(video_preview_layer()) is False


def test_is_anchored_to_editor_uses_horizontal_column():
    """锚定判据看的是"浮层水平中心在不在正文栏这一列"。"""
    assert is_anchored_to_editor(real_dropdown_layer(), VIDEO_EDITOR_RECT) is True
    assert is_anchored_to_editor(video_preview_layer(), VIDEO_EDITOR_RECT) is False
    assert is_anchored_to_editor(real_dropdown_layer(), None) is False
