#!/usr/bin/env python3
"""sr — Spaced Repetition System. Single-file core."""

import argparse
import dataclasses
import hashlib
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Relation:
    target_key: str
    relation_type: str
    target_source: str | None = None

@dataclass
class Card:
    key: str
    content: dict
    display_text: str = ""
    gradable: bool = True
    tags: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)

@dataclass
class Recommendation:
    card_id: int
    time: str
    precision_seconds: int

@dataclass
class ReviewEvent:
    card_id: int
    timestamp: str
    grade: int
    time_on_front_ms: int
    time_on_card_ms: int
    feedback: str | None
    response: dict | None

# ─── Config / paths ─────────────────────────────────────────────────────────

def get_sr_dir() -> pathlib.Path:
    config_path = pathlib.Path.home() / ".config" / "sr" / "config"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DIR="):
                return pathlib.Path(line[4:].strip())
    default = pathlib.Path.home() / ".local" / "share" / "sr"
    return default

def load_settings(sr_dir: pathlib.Path) -> dict:
    settings_path = sr_dir / "settings.toml"
    settings = {"scheduler": "sm2", "review_port": 8791}
    if settings_path.exists():
        settings.update(_parse_toml_simple(settings_path.read_text()))
    return settings

def _parse_toml_simple(text: str) -> dict:
    """Minimal TOML parser for flat key=value files."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.isdigit():
                v = int(v)
            elif v == "true":
                v = True
            elif v == "false":
                v = False
            result[k] = v
    return result

# ─── Database ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    card_key TEXT NOT NULL,
    adapter TEXT NOT NULL,
    content JSON NOT NULL,
    content_hash TEXT NOT NULL,
    display_text TEXT,
    gradable BOOLEAN NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_path, card_key, adapter)
);

CREATE TABLE IF NOT EXISTS card_state (
    card_id INTEGER PRIMARY KEY REFERENCES cards(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive','deleted')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS card_relations (
    upstream_card_id INTEGER NOT NULL REFERENCES cards(id),
    downstream_card_id INTEGER NOT NULL REFERENCES cards(id),
    relation_type TEXT NOT NULL,
    PRIMARY KEY (upstream_card_id, downstream_card_id, relation_type)
);

CREATE TABLE IF NOT EXISTS card_tags (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (card_id, tag)
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    session_id TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    grade INTEGER NOT NULL CHECK(grade IN (0, 1)),
    time_on_front_ms INTEGER,
    time_on_card_ms INTEGER,
    feedback TEXT CHECK(feedback IS NULL OR feedback IN ('too_hard','just_right','too_easy')),
    response JSON
);

CREATE TABLE IF NOT EXISTS recommendations (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    scheduler_id TEXT NOT NULL,
    time TEXT NOT NULL,
    precision_seconds INTEGER NOT NULL,
    PRIMARY KEY (card_id, scheduler_id)
);

CREATE TABLE IF NOT EXISTS card_flags (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    flag TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (card_id, flag)
);
"""

def init_db(db_path: pathlib.Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

# ─── Adapter loading ────────────────────────────────────────────────────────

def load_adapter(name: str, sr_dir: pathlib.Path):
    adapter_path = sr_dir / "adapters" / f"{name}.py"
    if not adapter_path.exists():
        raise FileNotFoundError(f"Adapter not found: {adapter_path}")
    spec = importlib.util.spec_from_file_location(f"sr_adapter_{name}", str(adapter_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Adapter()

_adapter_cache: dict[str, Any] = {}

def get_adapter(name: str, sr_dir: pathlib.Path):
    if name not in _adapter_cache:
        _adapter_cache[name] = load_adapter(name, sr_dir)
    return _adapter_cache[name]

# ─── Scheduler loading ──────────────────────────────────────────────────────

def load_scheduler(name: str, sr_dir: pathlib.Path, core_db_path: pathlib.Path):
    sched_dir = sr_dir / "schedulers" / name
    sched_path = sched_dir / f"{name}.py"
    if not sched_path.exists():
        raise FileNotFoundError(f"Scheduler not found: {sched_path}")
    spec = importlib.util.spec_from_file_location(f"sr_scheduler_{name}", str(sched_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Scheduler(str(sched_dir), str(core_db_path))

# ─── YAML frontmatter parsing ───────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown. Returns (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    yaml_block = text[3:end].strip()
    body = text[end + 4:].strip()
    meta = {}
    for line in yaml_block.splitlines():
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
            elif v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            elif v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            elif v.isdigit():
                v = int(v)
            meta[k] = v
    return meta, body

# ─── Source scanning ─────────────────────────────────────────────────────────

def content_hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

def scan_sources(paths: list[pathlib.Path], sr_dir: pathlib.Path) -> list[tuple[str, str, list[Card], dict]]:
    """Returns list of (source_path, adapter_name, cards, config)."""
    results = []
    seen_paths = set()

    for path in paths:
        path = path.resolve()
        if path.is_file() and path.suffix == ".md":
            _scan_md_file(path, sr_dir, results, seen_paths)
        elif path.is_dir():
            _scan_directory(path, sr_dir, results, seen_paths)

    return results

def _scan_md_file(path: pathlib.Path, sr_dir: pathlib.Path,
                  results: list, seen_paths: set):
    if str(path) in seen_paths:
        return
    seen_paths.add(str(path))
    try:
        text = path.read_text()
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return
    meta, body = parse_frontmatter(text)
    adapter_name = meta.get("sr_adapter")
    if not adapter_name:
        return
    try:
        adapter = get_adapter(adapter_name, sr_dir)
        cards = adapter.parse(text, str(path), meta)
        results.append((str(path), adapter_name, cards, meta))
    except Exception as e:
        print(f"Warning: adapter '{adapter_name}' failed on {path}: {e}", file=sys.stderr)

def _scan_directory(dirpath: pathlib.Path, sr_dir: pathlib.Path,
                    results: list, seen_paths: set):
    config_path = dirpath / ".sr.config"
    if config_path.exists():
        config = _parse_toml_simple(config_path.read_text())
        adapter_name = config.get("adapter")
        if not adapter_name:
            print(f"Warning: .sr.config in {dirpath} missing 'adapter'", file=sys.stderr)
            return
        try:
            adapter = get_adapter(adapter_name, sr_dir)
        except Exception as e:
            print(f"Warning: cannot load adapter '{adapter_name}': {e}", file=sys.stderr)
            return
        for f in sorted(dirpath.iterdir()):
            if f.is_file() and f.name != ".sr.config" and str(f) not in seen_paths:
                seen_paths.add(str(f))
                try:
                    text = f.read_text()
                    cards = adapter.parse(text, str(f), config)
                    results.append((str(f), adapter_name, cards, config))
                except Exception as e:
                    print(f"Warning: adapter '{adapter_name}' failed on {f}: {e}", file=sys.stderr)
    else:
        # Recurse into subdirs, scan .md files
        try:
            entries = sorted(dirpath.iterdir())
        except PermissionError:
            return
        for item in entries:
            if item.is_dir() and not item.name.startswith("."):
                _scan_directory(item, sr_dir, results, seen_paths)
            elif item.is_file() and item.suffix == ".md":
                _scan_md_file(item, sr_dir, results, seen_paths)

def sync_cards(conn: sqlite3.Connection, scan_results: list[tuple[str, str, list[Card], dict]],
               scheduler=None, scanned_paths: list[pathlib.Path] | None = None) -> dict:
    """Sync scanned cards to DB. Returns stats dict."""
    stats = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    # Collect all source_paths from scan
    scanned_sources = set()
    scanned_keys = {}  # (source_path, card_key, adapter) -> Card
    source_suspended = {}  # source_path -> bool

    for source_path, adapter_name, cards, config in scan_results:
        scanned_sources.add(source_path)
        source_suspended[source_path] = bool(config.get("suspended", False))
        for card in cards:
            scanned_keys[(source_path, card.key, adapter_name)] = card

    # Get existing active cards for scanned sources
    # ALSO include cards whose source_path falls under the scanned directories
    # (to detect deleted source files)
    existing_conditions = []
    existing_params = []
    if scanned_sources:
        placeholders = ",".join("?" * len(scanned_sources))
        existing_conditions.append(f"c.source_path IN ({placeholders})")
        existing_params.extend(scanned_sources)
    if scanned_paths:
        for sp in scanned_paths:
            sp_str = str(sp.resolve())
            if sp.is_dir():
                existing_conditions.append("c.source_path LIKE ?")
                existing_params.append(f"{sp_str}/%")
            else:
                existing_conditions.append("c.source_path = ?")
                existing_params.append(sp_str)

    if existing_conditions:
        where = " OR ".join(existing_conditions)
        existing = conn.execute(f"""
            SELECT c.id, c.source_path, c.card_key, c.adapter, c.content_hash, cs.status
            FROM cards c JOIN card_state cs ON c.id = cs.card_id
            WHERE ({where}) AND cs.status IN ('active', 'inactive')
        """, existing_params).fetchall()
    else:
        existing = []

    existing_map = {}
    for row in existing:
        existing_map[(row["source_path"], row["card_key"], row["adapter"])] = row

    # Process scanned cards
    for key_tuple, card in scanned_keys.items():
        source_path, card_key, adapter_name = key_tuple
        chash = content_hash(card.content)

        if key_tuple in existing_map:
            row = existing_map[key_tuple]
            current_status = row["status"]

            if row["content_hash"] == chash:
                # Content unchanged — leave status as-is (respect user suspensions)
                stats["unchanged"] += 1
                _sync_tags(conn, row["id"], card.tags)
            else:
                # Content changed — mark old as deleted, insert new
                old_id = row["id"]
                # Preserve inactive status if user had suspended this card
                new_status = current_status if current_status == "inactive" else "active"
                conn.execute("UPDATE card_state SET status='deleted', updated_at=datetime('now') WHERE card_id=?", (old_id,))
                # Retire old card's unique key so the new card can use it
                conn.execute("UPDATE cards SET card_key = card_key || '__replaced_' || CAST(id AS TEXT) WHERE id=?", (old_id,))
                new_id = _insert_card(conn, source_path, card_key, adapter_name, card, chash,
                                      status=new_status)
                conn.execute("""
                    INSERT INTO card_relations (upstream_card_id, downstream_card_id, relation_type)
                    VALUES (?, ?, 'is_replaced_by')
                """, (old_id, new_id))
                if scheduler and new_status == "active":
                    try:
                        rec = scheduler.on_card_replaced(old_id, new_id)
                        if rec:
                            _upsert_recommendation(conn, rec, scheduler)
                    except Exception as e:
                        print(f"Warning: scheduler on_card_replaced failed: {e}", file=sys.stderr)
                stats["updated"] += 1
            del existing_map[key_tuple]
        else:
            # New card — default to inactive if source has suspended: true
            new_status = "inactive" if source_suspended.get(source_path, False) else "active"
            new_id = _insert_card(conn, source_path, card_key, adapter_name, card, chash,
                                  status=new_status)
            if scheduler and new_status == "active":
                try:
                    rec = scheduler.on_card_created(new_id)
                    if rec:
                        _upsert_recommendation(conn, rec, scheduler)
                except Exception as e:
                    print(f"Warning: scheduler on_card_created failed: {e}", file=sys.stderr)
            stats["new"] += 1

    # Remaining in existing_map are missing from source → delete
    for key_tuple, row in existing_map.items():
        conn.execute("UPDATE card_state SET status='deleted', updated_at=datetime('now') WHERE card_id=?", (row["id"],))
        conn.execute("DELETE FROM recommendations WHERE card_id=?", (row["id"],))
        if scheduler:
            try:
                scheduler.on_card_status_changed(row["id"], "deleted")
            except Exception as e:
                print(f"Warning: scheduler on_card_status_changed failed: {e}", file=sys.stderr)
        stats["deleted"] += 1

    # Sync relations for new/updated cards
    _sync_relations(conn, scan_results, scheduler)

    conn.commit()
    return stats

def _insert_card(conn: sqlite3.Connection, source_path: str, card_key: str,
                 adapter_name: str, card: Card, chash: str,
                 status: str = "active") -> int:
    cur = conn.execute("""
        INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (source_path, card_key, adapter_name,
          json.dumps(card.content, sort_keys=True), chash,
          card.display_text, card.gradable))
    card_id = cur.lastrowid
    conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, ?)", (card_id, status))
    _sync_tags(conn, card_id, card.tags)
    return card_id

def _sync_tags(conn: sqlite3.Connection, card_id: int, tags: list[str]):
    existing = {r["tag"] for r in conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (card_id,))}
    new_tags = set(tags)
    for tag in new_tags - existing:
        conn.execute("INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)", (card_id, tag))
    for tag in existing - new_tags:
        conn.execute("DELETE FROM card_tags WHERE card_id=? AND tag=?", (card_id, tag))

def _sync_relations(conn: sqlite3.Connection, scan_results, scheduler):
    """Sync card relations from scan results."""
    for source_path, adapter_name, cards, _config in scan_results:
        for card in cards:
            if not card.relations:
                continue
            row = conn.execute(
                "SELECT id FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE c.source_path=? AND c.card_key=? AND c.adapter=? AND cs.status='active'",
                (source_path, card.key, adapter_name)
            ).fetchone()
            if not row:
                continue
            card_id = row["id"]
            for rel in card.relations:
                target_source = rel.target_source or source_path
                target_row = conn.execute(
                    "SELECT id FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE c.source_path=? AND c.card_key=? AND cs.status='active'",
                    (target_source, rel.target_key)
                ).fetchone()
                if target_row:
                    conn.execute("""
                        INSERT OR IGNORE INTO card_relations (upstream_card_id, downstream_card_id, relation_type)
                        VALUES (?, ?, ?)
                    """, (card_id, target_row["id"], rel.relation_type))

def _upsert_recommendation(conn: sqlite3.Connection, rec: Recommendation, scheduler):
    sched_id = type(scheduler).__module__.split("_")[-1] if hasattr(scheduler, '__module__') else "unknown"
    # Try to get a proper name
    sched_id = getattr(scheduler, 'scheduler_id', sched_id)
    conn.execute("""
        INSERT OR REPLACE INTO recommendations (card_id, scheduler_id, time, precision_seconds)
        VALUES (?, ?, ?, ?)
    """, (rec.card_id, sched_id, rec.time, rec.precision_seconds))

# ─── Flag helpers ─────────────────────────────────────────────────────────

def add_flag(conn, card_id, flag, note=None):
    conn.execute("""INSERT OR REPLACE INTO card_flags (card_id, flag, note) VALUES (?, ?, ?)""",
                 (card_id, flag, note))
    conn.commit()

def remove_flag(conn, card_id, flag):
    conn.execute("DELETE FROM card_flags WHERE card_id=? AND flag=?", (card_id, flag))
    conn.commit()

def get_flags(conn, card_id):
    return [dict(r) for r in conn.execute("SELECT flag, note FROM card_flags WHERE card_id=?", (card_id,))]

# ─── Review Web Server ──────────────────────────────────────────────────────

REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sr review</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">
<style>
:root {
    --bg: #1c1b1a;
    --bg-card: #272524;
    --bg-inset: #1f1e1d;
    --border: #3a3634;
    --border-focus: #6b6560;
    --text: #d4cfc9;
    --text-muted: #8a8480;
    --text-dim: #5c5753;
    --accent: #87CEAB;
    --correct: #5cb85c;
    --wrong: #d9534f;
    --flag: #f0c040;
    --tag: #8bb878;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 2rem;
}
#progress {
    width: 100%; max-width: 600px; text-align: center;
    color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem;
}
#card-container {
    width: 100%; max-width: 600px;
    background: var(--bg-card); border-radius: 8px;
    border: 1px solid var(--border);
    padding: 2rem; min-height: 300px;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    cursor: pointer; user-select: none;
    transition: border-color 0.2s;
}
#card-container:hover { border-color: var(--border-focus); }
#card-container.flipped { cursor: default; }
#card-front, #card-back { width: 100%; text-align: center; }
#card-front { font-size: 1.3rem; line-height: 1.6; }
#card-back {
    display: none; margin-top: 1.5rem; padding-top: 1.5rem;
    border-top: 1px solid var(--border); font-size: 1.1rem; line-height: 1.5;
}
.flip-hint { color: var(--text-dim); font-size: 0.85rem; margin-top: 1rem; }
#controls { display: none; margin-top: 1.5rem; gap: 1rem; flex-wrap: wrap; justify-content: center; }
.grade-btn {
    padding: 0.7rem 2rem; border-radius: 6px;
    font-size: 1rem; cursor: pointer; font-weight: 600;
    transition: transform 0.1s, opacity 0.1s;
}
.grade-btn:hover { transform: scale(1.05); }
.grade-btn:active { transform: scale(0.97); }
#btn-wrong { background: rgba(217,83,79,0.15); color: var(--wrong); border: 1px solid var(--wrong); }
#btn-correct { background: rgba(92,184,92,0.15); color: var(--correct); border: 1px solid var(--correct); }
#feedback-row { display: none; margin-top: 0.8rem; gap: 0.5rem; justify-content: center; }
.fb-btn {
    padding: 0.4rem 0.8rem; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--text-muted); font-size: 0.8rem; cursor: pointer;
}
.fb-btn:hover { border-color: var(--border-focus); color: var(--text); }
.fb-btn.selected { border-color: var(--accent); color: var(--accent); }
#undo-btn {
    margin-top: 1rem; padding: 0.4rem 1rem; border: 1px solid var(--border);
    border-radius: 6px; background: transparent; color: var(--text-muted);
    font-size: 0.8rem; cursor: pointer; display: none;
}
#undo-btn:hover { border-color: var(--border-focus); color: var(--text); }
#action-bar {
    display: none; margin-top: 1rem; gap: 0.5rem; justify-content: center;
}
.action-btn {
    padding: 0.4rem 0.8rem; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--text-muted); font-size: 0.8rem; cursor: pointer;
}
.action-btn:hover { border-color: var(--border-focus); color: var(--text); }
#flag-btn.flagged { border-color: var(--flag); color: var(--flag); }
#done-msg { display: none; font-size: 1.3rem; color: var(--accent); margin-top: 2rem; }
#error-msg { color: var(--wrong); margin-top: 1rem; display: none; }
pre { text-align: left; background: var(--bg-inset); padding: 1rem; border-radius: 6px; overflow-x: auto; }
code { font-family: "JetBrains Mono", "Fira Code", monospace; }
#autograde-result {
    display: none; margin-top: 1rem; padding: 0.6rem 1.2rem;
    border-radius: 6px; font-weight: 600; font-size: 1rem; text-align: center;
}
#autograde-result.correct { background: rgba(92,184,92,0.15); color: var(--correct); border: 1px solid var(--correct); }
#autograde-result.wrong { background: rgba(217,83,79,0.15); color: var(--wrong); border: 1px solid var(--wrong); }
#btn-next {
    display: none; padding: 0.7rem 2rem; border-radius: 6px;
    font-size: 1rem; cursor: pointer; font-weight: 600;
    background: rgba(135,206,171,0.15); color: var(--accent); border: 1px solid var(--accent);
    transition: transform 0.1s, opacity 0.1s;
}
#btn-next:hover { transform: scale(1.05); }
#btn-next:active { transform: scale(0.97); }
</style>
</head>
<body>
<div id="progress"></div>
<div id="card-container" onclick="flipCard()">
    <div id="card-front">Loading...</div>
    <div id="card-back"></div>
    <div class="flip-hint" id="flip-hint">click to flip</div>
</div>
<div id="autograde-result"></div>
<div id="controls">
    <button class="grade-btn" id="btn-wrong" onclick="grade(0)">&#10008; Wrong</button>
    <button class="grade-btn" id="btn-correct" onclick="grade(1)">&#10004; Correct</button>
    <button class="grade-btn" id="btn-next" onclick="submitAutoGrade()">Next &#8594;</button>
</div>
<div id="feedback-row">
    <button class="fb-btn" data-fb="too_hard" onclick="setFeedback('too_hard')">too hard</button>
    <button class="fb-btn" data-fb="just_right" onclick="setFeedback('just_right')">just right</button>
    <button class="fb-btn" data-fb="too_easy" onclick="setFeedback('too_easy')">too easy</button>
</div>
<div id="action-bar">
    <button class="action-btn" id="flag-btn" onclick="toggleFlag()" title="Flag (f)">&#9873; Flag</button>
    <button class="action-btn" id="edit-btn" onclick="editCard()" title="Edit (e)">&#9998; Edit</button>
    <button class="action-btn" id="suspend-btn" onclick="suspendCard()" title="Suspend (s)">&#9208; Suspend</button>
</div>
<button id="undo-btn" onclick="undo()">&#8630; Undo</button>
<div id="done-msg">Session complete!</div>
<div id="error-msg"></div>
<script>
let sessionToken = null;
let currentFeedback = null;
let hasFlipped = false;
let hasCard = false;
let currentGradable = true;
let autoGradeResult = null;  // {grade, response} set by adapter JS
let currentFlags = [];

async function api(method, path, body) {
    const opts = {method, headers: {"Content-Type": "application/json"}};
    if (sessionToken) opts.headers["X-Session-Token"] = sessionToken;
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) {
        const t = await r.text();
        throw new Error(t);
    }
    return r.json();
}

async function init() {
    try {
        const data = await api("GET", "/api/session");
        sessionToken = data.session_token;
        await loadNext();
    } catch(e) { showError(e.message); }
}

async function loadNext() {
    try {
        const data = await api("GET", "/api/next");
        if (data.done) {
            document.getElementById("card-container").style.display = "none";
            document.getElementById("controls").style.display = "none";
            document.getElementById("feedback-row").style.display = "none";
            document.getElementById("autograde-result").style.display = "none";
            document.getElementById("action-bar").style.display = "none";
            document.getElementById("done-msg").style.display = "block";
            hasCard = false;
            return;
        }
        currentGradable = data.gradable;
        autoGradeResult = null;
        currentFlags = data.flags || [];
        document.getElementById("card-front").innerHTML = data.front_html;
        document.getElementById("card-back").innerHTML = "";
        document.getElementById("card-back").style.display = "none";
        document.getElementById("controls").style.display = "none";
        document.getElementById("feedback-row").style.display = "none";
        document.getElementById("autograde-result").style.display = "none";
        document.getElementById("btn-next").style.display = "none";
        document.getElementById("btn-wrong").style.display = "";
        document.getElementById("btn-correct").style.display = "";
        document.getElementById("flip-hint").style.display = currentGradable ? "block" : "block";
        document.getElementById("card-container").classList.remove("flipped");
        document.getElementById("progress").textContent =
            `Reviewed: ${data.session_stats.reviewed} | Remaining: ${data.session_stats.remaining}`;
        document.getElementById("action-bar").style.display = "flex";
        updateFlagButton();
        hasFlipped = false;
        hasCard = true;
        currentFeedback = null;
        document.querySelectorAll(".fb-btn").forEach(b => b.classList.remove("selected"));
        document.getElementById("error-msg").style.display = "none";
    } catch(e) { showError(e.message); }
}

async function flipCard() {
    if (hasFlipped || !hasCard) return;
    try {
        const data = await api("POST", "/api/flip");
        document.getElementById("card-back").innerHTML = data.back_html;
        document.getElementById("card-back").style.display = "block";
        document.getElementById("flip-hint").style.display = "none";
        document.getElementById("card-container").classList.add("flipped");
        hasFlipped = true;
        if (!currentGradable) {
            // Non-gradable: just show Next
            document.getElementById("controls").style.display = "flex";
            document.getElementById("btn-wrong").style.display = "none";
            document.getElementById("btn-correct").style.display = "none";
            document.getElementById("btn-next").style.display = "";
        } else {
            document.getElementById("controls").style.display = "flex";
            document.getElementById("feedback-row").style.display = "flex";
        }
    } catch(e) { showError(e.message); }
}

// Global hook for adapter JS to call for autograding
// Flow: adapter calls srAutoGrade() → core flips card, shows back + result → user clicks Next
window.srAutoGrade = async function(grade, response) {
    if (!hasCard || autoGradeResult) return;
    autoGradeResult = {grade: grade, response: response || null};

    // Flip the card to show the back
    if (!hasFlipped) {
        try {
            const data = await api("POST", "/api/flip");
            document.getElementById("card-back").innerHTML = data.back_html;
            document.getElementById("card-back").style.display = "block";
            document.getElementById("flip-hint").style.display = "none";
            document.getElementById("card-container").classList.add("flipped");
            hasFlipped = true;
        } catch(e) { showError(e.message); return; }
    }

    // Show result indicator
    const el = document.getElementById("autograde-result");
    el.className = grade === 1 ? "correct" : "wrong";
    el.textContent = grade === 1 ? "✓ Correct" : "✗ Wrong";
    el.style.display = "block";

    // Show Next button (hide correct/wrong buttons)
    document.getElementById("controls").style.display = "flex";
    document.getElementById("btn-wrong").style.display = "none";
    document.getElementById("btn-correct").style.display = "none";
    document.getElementById("btn-next").style.display = "";
    document.getElementById("feedback-row").style.display = "flex";
};

async function submitAutoGrade() {
    if (autoGradeResult) {
        // Autograded card — submit the adapter's grade
        try {
            await api("POST", "/api/grade", {
                grade: autoGradeResult.grade,
                feedback: currentFeedback,
                response: autoGradeResult.response
            });
            document.getElementById("undo-btn").style.display = "inline-block";
            await loadNext();
        } catch(e) { showError(e.message); }
    } else {
        // Non-gradable card — just move on, no grade logged
        try {
            await api("POST", "/api/skip");
            await loadNext();
        } catch(e) { showError(e.message); }
    }
}

async function grade(g) {
    try {
        await api("POST", "/api/grade", {grade: g, feedback: currentFeedback});
        document.getElementById("undo-btn").style.display = "inline-block";
        await loadNext();
    } catch(e) { showError(e.message); }
}

function setFeedback(fb) {
    currentFeedback = (currentFeedback === fb) ? null : fb;
    document.querySelectorAll(".fb-btn").forEach(b => {
        b.classList.toggle("selected", b.dataset.fb === currentFeedback);
    });
}

async function undo() {
    try {
        const data = await api("POST", "/api/undo");
        if (data.ok) {
            document.getElementById("done-msg").style.display = "none";
            document.getElementById("card-container").style.display = "flex";
            document.getElementById("card-front").innerHTML = data.front_html;
            document.getElementById("card-back").innerHTML = data.back_html;
            document.getElementById("card-back").style.display = "block";
            document.getElementById("controls").style.display = "flex";
            document.getElementById("feedback-row").style.display = "flex";
            document.getElementById("flip-hint").style.display = "none";
            document.getElementById("card-container").classList.add("flipped");
            document.getElementById("autograde-result").style.display = "none";
            document.getElementById("btn-next").style.display = "none";
            document.getElementById("btn-wrong").style.display = "";
            document.getElementById("btn-correct").style.display = "";
            hasFlipped = true;
            hasCard = true;
            autoGradeResult = null;
            document.getElementById("undo-btn").style.display = "none";
        }
    } catch(e) { showError(e.message); }
}

function updateFlagButton() {
    const btn = document.getElementById("flag-btn");
    const isFlagged = currentFlags.some(f => f.flag === "edit_later");
    btn.classList.toggle("flagged", isFlagged);
    btn.innerHTML = isFlagged ? "&#9873; Flagged" : "&#9873; Flag";
}

async function toggleFlag() {
    if (!hasCard) return;
    const isFlagged = currentFlags.some(f => f.flag === "edit_later");
    try {
        const endpoint = isFlagged ? "/api/unflag" : "/api/flag";
        const data = await api("POST", endpoint, {flag: "edit_later"});
        currentFlags = data.flags || [];
        updateFlagButton();
    } catch(e) { showError(e.message); }
}

async function editCard() {
    if (!hasCard) return;
    try {
        await api("POST", "/api/edit");
    } catch(e) { showError(e.message); }
}

async function suspendCard() {
    if (!hasCard) return;
    try {
        await api("POST", "/api/suspend");
        document.getElementById("undo-btn").style.display = "inline-block";
        await loadNext();
    } catch(e) { showError(e.message); }
}

function showError(msg) {
    const el = document.getElementById("error-msg");
    el.textContent = msg;
    el.style.display = "block";
}

document.addEventListener("keydown", (e) => {
    if (e.key === " " && !hasFlipped && hasCard) { e.preventDefault(); flipCard(); }
    else if (e.key === "Enter" && hasFlipped && (autoGradeResult || !currentGradable)) submitAutoGrade();
    else if (e.key === "1" && hasFlipped && !autoGradeResult && currentGradable) grade(0);
    else if (e.key === "2" && hasFlipped && !autoGradeResult && currentGradable) grade(1);
    else if (e.key === "f" && hasCard) toggleFlag();
    else if (e.key === "e" && hasCard) editCard();
    else if (e.key === "s" && hasCard) suspendCard();
    else if (e.key === "z" || e.key === "u") undo();
});

init();
</script>
</body>
</html>"""


class ReviewSession:
    def __init__(self, conn, scheduler, sr_dir, settings=None,
                 tag_filter=None, path_filter=None, flag_filter=None):
        self.conn = conn
        self.scheduler = scheduler
        self.sr_dir = sr_dir
        self.settings = settings or {}
        self.tag_filter = tag_filter
        self.path_filter = path_filter
        self.flag_filter = flag_filter
        self.session_id = str(uuid.uuid4())
        self.token = str(uuid.uuid4())
        self.current_card = None
        self.previous_card = None
        self.flip_time = None
        self.serve_time = None
        self.reviewed = 0
        self.reviewed_ids = set()

    def get_next_card(self) -> dict | None:
        """Get next card to review."""
        query = """
            SELECT c.id, c.source_path, c.adapter, c.content, c.gradable
            FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
        """
        params = []

        if self.tag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)"
            params.append(self.tag_filter)

        if self.path_filter:
            query += " AND c.source_path LIKE ?"
            params.append(f"{self.path_filter}%")

        if self.flag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)"
            params.append(self.flag_filter)

        # Exclude already reviewed in this session
        if self.reviewed_ids:
            placeholders = ",".join("?" * len(self.reviewed_ids))
            query += f" AND c.id NOT IN ({placeholders})"
            params.extend(self.reviewed_ids)

        # NULLs (no recommendation) sort last — show due cards first
        query += " ORDER BY CASE WHEN r.time IS NULL THEN 1 ELSE 0 END, r.time ASC, c.id ASC LIMIT 1"

        row = self.conn.execute(query, params).fetchone()
        if not row:
            return None

        self.current_card = dict(row)
        self.serve_time = time.time()
        self.flip_time = None
        return self.current_card

    def flip(self) -> str:
        if not self.current_card:
            raise ValueError("No current card")
        self.flip_time = time.time()
        adapter = get_adapter(self.current_card["adapter"], self.sr_dir)
        content = json.loads(self.current_card["content"])
        try:
            return adapter.render_back(content)
        except Exception as e:
            return f'<div style="color:var(--wrong)">Render error (card {self.current_card["id"]}): {e}</div>'

    def grade_current(self, grade: int, feedback: str | None = None,
                      response: dict | None = None):
        if not self.current_card:
            raise ValueError("No current card")
        now = time.time()
        time_on_front_ms = int((self.flip_time - self.serve_time) * 1000) if self.flip_time else None
        time_on_card_ms = int((now - self.serve_time) * 1000) if self.serve_time else None

        card_id = self.current_card["id"]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("""
            INSERT INTO review_log (card_id, session_id, timestamp, grade, time_on_front_ms, time_on_card_ms, feedback, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (card_id, self.session_id, ts, grade, time_on_front_ms, time_on_card_ms,
              feedback, json.dumps(response) if response else None))
        self.conn.commit()

        # Notify scheduler
        if self.scheduler:
            event = ReviewEvent(
                card_id=card_id, timestamp=ts, grade=grade,
                time_on_front_ms=time_on_front_ms or 0,
                time_on_card_ms=time_on_card_ms or 0,
                feedback=feedback, response=response
            )
            try:
                recs = self.scheduler.on_review(card_id, event)
                for rec in (recs or []):
                    _upsert_recommendation(self.conn, rec, self.scheduler)
                self.conn.commit()
            except Exception as e:
                print(f"Warning: scheduler on_review failed: {e}", file=sys.stderr)

        self.previous_card = self.current_card
        self.reviewed_ids.add(card_id)
        self.reviewed += 1
        self.current_card = None

    def remaining_count(self) -> int:
        query = """
            SELECT COUNT(*) as cnt FROM cards c
            JOIN card_state cs ON c.id = cs.card_id
            LEFT JOIN recommendations r ON c.id = r.card_id
            WHERE cs.status = 'active' AND c.gradable = 1
        """
        params = []
        if self.tag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)"
            params.append(self.tag_filter)
        if self.path_filter:
            query += " AND c.source_path LIKE ?"
            params.append(f"{self.path_filter}%")
        if self.flag_filter:
            query += " AND c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)"
            params.append(self.flag_filter)
        if self.reviewed_ids:
            placeholders = ",".join("?" * len(self.reviewed_ids))
            query += f" AND c.id NOT IN ({placeholders})"
            params.extend(self.reviewed_ids)
        return self.conn.execute(query, params).fetchone()["cnt"]

    def render_front(self, card: dict) -> str:
        adapter = get_adapter(card["adapter"], self.sr_dir)
        content = json.loads(card["content"])
        try:
            return adapter.render_front(content)
        except Exception as e:
            return f'<div style="color:var(--wrong)">Render error (card {card["id"]}): {e}</div>'


class ReviewHandler(http.server.BaseHTTPRequestHandler):
    session: ReviewSession = None
    settings: dict = {}

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json_response({"error": msg}, status)

    def _check_token(self) -> bool:
        token = self.headers.get("X-Session-Token")
        if token != self.session.token:
            self._error(403, "Invalid session token")
            return False
        return True

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self):
        if self.path == "/":
            body = REVIEW_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/session":
            self._json_response({"session_token": self.session.token})
        elif self.path == "/api/next":
            if not self._check_token():
                return
            card = self.session.get_next_card()
            if not card:
                self._json_response({"done": True, "session_stats": {
                    "reviewed": self.session.reviewed, "remaining": 0}})
            else:
                front_html = self.session.render_front(card)
                flags = get_flags(self.session.conn, card["id"])
                self._json_response({
                    "done": False,
                    "id": card["id"],
                    "gradable": bool(card["gradable"]),
                    "front_html": front_html,
                    "flags": flags,
                    "session_stats": {
                        "reviewed": self.session.reviewed,
                        "remaining": self.session.remaining_count()
                    }
                })
        elif self.path == "/api/status":
            if not self._check_token():
                return
            self._json_response({
                "reviewed": self.session.reviewed,
                "remaining": self.session.remaining_count()
            })
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/flip":
            if not self._check_token():
                return
            try:
                back_html = self.session.flip()
                self._json_response({"back_html": back_html})
            except ValueError as e:
                self._error(400, str(e))

        elif self.path == "/api/grade":
            if not self._check_token():
                return
            body = self._read_body()
            grade = body.get("grade")
            if grade not in (0, 1):
                self._error(400, "grade must be 0 or 1")
                return
            try:
                self.session.grade_current(
                    grade, body.get("feedback"), body.get("response"))
                self._json_response({"ok": True})
            except ValueError as e:
                self._error(400, str(e))

        elif self.path == "/api/skip":
            if not self._check_token():
                return
            # Non-gradable card — advance without logging a grade
            if self.session.current_card:
                self.session.previous_card = self.session.current_card
                self.session.reviewed_ids.add(self.session.current_card["id"])
                self.session.reviewed += 1
                self.session.current_card = None
            self._json_response({"ok": True})

        elif self.path == "/api/undo":
            if not self._check_token():
                return
            prev = self.session.previous_card
            if not prev:
                self._error(400, "Nothing to undo")
                return
            # Re-present previous card
            self.session.reviewed_ids.discard(prev["id"])
            self.session.current_card = prev
            self.session.serve_time = time.time()
            self.session.flip_time = time.time()  # Already seen back
            self.session.previous_card = None
            front_html = self.session.render_front(prev)
            adapter = get_adapter(prev["adapter"], self.session.sr_dir)
            content = json.loads(prev["content"])
            try:
                back_html = adapter.render_back(content)
            except Exception as e:
                back_html = f'<div style="color:var(--wrong)">Render error: {e}</div>'
            self._json_response({"ok": True, "front_html": front_html, "back_html": back_html})

        elif self.path == "/api/flag":
            if not self._check_token():
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            add_flag(self.session.conn, card["id"], flag, body.get("note"))
            self._json_response({"ok": True, "flags": get_flags(self.session.conn, card["id"])})

        elif self.path == "/api/unflag":
            if not self._check_token():
                return
            body = self._read_body()
            flag = body.get("flag")
            if not flag:
                self._error(400, "flag is required")
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            remove_flag(self.session.conn, card["id"], flag)
            self._json_response({"ok": True, "flags": get_flags(self.session.conn, card["id"])})

        elif self.path == "/api/edit":
            if not self._check_token():
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            try:
                content = json.loads(card["content"])
                source_line = content.get("source_line", 1)
                cmd = _build_edit_command(self.session.settings, card["source_path"], source_line)
                subprocess.Popen(cmd, shell=True, start_new_session=True)
                self._json_response({"ok": True})
            except Exception as e:
                self._error(500, str(e))

        elif self.path == "/api/suspend":
            if not self._check_token():
                return
            card = self.session.current_card
            if not card:
                self._error(400, "No current card")
                return
            card_id = card["id"]
            self.session.conn.execute(
                "UPDATE card_state SET status='inactive', updated_at=datetime('now') WHERE card_id=?",
                (card_id,))
            self.session.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
            self.session.conn.commit()
            if self.session.scheduler:
                try:
                    self.session.scheduler.on_card_status_changed(card_id, "inactive")
                except Exception:
                    pass
            # Advance like a grade
            self.session.previous_card = self.session.current_card
            self.session.reviewed_ids.add(card_id)
            self.session.current_card = None
            self._json_response({"ok": True, "suspended": True})

        else:
            self._error(404, "Not found")


def _build_edit_command(settings, file_path, line=1):
    template = settings.get("edit_command")
    if template:
        return template.replace("{file}", shlex.quote(file_path)).replace("{line}", str(line))
    editor = os.environ.get("EDITOR", "vim")
    for term_cmd in ["kitty -e", "alacritty -e", "foot", "xterm -e"]:
        if shutil.which(term_cmd.split()[0]):
            return f"{term_cmd} {editor} +{line} {shlex.quote(file_path)}"
    return f"{editor} +{line} {shlex.quote(file_path)}"


def start_review_server(conn, scheduler, sr_dir, settings,
                        tag_filter=None, path_filter=None, flag_filter=None):
    port = settings.get("review_port", 8791)
    session = ReviewSession(conn, scheduler, sr_dir, settings,
                            tag_filter, path_filter, flag_filter)
    ReviewHandler.session = session
    ReviewHandler.settings = settings

    server = http.server.HTTPServer(("127.0.0.1", port), ReviewHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Review server running at {url}")
    print(f"Press Ctrl+C to stop")

    # Try to open browser
    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSession ended.")
        print(f"  Reviewed: {session.reviewed} cards")
    finally:
        server.server_close()

# ─── Browse Web Server ───────────────────────────────────────────────────────

BROWSE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sr browse</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">
<style>
:root {
    --bg: #1c1b1a;
    --bg-card: #272524;
    --bg-inset: #1f1e1d;
    --border: #3a3634;
    --border-focus: #6b6560;
    --text: #d4cfc9;
    --text-muted: #8a8480;
    --text-dim: #5c5753;
    --accent: #87CEAB;
    --correct: #5cb85c;
    --wrong: #d9534f;
    --flag: #f0c040;
    --tag: #8bb878;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text); padding: 1.5rem;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#top-bar {
    display: flex; gap: 0.8rem; flex-wrap: wrap; align-items: center;
    margin-bottom: 1rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
}
#top-bar select, #top-bar input {
    padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg-card); color: var(--text); font-size: 0.85rem;
}
#top-bar input { width: 200px; }
#card-table { width: 100%; border-collapse: collapse; }
#card-table th {
    text-align: left; padding: 0.5rem 0.6rem; border-bottom: 2px solid var(--border);
    font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;
}
#card-table td { padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
#card-table tr:hover { background: var(--bg-card); cursor: pointer; }
.badge {
    display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
    font-size: 0.75rem; font-weight: 600;
}
.badge-active { background: rgba(92,184,92,0.12); color: var(--correct); }
.badge-inactive { background: rgba(217,83,79,0.12); color: var(--wrong); }
.badge-flag { background: rgba(240,192,64,0.12); color: var(--flag); margin-left: 0.3rem; }
.badge-tag { background: rgba(139,184,120,0.12); color: var(--tag); margin-right: 0.3rem; }
#pagination {
    display: flex; gap: 1rem; align-items: center; justify-content: center;
    margin-top: 1rem; color: var(--text-muted); font-size: 0.85rem;
}
#pagination button {
    padding: 0.3rem 0.8rem; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--text-muted); cursor: pointer; font-size: 0.85rem;
}
#pagination button:hover { border-color: var(--border-focus); color: var(--text); }
#pagination button:disabled { opacity: 0.4; cursor: default; }
#detail-panel {
    display: none; position: fixed; right: 0; top: 0; width: 420px; height: 100vh;
    background: var(--bg-card); border-left: 2px solid var(--border); padding: 1.5rem;
    overflow-y: auto; z-index: 100;
}
#detail-panel .close-btn {
    position: absolute; top: 0.8rem; right: 0.8rem; background: none; border: none;
    color: var(--text-muted); font-size: 1.2rem; cursor: pointer;
}
#detail-panel .close-btn:hover { color: var(--text); }
#detail-panel h3 { margin-bottom: 0.8rem; color: var(--accent); }
#detail-panel .section { margin-bottom: 1.2rem; }
#detail-panel .section-title { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.3rem; }
#detail-panel .content-box {
    background: var(--bg-inset); padding: 0.8rem; border-radius: 6px;
    font-size: 0.85rem; line-height: 1.5; white-space: pre-wrap;
}
.detail-btn {
    padding: 0.3rem 0.7rem; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--text-muted); cursor: pointer; font-size: 0.8rem;
    margin-right: 0.3rem; margin-bottom: 0.3rem;
}
.detail-btn:hover { border-color: var(--border-focus); color: var(--text); }
.detail-btn.active { border-color: var(--flag); color: var(--flag); }
.card-preview {
    background: var(--bg-inset); padding: 1rem; border-radius: 8px;
    font-size: 0.95rem; line-height: 1.6; text-align: center;
}
.card-preview pre { text-align: left; background: var(--bg); padding: 0.8rem; border-radius: 6px; overflow-x: auto; }
.card-preview code { font-family: "JetBrains Mono", "Fira Code", monospace; }
.card-divider {
    border: none; border-top: 1px solid var(--border); margin: 0.8rem 0;
}
#total-count { color: var(--text-muted); font-size: 0.85rem; margin-left: auto; }
</style>
</head>
<body>
<div id="top-bar">
    <select id="filter-status" onchange="loadCards()">
        <option value="">All statuses</option>
        <option value="active" selected>Active</option>
        <option value="inactive">Inactive</option>
    </select>
    <select id="filter-tag" onchange="loadCards()">
        <option value="">All tags</option>
    </select>
    <select id="filter-flag" onchange="loadCards()">
        <option value="">All flags</option>
    </select>
    <input id="filter-search" type="text" placeholder="Search..." oninput="debounceSearch()">
    <span id="total-count"></span>
</div>
<table id="card-table">
    <thead><tr>
        <th>Card</th><th>Status</th><th>Tags</th><th>Source</th><th>Flags</th>
    </tr></thead>
    <tbody id="card-tbody"></tbody>
</table>
<div id="pagination">
    <button id="prev-btn" onclick="prevPage()" disabled>&larr; Prev</button>
    <span id="page-info"></span>
    <button id="next-btn" onclick="nextPage()" disabled>Next &rarr;</button>
</div>
<div id="detail-panel">
    <button class="close-btn" onclick="closeDetail()">&times;</button>
    <div id="detail-content"></div>
</div>
<script>
let offset = 0;
const limit = 50;
let totalCards = 0;
let searchTimeout = null;

async function api(method, path, body) {
    const opts = {method, headers: {"Content-Type": "application/json"}};
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
}

async function loadFilters() {
    try {
        const [tags, flags] = await Promise.all([api("GET", "/api/tags"), api("GET", "/api/flags")]);
        const tagSel = document.getElementById("filter-tag");
        tags.forEach(t => { const o = document.createElement("option"); o.value = t; o.textContent = t; tagSel.appendChild(o); });
        const flagSel = document.getElementById("filter-flag");
        flags.forEach(f => { const o = document.createElement("option"); o.value = f; o.textContent = f; flagSel.appendChild(o); });
    } catch(e) { console.error(e); }
}

async function loadCards() {
    offset = 0;
    await fetchCards();
}

function debounceSearch() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(loadCards, 300);
}

async function fetchCards() {
    const params = new URLSearchParams();
    const status = document.getElementById("filter-status").value;
    const tag = document.getElementById("filter-tag").value;
    const flag = document.getElementById("filter-flag").value;
    const q = document.getElementById("filter-search").value;
    if (status) params.set("status", status);
    if (tag) params.set("tag", tag);
    if (flag) params.set("flag", flag);
    if (q) params.set("q", q);
    params.set("offset", offset);
    params.set("limit", limit);
    try {
        const data = await api("GET", "/api/cards?" + params.toString());
        totalCards = data.total;
        document.getElementById("total-count").textContent = totalCards + " card(s)";
        renderTable(data.cards);
        updatePagination();
    } catch(e) { console.error(e); }
}

function renderTable(cards) {
    const tbody = document.getElementById("card-tbody");
    tbody.innerHTML = "";
    cards.forEach(c => {
        const tr = document.createElement("tr");
        tr.onclick = () => showDetail(c.id);
        const source = c.source_path.split("/").pop();
        const flagBadges = (c.flags || []).map(f => `<span class="badge badge-flag">${esc(f)}</span>`).join("");
        const tagBadges = (c.tags || []).map(t => `<span class="badge badge-tag">${esc(t)}</span>`).join("");
        const statusCls = c.status === "active" ? "badge-active" : "badge-inactive";
        tr.innerHTML = `
            <td>${esc(c.display_text || "(no text)")}</td>
            <td><span class="badge ${statusCls}">${c.status}</span></td>
            <td>${tagBadges}</td>
            <td title="${esc(c.source_path)}">${esc(source)}</td>
            <td>${flagBadges}</td>
        `;
        tbody.appendChild(tr);
    });
}

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function updatePagination() {
    const maxPage = Math.max(1, Math.ceil(totalCards / limit));
    const curPage = Math.floor(offset / limit) + 1;
    document.getElementById("page-info").textContent = `Page ${curPage} of ${maxPage}`;
    document.getElementById("prev-btn").disabled = offset === 0;
    document.getElementById("next-btn").disabled = offset + limit >= totalCards;
}

function prevPage() { offset = Math.max(0, offset - limit); fetchCards(); }
function nextPage() { offset += limit; fetchCards(); }

async function showDetail(cardId) {
    try {
        const data = await api("GET", "/api/cards/" + cardId);
        const c = data;
        const statusCls = c.status === "active" ? "badge-active" : "badge-inactive";
        const toggleLabel = c.status === "active" ? "Suspend" : "Activate";
        const flagBtns = (c.flags || []).map(f =>
            `<button class="detail-btn active" onclick="removeFlag(${c.id},'${esc(f.flag)}')">${esc(f.flag)} &times;</button>`
        ).join("");
        const tagBtns = (c.tags || []).map(t =>
            `<button class="detail-btn" style="border-color:var(--tag);color:var(--tag);" onclick="removeTag(${c.id},'${esc(t)}')">${esc(t)} &times;</button>`
        ).join("");
        const reviews = (c.reviews || []).map(r =>
            `<div style="font-size:0.8rem;color:var(--text-muted);">${r.timestamp} — ${r.grade === 1 ? "✓" : "✗"}${r.feedback ? " (" + r.feedback + ")" : ""}</div>`
        ).join("");
        document.getElementById("detail-content").innerHTML = `
            <h3>Card #${c.id}</h3>
            <div class="section">
                <div class="section-title">Front</div>
                <div class="card-preview">${c.front_html || esc(c.display_text || "")}</div>
            </div>
            <div class="section">
                <div class="section-title">Back</div>
                <div class="card-preview">${c.back_html || ""}</div>
            </div>
            <div class="section">
                <div class="section-title">Source</div>
                <div style="font-size:0.85rem; display:flex; align-items:center; gap:0.5rem;">
                    ${esc(c.source_path)}
                    <button class="detail-btn" onclick="editCard(${c.id})">&#9998; Edit</button>
                </div>
            </div>
            <div class="section">
                <div class="section-title">Status</div>
                <span class="badge ${statusCls}">${c.status}</span>
                <button class="detail-btn" onclick="toggleStatus(${c.id},'${c.status}')" style="margin-left:0.5rem">${toggleLabel}</button>
            </div>
            <div class="section">
                <div class="section-title">Tags</div>
                ${tagBtns}
                <button class="detail-btn" onclick="promptAddTag(${c.id})">+ Add tag</button>
            </div>
            <div class="section">
                <div class="section-title">Flags</div>
                ${flagBtns}
                <button class="detail-btn" onclick="promptAddFlag(${c.id})">+ Add flag</button>
            </div>
            <div class="section">
                <div class="section-title">Review History</div>
                ${reviews || '<div style="font-size:0.85rem;color:var(--text-dim);">No reviews yet.</div>'}
            </div>
        `;
        document.getElementById("detail-panel").style.display = "block";
    } catch(e) { console.error(e); }
}

function closeDetail() { document.getElementById("detail-panel").style.display = "none"; }

async function toggleStatus(cardId, current) {
    const newStatus = current === "active" ? "inactive" : "active";
    try {
        await api("POST", "/api/cards/" + cardId + "/status", {status: newStatus});
        showDetail(cardId);
        fetchCards();
    } catch(e) { console.error(e); }
}

async function removeFlag(cardId, flag) {
    try {
        await api("POST", "/api/cards/" + cardId + "/unflag", {flag: flag});
        showDetail(cardId);
        fetchCards();
    } catch(e) { console.error(e); }
}

async function removeTag(cardId, tag) {
    try {
        await api("POST", "/api/cards/" + cardId + "/untag", {tag: tag});
        showDetail(cardId);
        fetchCards();
    } catch(e) { console.error(e); }
}

async function promptAddTag(cardId) {
    const tag = prompt("Tag name:");
    if (!tag) return;
    try {
        await api("POST", "/api/cards/" + cardId + "/tag", {tag: tag});
        showDetail(cardId);
        fetchCards();
    } catch(e) { console.error(e); }
}

async function editCard(cardId) {
    try {
        await api("POST", "/api/cards/" + cardId + "/edit");
    } catch(e) { console.error(e); }
}

async function promptAddFlag(cardId) {
    const flag = prompt("Flag name (e.g. edit_later, needs_work):");
    if (!flag) return;
    const note = prompt("Note (optional):");
    try {
        await api("POST", "/api/cards/" + cardId + "/flag", {flag: flag, note: note || null});
        showDetail(cardId);
        fetchCards();
    } catch(e) { console.error(e); }
}

loadFilters();
loadCards();
</script>
</body>
</html>"""


class BrowseHandler(http.server.BaseHTTPRequestHandler):
    conn: sqlite3.Connection = None
    sr_dir: pathlib.Path = None
    settings: dict = {}

    def log_message(self, format, *args):
        pass

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json_response({"error": msg}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        path, qs = self._parse_path()

        if path == "/":
            body = BROWSE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/cards":
            status = qs.get("status", [None])[0]
            tag = qs.get("tag", [None])[0]
            flag = qs.get("flag", [None])[0]
            q = qs.get("q", [None])[0]
            off = int(qs.get("offset", [0])[0])
            lim = int(qs.get("limit", [50])[0])
            lim = min(lim, 200)

            where = ["cs.status != 'deleted'"]
            params = []
            if status:
                where.append("cs.status = ?")
                params.append(status)
            if tag:
                where.append("c.id IN (SELECT card_id FROM card_tags WHERE tag = ?)")
                params.append(tag)
            if flag:
                where.append("c.id IN (SELECT card_id FROM card_flags WHERE flag = ?)")
                params.append(flag)
            if q:
                where.append("(c.display_text LIKE ? OR c.source_path LIKE ?)")
                params.extend([f"%{q}%", f"%{q}%"])

            where_sql = " AND ".join(where)

            total = self.conn.execute(
                f"SELECT COUNT(*) as cnt FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE {where_sql}",
                params).fetchone()["cnt"]

            rows = self.conn.execute(f"""
                SELECT c.id, c.display_text, c.source_path, cs.status
                FROM cards c JOIN card_state cs ON c.id=cs.card_id
                WHERE {where_sql}
                ORDER BY c.id DESC LIMIT ? OFFSET ?
            """, params + [lim, off]).fetchall()

            cards = []
            for r in rows:
                cid = r["id"]
                tags = [row["tag"] for row in self.conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (cid,))]
                flags = [row["flag"] for row in self.conn.execute("SELECT flag FROM card_flags WHERE card_id=?", (cid,))]
                cards.append({
                    "id": cid, "display_text": r["display_text"],
                    "source_path": r["source_path"], "status": r["status"],
                    "tags": tags, "flags": flags
                })
            self._json_response({"cards": cards, "total": total, "offset": off, "limit": lim})

        elif path.startswith("/api/cards/") and path.count("/") == 3:
            try:
                card_id = int(path.split("/")[3])
            except (ValueError, IndexError):
                self._error(400, "Invalid card ID")
                return
            row = self.conn.execute("""
                SELECT c.id, c.display_text, c.source_path, c.adapter, c.content, cs.status
                FROM cards c JOIN card_state cs ON c.id=cs.card_id WHERE c.id=?
            """, (card_id,)).fetchone()
            if not row:
                self._error(404, "Card not found")
                return
            tags = [r["tag"] for r in self.conn.execute("SELECT tag FROM card_tags WHERE card_id=?", (card_id,))]
            flags = get_flags(self.conn, card_id)
            reviews = [dict(r) for r in self.conn.execute(
                "SELECT timestamp, grade, feedback FROM review_log WHERE card_id=? ORDER BY timestamp DESC LIMIT 20",
                (card_id,))]
            content = json.loads(row["content"])
            # Render front/back via adapter
            front_html = ""
            back_html = ""
            try:
                adapter = get_adapter(row["adapter"], self.sr_dir)
                front_html = adapter.render_front(content)
                back_html = adapter.render_back(content)
            except Exception as e:
                front_html = f'<div style="color:var(--wrong)">Render error: {e}</div>'
            self._json_response({
                "id": row["id"], "display_text": row["display_text"],
                "source_path": row["source_path"], "adapter": row["adapter"],
                "content": content, "status": row["status"],
                "tags": tags, "flags": flags, "reviews": reviews,
                "front_html": front_html, "back_html": back_html
            })

        elif path == "/api/tags":
            tags = [r["tag"] for r in self.conn.execute(
                "SELECT DISTINCT tag FROM card_tags ct JOIN card_state cs ON ct.card_id=cs.card_id WHERE cs.status != 'deleted' ORDER BY tag")]
            self._json_response(tags)

        elif path == "/api/flags":
            flags = [r["flag"] for r in self.conn.execute(
                "SELECT DISTINCT flag FROM card_flags cf JOIN card_state cs ON cf.card_id=cs.card_id WHERE cs.status != 'deleted' ORDER BY flag")]
            self._json_response(flags)

        else:
            self._error(404, "Not found")

    def do_POST(self):
        path, _ = self._parse_path()

        # Match /api/cards/{id}/status, /api/cards/{id}/flag, /api/cards/{id}/unflag
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "cards":
            try:
                card_id = int(parts[2])
            except ValueError:
                self._error(400, "Invalid card ID")
                return
            action = parts[3]
            body = self._read_body()

            if action == "status":
                new_status = body.get("status")
                if new_status not in ("active", "inactive"):
                    self._error(400, "status must be 'active' or 'inactive'")
                    return
                self.conn.execute(
                    "UPDATE card_state SET status=?, updated_at=datetime('now') WHERE card_id=?",
                    (new_status, card_id))
                if new_status == "inactive":
                    self.conn.execute("DELETE FROM recommendations WHERE card_id=?", (card_id,))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "flag":
                flag = body.get("flag")
                if not flag:
                    self._error(400, "flag is required")
                    return
                add_flag(self.conn, card_id, flag, body.get("note"))
                self._json_response({"ok": True})

            elif action == "unflag":
                flag = body.get("flag")
                if not flag:
                    self._error(400, "flag is required")
                    return
                remove_flag(self.conn, card_id, flag)
                self._json_response({"ok": True})

            elif action == "tag":
                tag = body.get("tag")
                if not tag:
                    self._error(400, "tag is required")
                    return
                self.conn.execute(
                    "INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)",
                    (card_id, tag))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "untag":
                tag = body.get("tag")
                if not tag:
                    self._error(400, "tag is required")
                    return
                self.conn.execute(
                    "DELETE FROM card_tags WHERE card_id=? AND tag=?",
                    (card_id, tag))
                self.conn.commit()
                self._json_response({"ok": True})

            elif action == "edit":
                row = self.conn.execute(
                    "SELECT source_path, content FROM cards WHERE id=?", (card_id,)).fetchone()
                if not row:
                    self._error(404, "Card not found")
                    return
                try:
                    content = json.loads(row["content"])
                    source_line = content.get("source_line", 1)
                    cmd = _build_edit_command(self.settings, row["source_path"], source_line)
                    subprocess.Popen(cmd, shell=True, start_new_session=True)
                    self._json_response({"ok": True})
                except Exception as e:
                    self._error(500, str(e))

            else:
                self._error(404, "Not found")
        else:
            self._error(404, "Not found")


def start_browse_server(conn, sr_dir, settings):
    port = settings.get("review_port", 8791) + 1
    BrowseHandler.conn = conn
    BrowseHandler.sr_dir = sr_dir
    BrowseHandler.settings = settings

    server = http.server.HTTPServer(("127.0.0.1", port), BrowseHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Browse server running at {url}")
    print(f"Press Ctrl+C to stop")

    try:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBrowse session ended.")
    finally:
        server.server_close()


def cmd_browse(args, sr_dir, settings):
    db_path = sr_dir / "sr.db"
    if not db_path.exists():
        print("No database found. Run 'sr scan' first.")
        return
    conn = init_db(db_path)
    if hasattr(args, 'port') and args.port:
        settings = dict(settings)
        settings["review_port"] = args.port - 1  # browse = review_port + 1
    start_browse_server(conn, sr_dir, settings)
    conn.close()


# ─── CLI Commands ────────────────────────────────────────────────────────────

def cmd_scan(args, sr_dir, settings):
    db_path = sr_dir / "sr.db"
    conn = init_db(db_path)

    # Load scheduler
    scheduler = None
    sched_name = settings.get("scheduler", "sm2")
    try:
        scheduler = load_scheduler(sched_name, sr_dir, db_path)
    except Exception as e:
        print(f"Warning: cannot load scheduler '{sched_name}': {e}", file=sys.stderr)

    # Determine paths to scan
    paths = []
    if args.path:
        for p in args.path:
            paths.append(pathlib.Path(p).resolve())
    else:
        # Default: scan current directory
        paths.append(pathlib.Path.cwd())

    print(f"Scanning {len(paths)} path(s)...")
    results = scan_sources(paths, sr_dir)

    total_cards = sum(len(cards) for _, _, cards, _ in results)
    print(f"Found {total_cards} cards from {len(results)} source(s)")

    stats = sync_cards(conn, results, scheduler, scanned_paths=paths)
    print(f"Synced: {stats['new']} new, {stats['updated']} updated, "
          f"{stats['deleted']} deleted, {stats['unchanged']} unchanged")
    conn.close()

def cmd_review(args, sr_dir, settings):
    db_path = sr_dir / "sr.db"
    conn = init_db(db_path)

    # Load scheduler
    scheduler = None
    sched_name = settings.get("scheduler", "sm2")
    try:
        scheduler = load_scheduler(sched_name, sr_dir, db_path)
    except Exception as e:
        print(f"Warning: cannot load scheduler '{sched_name}': {e}", file=sys.stderr)

    # Scan first if paths given
    path_filter = None
    if args.path:
        paths = [pathlib.Path(p).resolve() for p in args.path]
        results = scan_sources(paths, sr_dir)
        stats = sync_cards(conn, results, scheduler, scanned_paths=paths)
        print(f"Scanned: {stats['new']} new, {stats['updated']} updated, "
              f"{stats['deleted']} deleted, {stats['unchanged']} unchanged")
        # Filter review to scanned paths
        path_filter = str(paths[0])
    else:
        # Still scan cwd
        paths = [pathlib.Path.cwd()]
        results = scan_sources(paths, sr_dir)
        stats = sync_cards(conn, results, scheduler, scanned_paths=paths)
        if stats['new'] or stats['updated'] or stats['deleted']:
            print(f"Scanned: {stats['new']} new, {stats['updated']} updated, "
                  f"{stats['deleted']} deleted, {stats['unchanged']} unchanged")

    # Check we have cards
    count = conn.execute("""
        SELECT COUNT(*) as cnt FROM cards c
        JOIN card_state cs ON c.id = cs.card_id
        WHERE cs.status = 'active' AND c.gradable = 1
    """).fetchone()["cnt"]

    if count == 0:
        print("No cards to review.")
        conn.close()
        return

    print(f"{count} active card(s)")
    tag_filter = getattr(args, 'tag', None)
    flag_filter = getattr(args, 'flag', None)
    start_review_server(conn, scheduler, sr_dir, settings, tag_filter, path_filter, flag_filter)
    conn.close()

def cmd_status(args, sr_dir, settings):
    db_path = sr_dir / "sr.db"
    if not db_path.exists():
        print("No database found. Run 'sr scan' first.")
        return

    conn = init_db(db_path)

    total = conn.execute("""
        SELECT COUNT(*) as cnt FROM cards c
        JOIN card_state cs ON c.id = cs.card_id WHERE cs.status = 'active'
    """).fetchone()["cnt"]

    gradable = conn.execute("""
        SELECT COUNT(*) as cnt FROM cards c
        JOIN card_state cs ON c.id = cs.card_id
        WHERE cs.status = 'active' AND c.gradable = 1
    """).fetchone()["cnt"]

    due = conn.execute("""
        SELECT COUNT(*) as cnt FROM recommendations r
        JOIN card_state cs ON r.card_id = cs.card_id
        WHERE cs.status = 'active' AND r.time <= datetime('now')
    """).fetchone()["cnt"]

    reviewed_today = conn.execute("""
        SELECT COUNT(*) as cnt FROM review_log
        WHERE timestamp >= date('now')
    """).fetchone()["cnt"]

    total_reviews = conn.execute("SELECT COUNT(*) as cnt FROM review_log").fetchone()["cnt"]

    print(f"Cards:          {total} total ({gradable} gradable)")
    print(f"Due now:        {due}")
    print(f"Reviewed today: {reviewed_today}")
    print(f"Total reviews:  {total_reviews}")

    # Show sources
    sources = conn.execute("""
        SELECT c.source_path, COUNT(*) as cnt
        FROM cards c JOIN card_state cs ON c.id = cs.card_id
        WHERE cs.status = 'active'
        GROUP BY c.source_path ORDER BY c.source_path
    """).fetchall()
    if sources:
        print(f"\nSources:")
        for s in sources:
            print(f"  {s['source_path']}: {s['cnt']} cards")

    conn.close()

def main():
    parser = argparse.ArgumentParser(prog="sr", description="Spaced Repetition System")
    subparsers = parser.add_subparsers(dest="command")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan sources and sync cards to DB")
    p_scan.add_argument("path", nargs="*", help="Paths to scan (default: cwd)")

    # review
    p_review = subparsers.add_parser("review", help="Scan and start review session")
    p_review.add_argument("path", nargs="*", help="Paths to scan/review")
    p_review.add_argument("--tag", help="Filter by tag")
    p_review.add_argument("--flag", help="Filter by flag (e.g. edit_later)")

    # status
    p_status = subparsers.add_parser("status", help="Show card counts and stats")

    # browse
    p_browse = subparsers.add_parser("browse", help="Browse and manage cards in browser")
    p_browse.add_argument("--port", type=int, help="Server port (default: review_port + 1)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    sr_dir = get_sr_dir()
    if not sr_dir.exists():
        sr_dir.mkdir(parents=True, exist_ok=True)
        print(f"Created SR directory: {sr_dir}")

    settings = load_settings(sr_dir)

    if args.command == "scan":
        cmd_scan(args, sr_dir, settings)
    elif args.command == "review":
        cmd_review(args, sr_dir, settings)
    elif args.command == "status":
        cmd_status(args, sr_dir, settings)
    elif args.command == "browse":
        cmd_browse(args, sr_dir, settings)

if __name__ == "__main__":
    main()
