"""Scheduler loading."""

import importlib.util
import pathlib


def load_scheduler(name: str, sr_dir: pathlib.Path, core_db_path: pathlib.Path):
    sched_dir = sr_dir / "schedulers" / name
    sched_path = sched_dir / f"{name}.py"
    if not sched_path.exists():
        raise FileNotFoundError(f"Scheduler not found: {sched_path}")
    spec = importlib.util.spec_from_file_location(f"sr_scheduler_{name}", str(sched_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Scheduler(str(sched_dir), str(core_db_path))
