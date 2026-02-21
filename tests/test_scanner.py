"""Tests for sr.scanner."""

import pathlib

from sr.models import Card
from sr.scanner import content_hash, scan_sources


class FakeAdapter:
    def parse(self, text, path, config):
        return [Card(key="fake_1", content={"text": text[:20]}, display_text="fake")]


def _fake_get_adapter(name):
    return FakeAdapter()


def test_content_hash():
    h1 = content_hash({"a": 1, "b": 2})
    h2 = content_hash({"b": 2, "a": 1})
    assert h1 == h2  # sorted keys


def test_content_hash_different():
    h1 = content_hash({"a": 1})
    h2 = content_hash({"a": 2})
    assert h1 != h2


def test_scan_md_file(tmp_path):
    md = tmp_path / "test.md"
    md.write_text("---\nsr_adapter: basic_qa\n---\nQ: hi\nA: lo\n")
    results = scan_sources([md], _fake_get_adapter)
    assert len(results) == 1
    assert results[0][1] == "basic_qa"
    assert len(results[0][2]) == 1


def test_scan_md_file_no_adapter(tmp_path):
    md = tmp_path / "test.md"
    md.write_text("# Just a readme\nNo frontmatter adapter.\n")
    results = scan_sources([md], _fake_get_adapter)
    assert len(results) == 0


def test_scan_directory_with_md(tmp_path):
    subdir = tmp_path / "notes"
    subdir.mkdir()
    (subdir / "a.md").write_text("---\nsr_adapter: basic_qa\n---\nQ: q1\nA: a1\n")
    (subdir / "b.txt").write_text("not a markdown")
    results = scan_sources([subdir], _fake_get_adapter)
    assert len(results) == 1


def test_scan_directory_recursive(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "c.md").write_text("---\nsr_adapter: basic_qa\n---\nQ: q\nA: a\n")
    results = scan_sources([tmp_path], _fake_get_adapter)
    assert len(results) == 1


def test_scan_sr_config_dir(tmp_path):
    (tmp_path / ".sr.config").write_text('adapter = "basic_qa"\n')
    (tmp_path / "file1.txt").write_text("some content")
    (tmp_path / "file2.txt").write_text("more content")
    results = scan_sources([tmp_path], _fake_get_adapter)
    assert len(results) == 2  # Both files parsed by adapter


def test_scan_deduplicates(tmp_path):
    md = tmp_path / "test.md"
    md.write_text("---\nsr_adapter: basic_qa\n---\nQ: hi\nA: lo\n")
    results = scan_sources([md, md], _fake_get_adapter)
    assert len(results) == 1
