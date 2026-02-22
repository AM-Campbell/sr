# sr — Development Guide

## Project Structure

```
sr/                 # Python package (the application)
  adapters/         # Built-in adapters (mnmd)
  templates/        # HTML templates for web UIs
schedulers/         # Bundled schedulers (sm2) — external to the package
sr_dir/             # Development vault (see below)
  .sr/              # sr directory (db, settings, schedulers)
tests/              # Test suite
```

## Vaults and SR Directory

A **vault** is any directory the user points sr at. Inside it, sr keeps a hidden `.sr/` directory containing configuration, schedulers, and the database. The user never specifies `.sr` directly — they point to the vault root and sr derives `.sr` automatically.

- **Vault root**: the user's content directory (e.g. `~/my-notes/`)
- **sr_dir**: always `vault/.sr/` — holds `settings.toml`, `schedulers/`, `sr.db`

### Discovery order

1. `SR_DIR` environment variable → points to the **vault root** (always prints a warning to stderr)
2. `~/.config/sr/config` file with `DIR=/path/to/vault`
3. If neither is set: **exits with an error**

In all cases, the actual sr working directory is `vault_root/.sr/`.

### Vault registry

Known vaults are stored as `VAULT=` lines in `~/.config/sr/config`. The active vault is auto-registered on startup. Users can switch vaults from the web UI.

### Development vault

The project includes `sr_dir/` at the root for development use. To use it:

```bash
export SR_DIR=$PWD/sr_dir
```

This vault's `.sr/` directory contains `settings.toml` and a symlink to the bundled sm2 scheduler. The `*.db` files it creates are gitignored.

**Never develop without `SR_DIR` set** — without it, `sr` will either use your personal vault (if configured in `~/.config/sr/config`) or error out.

### Production setup

Users set up their own vault:

```bash
mkdir -p ~/my-vault/.sr/schedulers/sm2
cp schedulers/sm2/sm2.py ~/my-vault/.sr/schedulers/sm2/
echo 'DIR=~/my-vault' > ~/.config/sr/config
```

## Running Tests

```bash
.venv/bin/pytest tests/ -q
```

Tests use in-memory SQLite databases and temporary vaults — they never touch any real vault. The `conftest.py` fixtures handle this automatically.

## Architecture

- **Adapters** parse source files into cards and render card HTML. `mnmd` (cloze deletion from markdown) is the built-in adapter. Adapters are loaded by name: built-ins from `sr/adapters/`, user overrides from `vault/.sr/adapters/`.
- **Schedulers** determine review order using their own SQLite databases. `sm2` is the bundled scheduler, loaded from `vault/.sr/schedulers/sm2/sm2.py`.
- **The database** (`vault/.sr/sr.db`) stores cards, review history, and scheduling recommendations. Schema is in `sr/db.py`.
- **Web server** serves JSON APIs + HTML templates on localhost. Uses `http.server` with class-level state on the handler.

## Key Conventions

- Adapter name strings in the DB and tests use `"mnmd"` (the only built-in adapter).
- `Card`, `Relation`, `Recommendation`, `ReviewEvent` are dataclasses in `sr/models.py`.
- All SQL uses `sqlite3.Row` for dict-style access (`row["column"]`).
- Tests that need HTTP servers use ephemeral ports (`("127.0.0.1", 0)`) and `FakeAdapter` classes for rendering.
