"""Configuration helpers: sr directory discovery, settings, frontmatter parsing."""

import pathlib


def get_sr_dir() -> pathlib.Path:
    config_path = pathlib.Path.home() / ".config" / "sr" / "config"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DIR="):
                return pathlib.Path(line[4:].strip())
    default = pathlib.Path.home() / ".local" / "share" / "sr"
    return default


def load_settings(sr_dir: pathlib.Path) -> dict:
    settings_path = sr_dir / "settings.toml"
    settings = {"scheduler": "sm2", "review_port": 8791}
    if settings_path.exists():
        settings.update(_parse_toml_simple(settings_path.read_text()))
    return settings


def _parse_toml_simple(text: str) -> dict:
    """Minimal TOML parser for flat key=value files."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.isdigit():
                v = int(v)
            elif v == "true":
                v = True
            elif v == "false":
                v = False
            result[k] = v
    return result


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown. Returns (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    yaml_block = text[3:end].strip()
    body = text[end + 4:].strip()
    meta = {}
    for line in yaml_block.splitlines():
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
            elif v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            elif v.isdigit():
                v = int(v)
            meta[k] = v
    return meta, body
