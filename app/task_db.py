"""SQLite 状态管理：收藏笔记登记、分类决策、增量门控、失败重试、链接表。

表 notes:      每一条小红书收藏笔记的登记与各阶段完成标志
表 runs:       每次扫描的运行日志（桥状态 + 各类计数）
表 links:      笔记之间已建立的关联（topic / keyword），UNIQUE 去重
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# 项目根（app/ 的上一级）
BASE = Path(__file__).resolve().parents[1]
DB_PATH = BASE / "data" / "state.db"

# 分类枚举（与 classifier.py 一致）
NOT_USEFUL = 0
USEFUL = 1
UNCERTAIN = 2


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT,
            url TEXT,
            note_type TEXT,          -- opencli 给出的类型（normal / video），仅驱动流程不决定目录
            caption TEXT,            -- 笔记正文（作者原文），入库便于断点重试
            classify INTEGER DEFAULT 2,          -- 0 无用 / 1 有用 / 2 uncertain
            classify_reason TEXT,
            classified_at TEXT,
            caption_done INTEGER DEFAULT 0,
            media_done INTEGER DEFAULT 0,
            notes_done INTEGER DEFAULT 0,
            note_path TEXT,
            topics TEXT,             -- JSON 数组
            error TEXT,
            attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_pending ON notes(notes_done, classify)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_classify ON notes(classify)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT DEFAULT (datetime('now','localtime')),
            bridge_ok INTEGER,
            seen INTEGER,
            new_seen INTEGER,
            classified INTEGER,
            useful INTEGER,
            processed INTEGER,
            failed INTEGER,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id TEXT NOT NULL,
            to_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'topic',   -- topic | keyword
            UNIQUE(from_id, to_id, kind)
        )
    """)
    conn.commit()
    return conn


# ---------- notes ----------

def get_note(note_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM notes WHERE note_id=?", (note_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def register_note(note_id: str, title: str, author: str, url: str, note_type: str = "") -> bool:
    """登记一条收藏（不存在则插入）。返回是否为新插入。"""
    conn = _conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO notes (note_id, title, author, url, note_type) VALUES (?,?,?,?,?)",
        (note_id, title, author, url, note_type),
    )
    conn.commit()
    is_new = cur.rowcount > 0
    conn.close()
    return is_new


def set_classify(note_id: str, verdict: int, reason: str = "") -> None:
    conn = _conn()
    conn.execute(
        "UPDATE notes SET classify=?, classify_reason=?, classified_at=datetime('now','localtime'), "
        "updated_at=datetime('now','localtime') WHERE note_id=?",
        (verdict, reason, note_id),
    )
    conn.commit()
    conn.close()


def set_caption(note_id: str, caption: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE notes SET caption=?, caption_done=1, updated_at=datetime('now','localtime') "
        "WHERE note_id=?",
        (caption, note_id),
    )
    conn.commit()
    conn.close()


def set_stage(note_id: str, stage: str, done: int = 1, path: str = "") -> None:
    """标记某阶段完成。stage: caption | media | notes。path 可选：note_path。"""
    conn = _conn()
    col = f"{stage}_done"
    sql = f"UPDATE notes SET {col}=?, updated_at=datetime('now','localtime')"
    args: list = [done]
    if path and stage == "notes":
        sql += ", note_path=?"
        args.append(path)
    sql += " WHERE note_id=?"
    args.append(note_id)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def is_stage_done(note_id: str, stage: str) -> bool:
    n = get_note(note_id)
    if not n:
        return False
    return bool(n.get(f"{stage}_done", 0))


def record_error(note_id: str, message: str) -> None:
    """记录一条笔记的处理错误，attempts+1。"""
    conn = _conn()
    conn.execute(
        "UPDATE notes SET error=?, attempts=attempts+1, updated_at=datetime('now','localtime') "
        "WHERE note_id=?",
        (message[:2000], note_id),
    )
    conn.commit()
    conn.close()


def clear_error(note_id: str) -> None:
    conn = _conn()
    conn.execute("UPDATE notes SET error=NULL WHERE note_id=?", (note_id,))
    conn.commit()
    conn.close()


def set_topics(note_id: str, topics: list[str]) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE notes SET topics=?, updated_at=datetime('now','localtime') WHERE note_id=?",
        (json.dumps(topics, ensure_ascii=False), note_id),
    )
    conn.commit()
    conn.close()


def get_topics(note_id: str) -> list[str]:
    n = get_note(note_id)
    if not n or not n.get("topics"):
        return []
    try:
        return json.loads(n["topics"])
    except (ValueError, TypeError):
        return []


def pending_notes() -> list[dict]:
    """待处理：已分类为有用但笔记未完成，且未超重试上限的。"""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM notes WHERE classify=1 AND notes_done=0 AND attempts<3 ORDER BY created_at",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def candidates() -> list[dict]:
    """候选：有用但未完成 + 尚未分类（会在处理时分类），未超重试上限。

    不含已判「无用」的（classify=0 是终态，直接排除）。
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM notes WHERE classify IN (1,2) AND notes_done=0 AND attempts<3 "
        "ORDER BY created_at",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_notes() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM notes ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def notes_with_notes() -> list[dict]:
    """已完成笔记（notes_done=1 且 note_path 存在）。"""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM notes WHERE notes_done=1 AND note_path IS NOT NULL ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- runs ----------

def record_run(*, bridge_ok: int, seen: int, new_seen: int, classified: int,
               useful: int, processed: int, failed: int, error: str = "") -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (bridge_ok, seen, new_seen, classified, useful, processed, failed, error) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (bridge_ok, seen, new_seen, classified, useful, processed, failed, error),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


# ---------- links ----------

def add_link(from_id: str, to_id: str, kind: str = "topic") -> None:
    """记录一条笔记关联（UNIQUE 去重）。"""
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO links (from_id, to_id, kind) VALUES (?,?,?)",
        (from_id, to_id, kind),
    )
    conn.commit()
    conn.close()


def links_from(note_id: str) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM links WHERE from_id=? ORDER BY kind, to_id", (note_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_links() -> None:
    conn = _conn()
    conn.execute("DELETE FROM links")
    conn.commit()
    conn.close()
