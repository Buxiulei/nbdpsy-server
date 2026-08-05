"""导出笔记内容台账:每号一个 Markdown(标题/正文全文/媒体永久链接/目的/互动数)。

数据全部来自 ``published_notes``(创作中心台账同步 + 编辑页回读的平台真值)。
媒体只给**归一化后的永久链接**(见 app/browser/note_media.py 的实测记录:平台原始
链接 18 天就过期,归一化后 9 个月前的仍能取回原图),要文件时跑
``scripts/download_note_media.py`` 按需下载。

用法::

    python scripts/export_content_ledger.py                      # 全部账号
    python scripts/export_content_ledger.py --account 1
    python scripts/export_content_ledger.py --out /path/to/dir
"""

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "nbdpsy.db"
DEFAULT_OUT = Path("/home/roots/NBDpsy/文档/笔记内容台账")


def _visibility(permission_code) -> str:
    if permission_code == 0:
        return "公开"
    if permission_code is None:
        return "未知"
    return "私密/仅自己可见"


def _note_section(index: int, row: sqlite3.Row) -> list[str]:
    lines = [f"## {index}. {row['title'] or '(无标题)'}"]
    meta = [
        f"note_id `{row['note_id'] or 'pending 未关联'}`",
        f"发布 {str(row['platform_published_at'] or row['published_at'] or '')[:16]}",
        f"可见性 {_visibility(row['permission_code'])}",
    ]
    if row["note_type"]:
        meta.append(f"类型 {row['note_type']}")
    if row["note_purpose"]:
        meta.append(f"目的 {row['note_purpose']}({row['purpose_source'] or '?'})")
    if row["related_counselor"]:
        meta.append(f"关联咨询师 {row['related_counselor']}")
    meta.append(f"赞{row['likes'] or 0}/藏{row['collects'] or 0}/评{row['comments'] or 0}")
    lines.append("｜".join(meta) + "\n")

    media = json.loads(row["media_json"] or "[]")
    if media:
        lines.append(f"**媒体 {len(media)} 项**(永久直链,可直接下载原图):\n")
        lines += [f"{m['ordinal']}. {m['url']}" for m in media]
        lines.append("")
    elif row["media_fetched_at"] and row["permission_code"] == 0:
        kind = "视频笔记(视频本体待支持)" if row["note_type"] == "video" else "编辑页图片区为空"
        lines.append(f"*(媒体:{kind})*\n")

    if row["content_text"]:
        lines.append(row["content_text"].strip() + "\n")
    elif row["permission_code"] not in (0, None):
        lines.append("*(私密篇,正文与媒体均未抓取——读者不可见,不花风控预算)*\n")
    elif not row["note_id"]:
        lines.append("*(平台侧尚未关联到 note_id,内容待关联后回填)*\n")
    else:
        lines.append("*(公开但编辑页进不去,疑似平台侧异常或已删;待人工复核)*\n")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="导出笔记内容台账 Markdown")
    ap.add_argument("--account", type=int, help="只导这个账号")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    accounts = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM xhs_accounts")}
    if args.account:
        accounts = {k: v for k, v in accounts.items() if k == args.account}
    args.out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    index = [
        f"# NBDpsy 小红书笔记内容台账 · 总览({today})\n",
        "数据源:nbdpsy-server `published_notes`(创作中心台账同步 + 编辑页回读的平台真值)。",
        "每号一个文件:标题/note_id/发布时间/可见性/核心目的/互动数/**正文全文**/**媒体永久链接**。",
        "",
        "媒体只存链接不存文件:平台原始图片链接约 18 天过期,本台账存的是归一化后的",
        "永久直链(实测 9 个月前的仍可取回**原图**)。要文件时跑",
        "`python scripts/download_note_media.py --account N` 按需下载(纯 HTTP,不用登录)。",
        "",
        "| 账号 | 总篇数 | 有正文 | 有媒体 | 媒体项 | 私密 | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]

    for acc_id, name in sorted(accounts.items()):
        rows = con.execute(
            "SELECT * FROM published_notes WHERE account_id=? "
            "ORDER BY COALESCE(platform_published_at, published_at) DESC",
            (acc_id,),
        ).fetchall()
        if not rows:
            continue
        with_content = sum(1 for r in rows if r["content_text"])
        with_media = sum(1 for r in rows if json.loads(r["media_json"] or "[]"))
        media_items = sum(len(json.loads(r["media_json"] or "[]")) for r in rows)
        private = sum(1 for r in rows if r["permission_code"] not in (0, None))
        stuck = [r for r in rows if r["permission_code"] == 0 and r["note_id"]
                 and not r["content_text"] and not r["media_fetched_at"]]
        note = f"{len(stuck)} 篇编辑页进不去(疑已删/平台异常)" if stuck else ""
        index.append(
            f"| {name}({acc_id}) | {len(rows)} | {with_content} | {with_media} | "
            f"{media_items} | {private} | {note} |"
        )

        lines = [
            f"# {name}(账号 {acc_id})· 笔记内容台账({today})\n",
            f"共 {len(rows)} 篇;正文 {with_content} 篇;媒体 {with_media} 篇共 {media_items} 项;"
            f"私密 {private} 篇(按纪律不抓取)。\n",
        ]
        for i, row in enumerate(rows, 1):
            lines += _note_section(i, row)
        path = args.out / f"{acc_id}-{name.replace('/', '_')}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"已写 {path.name}({path.stat().st_size // 1024}KB)")

    (args.out / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"已写 README.md;目录 {args.out}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
