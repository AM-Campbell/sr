"""Shared test fixtures."""

import pathlib
import shutil
import sqlite3

import pytest

from sr.app import App
from sr.db import init_db


@pytest.fixture
def tmp_sr_dir(tmp_path):
    """Create a temporary sr directory with schedulers/ subdir."""
    sr_dir = tmp_path / "sr_dir"
    sr_dir.mkdir()
    (sr_dir / "schedulers" / "sm2").mkdir(parents=True)

    # Copy the example scheduler
    bundled_scheduler = pathlib.Path(__file__).parent.parent / "schedulers" / "sm2" / "sm2.py"
    if bundled_scheduler.exists():
        shutil.copy(bundled_scheduler, sr_dir / "schedulers" / "sm2" / "sm2.py")

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
def sample_scheduler(tmp_sr_dir):
    """SM-2 scheduler with temp db_dir."""
    from sr.schedulers import load_scheduler
    db_path = tmp_sr_dir / "sr.db"
    return load_scheduler("sm2", tmp_sr_dir, db_path)
