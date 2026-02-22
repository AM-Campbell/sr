# sr — Development Guide

## Project Structure

```
sr/                 # Python package (the application)
  adapters/         # Built-in adapters (mnmd)
  templates/        # HTML templates for web UIs
schedulers/         # Bundled schedulers (sm2) — external to the package
sr_dir/             # Development sr directory (see below)
tests/              # Test suite
```

## SR Directory (sr_dir)

sr requires an **sr directory** — a folder containing user configuration, schedulers, and the database. There is no silent default location. It must be configured explicitly.

### Discovery order

1. `SR_DIR` environment variable (always prints a warning to stderr when used)
2. `~/.config/sr/config` file with `DIR=/path/to/sr`
3. If neither is set: **exits with an error**

### Development sr_dir

The project includes `sr_dir/` at the root for development use. To use it:

```bash
export SR_DIR=$PWD/sr_dir
```

This directory contains `settings.toml` and a symlink to the bundled sm2 scheduler. The `*.db` files it creates are gitignored.

**Never develop without `SR_DIR` set** — without it, `sr` will either use your personal sr directory (if configured in `~/.config/sr/config`) or error out.

### Production sr_dir

Users set up their own sr directory:

```bash
mkdir -p ~/my-sr/schedulers/sm2
cp schedulers/sm2/sm2.py ~/my-sr/schedulers/sm2/
echo 'DIR=~/my-sr' > ~/.config/sr/config
```

## Running Tests

```bash
.venv/bin/pytest tests/ -q
```

Tests use in-memory SQLite databases and temporary sr directories — they never touch any real sr_dir. The `conftest.py` fixtures handle this automatically.

## Architecture

- **Adapters** parse source files into cards and render card HTML. `mnmd` (cloze deletion from markdown) is the built-in adapter. Adapters are loaded by name: built-ins from `sr/adapters/`, user overrides from `sr_dir/adapters/`.
- **Schedulers** determine review order using their own SQLite databases. `sm2` is the bundled scheduler, loaded from `sr_dir/schedulers/sm2/sm2.py`.
- **The database** (`sr_dir/sr.db`) stores cards, review history, and scheduling recommendations. Schema is in `sr/db.py`.
- **Web servers** (`server_review`, `server_browse`, `server_decks`) serve JSON APIs + HTML templates on localhost. They use `http.server` with class-level state on the handler.

## Key Conventions

- Adapter name strings in the DB and tests use `"mnmd"` (the only built-in adapter).
- `Card`, `Relation`, `Recommendation`, `ReviewEvent` are dataclasses in `sr/models.py`.
- All SQL uses `sqlite3.Row` for dict-style access (`row["column"]`).
- Tests that need HTTP servers use ephemeral ports (`("127.0.0.1", 0)`) and `FakeAdapter` classes for rendering.
