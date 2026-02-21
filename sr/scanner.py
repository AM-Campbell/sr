"""Source scanning: find markdown files and directories, parse cards via adapters."""

import hashlib
import json
import pathlib
import sys
from typing import Callable

from sr.config import parse_frontmatter, _parse_toml_simple
from sr.models import Card


def content_hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def scan_sources(paths: list[pathlib.Path], get_adapter_fn: Callable[[str], object]
                 ) -> list[tuple[str, str, list[Card], dict]]:
    """Scan paths for card sources.

    Args:
        paths: File/directory paths to scan.
        get_adapter_fn: Callable that takes adapter name and returns adapter instance.

    Returns list of (source_path, adapter_name, cards, config).
    """
    results = []
    seen_paths: set[str] = set()

    for path in paths:
        path = path.resolve()
        if path.is_file() and path.suffix == ".md":
            _scan_md_file(path, get_adapter_fn, results, seen_paths)
        elif path.is_dir():
            _scan_directory(path, get_adapter_fn, results, seen_paths)

    return results


def _scan_md_file(path: pathlib.Path, get_adapter_fn: Callable[[str], object],
                  results: list, seen_paths: set):
    if str(path) in seen_paths:
        return
    seen_paths.add(str(path))
    try:
        text = path.read_text()
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return
    meta, body = parse_frontmatter(text)
    adapter_name = meta.get("sr_adapter")
    if not adapter_name:
        return
    try:
        adapter = get_adapter_fn(adapter_name)
        cards = adapter.parse(text, str(path), meta)
        results.append((str(path), adapter_name, cards, meta))
    except Exception as e:
        print(f"Warning: adapter '{adapter_name}' failed on {path}: {e}", file=sys.stderr)


def _scan_directory(dirpath: pathlib.Path, get_adapter_fn: Callable[[str], object],
                    results: list, seen_paths: set):
    config_path = dirpath / ".sr.config"
    if config_path.exists():
        config = _parse_toml_simple(config_path.read_text())
        adapter_name = config.get("adapter")
        if not adapter_name:
            print(f"Warning: .sr.config in {dirpath} missing 'adapter'", file=sys.stderr)
            return
        try:
            adapter = get_adapter_fn(adapter_name)
        except Exception as e:
            print(f"Warning: cannot load adapter '{adapter_name}': {e}", file=sys.stderr)
            return
        for f in sorted(dirpath.iterdir()):
            if f.is_file() and f.name != ".sr.config" and str(f) not in seen_paths:
                seen_paths.add(str(f))
                try:
                    text = f.read_text()
                    cards = adapter.parse(text, str(f), config)
                    results.append((str(f), adapter_name, cards, config))
                except Exception as e:
                    print(f"Warning: adapter '{adapter_name}' failed on {f}: {e}", file=sys.stderr)
    else:
        try:
            entries = sorted(dirpath.iterdir())
        except PermissionError:
            return
        for item in entries:
            if item.is_dir() and not item.name.startswith("."):
                _scan_directory(item, get_adapter_fn, results, seen_paths)
            elif item.is_file() and item.suffix == ".md":
                _scan_md_file(item, get_adapter_fn, results, seen_paths)
