"""still_image 渲染器：模板填充（纯函数）+ 截图与出段冒烟。"""
from pathlib import Path

import pytest

from app.video.pipeline.remake import style
from app.video.pipeline.remake.renderers import still_image


def _scene(**kw):
    base = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "title_card",
            "renderer": "still_image", "content": {"title": "引言"},
            "transition": "fade"}
    base.update(kw)
    return base


class TestFillTemplate:
    pytestmark = pytest.mark.unit

    def test_title_card_contains_title_and_tokens(self):
        html = still_image._fill_template(_scene())
        assert "引言" in html
        assert style.CARD_BG in html            # 品牌底色进了模板
        # logo 由 muxer 水印层统一叠加，模板不得再烘 logo（防卡片段双 logo 重影）
        assert "data:image/png;base64," not in html
        assert 'class="logo"' not in html

    def test_text_card_contains_body(self):
        sc = _scene(type="text_card",
                    content={"title": "使用须知", "body": "正文内容"})
        html = still_image._fill_template(sc)
        assert "使用须知" in html and "正文内容" in html

    def test_html_escaped(self):
        sc = _scene(content={"title": "<b>x&y</b>"})
        html = still_image._fill_template(sc)
        assert "<b>x&y</b>" not in html and "&lt;b&gt;" in html


@pytest.mark.integration
@pytest.mark.slow
class TestRenderSmoke:
    # 真 Playwright 卡片截图 + 真 ffmpeg 出段（宿主 CI 跑 not slow，慢测本地跑）。
    @pytest.mark.asyncio
    async def test_render_card_segment(self, tmp_path):
        out = await still_image.render(_scene(), tmp_path / "card.mp4")
        import asyncio, json
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries",
            "format=duration:stream=width,height", "-of", "json", str(out),
            stdout=asyncio.subprocess.PIPE)
        raw, _ = await proc.communicate()
        info = json.loads(raw)
        assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.2)
        assert info["streams"][0]["width"] == 1920


def test_registry_matches_storyboard_constant():
    from app.video.pipeline.remake import storyboard
    from app.video.pipeline.remake.renderers import RENDERERS
    assert set(RENDERERS) == storyboard.IMPLEMENTED_RENDERERS
