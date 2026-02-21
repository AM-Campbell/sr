"""Shared test fixtures."""

import pathlib
import shutil
import sqlite3

import pytest

from sr.app import App
from sr.db import init_db


@pytest.fixture
def tmp_sr_dir(tmp_path):
    """Create a temporary sr directory with adapters/ and schedulers/ subdirs."""
    sr_dir = tmp_path / "sr_dir"
    sr_dir.mkdir()
    (sr_dir / "adapters").mkdir()
    (sr_dir / "schedulers" / "sm2").mkdir(parents=True)

    # Copy the example adapter
    example_adapter = pathlib.Path(__file__).parent.parent / "example_sr_dir" / "adapters" / "basic_qa.py"
    if example_adapter.exists():
        shutil.copy(example_adapter, sr_dir / "adapters" / "basic_qa.py")

    # Copy the example scheduler
    example_scheduler = pathlib.Path(__file__).parent.parent / "example_sr_dir" / "schedulers" / "sm2" / "sm2.py"
    if example_scheduler.exists():
        shutil.copy(example_scheduler, sr_dir / "schedulers" / "sm2" / "sm2.py")

    return sr_dir


@pytest.fixture
def db_conn():
    """In-memory SQLite database with schema applied."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def app(tmp_sr_dir):
    """App instance with tmp sr_dir and in-memory DB."""
    a = App(sr_dir=tmp_sr_dir)
    a.init_db(":memory:")
    return a


@pytest.fixture
def sample_adapter(tmp_sr_dir):
    """Load the basic_qa adapter."""
    from sr.adapters import load_adapter
    return load_adapter("basic_qa", tmp_sr_dir)


@pytest.fixture
def sample_scheduler(tmp_sr_dir):
    """SM-2 scheduler with temp db_dir."""
    from sr.schedulers import load_scheduler
    db_path = tmp_sr_dir / "sr.db"
    return load_scheduler("sm2", tmp_sr_dir, db_path)
