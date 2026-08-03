"""已发布笔记编辑纯逻辑层单测(设计 7.1「纯函数」那一档)。

锁的是四件事,每件都有对应的事故/证据背书:

- **删除下标必须降序**(设计 4.2③):删一张后面的序号左移,升序删就删错图,而误删不可逆;
- **图数等式**(设计 3.2):``image_add`` / ``image_remove`` 共用同一条判据,别各算各的;
- **话题提取**(设计附录 B / E9 实证:chip 的 innerText 就是 ``#xx[话题]#`` 纯文本);
- **回读判据宁可漏报不可谎报**:正文用前缀(活动会往末尾追话题),但空目标不许恒真。

本文件不碰浏览器,也不需要页面夹具 —— 被测模块本身就是零 DOM 假设的那一半。
"""

import pytest

from app.browser import note_editing as ne


# ---------------- plan_image_removal:去重 + 降序 ----------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, expected",
    [
        ([], []),                          # 空:没请求删除
        ([3], [3]),                        # 单元素
        ([1, 2, 3], [3, 2, 1]),            # 升序输入 → 翻成降序
        ([3, 2, 1], [3, 2, 1]),            # 逆序输入 → 原样
        ([2, 1, 3], [3, 2, 1]),            # 乱序
        ([2, 2, 2], [2]),                  # 全重复 → 只删一次
        ([1, 3, 1, 3, 2], [3, 2, 1]),      # 重复混乱序
        ([1, 18], [18, 1]),                # 图数上限边界
    ],
)
def test_plan_image_removal_dedupes_and_sorts_desc(raw, expected):
    assert ne.plan_image_removal(raw) == expected


@pytest.mark.unit
def test_plan_image_removal_does_not_mutate_input():
    """调用方(编排层)后面还要拿原列表算 removed 计数/上报,别被就地改了。"""
    raw = [1, 3, 2]
    ne.plan_image_removal(raw)
    assert raw == [1, 3, 2]


@pytest.mark.unit
def test_plan_image_removal_descending_is_the_point():
    """降序不是审美:升序删第 1 张后,原第 3 张已左移成第 2 张。

    这条用例存在的意义是防止将来有人"顺手"把 reverse 去掉 —— 断言写成"最大的先删"。
    """
    plan = ne.plan_image_removal([1, 2, 3])
    assert plan[0] == max(plan)
    assert plan == sorted(plan, reverse=True)


# ---------------- 图数等式 ----------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "before, removed, added, expected",
    [
        (6, 0, 0, 6),      # 什么都没动(混合请求里只改了文本)
        (6, 1, 0, 5),      # 只删一张
        (6, 0, 2, 8),      # 只追加
        (6, 2, 3, 7),      # 删加同时
        (1, 0, 17, 18),    # 追加到上限
        (18, 17, 0, 1),    # 删到只剩一张(剩余 ≥1 的边界)
    ],
)
def test_image_count_equation(before, removed, added, expected):
    assert ne.image_count_equation(before, removed, added) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "current, remove, add, expected",
    [
        (6, [], [], 6),
        (6, [2], [], 5),
        (6, [], ["a", "b"], 8),
        (6, [1, 3], ["a"], 5),
        (6, [3, 1], ["a"], 5),          # 顺序不影响预测
        (6, [2, 2, 2], [], 5),          # 重复下标只算一张(去重口径与 plan 一致)
        (6, [1, 2, 1, 2], ["a"], 5),
    ],
)
def test_surviving_count(current, remove, add, expected):
    assert ne.surviving_count(current, remove, add) == expected


@pytest.mark.unit
def test_surviving_count_uses_same_equation_as_readback():
    """计划态预测与回读判据必须是同一条等式 —— 两边算法漂移会让"删成了却报没生效"。"""
    current, remove, add = 6, [1, 3], ["x", "y"]
    plan = ne.plan_image_removal(remove)
    assert ne.surviving_count(current, remove, add) == ne.image_count_equation(
        current, len(plan), len(add)
    )


@pytest.mark.unit
def test_surviving_count_counts_added_images_too():
    """总数含追加:设计里"剩余 ≥1"那条红线口径是**原图**剩余,不是本函数返回值。

    锁住这个语义,免得将来有人拿它去当 ≥1 校验用 —— 追加可能失败,失败后就是删光。
    """
    assert ne.surviving_count(1, [1], ["new"]) == 1   # 原图已被删光,总数却是 1


# ---------------- extract_topics ----------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "body, expected",
    [
        ("", []),
        ("   ", []),
        ("今天聊聊焦虑,没有话题", []),
        ("正文 #身边的心理学[话题]#", ["身边的心理学"]),
        (
            "正文 #身边的心理学[话题]# #情绪管理[话题]#",
            ["身边的心理学", "情绪管理"],
        ),
        # 话题名含数字/英文/中英混排(平台上很常见)
        ("#CPTSD[话题]# #2026心理年报[话题]#", ["CPTSD", "2026心理年报"]),
        ("#ADHD成人[话题]#", ["ADHD成人"]),
        # 首尾空白归一到实体名上
        ("#  留学生心理  [话题]#", ["留学生心理"]),
        # 空实体名不算话题
        ("#[话题]#", []),
        ("#   [话题]#", []),
        # 形似但不是实体:缺后缀 / 缺井号 / 普通标签
        ("#焦虑#", []),
        ("#焦虑[话题]", []),
        ("焦虑[话题]#", []),
        # 正文里带话题也带普通井号标签,只取实体
        ("#日常# 正文 #职场倦怠[话题]#", ["职场倦怠"]),
        # 换行分隔
        ("第一段\n#依恋修复[话题]#\n第二段 #亲密关系[话题]#",
         ["依恋修复", "亲密关系"]),
    ],
)
def test_extract_topics(body, expected):
    assert ne.extract_topics(body) == expected


@pytest.mark.unit
def test_extract_topics_keeps_duplicates_in_order():
    """如实上报:同名话题出现两次就报两次,``topics_dropped`` 是"丢了什么"的流水,不是集合。"""
    body = "#焦虑[话题]# 中间 #焦虑[话题]#"
    assert ne.extract_topics(body) == ["焦虑", "焦虑"]


@pytest.mark.unit
def test_extract_topics_tolerates_none():
    """回读拿不到正文时给的是 None,判据函数不许在这儿炸。"""
    assert ne.extract_topics(None) == []


# ---------------- content_prefix_ok ----------------


@pytest.mark.unit
def test_content_prefix_ok_exact_match():
    assert ne.content_prefix_ok("新的正文内容", "新的正文内容") is True


@pytest.mark.unit
def test_content_prefix_ok_activity_topic_appended():
    """活动步把话题追加到正文末尾 —— 全等会误判成没生效,这正是用前缀的原因。"""
    target = "新的正文内容"
    read_back = "新的正文内容 #身边的心理学[话题]#"
    assert ne.content_prefix_ok(read_back, target) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "read_back, target",
    [
        ("新的正文", "新的正文内容"),        # 回读比目标短(写了一半 / 被截断)
        ("", "新的正文内容"),                 # 完全没写进去
        ("旧的正文内容", "新的正文内容"),      # 压根没换
        ("前缀 新的正文内容", "新的正文内容"),  # 目标出现在中间不算"以它开头"
    ],
)
def test_content_prefix_ok_rejects(read_back, target):
    assert ne.content_prefix_ok(read_back, target) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "read_back, target",
    [
        ("新的  正文\n内容 #话题名[话题]#", "新的 正文 内容"),  # 多空格 / 换行归一
        ("  新的正文内容  ", "新的正文内容"),                    # 首尾空白
        ("新的正文内容", "  新的正文内容  "),                    # 目标侧带空白
    ],
)
def test_content_prefix_ok_normalizes_whitespace(read_back, target):
    assert ne.content_prefix_ok(read_back, target) is True


@pytest.mark.unit
@pytest.mark.parametrize("target", ["", "   ", "\n\t"])
def test_content_prefix_ok_empty_target_is_false(target):
    """空目标下 startswith 恒真 —— 宁可漏报不可把"什么都没写进去"谎报成生效。"""
    assert ne.content_prefix_ok("任意回读内容", target) is False


@pytest.mark.unit
def test_content_prefix_ok_tolerates_none_read_back():
    """回读失败给 None:判成没生效(调用方按 false / null 三态处理),不炸。"""
    assert ne.content_prefix_ok(None, "新的正文内容") is False


@pytest.mark.unit
def test_content_prefix_ok_chinese_english_digit_mix():
    target = "CPTSD 与 2026 年的自我照顾"
    assert ne.content_prefix_ok(f"{target} 后面还有 #心理[话题]#", target) is True


# ---------------- title_length_ok ----------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "title, ok",
    [
        ("", True),                 # 清空标题是合法显式意图(设计 3.1)
        ("短标题", True),
        ("一" * 20, True),           # 恰好上限
        ("一" * 21, False),          # 超一个字
        ("a" * 20, True),
        ("a" * 21, False),
        ("焦虑自救指南2026版ABC", True),   # 中英数混排
    ],
)
def test_title_length_ok(title, ok):
    assert ne.title_length_ok(title) is ok


@pytest.mark.unit
def test_title_length_ok_counts_emoji_like_platform():
    """emoji 度量必须与 REST 层同一份实现(get_display_length:len + 可见 emoji 个数)。

    否则入口 422 放行的标题会在浏览器层被判超长(或反之)。
    """
    from app.browser.text_formatter import get_display_length

    title = "🏃‍♀️5分钟缓解焦虑"
    assert ne.title_length_ok(title) is (get_display_length(title) <= 20)
    # 19 个中文 + 一个 emoji 序列(额外 +1)= 超限
    assert ne.title_length_ok("一" * 19 + "😀") is False


@pytest.mark.unit
def test_title_length_ok_never_truncates():
    """只判不截 —— 编辑场景截断 = 替调用方改错内容(全量覆盖提交,改错要人工改回)。"""
    long_title = "一" * 30
    assert ne.title_length_ok(long_title) is False
    assert len(long_title) == 30   # 入参没被动过
