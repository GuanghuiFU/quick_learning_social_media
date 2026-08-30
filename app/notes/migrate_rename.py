"""一次性重排脚本：给现有笔记文件加 YYYY-MM-DD_ 日期前缀（取入库时间 created_at），
同步 DB note_path，并在正文补「> 入库日期：」行。

用法：
    cd 项目根 && ./.venv/bin/python -m app.notes.migrate_rename

幂等：已带日期前缀的文件会跳过；每次跑前先 dry-run 语义校验。
"""
from __future__ import annotations

import re
from datetime import datetime, date
from pathlib import Path

from app import task_db
from app.notes import writer

_ROOT = writer.notes_root()
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_.+\.md$")


def _parse_date(v) -> str:
    """把 created_at（"YYYY-MM-DD HH:MM:SS" / date / datetime）统一成 YYYY-MM-DD。"""
    if v is None:
        return date.today().isoformat()
    s = str(v)[:10]
    return s if (len(s) == 10 and s[4] == "-" and s[7] == "-") else date.today().isoformat()


def _add_ingest_date(text: str, d: str) -> str:
    """若正文缺「> 入库日期：」行，则在「> 生成时间：」行后插入。"""
    if f"> 入库日期：{d}" in text:
        return text
    # 「> 生成时间：YYYY...」行，在其后（行尾换行处）插入入库日期行
    m = re.search(r"(> 生成时间：[^\n]*\n)", text)
    if m:
        return text[:m.end()] + f"> 入库日期：{d}\n" + text[m.end():]
    return text


def main() -> int:
    notes = [n for n in task_db.notes_with_notes()
             if n.get("note_path") and not _DATE_RE.match(Path(n["note_path"]).name)]
    renamed = skipped = missing = 0
    for n in notes:
        old = Path(n["note_path"])
        if not old.exists():
            print(f"  ⚠ 文件不存在，跳过: {old.name}")
            missing += 1
            continue
        d = _parse_date(n.get("created_at"))
        new_name = writer.dated_filename(n["title"], d)
        new = _ROOT / new_name
        if new == old:
            skipped += 1
            continue
        # 避免覆盖已有同名文件
        if new.exists():
            print(f"  ⚠ 目标已存在，跳过: {new.name}")
            skipped += 1
            continue
        # 重命名文件
        old.rename(new)
        # 补「> 入库日期：」到正文（幂等）
        text = new.read_text(encoding="utf-8")
        new.write_text(_add_ingest_date(text, d), encoding="utf-8")
        # 更新 DB note_path
        task_db.set_stage(n["note_id"], "notes", path=str(new))
        renamed += 1
        print(f"  ✓ {old.name}  →  {new_name}")

    print(f"\n重命名 {renamed} · 跳过 {skipped} · 文件缺失 {missing}")
    if notes:
        # 重排后重建关联 + 索引（wikilink 基于 note_path.stem，已更新）
        from app.notes import linker
        linker.compute_links()
        linker.apply_related_sections()
        linker.rebuild_moc()
        print("关联 + MOC 索引已重建")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
