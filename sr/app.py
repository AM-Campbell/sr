"""App: central object that wires together sr_dir, db, adapters, scheduler."""

import pathlib
import sqlite3
from typing import Any

from sr.adapters import load_adapter
from sr.config import get_sr_dir, load_settings
from sr.db import init_db
from sr.schedulers import load_scheduler


class App:
    """Holds all shared state for an sr session.

    Usage:
        app = App(sr_dir="/path/to/sr")
        app.init_db()                    # uses sr_dir/sr.db
        app.load_scheduler()             # uses settings["scheduler"]
        results = app.scan_sources([...])
        app.sync_cards(results, ...)
        app.close()

    For testing:
        app = App(sr_dir=tmp_path)
        app.init_db(":memory:")
    """

    def __init__(self, sr_dir: pathlib.Path | str | None = None):
        if sr_dir is None:
            sr_dir = get_sr_dir()
        self.sr_dir = pathlib.Path(sr_dir)
        self.settings = load_settings(self.sr_dir)
        self._adapter_cache: dict[str, Any] = {}
        self.conn: sqlite3.Connection | None = None
        self.scheduler = None

    def init_db(self, db_path: pathlib.Path | str | None = None) -> sqlite3.Connection:
        """Initialize (or connect to) the database.

        Args:
            db_path: Path to the SQLite database file, or ":memory:" for
                     in-memory databases (useful for testing). Defaults to
                     sr_dir/sr.db.
        """
        if db_path is None:
            db_path = self.sr_dir / "sr.db"
        self.conn = init_db(db_path)
        return self.conn

    def get_adapter(self, name: str):
        """Load an adapter by name (cached)."""
        if name not in self._adapter_cache:
            self._adapter_cache[name] = load_adapter(name, self.sr_dir)
        return self._adapter_cache[name]

    def load_scheduler(self, name: str | None = None):
        """Load and store the scheduler.

        Args:
            name: Scheduler name. Defaults to settings["scheduler"].
        """
        if name is None:
            name = self.settings.get("scheduler", "sm2")
        db_path = self.sr_dir / "sr.db"
        self.scheduler = load_scheduler(name, self.sr_dir, db_path)
        return self.scheduler

    def scan_sources(self, paths: list[pathlib.Path]):
        """Scan paths for card sources using this app's adapter resolver."""
        from sr.scanner import scan_sources
        return scan_sources(paths, self.get_adapter)

    def sync_cards(self, scan_results, scanned_paths=None) -> dict:
        """Sync scanned cards to the database."""
        from sr.sync import sync_cards
        return sync_cards(self.conn, scan_results, self.scheduler, scanned_paths)

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
