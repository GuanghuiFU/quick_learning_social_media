"""writer/linker 单测：目录岔开硬校验、frontmatter、图片引用、MOC 重建。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import app.task_db as tdb
from app.notes import linker, writer

TMP = tempfile.mkdtemp()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # 指向临时 vault，避免污染真实 Obsidian
    monkeypatch.setattr(writer.settings, "vault_path", str(Path(TMP) / "vault"))
    monkeypatch.setattr(writer.settings, "notes_dir", "小红书学习")
    monkeypatch.setattr(tdb, "DB_PATH", Path(TMP) / "test_state.db")
    yield


def _make_note(note_id: str, title: str, topics: list[str], body: str = "内容"):
    tdb.register_note(note_id, title, "作者", f"http://x/{note_id}&xsec_token=t")
    tdb.set_classify(note_id, tdb.USEFUL)
    path = writer.save_note(title=title, source_url=f"http://x/{note_id}&xsec_token=t",
                            body=body, author="作者", xhs_id=note_id, topics=topics)
    tdb.set_topics(note_id, topics)
    tdb.set_stage(note_id, "notes", path=str(path))
    return path


def test_never_writes_learning_notes_dir():
    """硬校验：笔记绝不能写进视频项目的「学习笔记」目录。"""
    path = writer.save_note(title="测试", source_url="http://x&xsec_token=t", body="x")
    assert "学习笔记" not in path.parts
    assert path.parent.name == "小红书学习"


def test_frontmatter_fields():
    path = writer.save_note(title="医学影像分割", source_url="http://x&xsec_token=t",
                            body="内容", author="作者", likes="99",
                            note_type="normal", topics=["分割", "医学"], xhs_id="abc")
    text = path.read_text(encoding="utf-8")
    for field in ("title:", "source:", "date:", "created:", "tags:", "xhs_id:", "author:", "likes:", "topics:"):
        assert field in text, f"缺 frontmatter 字段 {field}"


def test_image_ref_fixing():
    fixed = writer.fix_image_refs("！[[图1.jpg]] ![[`图2.jpg`]] ![[ 图3.jpg ]]")
    assert "![[图1.jpg]]" in fixed
    assert "`" not in fixed
    assert fixed.count("![[") == 3


def test_save_attachment_goes_to_attachments():
    p = writer.save_attachment(b"abc", "img.png")
    assert "小红书学习" in p.parts
    assert "attachments" in p.parts


def test_links_and_moc():
    p1 = _make_note("a", "医学影像分割", ["医学", "分割"])
    p2 = _make_note("b", "CT 分割新方法", ["医学", "分割"])
    p3 = _make_note("c", "日常美食", ["美食"])
    linker.compute_links()
    assert len(tdb.links_from("a")) >= 1
    linker.apply_related_sections()
    linker.rebuild_moc()
    # 主题页存在
    index_dir = writer.index_dir()
    assert (index_dir / "医学.md").exists()
    assert (index_dir / "索引.md").exists()
    # 相关笔记段引用的是真实文件名
    text = Path(p1).read_text(encoding="utf-8")
    assert "## 相关笔记" in text
    assert p2.stem in text          # 共享主题 → 链接到 b
    assert p3.stem not in text      # 不同主题 → 不链接


def test_moc_link_uses_real_stem():
    """MOC 里 wikilink 必须是真实存在的文件名。"""
    p = _make_note("a", "医学影像分割", ["医学"])
    linker.rebuild_moc()
    page = (writer.index_dir() / "医学.md").read_text(encoding="utf-8")
    assert p.stem in page
