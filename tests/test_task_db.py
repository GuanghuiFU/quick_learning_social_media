"""task_db 单测：去重幂等、阶段门控、分类、重试上限。用独立临时 DB。"""
from __future__ import annotations

import os
import tempfile

import pytest

import app.task_db as tdb

DB_PATH = tdb.DB_PATH
TMP = tempfile.mkdtemp()


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    monkeypatch.setattr(tdb, "DB_PATH", __import__("pathlib").Path(TMP) / "test_state.db")
    tdb._conn().executescript("DROP TABLE IF EXISTS notes; DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS links;")
    tdb._conn().commit()
    yield


def test_register_dedup():
    assert tdb.register_note("id1", "标题", "作者", "http://u?id=1&xsec_token=t") is True
    assert tdb.register_note("id1", "标题", "作者", "http://u?id=1&xsec_token=t") is False
    assert len(tdb.all_notes()) == 1


def test_classify_and_stage_gates():
    tdb.register_note("id1", "标题", "作者", "http://u?id=1&xsec_token=t")
    assert tdb.get_note("id1")["classify"] == tdb.UNCERTAIN
    tdb.set_classify("id1", tdb.USEFUL, "测试")
    assert tdb.get_note("id1")["classify"] == tdb.USEFUL
    assert tdb.is_stage_done("id1", "notes") is False
    tdb.set_stage("id1", "notes", path="/tmp/x.md")
    assert tdb.is_stage_done("id1", "notes") is True
    assert tdb.get_note("id1")["note_path"] == "/tmp/x.md"


def test_attempts_and_pending():
    tdb.register_note("id1", "标题", "作者", "http://u?id=1&xsec_token=t")
    tdb.set_classify("id1", tdb.USEFUL)
    for _ in range(3):
        tdb.record_error("id1", "boom")
    # 3 次后 candidates 排除（attempts>=3）
    assert all(n["note_id"] != "id1" for n in tdb.candidates())


def test_candidates_include_uncertain_exclude_done():
    tdb.register_note("u1", "未分类", "作者", "http://u1&xsec_token=t")
    tdb.register_note("d1", "已完成", "作者", "http://d1&xsec_token=t")
    tdb.set_classify("d1", tdb.USEFUL)
    tdb.set_stage("d1", "notes", path="/tmp/d.md")
    ids = {n["note_id"] for n in tdb.candidates()}
    assert "u1" in ids       # 未分类 → 候选
    assert "d1" not in ids   # 已完成 → 排除


def test_links_unique():
    tdb.register_note("a", "A", "", "http://a&xsec_token=t")
    tdb.register_note("b", "B", "", "http://b&xsec_token=t")
    tdb.add_link("a", "b", "topic")
    tdb.add_link("a", "b", "topic")
    assert len(tdb.links_from("a")) == 1


def test_caption_storage():
    tdb.register_note("id1", "标题", "作者", "http://u?id=1&xsec_token=t")
    tdb.set_caption("id1", "正文内容")
    assert tdb.get_note("id1")["caption"] == "正文内容"
    assert tdb.is_stage_done("id1", "caption") is True
