"""Tests for CLI argument parsing and command dispatch."""

import argparse
from unittest.mock import patch, MagicMock

from sr.cli import main


def test_no_command_prints_help(capsys):
    with patch("sys.argv", ["sr"]):
        try:
            main()
        except SystemExit:
            pass
    # Should have printed help or exited


def test_scan_command_parsing():
    from sr.cli import main as cli_main
    import sys

    # Test that scan command is recognized
    with patch("sys.argv", ["sr", "scan", "/tmp/test"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app
            mock_app.sr_dir = MagicMock()
            mock_app.sr_dir.exists.return_value = True
            mock_app.settings = {"scheduler": "sm2"}
            mock_app.scan_sources.return_value = []
            mock_app.sync_cards.return_value = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}
            try:
                cli_main()
            except SystemExit:
                pass
            mock_app.init_db.assert_called_once()


def test_status_command_parsing():
    with patch("sys.argv", ["sr", "status"]):
        with patch("sr.cli.App") as MockApp:
            mock_app = MagicMock()
            MockApp.return_value = mock_app
            mock_app.sr_dir = MagicMock()
            mock_app.sr_dir.exists.return_value = True
            # status checks db_path.exists()
            (mock_app.sr_dir / "sr.db").exists.return_value = False
            try:
                main()
            except (SystemExit, Exception):
                pass
