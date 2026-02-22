"""Tests for CLI argument parsing and command dispatch."""

import pathlib
from unittest.mock import patch, MagicMock, call

from sr.cli import main, cmd_scan, cmd_status


def test_no_command_prints_help(capsys):
    with patch("sys.argv", ["sr"]):
        try:
            main()
        except SystemExit:
            pass
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower() or "sr" in captured.out


def test_scan_dispatches_correctly():
    """Verify scan command calls init_db, load_scheduler, scan_sources, sync_cards."""
    with patch("sys.argv", ["sr", "scan", "/tmp/test"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app
            mock_app.sr_dir = MagicMock()
            mock_app.sr_dir.exists.return_value = True
            mock_app.settings = {"scheduler": "sm2"}
            mock_app.scan_sources.return_value = []
            mock_app.sync_cards.return_value = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}

            main()

            mock_app.init_db.assert_called_once()
            mock_app.load_scheduler.assert_called_once_with("sm2")
            mock_app.scan_sources.assert_called_once()
            # Verify the path was resolved and passed
            scan_args = mock_app.scan_sources.call_args[0][0]
            assert len(scan_args) == 1
            assert str(scan_args[0]) == str(pathlib.Path("/tmp/test").resolve())
            mock_app.sync_cards.assert_called_once()
            mock_app.close.assert_called_once()


def test_scan_default_path_is_cwd():
    """When no path is given, scan should use cwd."""
    with patch("sys.argv", ["sr", "scan"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app
            mock_app.sr_dir = MagicMock()
            mock_app.sr_dir.exists.return_value = True
            mock_app.settings = {"scheduler": "sm2"}
            mock_app.scan_sources.return_value = []
            mock_app.sync_cards.return_value = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}

            main()

            scan_args = mock_app.scan_sources.call_args[0][0]
            assert scan_args == [pathlib.Path.cwd()]


def test_status_no_db(capsys):
    """Status command with no database prints message and returns."""
    with patch("sys.argv", ["sr", "status"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app
            mock_app.sr_dir = MagicMock()
            mock_app.sr_dir.exists.return_value = True
            # Make db_path.exists() return False
            db_path_mock = MagicMock()
            db_path_mock.exists.return_value = False
            mock_app.sr_dir.__truediv__ = MagicMock(return_value=db_path_mock)

            main()

            captured = capsys.readouterr()
            assert "No database" in captured.out
            # init_db should NOT have been called
            mock_app.init_db.assert_not_called()


def test_status_with_db(capsys):
    """Status command with a database queries and prints stats."""
    from sr.db import init_db

    with patch("sys.argv", ["sr", "status"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app

            # Use a real sr_dir path so (sr_dir / "sr.db").exists() works
            mock_sr_dir = MagicMock()
            mock_sr_dir.exists.return_value = True
            db_path_mock = MagicMock()
            db_path_mock.exists.return_value = True
            mock_sr_dir.__truediv__ = MagicMock(return_value=db_path_mock)
            mock_app.sr_dir = mock_sr_dir

            # Use a real in-memory DB so queries work
            conn = init_db(":memory:")
            mock_app.conn = conn
            mock_app.settings = {"scheduler": "sm2"}

            main()

            captured = capsys.readouterr()
            assert "Cards:" in captured.out
            assert "Due now:" in captured.out
            mock_app.close.assert_called_once()
            conn.close()
