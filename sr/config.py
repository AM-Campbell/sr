"""Configuration helpers: sr directory discovery, settings, frontmatter parsing."""

import os
import pathlib
import sys


def get_sr_dir() -> pathlib.Path:
    """Discover the sr directory (vault_root/.sr).

    Priority: SR_DIR env var > ~/.config/sr/config DIR= line.
    Both should point to the vault root; sr_dir is always vault/.sr.
    No silent default — errors if neither is set.
    """
    env_dir = os.environ.get("SR_DIR")
    if env_dir:
        vault = pathlib.Path(env_dir)
        register_vault(vault)
        return vault / ".sr"

    config_path = pathlib.Path.home() / ".config" / "sr" / "config"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DIR="):
                vault = pathlib.Path(line[4:].strip()).expanduser()
                register_vault(vault)
                return vault / ".sr"

    print("sr: no vault configured.", file=sys.stderr)
    print(f"  Run 'sr init' in your notes directory, or 'sr init /path/to/vault'", file=sys.stderr)
    sys.exit(1)


def _config_path() -> pathlib.Path:
    """Return the path to ~/.config/sr/config."""
    return pathlib.Path.home() / ".config" / "sr" / "config"


def list_vaults() -> list[pathlib.Path]:
    """Read VAULT= lines from the config file, return list of existing paths."""
    config = _config_path()
    if not config.exists():
        return []
    paths = []
    for line in config.read_text().splitlines():
        line = line.strip()
        if line.startswith("VAULT="):
            p = pathlib.Path(line[6:].strip()).expanduser()
            if p.exists():
                paths.append(p)
    return paths


def register_vault(path: pathlib.Path) -> None:
    """Add a VAULT= line to the config file if not already listed."""
    resolved = path.resolve()
    config = _config_path()
    existing = set()
    lines = []
    if config.exists():
        lines = config.read_text().splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("VAULT="):
                existing.add(pathlib.Path(stripped[6:].strip()).expanduser().resolve())
    if resolved in existing:
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    lines.append(f"VAULT={resolved}")
    config.write_text("\n".join(lines) + "\n")


def set_active_vault(path: pathlib.Path) -> None:
    """Set the DIR= line in the config file to the given vault path."""
    resolved = path.resolve()
    config = _config_path()
    lines = []
    if config.exists():
        lines = [l for l in config.read_text().splitlines()
                 if not l.strip().startswith("DIR=")]
    lines.insert(0, f"DIR={resolved}")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n")
    register_vault(resolved)


def get_active_vault() -> pathlib.Path | None:
    """Return the active vault path from DIR= line, or None."""
    config = _config_path()
    if not config.exists():
        return None
    for line in config.read_text().splitlines():
        line = line.strip()
        if line.startswith("DIR="):
            p = pathlib.Path(line[4:].strip()).expanduser()
            if p.exists():
                return p
    return None


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
