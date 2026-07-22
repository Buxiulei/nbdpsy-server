"""产物组装：final.mp4 + 双SRT + 双语md + meta.json。

平移自 video_transport/deliver.py：纯同步文件写（小文本，非阻塞红线「大读写」不涉及），仅
paths 走 ``app.video.paths``（产物根 DATA_DIR/uploads/video）。handler 层直接调（stages.py）。
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from app.video import paths


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[dict], path: Path, *, key: str) -> Path:
    lines = []
    n = 0
    for seg in segments:
        text = (seg.get(key) or "").strip()
        if not text:
            continue
        n += 1
        lines.append(f"{n}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{text}\n\n")
    Path(path).write_text("".join(lines), encoding="utf-8")
    return Path(path)


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def write_bilingual_md(translated: list[dict], term_sheet: list[dict],
                       video_meta: dict, path: Path) -> Path:
    parts = [f"# {video_meta.get('title', '')} 中英对照逐字稿\n\n",
             f"- 原视频：{video_meta.get('webpage_url', '')}\n",
             f"- 作者：{video_meta.get('uploader', '')}\n",
             f"- 时长：{video_meta.get('duration', 0)}s\n",
             f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}\n\n"]
    if term_sheet:
        parts.append("## 术语表\n\n| 英文 | 中文 | 来源 |\n|---|---|---|\n")
        for t in term_sheet:
            parts.append(f"| {t['en']} | {t['zh']} | {t['source']} |\n")
        parts.append("\n")
    parts.append("## 逐字稿\n\n")
    for seg in translated:
        parts.append(f"[{_mmss(seg['start'])}] {seg['en']}\n\n> {seg['zh']}\n\n")
    Path(path).write_text("".join(parts), encoding="utf-8")
    return Path(path)


def assemble_products(job_id: int, *, final_video: Path, translated: list[dict],
                      term_sheet: list[dict], video_meta: dict, stats: dict,
                      storyboard: Path | None = None,
                      attribution: str | None = None,
                      revision: dict | None = None) -> dict:
    out = paths.out_dir(job_id)
    video_dst = out / "final.mp4"
    if Path(final_video).resolve() != video_dst.resolve():
        shutil.move(str(final_video), str(video_dst))
    zh_srt = write_srt(translated, out / "transcript_zh.srt", key="zh")
    en_srt = write_srt(translated, out / "transcript_en.srt", key="en")
    bilingual = write_bilingual_md(translated, term_sheet, video_meta,
                                   out / "transcript_bilingual.md")
    meta = out / "meta.json"
    meta_payload = {"video": video_meta, "stats": stats,
                    "term_count": len(term_sheet)}
    if attribution:
        meta_payload["attribution"] = attribution   # remake 出处声明入 meta，随产物公开
    if revision is not None:
        # revision 成片：溯源块入 meta（父 job / 原始意见 / 解析出的编辑清单），随产物公开
        meta_payload["revision"] = revision
    meta.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    products = {
        "video_url": paths.to_public_url(video_dst),
        "transcript_zh_srt_url": paths.to_public_url(zh_srt),
        "transcript_en_srt_url": paths.to_public_url(en_srt),
        "transcript_bilingual_url": paths.to_public_url(bilingual),
        "meta_url": paths.to_public_url(meta),
    }
    if storyboard is not None:
        sb_dst = out / "storyboard.json"
        shutil.copy(str(storyboard), str(sb_dst))
        products["storyboard_url"] = paths.to_public_url(sb_dst)
    return products
