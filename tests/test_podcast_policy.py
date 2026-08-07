"""播客音频发布的纯函数层契约测试(app/publish/policy.py 的 audio 一族)。

全部不起浏览器、不碰 DB。时长夹具用 ffmpeg 现造静音 wav
(``-f lavfi -i anullsrc -t N``,秒级生成),测完 tmp_path 自动清。

覆盖:
- ``audio_ext_allowed`` 白名单边界(含大小写不敏感、视频扩展名被拒);
- ``audio_duration_s`` 真读时长 + 读不出来给 None(**不假装知道**);
- ``audio_reject`` 的四条准入(存在性 / 扩展名 / 体积 / 时长)与**闭区间**边界;
- 两档封面准入(音频封面 32MB / 播客合集封面 5MB)体积上限确实不同。
"""

import subprocess

import pytest

from app.publish.policy import (
    AUDIO_COVER_MAX_BYTES,
    AUDIO_MAX_DURATION_S,
    AUDIO_MIN_DURATION_S,
    PODCAST_COLLECTION_COVER_MAX_BYTES,
    XHS_AUDIO_EXTENSIONS,
    audio_cover_reject,
    audio_duration_s,
    audio_ext_allowed,
    audio_reject,
    podcast_collection_cover_reject,
)


def _silence(tmp_path, seconds: float, name: str = "a.wav") -> str:
    """用 ffmpeg 造一段 ``seconds`` 秒的静音 wav,返回路径。

    采样率压到 8000 单声道:7200 秒的夹具在默认 44.1kHz 立体声下要 1.2GB,
    这里只有 57MB,测试跑得动、也不占盘。
    """
    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds), str(path)],
        check=True,
    )
    return str(path)


# ---------------- 扩展名白名单 ----------------


def test_audio_ext_whitelist_exactly_five():
    """白名单就是实拍规格那五种,不多不少(多一种就是我们在替平台放宽)。"""
    assert set(XHS_AUDIO_EXTENSIONS) == {".m4a", ".mp3", ".wav", ".flac", ".aac"}


@pytest.mark.parametrize("name", ["a.m4a", "a.mp3", "a.wav", "a.flac", "a.aac", "A.MP3"])
def test_audio_ext_allowed_accepts(name):
    """五种格式 + 大写扩展名都放行(大小写不敏感,与 video_ext_allowed 同款)。"""
    assert audio_ext_allowed(name) is True


@pytest.mark.parametrize("name", ["a.mp4", "a.mov", "a.ogg", "a.wma", "a", "", None])
def test_audio_ext_allowed_rejects(name):
    """视频扩展名 / 平台没写的音频格式 / 空值一律拒 —— 不猜平台会不会收。"""
    assert audio_ext_allowed(name) is False


# ---------------- 时长 ----------------


def test_audio_duration_reads_real_file(tmp_path):
    """ffprobe 真读时长(容 0.2s 误差:容器时长与采样数换算有零头)。"""
    path = _silence(tmp_path, 3, "three.wav")
    assert abs(audio_duration_s(path) - 3.0) < 0.2


def test_audio_duration_unreadable_returns_none(tmp_path):
    """不是音频 / 文件不存在 → None,**不假装知道**(交调用方拒收,不放行)。"""
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all")
    assert audio_duration_s(str(junk)) is None
    assert audio_duration_s(str(tmp_path / "nope.mp3")) is None
    assert audio_duration_s("") is None


# ---------------- audio_reject:四条准入 ----------------


def test_audio_reject_passes_valid(tmp_path, monkeypatch):
    """扩展名/体积/时长全合格 → None(放行)。

    时长门用 monkeypatch 把下限压到 1 秒:真造一段 600 秒夹具只为过门没有信息量,
    **边界本身**另有专门的用例按真实常量测(见下)。
    """
    path = _silence(tmp_path, 2, "ok.wav")
    monkeypatch.setattr("app.publish.policy.AUDIO_MIN_DURATION_S", 1)
    assert audio_reject(path) is None


def test_audio_reject_missing_file(tmp_path):
    """路径不存在 → 拒,理由点名"文件不存在"。"""
    reason = audio_reject(str(tmp_path / "nope.mp3"))
    assert reason is not None and "不存在" in reason


def test_audio_reject_bad_ext(tmp_path):
    """扩展名不在白名单 → 拒(**先于**读时长,免得为一个必拒的文件白跑 ffprobe)。"""
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    reason = audio_reject(str(path))
    assert reason is not None and "格式不支持" in reason


def test_audio_reject_oversize(tmp_path, monkeypatch):
    """超体积上限 → 拒(把常量压到 1 字节验判据,不真造 1GB 文件)。"""
    path = _silence(tmp_path, 1, "big.wav")
    monkeypatch.setattr("app.publish.policy.XHS_AUDIO_MAX_BYTES", 1)
    reason = audio_reject(path)
    assert reason is not None and "大小" in reason


def test_audio_reject_duration_unreadable(tmp_path):
    """扩展名对但读不出时长 → **拒**,不放行。

    放行的后果是造一条注定失败的 pending job:平台侧超长会拒,而我们要等到发布
    那一刻才知道。宁可在入口误杀一个坏文件。
    """
    path = tmp_path / "fake.mp3"
    path.write_bytes(b"definitely not mp3")
    reason = audio_reject(str(path))
    assert reason is not None and "时长" in reason


def test_audio_duration_bounds_are_closed_interval(tmp_path, monkeypatch):
    """时长门取**闭区间**:恰好 10:00 / 2:00:00 放行,差一秒就拒。

    平台真实边界语义未取证(E6),先取宽松侧避免误杀合法输入——真号验出平台
    拒收 600s 整时再收紧,那时改这里一个常量即可。
    """
    assert (AUDIO_MIN_DURATION_S, AUDIO_MAX_DURATION_S) == (600, 7200)
    # 把边界压到 3/5 秒,用三段真夹具验开闭:2s 拒、3s 过、5s 过、6s 拒
    monkeypatch.setattr("app.publish.policy.AUDIO_MIN_DURATION_S", 3)
    monkeypatch.setattr("app.publish.policy.AUDIO_MAX_DURATION_S", 5)
    assert audio_reject(_silence(tmp_path, 2, "s2.wav")) is not None
    assert audio_reject(_silence(tmp_path, 3, "s3.wav")) is None
    assert audio_reject(_silence(tmp_path, 5, "s5.wav")) is None
    assert audio_reject(_silence(tmp_path, 6, "s6.wav")) is not None


# ---------------- 两档封面 ----------------


def test_cover_caps_differ():
    """音频封面 32MB / 合集封面 5MB —— 两个上限确实不同,别合并成一个。"""
    assert AUDIO_COVER_MAX_BYTES == 32 * 1024 * 1024
    assert PODCAST_COLLECTION_COVER_MAX_BYTES == 5 * 1024 * 1024


@pytest.mark.parametrize(
    "reject", [audio_cover_reject, podcast_collection_cover_reject]
)
def test_cover_reject_common_rules(tmp_path, reject):
    """两档封面共用的三条:文件要在、扩展名要对(含 webp)、合格返 None。

    ⚠️ **webp 是放行的** —— 真号取证读到合集封面 input 的
    ``accept=".jpg,.jpeg,.png,.webp"``,与设计文档「合集封面无 webp」的假设相反,
    以实测 DOM 为准。
    """
    ok = tmp_path / "c.webp"
    ok.write_bytes(b"x" * 10)
    assert reject(str(ok)) is None

    bad_ext = tmp_path / "c.gif"
    bad_ext.write_bytes(b"x")
    assert "格式不支持" in reject(str(bad_ext))

    assert "不存在" in reject(str(tmp_path / "gone.png"))


def test_audio_cover_size_gate(tmp_path, monkeypatch):
    """音频封面超 32MB → 拒(压常量验判据)。"""
    path = tmp_path / "c.png"
    path.write_bytes(b"x" * 100)
    monkeypatch.setattr("app.publish.policy.AUDIO_COVER_MAX_BYTES", 10)
    assert "大小" in audio_cover_reject(str(path))


def test_collection_cover_size_gate_is_stricter(tmp_path, monkeypatch):
    """同一个 8MB 文件:音频封面放行,合集封面按 5MB 上限拒——两档确实独立生效。"""
    path = tmp_path / "c.png"
    path.write_bytes(b"x" * (8 * 1024 * 1024))
    assert audio_cover_reject(str(path)) is None
    assert "大小" in podcast_collection_cover_reject(str(path))
