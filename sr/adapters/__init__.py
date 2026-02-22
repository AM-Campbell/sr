"""Adapter loading with built-in adapter registry."""

import importlib
import importlib.util
import pathlib

_BUILTIN_ADAPTERS = {
    "mnmd": "sr.adapters.mnmd",
}


def load_adapter(name: str, sr_dir: pathlib.Path | None = None):
    # Check user override first
    if sr_dir is not None:
        adapter_path = sr_dir / "adapters" / f"{name}.py"
        if adapter_path.exists():
            spec = importlib.util.spec_from_file_location(f"sr_adapter_{name}", str(adapter_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.Adapter()

    # Check built-in adapters
    if name in _BUILTIN_ADAPTERS:
        mod = importlib.import_module(_BUILTIN_ADAPTERS[name])
        return mod.Adapter()

    raise FileNotFoundError(f"Adapter not found: {name}")
