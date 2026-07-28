"""去水印编排 postprocess.dewatermark / dewatermark_all 单测(纯 PIL、不起浏览器、零网络)。

契约锚(2026-07-28 换代):去水印 = **非整数缩小 0.855 + 存 PNG**——
- **无标记图**(全新图 / 任何外源图,**不管尺寸/来源**)→ 缩到 round(*0.855) 的 PNG 新文件
  ({stem}.clean.png)并写入 ``_MARK_KEY`` 标记,返回新路径;
- **内容标记幂等**:输入已带 ``_MARK_KEY`` 标记(op_images 已缩过的图)→ 不二次缩,原样
  返回输入路径(发布口对同一张图重复调用不会把 0.855 复利成 0.731);
- 失败(文件不存在/打不开)→ None(fail-closed,绝不拿未处理图冒充交付)。

**为何从"尺寸感知"改成"内容标记"**:尺寸不是可靠判据——生产上 14%(job28 那批)是
1152×1536 等非 gpt 原生尺寸,旧的尺寸门会原样透传、带 C2PA 发出(事故复活)。改用内容
标记后无标记即缩,外源非标尺寸图也一律被处理,不再遗漏。

批量入口 ``dewatermark_all``(发布口 fail-closed 闸)契约不变:等长同序返回清洗后路径;
任一张失败即抛 RuntimeError(文案带页序),绝不静默退回原图。
"""

import pytest
from PIL import Image, PngImagePlugin

from app.imagegen import postprocess


def _make_img(path, size):
    """造一张真实的、**无标记**的纯色小图(内容不重要,用来模拟全新/外源图)。"""
    Image.new("RGB", size, (123, 200, 77)).save(str(path), format="PNG")
    return path


def _make_marked_img(path, size):
    """造一张带 ``_MARK_KEY`` 标记的 PNG(模拟 op_images 已缩过、已打标的图)。"""
    meta = PngImagePlugin.PngInfo()
    meta.add_text(postprocess._MARK_KEY, "1")
    Image.new("RGB", size, (123, 200, 77)).save(str(path), format="PNG", pnginfo=meta)
    return path


async def test_dewatermark_shrinks_new_unmarked_square(tmp_path):
    """全新无标记方图 1024×1024 → 缩到 round(*0.855)=876 的 PNG 新文件 + 打标,原图保留。"""
    src = _make_img(tmp_path / "p_00.png", (1024, 1024))

    out = await postprocess.dewatermark(str(src))

    assert out == str(tmp_path / "p_00.clean.png")
    with Image.open(out) as im:
        assert im.size == (876, 876)      # round(1024*0.855)=876
        assert im.format == "PNG"
        assert im.info.get(postprocess._MARK_KEY) == "1"   # 产物必带内容标记
    assert src.is_file()                  # 原图不动(op_images 另需归档 NN.orig)


async def test_dewatermark_shrinks_new_unmarked_portrait(tmp_path):
    """全新无标记竖图 1024×1536 → 876×1313(等比,比例精确不变) + 打标。"""
    src = _make_img(tmp_path / "p_00.png", (1024, 1536))

    out = await postprocess.dewatermark(str(src))

    with Image.open(out) as im:
        assert im.size == (876, 1313)     # round(1536*0.855)=1313
        assert im.info.get(postprocess._MARK_KEY) == "1"


@pytest.mark.parametrize("size,expected", [
    ((1152, 1536), (985, 1313)),   # job28 那批上传通道图:round(1152*0.855)=985 / round(1536*0.855)=1313
    ((768, 1344), (657, 1149)),    # Gemini 常见竖图:round(768*0.855)=657 / round(1344*0.855)=1149
])
async def test_dewatermark_processes_foreign_nonstandard_size(tmp_path, size, expected):
    """回归钉(本次 blocker):外源**非 gpt 三尺寸**无标记图必须被缩+打标,**不是透传**。

    旧的尺寸感知门只认 {1024×1536,1536×1024,1024×1024},对 1152×1536(生产 14%,job28)、
    768×1344(Gemini)原样透传、带 C2PA 发出——正是要解决的核心 case。内容标记判据下,
    这些无标记图一律被处理。
    """
    src = _make_img(tmp_path / "foreign.png", size)

    out = await postprocess.dewatermark(str(src))

    assert out == str(tmp_path / "foreign.clean.png")     # 产出新文件,非透传
    with Image.open(out) as im:
        assert im.size == expected                        # 缩到 round(*0.855)
        assert im.format == "PNG"
        assert im.info.get(postprocess._MARK_KEY) == "1"  # 且打了标


async def test_dewatermark_idempotent_on_marked_image(tmp_path):
    """内容标记幂等:已带标记的图(不管尺寸)→ 原样返回输入,不二次缩、不新建。"""
    # 用非缩后尺寸 1152×1536,确保透传是靠"标记"而非"尺寸",与旧尺寸门彻底区分。
    src = _make_marked_img(tmp_path / "img.png", (1152, 1536))

    out = await postprocess.dewatermark(str(src))

    assert out == str(src)                        # 原样透传输入路径
    assert not (tmp_path / "img.clean.png").exists()
    with Image.open(out) as im:
        assert im.size == (1152, 1536)            # 未被复利缩小


async def test_dewatermark_missing_source_returns_none(tmp_path):
    """源文件不存在 / 空路径 → None(fail-closed)。"""
    assert await postprocess.dewatermark(str(tmp_path / "nope.png")) is None
    assert await postprocess.dewatermark("") is None


async def test_dewatermark_unopenable_returns_none(tmp_path):
    """存在但不是合法图片 → None(PIL 打不开算失败,不抛异常)。"""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNGnot-a-real-image")

    assert await postprocess.dewatermark(str(bad)) is None


# ---------------- 批量闸 dewatermark_all(发布口 fail-closed) ----------------


async def test_dewatermark_all_keeps_order_and_length(monkeypatch):
    """全成功:返回等长列表,且第 i 位对应第 i 张输入(页序不能乱)。"""
    seen = []

    async def fake_dewatermark(path):
        seen.append(path)
        return f"{path}.clean.png"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)

    out = await postprocess.dewatermark_all(["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"])
    assert out == ["/w/img_00.png.clean.png", "/w/img_01.png.clean.png", "/w/img_02.png.clean.png"]
    assert seen == ["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"]


async def test_dewatermark_all_raises_on_any_failure(monkeypatch):
    """任一张失败即抛 RuntimeError(带页序 + 拒发声明),绝不返回含原图的列表。"""

    async def fake_dewatermark(path):
        return None if path.endswith("img_01.png") else f"{path}.clean.png"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)

    with pytest.raises(RuntimeError) as exc:
        await postprocess.dewatermark_all(["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"])
    assert "第 2 页" in str(exc.value)
    assert "拒绝发布未去水印的图" in str(exc.value)


async def test_dewatermark_all_empty_list_is_noop(monkeypatch):
    """空图列表:直接返回空,不触碰去水印(纯文字发布不该起去水印)。"""

    async def boom(path):
        raise AssertionError("空列表不应调用去水印")

    monkeypatch.setattr(postprocess, "dewatermark", boom)

    assert await postprocess.dewatermark_all([]) == []
