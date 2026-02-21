"""Adapter loading."""

import importlib.util
import pathlib
from typing import Any


def load_adapter(name: str, sr_dir: pathlib.Path):
    adapter_path = sr_dir / "adapters" / f"{name}.py"
    if not adapter_path.exists():
        raise FileNotFoundError(f"Adapter not found: {adapter_path}")
    spec = importlib.util.spec_from_file_location(f"sr_adapter_{name}", str(adapter_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Adapter()
