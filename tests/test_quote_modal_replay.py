"""用**真号实拍夹具**回放引用弹窗:钉死"下标同序"假设的证伪与现行修法的正确性。

这组用例与 ``test_note_components.py`` 里那些**不是重复**,区别恰恰是本文件存在的理由:

- 那边用**手写**的假页面 —— 手写时人自然照着代码的假设写,于是假设错了测试也跟着错。
  引用功能 100% 失败期间,1615 个单测全绿,就是这么来的;
- 这边的顺序、文案、数量来自 ``scripts/capture_page_scene.py`` 真号只读采集,
  **不是我想象出来的**。

## 夹具揭示的机制(采集之前谁都没想到)

接口返回 21 条,弹窗只渲染 20 张卡,且**从第 3 条起整体错位一格**。缺的那条正是
**当前正在编辑的这篇笔记** —— 弹窗会把它排除掉(不能自己引用自己),于是它之后的每张卡
下标全部偏移。

所以"响应第 i 条 ↔ 第 i 张卡"不是"偶尔不准",是**只要被编辑的笔记不在列表末尾就必然错**。
这也解释了为什么引用功能是 100% 失败而不是间歇失败。
"""

import pytest

from app.browser import note_components as nc
from tests.page_replay import ReplayPage, load_scene

_SCENE = "quote_modal"


@pytest.fixture
def scene():
    return load_scene(_SCENE)


def _cards(scene):
    return scene["dom"][nc._QUOTE_NOTE_CARD]


def _api_notes(scene):
    notes = []
    for rec in scene["api"]:
        if nc._POSTED_API_MARK in rec["url"]:
            notes.extend(((rec.get("body") or {}).get("data") or {}).get("notes") or [])
    return notes


def test_fixture_falsifies_index_assumption(scene):
    """夹具本身就是"同序假设不成立"的证据 —— 这条红了说明该重新采集夹具。"""
    cards, notes = _cards(scene), _api_notes(scene)
    assert cards and notes, "夹具没采到候选卡或接口响应"

    mismatched = 0
    for i in range(min(len(cards), len(notes))):
        title = nc._norm((notes[i] or {}).get("display_title"))
        if title and title not in nc._norm(cards[i]["text"]):
            mismatched += 1

    assert mismatched > 0, (
        "这份夹具里下标恰好对得上 —— 但线上确凿失败过。"
        "多半是采集时选的笔记正好排在列表末尾(它被排除后不影响其余下标),"
        "换一篇靠前的笔记重采。"
    )


def test_edited_note_is_excluded_from_candidates(scene):
    """机制:弹窗排除**当前正在编辑的那篇**,这正是下标错位的来源。

    钉住它是因为——只要还有人想"按下标取卡",这条会立刻提醒他为什么不行。
    """
    cards, notes = _cards(scene), _api_notes(scene)
    edited = scene["source_note_id"]

    assert any(str((n or {}).get("id")) == edited for n in notes), "接口里应含被编辑的那篇"
    edited_title = nc._norm(next(
        n.get("display_title") for n in notes if str((n or {}).get("id")) == edited
    ))
    rendered = [nc._norm(c["text"]) for c in cards]
    assert not any(edited_title in t for t in rendered), (
        f"被编辑的笔记「{edited_title}」不该出现在候选卡里"
    )
    assert len(cards) == len(notes) - 1, "候选卡应恰好比接口少一条(少的就是被编辑的那篇)"


def test_title_matching_picks_the_right_card_on_real_layout(scene):
    """现行修法(按标题在全部卡里找)在真实布局上确实选中正确那张。

    直接复刻 ``_set_quote_in_modal`` 的选卡逻辑:对每一篇**可引用**的笔记都验一遍,
    每篇都必须唯一命中,且命中的卡文案确实含该标题。
    """
    cards, notes = _cards(scene), _api_notes(scene)
    edited = scene["source_note_id"]
    checked = 0

    for note in notes:
        if str((note or {}).get("id")) == edited:
            continue                      # 被编辑的那篇本就不在候选里
        title = nc._norm(note.get("display_title"))
        if not title:
            continue                      # 空标题走「他人笔记」按 id 检索,不归这条路
        hits = [c for c in cards if title in nc._norm(c["text"])]
        assert len(hits) == 1, f"「{title}」命中 {len(hits)} 张(要求恰好 1 张)"
        checked += 1

    assert checked >= 10, f"只验了 {checked} 篇,夹具样本太小说明不了问题"


def test_replay_page_serves_recorded_dom_and_api(scene):
    """回放层本身可用:选择器按实拍返回,接口响应能喂进被动监听那条通路。"""
    page = ReplayPage(scene)

    assert len(page.query_selector_all(nc._QUOTE_NOTE_CARD)) == len(_cards(scene))
    assert page.query_selector(nc._QUOTE_NOTE_CARD) is not None
    # 夹具里没有的选择器返回空 —— 绝不猜(猜就退回手写假页面那条老路)
    assert page.query_selector_all(".never-captured-selector") == []

    responses = nc.ComponentResponses()
    responses.attach(page)
    sent = page.emit_recorded(nc._POSTED_API_MARK)

    assert sent > 0
    assert responses.count(nc._POSTED_API_MARK) == sent


def test_fixture_has_no_live_credentials(scene):
    """夹具是长期资产,不许带会过期的凭据(采集时 xsec_token 已抹除)。"""
    import json

    blob = json.dumps(scene, ensure_ascii=False)
    assert "xsec_token=" not in blob or "xsec_token=SCRUBBED" in blob
    for rec in scene["api"]:
        for note in ((rec.get("body") or {}).get("data") or {}).get("notes") or []:
            assert note.get("xsec_token") in (None, "SCRUBBED")
