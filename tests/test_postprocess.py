"""去水印编排 postprocess.dewatermark / dewatermark_all 单测(不起浏览器、零网络)。

契约锚(2026-07-26 收严):去水印**只有 reraster 一条路**——
- 成功 → 返回 reraster 产物路径;
- 失败 → 返回 None(**不再**退回原图、**不再**走"只剥元数据不动像素"的 PIL 兜底,
  因为那种产物的像素与原图完全一致,在 AI 检测上等同交原图,只会掩盖失败)。

批量入口 ``dewatermark_all``(发布口 fail-closed 闸)契约:等长同序返回清洗后路径;
任一张失败即抛 RuntimeError(文案带页序),绝不静默退回原图。
"""

import pytest

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


# ---------------- 批量闸 dewatermark_all(发布口 fail-closed) ----------------


async def test_dewatermark_all_keeps_order_and_length(monkeypatch):
    """全成功:返回等长列表,且第 i 位对应第 i 张输入(页序不能乱)。"""
    seen = []

    async def fake_dewatermark(path):
        seen.append(path)
        return f"{path}.shot.jpg"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)

    out = await postprocess.dewatermark_all(["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"])
    assert out == ["/w/img_00.png.shot.jpg", "/w/img_01.png.shot.jpg", "/w/img_02.png.shot.jpg"]
    assert seen == ["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"]


async def test_dewatermark_all_raises_on_any_failure(monkeypatch):
    """任一张失败即抛 RuntimeError(带页序 + 拒发声明),绝不返回含原图的列表。"""

    async def fake_dewatermark(path):
        return None if path.endswith("img_01.png") else f"{path}.shot.jpg"

    monkeypatch.setattr(postprocess, "dewatermark", fake_dewatermark)

    with pytest.raises(RuntimeError) as exc:
        await postprocess.dewatermark_all(["/w/img_00.png", "/w/img_01.png", "/w/img_02.png"])
    assert "第 2 页" in str(exc.value)
    assert "拒绝发布未去水印的图" in str(exc.value)


async def test_dewatermark_all_empty_list_is_noop(monkeypatch):
    """空图列表:直接返回空,不触碰去水印(纯文字发布不该起浏览器)。"""

    async def boom(path):
        raise AssertionError("空列表不应调用去水印")

    monkeypatch.setattr(postprocess, "dewatermark", boom)

    assert await postprocess.dewatermark_all([]) == []


async def test_reraster_accepts_relative_path(monkeypatch, tmp_path):
    """回归钉:reraster 必须能吃**相对路径**——生产发布口传的就是相对路径。

    2026-07-27 09:00 那批定时发布被去水印闸全数拦下,真图是好的,炸的是这里:
    ``Path(path).as_uri()`` 对相对路径直接抛 "relative path can't be expressed as a
    file URI"。生图侧一直传绝对路径所以从没暴露,而 account_worker 的 workdir 是
    ``settings.UPLOAD_DIR`` 下的相对路径(如 data/uploads/job_25/img_00.png)。

    **当初的实测之所以放过它,就是因为用了 tempfile.mkdtemp() 的绝对路径**——
    测试没还原生产的路径形态。这里显式钉住相对路径这一形态。
    不起真浏览器:把 guarded_chromium 换成抛哨兵异常,只验"走过了 URI 构造这一步"。
    """
    from PIL import Image

    import app.imagegen.playwright_guard as guard
    from app.imagegen import reraster

    img_dir = tmp_path / "rel_case"
    img_dir.mkdir()
    Image.new("RGB", (8, 8), "white").save(img_dir / "img_00.png")

    class _Sentinel(Exception):
        pass

    def fake_guarded_chromium(*_a, **_kw):
        raise _Sentinel("到这里就说明 URI 构造已通过")

    monkeypatch.setattr(guard, "guarded_chromium", fake_guarded_chromium)
    monkeypatch.chdir(tmp_path)  # 复刻生产:cwd 为基准,传相对路径

    result = await reraster.reraster_image("rel_case/img_00.png")

    assert result.success is False  # 浏览器被哨兵拦掉,预期失败
    # 关键断言:失败原因不能是相对路径 URI 那条——那说明又退回了老 bug
    assert "relative path" not in (result.error or "")
