"""SRT 时间码格式 + 双语md结构 + products url（平移自 test_deliver.py）。

宿主 paths 产物根为 DATA_DIR/uploads/video（源为 UPLOAD_DIR/video_transport），故打桩 settings.DATA_DIR
且 URL 断言改成 /uploads/video/...。
"""
import json
import re
import shutil
from pathlib import Path

from app.video.pipeline.deliver import (
    assemble_products,
    write_bilingual_md,
    write_srt,
)

TR = [{"start": 0.0, "end": 3.25, "en": "Attachment theory.", "zh": "依恋理论。"},
      {"start": 3.25, "end": 61.5, "en": "Developed by Bowlby.", "zh": "由鲍尔比提出。"}]
SHEET = [{"en": "attachment theory", "zh": "依恋理论", "source": "manual"}]
META = {"title": "T", "uploader": "U", "webpage_url": "https://youtu.be/x", "duration": 62}


class TestDeliver:
    def test_write_srt_timecodes(self, tmp_path):
        p = write_srt(TR, tmp_path / "zh.srt", key="zh")
        text = p.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:03,250" in text
        assert "00:01:01,500" in text
        assert "依恋理论。" in text

    def test_bilingual_md_has_terms_and_source(self, tmp_path):
        p = write_bilingual_md(TR, SHEET, META, tmp_path / "b.md")
        text = p.read_text(encoding="utf-8")
        assert "依恋理论" in text and "attachment theory" in text
        assert "https://youtu.be/x" in text          # 溯源信息
        assert "[00:00]" in text                      # 时间轴标记

    def test_assemble_products_urls(self, tmp_path, monkeypatch):
        # to_public_url 与 _base 同读 settings.DATA_DIR，故把 DATA_DIR 指到 tmp_path
        from app.video import paths as p_mod
        monkeypatch.setattr(p_mod.settings, "DATA_DIR", str(tmp_path))
        video = tmp_path / "f.mp4"; video.write_bytes(b"00")
        products = assemble_products(7, final_video=video, translated=TR,
                                     term_sheet=SHEET, video_meta=META, stats={"cost": 1})
        assert set(products) == {"video_url", "transcript_zh_srt_url",
                                 "transcript_en_srt_url", "transcript_bilingual_url", "meta_url"}
        # 父段 7-{token} 由 SECRET_KEY 派生（防跨租户枚举），断言放宽到 token 段
        assert re.match(r"/uploads/video/7-[0-9a-f]{16}/out/", products["video_url"])
        shutil.rmtree(tmp_path / "uploads", ignore_errors=True)

    def test_products_include_storyboard_and_attribution(self, tmp_path, monkeypatch):
        from app.video import paths as p_mod
        monkeypatch.setattr(p_mod.settings, "DATA_DIR", str(tmp_path))
        sb = tmp_path / "storyboard.json"
        sb.write_text("{}", encoding="utf-8")
        video = tmp_path / "muxed.mp4"
        video.write_bytes(b"x")
        products = assemble_products(
            1, final_video=video, translated=[], term_sheet=[], video_meta={},
            stats={}, storyboard=sb,
            attribution="练习设计参考国际公开的 EMDR 双侧刺激自助方法")
        assert "storyboard_url" in products
        meta = json.loads((p_mod.out_dir(1) / "meta.json").read_text(encoding="utf-8"))
        assert meta["attribution"].startswith("练习设计参考")
        shutil.rmtree(tmp_path / "uploads", ignore_errors=True)

    def test_meta_carries_revision_block(self, tmp_path, monkeypatch):
        # revision 成片 meta 增溯源块（parent/instructions/edit_plan）
        from app.video import paths as p_mod
        monkeypatch.setattr(p_mod.settings, "DATA_DIR", str(tmp_path))
        video = tmp_path / "muxed.mp4"; video.write_bytes(b"x")
        edit_plan = [{"type": "script_edit", "index": 0, "new_text": "引言改写"}]
        rev = {"parent_job_id": 5, "instructions": "引言口吻自然些", "edit_plan": edit_plan}
        assemble_products(9, final_video=video, translated=[], term_sheet=[],
                          video_meta={}, stats={}, revision=rev)
        meta = json.loads((p_mod.out_dir(9) / "meta.json").read_text(encoding="utf-8"))
        assert meta["revision"] == rev
        shutil.rmtree(tmp_path / "uploads", ignore_errors=True)

    def test_meta_omits_revision_for_plain_remake(self, tmp_path, monkeypatch):
        # 普通 remake（无 revision）meta 不含 revision 键
        from app.video import paths as p_mod
        monkeypatch.setattr(p_mod.settings, "DATA_DIR", str(tmp_path))
        video = tmp_path / "muxed.mp4"; video.write_bytes(b"x")
        assemble_products(10, final_video=video, translated=[], term_sheet=[],
                          video_meta={}, stats={})
        meta = json.loads((p_mod.out_dir(10) / "meta.json").read_text(encoding="utf-8"))
        assert "revision" not in meta
        shutil.rmtree(tmp_path / "uploads", ignore_errors=True)
