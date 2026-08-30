"""Obsidian 笔记写入：小红书收藏 → vault 顶层「小红书学习/」。

关键约束：绝不写进视频项目的「学习笔记/」目录（两者分开）。
内容优先于形式：笔记平铺在 <vault>/小红书学习/，不按图文/视频分子目录；
图片附件落 <vault>/小红书学习/attachments/，主题索引落 <vault>/小红书学习/_索引/。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from app.config import settings

DIR_ATTACHMENTS = "attachments"
DIR_INDEX = "_索引"

# 允许作为 wikilink 目标的笔记文件名（不含 .md），供 linker 校验防悬空
_IMG_REF_RE = re.compile(r"[!！]\[\[([^\]]+)\]\]")


def _safe_filename(title: str) -> str:
    """去掉标题里不安全的文件名字符。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())[:80] or "untitled"


def dated_filename(title: str, note_date: object | None) -> str:
    """以入库日期作前缀生成文件名：YYYY-MM-DD_标题.md。

    日期取 note_date（入库时间），保证 YYYY-MM-DD 字典序 = 时间顺序，便于按时间查找。
    """
    prefix = ""
    if note_date is not None:
        # 兼容 date / datetime / "YYYY-MM-DD..." 字符串
        s = str(note_date)[:10]
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            prefix = f"{s}_"
    return f"{prefix}{_safe_filename(title)}.md"


def notes_root() -> Path:
    root = settings.notes_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def attachments_dir() -> Path:
    d = notes_root() / DIR_ATTACHMENTS
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_dir() -> Path:
    d = notes_root() / DIR_INDEX
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_attachment(data: bytes, filename: str) -> Path:
    """保存图片附件，返回落盘路径。"""
    safe = Path(filename).name
    dest = attachments_dir() / safe
    dest.write_bytes(data)
    return dest


def fix_image_refs(markdown: str) -> str:
    """修正图片引用格式问题：中文感叹号、多余反引号、首尾空白。"""
    def _fix(m: re.Match) -> str:
        name = m.group(1).replace("`", "").strip()
        return f"![[{name}]]"
    return _IMG_REF_RE.sub(_fix, markdown)


def validate_image_refs(markdown: str) -> list[str]:
    """返回格式有问题的引用列表（空=全正常）。"""
    problems = []
    for m in _IMG_REF_RE.finditer(markdown):
        raw = m.group(0)
        name = m.group(1)
        if raw.startswith("！"):
            problems.append(f"{raw} → 中文感叹号")
        elif "`" in name:
            problems.append(f"{raw} → 含多余反引号")
        elif name != name.strip():
            problems.append(f"{raw} → 首尾空白")
    return problems


def build_note_frontmatter(*, title: str, source_url: str, author: str = "",
                           likes: str = "", note_type: str = "",
                           topics: list[str] | None = None, xhs_id: str = "",
                           tags: list[str] | None = None,
                           note_date: object | None = None) -> list[str]:
    """frontmatter 行。date 用入库日期 note_date，created 用实际生成时刻。"""
    # 入库日期（名字前缀 / date 字段）
    d_str = str(note_date)[:10] if note_date else datetime.date.today().isoformat()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _tags = tags or ["学习", "小红书"]
    tag_str = ", ".join(_tags)
    lines = [
        "---",
        f'title: "{title}"',
        f'source: "{source_url}"',
        f'date: "{d_str}"',
        f'created: "{now}"',
        f"tags: [{tag_str}]",
    ]
    if xhs_id:
        lines.append(f'xhs_id: "{xhs_id}"')
    if author:
        lines.append(f'author: "{author}"')
    if likes:
        lines.append(f'likes: "{likes}"')
    if note_type:
        lines.append(f'note_type: "{note_type}"')
    if topics:
        t = ", ".join(f'"{t}"' for t in topics)
        lines.append(f"topics: [{t}]")
    lines.append("---")
    return lines


def build_note(*, title: str, source_url: str, body: str, author: str = "",
               likes: str = "", note_type: str = "", topics: list[str] | None = None,
               xhs_id: str = "", note_date: object | None = None,
               transcript: str = "") -> str:
    """组装完整 Markdown 笔记（frontmatter + 生成时间 + 来源 + 正文 [+ 转写附录]）。"""
    fm = build_note_frontmatter(
        title=title, source_url=source_url, author=author, likes=likes,
        note_type=note_type, topics=topics, xhs_id=xhs_id, note_date=note_date,
    )
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d_str = str(note_date)[:10] if note_date else datetime.date.today().isoformat()
    lines = fm + [
        "",
        f"# {title}",
        "",
        f"> 生成时间：{now}",
        "",
        f"> 入库日期：{d_str}",
        "",
    ]
    if source_url:
        lines.append(f"> 📚 原文链接：{source_url}")
        lines.append("")
    lines.append("## 内容")
    lines.append("")
    lines.append(fix_image_refs(body.strip()))
    if transcript.strip():
        lines += ["", "## 语音转写（完整附录）", "",
                  "> 以下为视频音频 ASR 转写原文（带时间点），供核对与检索。", ""]
        lines.append(transcript.strip())
    return "\n".join(lines)


def save_note(*, title: str, source_url: str, body: str, author: str = "",
              likes: str = "", note_type: str = "", topics: list[str] | None = None,
              xhs_id: str = "", note_date: object | None = None,
              transcript: str = "") -> Path:
    """写入笔记到 <vault>/小红书学习/YYYY-MM-DD_标题.md。返回路径。

    note_date 为入库时间（文件名前缀 + frontmatter.date）；缺省用今天。
    transcript 为视频 ASR 转写原文，非空时作为附录写入。
    """
    path = notes_root() / dated_filename(title, note_date)
    path.write_text(
        build_note(title=title, source_url=source_url, body=body, author=author,
                   likes=likes, note_type=note_type, topics=topics, xhs_id=xhs_id,
                   note_date=note_date, transcript=transcript),
        encoding="utf-8",
    )
    return path
