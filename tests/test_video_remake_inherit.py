"""revision job 产物继承（spec §B3）：video.mp4/tts 硬链接、raw json 复制、失败回退复制。"""
import os
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.video import paths
from app.video.pipeline.remake.inherit import inherit_artifacts


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    """把产物根目录指到 tmp_path，raw_dir/tts_dir 走真实 HMAC token 目录（同 job_id 稳定）。

    换 import 面：源 paths 以 settings.UPLOAD_DIR 为根，宿主 paths 以 settings.DATA_DIR/uploads
    为根（paths 请求时读，monkeypatch 生效）。故钳 DATA_DIR 即钳住产物落盘位置。
    """
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    return tmp_path


def _seed_parent(parent_id: int):
    """造一个"已完成 remake 父 job"的 raw/tts 产物。"""
    praw = paths.raw_dir(parent_id)
    (praw / "video.mp4").write_bytes(b"VIDEO-BYTES")
    (praw / "scene_facts.json").write_text('[{"id": 1}]', encoding="utf-8")
    (praw / "translated.json").write_text('[{"zh": "甲"}]', encoding="utf-8")
    (praw / "rewritten.json").write_text('[{"zh": "甲改"}]', encoding="utf-8")
    ptts = paths.tts_dir(parent_id)
    (ptts / "00000_abcd1234.wav").write_bytes(b"WAV-0")
    (ptts / "00001_ef567890.wav").write_bytes(b"WAV-1")
    return praw, ptts


@pytest.mark.unit
class TestInheritArtifacts:
    def test_video_and_tts_hardlinked_json_copied(self, upload_root):
        parent_id, child_id = 100, 101
        praw, ptts = _seed_parent(parent_id)

        inherit_artifacts(parent_id, child_id)

        craw, ctts = paths.raw_dir(child_id), paths.tts_dir(child_id)
        # video.mp4：硬链接 → 内容一致且同 inode（共享底层数据块，省磁盘）
        assert (craw / "video.mp4").read_bytes() == b"VIDEO-BYTES"
        assert os.stat(craw / "video.mp4").st_ino == os.stat(praw / "video.mp4").st_ino
        # raw json：整文件复制 → 内容一致但异 inode（rewrite 覆写子副本不动父）
        for name in ("scene_facts.json", "translated.json", "rewritten.json"):
            assert (craw / name).read_text(encoding="utf-8") == \
                (praw / name).read_text(encoding="utf-8")
            assert os.stat(craw / name).st_ino != os.stat(praw / name).st_ino
        # tts/：逐文件硬链接 → 同 inode（未改句跨 job 命中缓存的物理基础）
        for name in ("00000_abcd1234.wav", "00001_ef567890.wav"):
            assert os.stat(ctts / name).st_ino == os.stat(ptts / name).st_ino
        # I1 不可变基底：父 rewritten 另存只读副本，revision rewrite 恒从它读（重入幂等）
        assert (craw / "rewritten_inherited.json").read_text(encoding="utf-8") == \
            (praw / "rewritten.json").read_text(encoding="utf-8")
        assert os.stat(craw / "rewritten_inherited.json").st_ino != \
            os.stat(praw / "rewritten.json").st_ino

    def test_json_copy_is_independent(self, upload_root):
        # 复制后改子 rewritten.json 不回写父（硬链接会连带改坏父 job）
        parent_id, child_id = 110, 111
        praw, _ = _seed_parent(parent_id)
        inherit_artifacts(parent_id, child_id)
        craw = paths.raw_dir(child_id)
        (craw / "rewritten.json").write_text('[{"zh": "子改"}]', encoding="utf-8")
        assert (praw / "rewritten.json").read_text(encoding="utf-8") == '[{"zh": "甲改"}]'

    def test_fallback_to_copy_when_link_fails(self, upload_root):
        # os.link 失败（跨文件系统 EXDEV 等）→ 回退 shutil.copy2，内容仍到位、异 inode
        parent_id, child_id = 200, 201
        praw, ptts = _seed_parent(parent_id)
        with patch("app.video.pipeline.remake.inherit.os.link",
                   side_effect=OSError("EXDEV")):
            inherit_artifacts(parent_id, child_id)
        craw, ctts = paths.raw_dir(child_id), paths.tts_dir(child_id)
        assert (craw / "video.mp4").read_bytes() == b"VIDEO-BYTES"
        assert os.stat(craw / "video.mp4").st_ino != os.stat(praw / "video.mp4").st_ino
        assert (ctts / "00000_abcd1234.wav").read_bytes() == b"WAV-0"
        assert os.stat(ctts / "00000_abcd1234.wav").st_ino != \
            os.stat(ptts / "00000_abcd1234.wav").st_ino

    def test_tts_inherits_only_hash_clips_excludes_aggregates(self, upload_root):
        # B4 收窄：只硬链接 hash 命名逐句缓存（?????_????????.wav），排除 dub.wav/tones.wav/
        # remake_mixed.wav 等聚合文件——共享 inode 会被子 compose 原地覆写连带改坏父中间件。
        parent_id, child_id = 400, 401
        praw = paths.raw_dir(parent_id)
        (praw / "video.mp4").write_bytes(b"V")
        ptts = paths.tts_dir(parent_id)
        (ptts / "00000_abcd1234.wav").write_bytes(b"CLIP-0")   # hash 逐句缓存：继承
        (ptts / "00001_ef567890.wav").write_bytes(b"CLIP-1")
        for agg in ("dub.wav", "tones.wav", "remake_mixed.wav", "mixed.wav"):
            (ptts / agg).write_bytes(b"AGG")                   # 聚合文件：不继承
        inherit_artifacts(parent_id, child_id)
        ctts = paths.tts_dir(child_id)
        assert (ctts / "00000_abcd1234.wav").exists()
        assert (ctts / "00001_ef567890.wav").exists()
        for agg in ("dub.wav", "tones.wav", "remake_mixed.wav", "mixed.wav"):
            assert not (ctts / agg).exists(), f"聚合文件 {agg} 不应被继承"

    def test_missing_optional_json_skipped(self, upload_root):
        # 父缺某 raw json（防御式）：跳过不报错，video.mp4 仍继承
        parent_id, child_id = 300, 301
        praw = paths.raw_dir(parent_id)
        (praw / "video.mp4").write_bytes(b"V")
        (praw / "translated.json").write_text("[]", encoding="utf-8")
        paths.tts_dir(parent_id)   # 空 tts 目录也不报错
        inherit_artifacts(parent_id, child_id)
        craw = paths.raw_dir(child_id)
        assert (craw / "video.mp4").exists()
        assert (craw / "translated.json").exists()
        assert not (craw / "scene_facts.json").exists()
