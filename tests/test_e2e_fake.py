"""Mock 端到端：用 fake 桥 + fake LLM/OCR/STT 跑通 daily_scan 主流程。

不触碰真实浏览器桥、不调用真实 LLM/ASR/OCR。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import app.daily_scan as ds
import app.task_db as tdb
from app.notes import writer

TMP = tempfile.mkdtemp()


class FakeXHS:
    """实现 app.xhs 的 doctor/fetch_saved/fetch_note/download_media 接口。"""
    def __init__(self):
        self._notes = [
            {"id": "n1", "title": "nnU-Net 医学影像分割复现", "author": "A",
             "likes": "10", "type": "normal", "url": "http://x/n1&xsec_token=t"},
            {"id": "n2", "title": "上海探店｜日料", "author": "B",
             "likes": "5", "type": "normal", "url": "http://x/n2&xsec_token=t"},
            {"id": "n3", "title": "Transformer 论文精读", "author": "C",
             "likes": "30", "type": "video", "url": "http://x/n3&xsec_token=t"},
        ]

    def doctor(self) -> bool:
        return True

    def fetch_saved(self, limit: int = 20) -> list:
        from app.xhs import SavedNote
        return [SavedNote(note_id=n["id"], title=n["title"], author=n["author"],
                          likes=n["likes"], note_type=n["type"], url=n["url"])
                for n in self._notes]

    def fetch_saved_videos(self, limit: int = 20) -> list:
        # 模拟「收藏视频」tab 来源：与 saved 部分重叠，验证去重
        from app.xhs import SavedNote
        vids = [n for n in self._notes if n["type"] == "video"]
        vids.append({"id": "n1", "title": "nnU-Net 医学影像分割复现", "author": "A",
                     "likes": "10", "type": "video", "url": "http://x/n1&xsec_token=t"})
        return [SavedNote(note_id=n["id"], title=n["title"], author=n["author"],
                          likes=n["likes"], note_type="video", url=n["url"])
                for n in vids]

    def fetch_note(self, url: str):
        from app.xhs import NoteDetail
        # 从 URL 提取 note_id：形如 http://x/<id>&xsec_token=t
        nid = url.split("/")[-1].split("&")[0]
        n = next(x for x in self._notes if x["id"] == nid)
        return NoteDetail(title=n["title"], author=n["author"],
                          content=f"{n['title']} 技术干货内容", likes=n["likes"])

    def download_media(self, url: str, note_id: str, output_dir: Path) -> Path:
        d = Path(output_dir) / note_id
        d.mkdir(parents=True, exist_ok=True)
        if note_id == "n3":
            (d / f"{note_id}_1.mp4").write_bytes(b"fakevideo")
        else:
            (d / f"{note_id}_1.jpg").write_bytes(b"fakeimage")
        return d


class FakeLLM:
    def chat(self, prompt, system="", model=None, **kw):
        # 返回 markdown 正文 + TOPICS 注释
        return "## 概述\n技术要点\n\n<!-- TOPICS: [\"医学\",\"分割\"] -->"


class FakeMedia:
    def extract_media(self, media_dir, title=""):
        from app.media import MediaExtract
        has_video = list(media_dir.glob("*.mp4"))
        if has_video:
            return MediaExtract(media_kind="视频", media_text="语音转写文本")
        return MediaExtract(media_kind="图文", media_text="【图1】OCR 文本")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    fake = FakeXHS()
    monkeypatch.setattr(writer.settings, "vault_path", str(Path(TMP) / "vault"))
    monkeypatch.setattr(writer.settings, "notes_dir", "小红书学习")
    monkeypatch.setattr(tdb, "DB_PATH", Path(TMP) / "test_state.db")
    monkeypatch.setattr(ds, "DOWNLOADS_DIR", Path(TMP) / "downloads")
    # 清库 + 清 vault，避免用例间状态残留
    tdb._conn().executescript("DROP TABLE IF EXISTS notes; DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS links;")
    tdb._conn().commit()
    vault = Path(TMP) / "vault" / "小红书学习"
    import shutil
    if vault.exists():
        shutil.rmtree(vault)
    # 注入 fake 桥（daily_scan 顶部 `from app.xhs import ...` → patch ds.* 名字）
    monkeypatch.setattr(ds, "doctor", fake.doctor)
    monkeypatch.setattr(ds, "fetch_saved", fake.fetch_saved)
    monkeypatch.setattr(ds, "fetch_saved_videos", fake.fetch_saved_videos)
    monkeypatch.setattr(ds, "fetch_note", fake.fetch_note)
    monkeypatch.setattr(ds, "download_media", fake.download_media)
    monkeypatch.setattr(ds, "_make_llm", lambda: FakeLLM())
    monkeypatch.setattr(ds, "extract_media", FakeMedia().extract_media)
    yield


def test_mock_e2e_full_run():
    processed = ds.run_scan(dry_run=False, limit=10)
    assert processed == 2  # n1 有用 + n3 有用；n2 生活 → 跳过

    # 分类落库
    by_id = {n["note_id"]: n for n in tdb.all_notes()}
    assert by_id["n1"]["classify"] == tdb.USEFUL
    assert by_id["n2"]["classify"] == tdb.NOT_USEFUL
    assert by_id["n3"]["classify"] == tdb.USEFUL

    # 笔记写盘
    root = writer.notes_root()
    files = [p.name for p in root.glob("*.md")]
    assert any("nnU-Net" in f for f in files)
    assert any("Transformer" in f for f in files)

    # 幂等：再次运行不重复处理
    processed2 = ds.run_scan(dry_run=False, limit=10)
    assert processed2 == 0

    # MOC 索引存在
    assert (writer.index_dir() / "索引.md").exists()


def test_mock_e2e_dry_run_non_mutating():
    ds.run_scan(dry_run=True, limit=10)
    # dry-run 不写笔记、不改分类
    assert not any((writer.notes_root().glob("*.md")))
    assert all(n["classify"] == tdb.UNCERTAIN for n in tdb.all_notes())


def test_clean_downloads_clears_cache(tmp_path, monkeypatch):
    """扫描前清空下载缓存（媒体 → 笔记后无需保留原始下载）。"""
    cache = tmp_path / "downloads"
    (cache / "abc123").mkdir(parents=True)
    (cache / "abc123" / "abc123_1.mp4").write_bytes(b"x" * 10)
    monkeypatch.setattr(ds, "DOWNLOADS_DIR", cache)
    monkeypatch.setattr(ds.settings, "clean_downloads", True)
    ds._clean_downloads()
    assert cache.exists()                      # 目录保留
    assert list(cache.iterdir()) == []         # 内容清空


def test_clean_downloads_disabled_keeps_cache(tmp_path, monkeypatch):
    cache = tmp_path / "downloads"
    (cache / "abc123").mkdir(parents=True)
    (cache / "abc123" / "f.mp4").write_bytes(b"x")
    monkeypatch.setattr(ds, "DOWNLOADS_DIR", cache)
    monkeypatch.setattr(ds.settings, "clean_downloads", False)
    ds._clean_downloads()
    assert (cache / "abc123" / "f.mp4").exists()  # 开关关了不清
