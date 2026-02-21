"""Tests for sr.config."""

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
    text = "---\nsr_adapter: basic_qa\ntags: [python, basics]\n---\nQ: hi\nA: lo"
    meta, body = parse_frontmatter(text)
    assert meta["sr_adapter"] == "basic_qa"
    assert meta["tags"] == ["python", "basics"]
    assert body.startswith("Q: hi")


def test_parse_frontmatter_no_frontmatter():
    text = "Just text"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "Just text"


def test_parse_frontmatter_unclosed():
    text = "---\nsr_adapter: basic_qa\nno closing"
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


def test_get_sr_dir_default():
    # Should return a Path (we can't guarantee config file state)
    import pathlib
    result = get_sr_dir()
    assert isinstance(result, pathlib.Path)


def test_load_settings_default(tmp_path):
    settings = load_settings(tmp_path)
    assert settings["scheduler"] == "sm2"
    assert settings["review_port"] == 8791


def test_load_settings_with_file(tmp_path):
    (tmp_path / "settings.toml").write_text('review_port = 9000\nscheduler = "custom"')
    settings = load_settings(tmp_path)
    assert settings["review_port"] == 9000
    assert settings["scheduler"] == "custom"
