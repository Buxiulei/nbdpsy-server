"""去水印编排 postprocess.dewatermark 单测(不起浏览器、零网络)。

契约锚(2026-07-26 收严):去水印**只有 reraster 一条路**——
- 成功 → 返回 reraster 产物路径;
- 失败 → 返回 None(**不再**退回原图、**不再**走"只剥元数据不动像素"的 PIL 兜底,
  因为那种产物的像素与原图完全一致,在 AI 检测上等同交原图,只会掩盖失败)。
"""

from app.imagegen import postprocess
from app.imagegen.reraster import ReRasterResult


def _src(tmp_path):
    """造一个真实存在的源图文件(内容不重要,dewatermark 只做 isfile 判定)。"""
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNGfake")
    return p


async def test_dewatermark_returns_reraster_output(tmp_path, monkeypatch):
    """reraster 成功 → 返回其产物路径(不是原图路径)。"""
    src = _src(tmp_path)
    shot = tmp_path / "img.shot.jpg"
    shot.write_bytes(b"\xff\xd8reraster")

    async def fake_reraster(path):
        assert path == str(src)
        return ReRasterResult(True, str(shot))

    monkeypatch.setattr(postprocess, "reraster_image", fake_reraster)

    out = await postprocess.dewatermark(str(src))
    assert out == str(shot)


async def test_dewatermark_returns_none_on_reraster_failure(tmp_path, monkeypatch):
    """reraster 失败 → None,且不产生 PIL 兜底产物 .clean.jpg(该兜底已删)。"""
    src = _src(tmp_path)

    async def fake_reraster(path):
        return ReRasterResult(False, path, error="chromium 起不来")

    monkeypatch.setattr(postprocess, "reraster_image", fake_reraster)

    out = await postprocess.dewatermark(str(src))
    assert out is None
    # 旧 ② 级兜底(只剥元数据、像素不动)必须已经不存在
    assert not (tmp_path / "img.clean.jpg").exists()
    # 原图本身不动(调用方另有原图提取通道)
    assert src.is_file()


async def test_dewatermark_missing_source_returns_none(tmp_path, monkeypatch):
    """源文件不存在 → None(旧版会原样退回该路径,现在算失败)。"""
    async def boom(path):  # 压根不该走到 reraster
        raise AssertionError("源文件不存在时不应调用 reraster")

    monkeypatch.setattr(postprocess, "reraster_image", boom)

    assert await postprocess.dewatermark(str(tmp_path / "nope.png")) is None
    assert await postprocess.dewatermark("") is None
