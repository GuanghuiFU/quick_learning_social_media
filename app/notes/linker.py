"""笔记关联 + 主题索引（MOC）——确定性方法，零 LLM 调用。

1. 用各笔记已入库的 topics 计算共享主题 → 写 links（topic 类）；标题关键词重合 → keyword 类。
2. 给每篇已生成笔记追加「## 相关笔记」wikilink 段（只引用真实存在的笔记文件名）。
3. 重建 <vault>/小红书学习/_索引/<主题>.md MOC 页 + 总览索引页。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from app import task_db
from app.notes import writer

_LINK_ANCHOR = "## 相关笔记"

# 标题里用于关键词关联的"技术词"（跨笔记标题含相同词即关联）
_KEYWORDS = (
    "agent", "codex", "llm", "gpt", "deepseek", "rag", "python", "pytorch",
    "ai", "大模型", "模型", "算法", "分割", "检测", "医学", "影像", "论文",
    "微调", "提示词", "代码", "架构", "workflow", "论文", "mcp", "embedding",
)


def _safe_token(t: str) -> str:
    """归一化主题词，用于 MOC 文件名。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", t.strip()).strip("_")
    return s[:40] or "未分类"


def _link_text(note: dict) -> str:
    """笔记的 wikilink 文本：[[笔记文件名]]（取自 note_path）。"""
    p = Path(note.get("note_path") or "")
    if p.suffix == ".md":
        return f"[[{p.stem}]]"
    return f"[[{_safe_filename(note['title'])}]]"


def _safe_filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:80] or "untitled"


def _norm_topic(t: str) -> str:
    """归一化主题词：去空格/大小写，便于模糊匹配（"多 Agent 协作"≈"多Agent系统"）。"""
    return re.sub(r"\s+", "", t).lower()


def compute_links() -> None:
    """根据 topics + 标题关键词重建 links 表（全量重建，UNIQUE 去重）。

    主题匹配是模糊的：归一化后按 包含/包含 或 短串 长度≥3 的交集 判定关联，
    避免 LLM 每次吐的主题措辞略异导致漏链。
    """
    notes = task_db.notes_with_notes()
    if not notes:
        return
    # 按 note_id 升序，保证下方 a < b 的防重方向判断成立
    # （小红书 id 内嵌时间戳，created_at 顺序可能是倒序，不能依赖入库顺序）
    notes = sorted(notes, key=lambda x: x["note_id"])
    pairs: set[tuple[str, str, str]] = set()
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            a, b = notes[i], notes[j]
            if a["note_id"] >= b["note_id"]:
                continue
            ta = [_norm_topic(t) for t in task_db.get_topics(a["note_id"])]
            tb = [_norm_topic(t) for t in task_db.get_topics(b["note_id"])]
            if _topics_overlap(ta, tb):
                pairs.add((a["note_id"], b["note_id"], "topic"))
    # 标题关键词重合 → keyword 关联
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            a, b = notes[i], notes[j]
            if a["note_id"] >= b["note_id"]:
                continue
            al = a["title"].lower()
            bl = b["title"].lower()
            hit = [w for w in _KEYWORDS if w and w in al and w in bl]
            if hit:
                pairs.add((a["note_id"], b["note_id"], "keyword"))
    task_db.clear_links()
    for f, t, kind in pairs:
        task_db.add_link(f, t, kind)


def _topics_overlap(ta: list[str], tb: list[str]) -> bool:
    """两篇笔记的主题是否关联：有相同主题，或一方主题包含另一方（长度≥3），
    或存在较长公共子串（连续 ≥4 个字符，如"多agent"）。"""
    sa, sb = set(ta), set(tb)
    if sa & sb:
        return True
    for x in ta:
        for y in tb:
            if len(x) >= 3 and len(y) >= 3 and (x in y or y in x):
                return True
            # 较长公共子串匹配：多agent系统 vs 多agent协作
            if _shared_substr_len(x, y) >= 4:
                return True
    return False


def _shared_substr_len(a: str, b: str) -> int:
    """两个字符串的最长公共连续子串长度（小规模暴力，够用）。"""
    best = 0
    la, lb = len(a), len(b)
    for i in range(la):
        for j in range(lb):
            k = 0
            while i + k < la and j + k < lb and a[i + k] == b[j + k]:
                k += 1
            if k > best:
                best = k
    return best


def _rewrite_note_with_links(path: Path, linked_stems: list[str]) -> None:
    """在笔记末尾追加「## 相关笔记」段（已存在则替换）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(rf"\n## 相关笔记\n.*$", "", text, flags=re.S).rstrip()
    if linked_stems:
        lines = ["", "## 相关笔记", ""]
        lines += [f"- [[{s}]]" for s in linked_stems]
        text += "\n" + "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")


def apply_related_sections() -> None:
    """给每篇笔记追加相关笔记段。链接视为无向：A 关联 B ⇔ B 也列出 A。"""
    notes = task_db.notes_with_notes()
    id_of = {n["note_id"]: n for n in notes}
    stem_of = {Path(n["note_path"]).stem: n["note_id"] for n in notes}
    # 构建无向邻接表
    import sqlite3
    conn = sqlite3.connect(task_db.DB_PATH)
    rows = conn.execute("SELECT from_id, to_id FROM links").fetchall()
    conn.close()
    adj: dict[str, set[str]] = {}
    for f, t in rows:
        adj.setdefault(f, set()).add(t)
        adj.setdefault(t, set()).add(f)
    for n in notes:
        linked_stems = []
        for other_id in adj.get(n["note_id"], set()):
            other = id_of.get(other_id)
            if other and other.get("note_path") and Path(other["note_path"]).exists():
                linked_stems.append(Path(other["note_path"]).stem)
        if linked_stems:
            linked_stems = sorted(set(linked_stems))
            _rewrite_note_with_links(Path(n["note_path"]), linked_stems)


def rebuild_moc() -> None:
    """重建 _索引/<主题>.md + 总览 索引.md。"""
    notes = task_db.notes_with_notes()
    index_dir = writer.index_dir()
    if not notes:
        # 空库也建一个空总览
        _write_index_page(index_dir / "索引.md", [], {})
        return
    topic_map: dict[str, list[dict]] = {}
    for n in notes:
        for t in task_db.get_topics(n["note_id"]):
            topic_map.setdefault(t, []).append(n)
    # 每个主题页
    for topic, ns in topic_map.items():
        ns = sorted(ns, key=lambda x: x["note_id"])
        page = index_dir / f"{_safe_token(topic)}.md"
        body = [f"# {topic}", "",
                "> 该主题下收集的小红书学习笔记。", ""]
        for n in ns:
            body.append(f"- {_link_text(n)} — {n['title']}")
        body.append("")
        body.append(f"> [[索引|← 返回总览]]")
        page.write_text("\n".join(body), encoding="utf-8")
    # 总览页
    _write_index_page(index_dir / "索引.md", notes, topic_map)


def _write_index_page(path: Path, notes: list[dict], topic_map: dict[str, list[dict]]) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = ["# 小红书学习 — 总索引", "",
            f"> 自动生成：{now} · 来源：小红书收藏夹", ""]
    if topic_map:
        body.append("## 主题索引")
        body.append("")
        for topic in sorted(topic_map, key=len, reverse=True):
            body.append(f"- [[{_safe_token(topic)}]]（{len(topic_map[topic])} 篇）")
        body.append("")
    body.append("## 全部笔记")
    body.append("")
    for n in sorted(notes, key=lambda x: x.get("created_at") or ""):
        body.append(f"- {_link_text(n)} — {n['title']}")
    body.append("")
    path.write_text("\n".join(body), encoding="utf-8")
