"""CLI: command-line interface for sr."""

import argparse
import pathlib
import sys

from sr.app import App
from sr.config import (list_vaults, register_vault, set_active_vault,
                       get_active_vault, _config_path)


def cmd_scan(args, app: App):
    app.init_db()

    sched_name = app.settings.get("scheduler", "sm2")
    try:
        app.load_scheduler(sched_name)
    except Exception as e:
        print(f"Warning: cannot load scheduler '{sched_name}': {e}", file=sys.stderr)

    paths = []
    if args.path:
        for p in args.path:
            paths.append(pathlib.Path(p).resolve())
    else:
        # Default: scan the vault root (parent of .sr directory)
        vault_root = app.sr_dir.parent
        paths.append(vault_root)

    print(f"Scanning {len(paths)} path(s)...")
    results = app.scan_sources(paths)

    total_cards = sum(len(cards) for _, _, cards, _ in results)
    print(f"Found {total_cards} cards from {len(results)} source(s)")

    stats = app.sync_cards(results, scanned_paths=paths)
    print(f"Synced: {stats['new']} new, {stats['updated']} updated, "
          f"{stats['deleted']} deleted, {stats['unchanged']} unchanged")
    app.close()


def cmd_status(args, app: App):
    db_path = app.sr_dir / "sr.db"
    if not db_path.exists():
        print("No database found. Run 'sr scan' first.")
        return

    app.init_db()

    total = app.conn.execute("""
        SELECT COUNT(*) as cnt FROM cards c
        JOIN card_state cs ON c.id = cs.card_id WHERE cs.status = 'active'
    """).fetchone()["cnt"]

    gradable = app.conn.execute("""
        SELECT COUNT(*) as cnt FROM cards c
        JOIN card_state cs ON c.id = cs.card_id
        WHERE cs.status = 'active' AND c.gradable = 1
    """).fetchone()["cnt"]

    due = app.conn.execute("""
        SELECT COUNT(*) as cnt FROM recommendations r
        JOIN card_state cs ON r.card_id = cs.card_id
        WHERE cs.status = 'active' AND r.time <= datetime('now')
    """).fetchone()["cnt"]

    reviewed_today = app.conn.execute("""
        SELECT COUNT(*) as cnt FROM review_log
        WHERE timestamp >= date('now')
    """).fetchone()["cnt"]

    total_reviews = app.conn.execute("SELECT COUNT(*) as cnt FROM review_log").fetchone()["cnt"]

    print(f"Cards:          {total} total ({gradable} gradable)")
    print(f"Due now:        {due}")
    print(f"Reviewed today: {reviewed_today}")
    print(f"Total reviews:  {total_reviews}")

    sources = app.conn.execute("""
        SELECT c.source_path, COUNT(*) as cnt
        FROM cards c JOIN card_state cs ON c.id = cs.card_id
        WHERE cs.status = 'active'
        GROUP BY c.source_path ORDER BY c.source_path
    """).fetchall()
    if sources:
        print(f"\nSources:")
        for s in sources:
            print(f"  {s['source_path']}: {s['cnt']} cards")

    app.close()


def cmd_launch(args, app: App):
    from sr.server import start_server

    db_path = app.sr_dir / "sr.db"
    if not db_path.exists():
        print("No database found. Run 'sr scan' first.")
        return

    app.init_db()

    sched_name = app.settings.get("scheduler", "sm2")
    try:
        app.load_scheduler(sched_name)
    except Exception as e:
        print(f"Warning: cannot load scheduler '{sched_name}': {e}", file=sys.stderr)

    if hasattr(args, 'port') and args.port:
        app.settings = dict(app.settings)
        app.settings["review_port"] = args.port

    start_server(app.conn, app.sr_dir, app.settings,
                 scheduler=app.scheduler,
                 get_adapter_fn=app.get_adapter)
    app.close()


def cmd_init(args):
    """Initialize a vault at a given directory (or cwd)."""
    vault_path = pathlib.Path(args.dir or ".").resolve()
    if not vault_path.exists():
        print(f"Directory does not exist: {vault_path}")
        sys.exit(1)
    sr_dir = vault_path / ".sr"
    if sr_dir.exists():
        print(f"Vault already initialized at {vault_path}")
    else:
        sr_dir.mkdir(parents=True)
        print(f"Initialized vault at {vault_path}")
    set_active_vault(vault_path)
    print(f"Active vault set to {vault_path}")


def cmd_vault(args):
    """Select the active vault from registered vaults."""
    vaults = list_vaults()
    active = get_active_vault()
    if not vaults:
        print("No vaults registered.")
        print("Use 'sr init [dir]' to create one.")
        return
    print("Registered vaults:\n")
    for i, v in enumerate(vaults):
        marker = " *" if active and v.resolve() == active.resolve() else ""
        print(f"  {i + 1}) {v}{marker}")
    print()
    try:
        choice = input("Select vault (number): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(vaults):
        print("Invalid selection.")
        return
    selected = vaults[int(choice) - 1]
    set_active_vault(selected)
    print(f"Active vault set to {selected}")


def main():
    parser = argparse.ArgumentParser(prog="sr", description="Spaced Repetition System")
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init", help="Initialize a new vault")
    p_init.add_argument("dir", nargs="?", help="Directory to initialize (default: current directory)")

    subparsers.add_parser("vault", help="Select the active vault")

    p_scan = subparsers.add_parser("scan", help="Scan sources and sync cards to DB")
    p_scan.add_argument("path", nargs="*", help="Paths to scan (default: vault root)")

    subparsers.add_parser("status", help="Show card counts and stats")

    p_launch = subparsers.add_parser("launch", help="Launch web UI (decks, browse, review)")
    p_launch.add_argument("--port", type=int, help="Server port (default: 8791)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Commands that don't need an existing vault
    if args.command == "init":
        cmd_init(args)
        return
    if args.command == "vault":
        cmd_vault(args)
        return

    app = App()
    if not app.sr_dir.exists():
        app.sr_dir.mkdir(parents=True, exist_ok=True)
        print(f"Created {app.sr_dir}")

    print(f"vault: {app.vault}")

    if args.command == "scan":
        cmd_scan(args, app)
    elif args.command == "status":
        cmd_status(args, app)
    elif args.command == "launch":
        cmd_launch(args, app)
