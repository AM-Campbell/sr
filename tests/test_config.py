"""Tests for sr.config."""

import pathlib

from sr.config import (
    _parse_toml_simple, parse_frontmatter, get_sr_dir, load_settings,
    _config_path, list_vaults, register_vault,
)


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


def test_get_sr_dir_from_env(monkeypatch, tmp_path):
    """SR_DIR env var points to vault root; sr_dir is vault/.sr."""
    vault = tmp_path / "my-vault"
    vault.mkdir()
    monkeypatch.setenv("SR_DIR", str(vault))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    result = get_sr_dir()
    assert result == vault / ".sr"


def test_get_sr_dir_from_config(monkeypatch, tmp_path):
    """Falls back to ~/.config/sr/config DIR= line; returns vault/.sr."""
    monkeypatch.delenv("SR_DIR", raising=False)
    config_dir = tmp_path / ".config" / "sr"
    config_dir.mkdir(parents=True)
    vault = tmp_path / "my-vault"
    vault.mkdir()
    (config_dir / "config").write_text(f"DIR={vault}\n")
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    result = get_sr_dir()
    assert result == vault / ".sr"


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


# ── Vault registry ────────────────────────────────────────────

def test_list_vaults_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    result = list_vaults()
    assert result == []


def test_register_and_list_vaults(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    vault_dir = tmp_path / "my-sr"
    vault_dir.mkdir()
    register_vault(vault_dir)
    vaults = list_vaults()
    assert len(vaults) == 1
    assert vaults[0] == vault_dir.resolve()


def test_register_vault_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    vault_dir = tmp_path / "my-sr"
    vault_dir.mkdir()
    register_vault(vault_dir)
    register_vault(vault_dir)
    config = _config_path()
    vault_lines = [l for l in config.read_text().splitlines() if l.strip().startswith("VAULT=")]
    assert len(vault_lines) == 1


def test_register_vault_preserves_config(monkeypatch, tmp_path):
    """Registering a vault preserves existing DIR= and other lines."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    config = _config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("DIR=/my/sr/dir\n")
    vault_dir = tmp_path / "my-sr"
    vault_dir.mkdir()
    register_vault(vault_dir)
    text = config.read_text()
    assert "DIR=/my/sr/dir" in text
    assert f"VAULT={vault_dir.resolve()}" in text


def test_list_vaults_filters_nonexistent(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    config = _config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("VAULT=/nonexistent/path\n")
    result = list_vaults()
    assert result == []


def test_get_sr_dir_auto_registers(monkeypatch, tmp_path):
    """get_sr_dir() should auto-register the vault."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    vault_dir = tmp_path / "my-sr"
    vault_dir.mkdir()
    monkeypatch.setenv("SR_DIR", str(vault_dir))
    get_sr_dir()
    vaults = list_vaults()
    assert any(v == vault_dir.resolve() for v in vaults)
