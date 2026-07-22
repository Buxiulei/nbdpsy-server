"""revision job 产物继承（spec §B3）。

从被修订的父 job 拷贝前五阶段中间产物，避免重跑最贵的下载/分析/转写/翻译。

- video.mp4 与 tts/ 音频：逐文件硬链接（同文件系统 inode 复用，父子共享底层数据块省磁盘）；
  os.link 失败（跨文件系统 EXDEV 等）回退整文件复制。
- raw 下 scene_facts/translated/rewritten json：整文件复制——rewrite 阶段会就地覆写
  子 job 的 rewritten.json 应用编辑清单，硬链接会连带改坏父 job，故必须复制成独立副本。

目标 raw/tts 目录由 paths 惰性创建（HMAC token 目录）。
"""
import os
import re
import shutil
from pathlib import Path

from app.video import paths

# 复制到子 job 的 raw json（前五阶段产物；rewrite 会覆写 rewritten.json，故复制非硬链）
_INHERIT_RAW_JSON = ("scene_facts.json", "translated.json", "rewritten.json")

# 只继承 dub 逐句缓存（hash 命名 {i:05d}_{md5(zh)[:8]}.wav），排除 dub.wav/tones.wav/
# remake_mixed.wav 等聚合文件：聚合文件由子 compose 就地重建，硬链接共享 inode 会让子
# compose 原地覆写连带改坏父中间件（当前无害但模式脆弱，收窄根治）。
_TTS_CLIP_RE = re.compile(r"^\d{5}_[0-9a-f]{8}\.wav$")


def _link_or_copy(src: Path, dst: Path) -> None:
    """优先硬链接（省磁盘、同 inode）；os.link 失败时回退整文件复制。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def inherit_artifacts(parent_id: int, child_id: int) -> None:
    """把父 job 的 raw/video.mp4、raw/*.json、tts/ 继承给子 revision job。

    video.mp4 与 tts/ 逐文件硬链接（失败回退复制）；raw json 整文件复制。
    """
    parent_raw, child_raw = paths.raw_dir(parent_id), paths.raw_dir(child_id)
    parent_tts, child_tts = paths.tts_dir(parent_id), paths.tts_dir(child_id)

    # video.mp4：硬链接省磁盘（下载阶段最贵产物，父子共享数据块）
    _link_or_copy(parent_raw / "video.mp4", child_raw / "video.mp4")

    # raw json：复制成独立副本（rewrite 阶段会覆写子 rewritten.json，不能连带改父）
    for name in _INHERIT_RAW_JSON:
        src = parent_raw / name
        if src.exists():
            shutil.copy2(src, child_raw / name)

    # revision 不可变基底（I1）：父 rewritten 另存一份只读副本 rewritten_inherited.json，
    # _handle_rewrite revision 分支恒从它读、apply 后只写 rewritten.json——崩溃后重入天然幂等
    # （绝不在已 apply 的产物上二次 apply，杜绝 delete 误删邻句 / insert 重复）。
    src_rew = parent_raw / "rewritten.json"
    if src_rew.exists():
        shutil.copy2(src_rew, child_raw / "rewritten_inherited.json")

    # 链式修订参数继承（Imp-1）：父若是 revision job，其 param_overrides.json 记着累积的
    # ball/global/card 覆盖——存为子 param_overrides_inherited.json（**不可变种子**）供 apply_edits
    # 累积，否则 revision-of-revision 时父层覆盖（如 ball_style.y_ratio）静默回退默认。
    # 关键：种子必须与 rewrite 输出的 param_overrides.json 分文件——seed 恒从只读种子读，绝不读
    # 自己落盘的可变输出，否则崩溃重入时 seed 含本层已合并覆盖，破坏 I1 幂等（closing_line 回归）。
    src_ov = parent_raw / "param_overrides.json"
    if src_ov.exists():
        shutil.copy2(src_ov, child_raw / "param_overrides_inherited.json")

    # tts/：只硬链接 hash 命名的逐句缓存（未改句跨 job 命中缓存，spec §B4）；
    # 聚合文件（dub.wav/tones.wav/remake_mixed.wav 等）不继承，交子 compose 重建。
    # 注：命名含下标（{i:05d}_{hash8}）——script_delete/insert 后置句因下标位移换名，即便文本未变
    # 也会 cache miss 重合成（spec 命名固有）；纯 script_edit 不移位，未改句稳定命中。
    for clip in sorted(parent_tts.iterdir()):
        if clip.is_file() and _TTS_CLIP_RE.match(clip.name):
            _link_or_copy(clip, child_tts / clip.name)
