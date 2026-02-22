"""Tests for the adapter loading/registry system (sr.adapters.__init__)."""

import pathlib
import tempfile

import pytest

from sr.adapters import load_adapter


def test_load_builtin():
    """Built-in adapters can be loaded by name."""
    adapter = load_adapter("mnmd")
    assert hasattr(adapter, "parse")
    assert hasattr(adapter, "render_front")
    assert hasattr(adapter, "render_back")


def test_load_builtin_no_sr_dir():
    """Built-in adapters load when sr_dir is explicitly None."""
    adapter = load_adapter("mnmd", sr_dir=None)
    assert hasattr(adapter, "parse")


def test_load_builtin_sr_dir_without_override():
    """Built-in adapters load when sr_dir exists but has no override file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = load_adapter("mnmd", pathlib.Path(tmpdir))
        assert hasattr(adapter, "parse")


def test_user_override_takes_precedence():
    """An adapter file in sr_dir/adapters/ overrides the built-in."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter_dir = pathlib.Path(tmpdir) / "adapters"
        adapter_dir.mkdir()
        (adapter_dir / "mnmd.py").write_text(
            "class Adapter:\n"
            "    custom = True\n"
            "    def parse(self, text, path, config): return []\n"
            "    def render_front(self, c): return ''\n"
            "    def render_back(self, c): return ''\n"
        )
        adapter = load_adapter("mnmd", pathlib.Path(tmpdir))
        assert adapter.custom is True


def test_user_adapter_no_builtin():
    """A user adapter that has no built-in counterpart still loads."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter_dir = pathlib.Path(tmpdir) / "adapters"
        adapter_dir.mkdir()
        (adapter_dir / "custom.py").write_text(
            "class Adapter:\n"
            "    def parse(self, text, path, config): return []\n"
            "    def render_front(self, c): return ''\n"
            "    def render_back(self, c): return ''\n"
        )
        adapter = load_adapter("custom", pathlib.Path(tmpdir))
        assert hasattr(adapter, "parse")


def test_not_found_raises():
    """Loading a nonexistent adapter raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_adapter("nonexistent")


def test_not_found_with_sr_dir_raises():
    """Loading a nonexistent adapter with an sr_dir still raises."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(FileNotFoundError):
            load_adapter("nonexistent", pathlib.Path(tmpdir))
