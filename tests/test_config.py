"""Tests for sr.config."""

import pathlib

from sr.config import _parse_toml_simple, parse_frontmatter, get_sr_dir, load_settings


def test_parse_toml_simple_basic():
    text = 'scheduler = "sm2"\nreview_port = 8791'
    result = _parse_toml_simple(text)
    assert result == {"scheduler": "sm2", "review_port": 8791}


def test_parse_toml_simple_booleans():
    text = "enabled = true\ndisabled = false"
    result = _parse_toml_simple(text)
    assert result == {"enabled": True, "disabled": False}


def test_parse_toml_simple_comments_and_blanks():
    text = "# comment\n\nkey = 42\n"
    result = _parse_toml_simple(text)
    assert result == {"key": 42}


def test_parse_frontmatter_basic():
    text = "---\nsr_adapter: mnmd\ntags: [python, basics]\n---\nQ: hi\nA: lo"
    meta, body = parse_frontmatter(text)
    assert meta["sr_adapter"] == "mnmd"
    assert meta["tags"] == ["python", "basics"]
    assert body.startswith("Q: hi")


def test_parse_frontmatter_no_frontmatter():
    text = "Just text"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Just text"


def test_parse_frontmatter_unclosed():
    text = "---\nsr_adapter: mnmd\nno closing"
    meta, body = parse_frontmatter(text)
    assert meta == {}


def test_parse_frontmatter_quoted_values():
    text = '---\nname: "hello world"\n---\nbody'
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "hello world"


def test_parse_frontmatter_boolean_values():
    text = "---\nsuspended: true\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta["suspended"] is True


def test_get_sr_dir_from_env(monkeypatch, capsys):
    """SR_DIR env var is used and a warning is printed."""
    monkeypatch.setenv("SR_DIR", "/tmp/test-sr")
    result = get_sr_dir()
    assert result == pathlib.Path("/tmp/test-sr")
    captured = capsys.readouterr()
    assert "SR_DIR" in captured.err


def test_get_sr_dir_from_config(monkeypatch, tmp_path):
    """Falls back to ~/.config/sr/config DIR= line."""
    monkeypatch.delenv("SR_DIR", raising=False)
    config_dir = tmp_path / ".config" / "sr"
    config_dir.mkdir(parents=True)
    (config_dir / "config").write_text("DIR=/my/sr/dir\n")
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    result = get_sr_dir()
    assert result == pathlib.Path("/my/sr/dir")


def test_get_sr_dir_no_config_exits(monkeypatch, tmp_path):
    """Errors if neither env var nor config file is set."""
    import pytest
    monkeypatch.delenv("SR_DIR", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit):
        get_sr_dir()


def test_load_settings_default(tmp_path):
    settings = load_settings(tmp_path)
    assert settings["scheduler"] == "sm2"
    assert settings["review_port"] == 8791


def test_load_settings_with_file(tmp_path):
    (tmp_path / "settings.toml").write_text('review_port = 9000\nscheduler = "custom"')
    settings = load_settings(tmp_path)
    assert settings["review_port"] == 9000
    assert settings["scheduler"] == "custom"
