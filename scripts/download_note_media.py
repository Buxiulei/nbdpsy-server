"""按需下载笔记媒体原图:读台账的媒体清单 → 直接 HTTP 拉 → 存到指定目录。

台账只存**归一化后的永久 URL**(见 app/browser/note_media.py 的实测记录):平台给的
带签名 URL 18 天就过期,而 ``sns-img-qc/{段}/{file_id}`` 永久有效**且是原图**。
所以"要图的时候再下"是可行的,且拿到的比页面展示图清晰得多(实测 424KB vs 56KB)。

**不需要 cookie、不需要浏览器、不消耗风控预算** —— 纯 HTTP GET(实测裸 curl 200)。

用法::

    python scripts/download_note_media.py --account 1                 # 一个号全下
    python scripts/download_note_media.py --note 6a707e9f...          # 单篇
    python scripts/download_note_media.py --account 1 --out /path/dir # 指定目录

默认落 ``data/media/{account_id}/{note_id}/{序号}_{file_id}.{jpg|mp4}``(按 kind);
已存在的跳过(幂等)。
"""

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "nbdpsy.db"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "media"
TIMEOUT_S = 60


def rows_to_download(account_id: int | None, note_id: str | None) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = (
        "SELECT account_id, note_id, title, media_json FROM published_notes "
        "WHERE media_json IS NOT NULL AND media_json != '' AND media_json != '[]'"
    )
    args: list = []
    if account_id:
        sql += " AND account_id = ?"
        args.append(account_id)
    if note_id:
        sql += " AND note_id = ?"
        args.append(note_id)
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return rows


def download_one(url: str, dest: Path) -> tuple[bool, str]:
    """下一张;已存在 → 跳过。返回 (是否新下, 说明)。"""
    if dest.exists() and dest.stat().st_size > 0:
        return False, "已存在"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = resp.read()
        if not data:
            return False, "空响应"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True, f"{len(data) // 1024}KB"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"失败 {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="按需下载笔记媒体原图")
    ap.add_argument("--account", type=int, help="只下这个账号")
    ap.add_argument("--note", help="只下这一篇(note_id)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出根目录")
    args = ap.parse_args()

    rows = rows_to_download(args.account, args.note)
    if not rows:
        print("台账里没有匹配的媒体清单(先跑一次 note_media_sync)")
        return 1

    new_files = skipped = failed = 0
    for row in rows:
        media = json.loads(row["media_json"] or "[]")
        note_dir = args.out / str(row["account_id"]) / row["note_id"]
        for item in media:
            # 扩展名按 kind 定:视频清单里的是裸 mp4(编辑页 <video> 给的),
            # 一律 .jpg 会存出打不开的"图片"
            ext = ".mp4" if item.get("kind") == "video" else ".jpg"
            name = f"{item['ordinal']:02d}_{item['file_id']}{ext}"
            ok, msg = download_one(item["url"], note_dir / name)
            if ok:
                new_files += 1
            elif msg == "已存在":
                skipped += 1
            else:
                failed += 1
                print(f"  ✗ {row['note_id']} #{item['ordinal']}: {msg}")
        print(f"{row['note_id']} {(row['title'] or '')[:20]}: {len(media)} 项 → {note_dir}")
    print(f"\n新下 {new_files} 张 / 跳过 {skipped} 张 / 失败 {failed} 张;根目录 {args.out}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
