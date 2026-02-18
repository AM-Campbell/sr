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
import sqlite3
import sys
import threading
import time
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
    suspended: bool = False
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

def scan_sources(paths: list[pathlib.Path], sr_dir: pathlib.Path) -> list[tuple[str, str, list[Card]]]:
    """Returns list of (source_path, adapter_name, cards)."""
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
        results.append((str(path), adapter_name, cards))
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
                    results.append((str(f), adapter_name, cards))
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

def sync_cards(conn: sqlite3.Connection, scan_results: list[tuple[str, str, list[Card]]],
               scheduler=None, scanned_paths: list[pathlib.Path] | None = None) -> dict:
    """Sync scanned cards to DB. Returns stats dict."""
    stats = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    # Collect all source_paths from scan
    scanned_sources = set()
    scanned_keys = {}  # (source_path, card_key, adapter) -> Card

    for source_path, adapter_name, cards in scan_results:
        scanned_sources.add(source_path)
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
        desired_status = "inactive" if card.suspended else "active"

        if key_tuple in existing_map:
            row = existing_map[key_tuple]
            current_status = row["status"]

            if row["content_hash"] == chash:
                # Content unchanged — but check suspend/unsuspend transitions
                if current_status != desired_status:
                    conn.execute("UPDATE card_state SET status=?, updated_at=datetime('now') WHERE card_id=?",
                                 (desired_status, row["id"]))
                    if scheduler:
                        try:
                            if desired_status == "active" and current_status == "inactive":
                                # Unsuspended — treat like new card for scheduling
                                rec = scheduler.on_card_created(row["id"])
                                if rec:
                                    _upsert_recommendation(conn, rec, scheduler)
                            else:
                                # Suspended — remove from review pool
                                conn.execute("DELETE FROM recommendations WHERE card_id=?", (row["id"],))
                                scheduler.on_card_status_changed(row["id"], desired_status)
                        except Exception as e:
                            print(f"Warning: scheduler status change failed: {e}", file=sys.stderr)
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
                _sync_tags(conn, row["id"], card.tags)
            else:
                # Content changed — mark old as deleted, insert new
                old_id = row["id"]
                conn.execute("UPDATE card_state SET status='deleted', updated_at=datetime('now') WHERE card_id=?", (old_id,))
                # Retire old card's unique key so the new card can use it
                conn.execute("UPDATE cards SET card_key = card_key || '__replaced_' || CAST(id AS TEXT) WHERE id=?", (old_id,))
                new_id = _insert_card(conn, source_path, card_key, adapter_name, card, chash,
                                      status=desired_status)
                conn.execute("""
                    INSERT INTO card_relations (upstream_card_id, downstream_card_id, relation_type)
                    VALUES (?, ?, 'is_replaced_by')
                """, (old_id, new_id))
                if scheduler and desired_status == "active":
                    try:
                        rec = scheduler.on_card_replaced(old_id, new_id)
                        if rec:
                            _upsert_recommendation(conn, rec, scheduler)
                    except Exception as e:
                        print(f"Warning: scheduler on_card_replaced failed: {e}", file=sys.stderr)
                stats["updated"] += 1
            del existing_map[key_tuple]
        else:
            # New card
            new_id = _insert_card(conn, source_path, card_key, adapter_name, card, chash,
                                  status=desired_status)
            if scheduler and desired_status == "active":
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
    for source_path, adapter_name, cards in scan_results:
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

# ─── Review Web Server ──────────────────────────────────────────────────────

REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sr review</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 2rem;
}
#progress {
    width: 100%; max-width: 600px; text-align: center;
    color: #888; font-size: 0.9rem; margin-bottom: 1rem;
}
#card-container {
    width: 100%; max-width: 600px;
    background: #16213e; border-radius: 12px;
    padding: 2rem; min-height: 300px;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    cursor: pointer; user-select: none;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    transition: box-shadow 0.2s;
}
#card-container:hover { box-shadow: 0 6px 28px rgba(0,0,0,0.5); }
#card-container.flipped { cursor: default; }
#card-front, #card-back { width: 100%; text-align: center; }
#card-front { font-size: 1.3rem; line-height: 1.6; }
#card-back {
    display: none; margin-top: 1.5rem; padding-top: 1.5rem;
    border-top: 1px solid #2a3a5e; font-size: 1.1rem; line-height: 1.5;
}
.flip-hint { color: #555; font-size: 0.85rem; margin-top: 1rem; }
#controls { display: none; margin-top: 1.5rem; gap: 1rem; flex-wrap: wrap; justify-content: center; }
.grade-btn {
    padding: 0.7rem 2rem; border: none; border-radius: 8px;
    font-size: 1rem; cursor: pointer; font-weight: 600;
    transition: transform 0.1s, opacity 0.1s;
}
.grade-btn:hover { transform: scale(1.05); }
.grade-btn:active { transform: scale(0.97); }
#btn-wrong { background: #e74c3c; color: white; }
#btn-correct { background: #2ecc71; color: white; }
#feedback-row { display: none; margin-top: 0.8rem; gap: 0.5rem; justify-content: center; }
.fb-btn {
    padding: 0.4rem 0.8rem; border: 1px solid #444; border-radius: 6px;
    background: transparent; color: #aaa; font-size: 0.8rem; cursor: pointer;
}
.fb-btn:hover { border-color: #888; color: #ddd; }
.fb-btn.selected { border-color: #6c5ce7; color: #6c5ce7; }
#undo-btn {
    margin-top: 1rem; padding: 0.4rem 1rem; border: 1px solid #444;
    border-radius: 6px; background: transparent; color: #888;
    font-size: 0.8rem; cursor: pointer; display: none;
}
#undo-btn:hover { border-color: #888; color: #ddd; }
#done-msg { display: none; font-size: 1.3rem; color: #6c5ce7; margin-top: 2rem; }
#error-msg { color: #e74c3c; margin-top: 1rem; display: none; }
pre { text-align: left; background: #0f1729; padding: 1rem; border-radius: 6px; overflow-x: auto; }
code { font-family: "JetBrains Mono", "Fira Code", monospace; }
#autograde-result {
    display: none; margin-top: 1rem; padding: 0.6rem 1.2rem;
    border-radius: 8px; font-weight: 600; font-size: 1rem; text-align: center;
}
#autograde-result.correct { background: rgba(46,204,113,0.15); color: #2ecc71; border: 1px solid #2ecc71; }
#autograde-result.wrong { background: rgba(231,76,60,0.15); color: #e74c3c; border: 1px solid #e74c3c; }
#btn-next {
    display: none; padding: 0.7rem 2rem; border: none; border-radius: 8px;
    font-size: 1rem; cursor: pointer; font-weight: 600; background: #6c5ce7; color: white;
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
            document.getElementById("done-msg").style.display = "block";
            return;
        }
        currentGradable = data.gradable;
        autoGradeResult = null;
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
    else if (e.key === "z" || e.key === "u") undo();
});

init();
</script>
</body>
</html>"""


class ReviewSession:
    def __init__(self, conn, scheduler, sr_dir, tag_filter=None, path_filter=None):
        self.conn = conn
        self.scheduler = scheduler
        self.sr_dir = sr_dir
        self.tag_filter = tag_filter
        self.path_filter = path_filter
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
            return f'<div style="color:#e74c3c">Render error (card {self.current_card["id"]}): {e}</div>'

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
            return f'<div style="color:#e74c3c">Render error (card {card["id"]}): {e}</div>'


class ReviewHandler(http.server.BaseHTTPRequestHandler):
    session: ReviewSession = None

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
                self._json_response({
                    "done": False,
                    "id": card["id"],
                    "gradable": bool(card["gradable"]),
                    "front_html": front_html,
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
                back_html = f'<div style="color:#e74c3c">Render error: {e}</div>'
            self._json_response({"ok": True, "front_html": front_html, "back_html": back_html})
        else:
            self._error(404, "Not found")


def start_review_server(conn, scheduler, sr_dir, settings,
                        tag_filter=None, path_filter=None):
    port = settings.get("review_port", 8791)
    session = ReviewSession(conn, scheduler, sr_dir, tag_filter, path_filter)
    ReviewHandler.session = session

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

    total_cards = sum(len(cards) for _, _, cards in results)
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
    start_review_server(conn, scheduler, sr_dir, settings, tag_filter, path_filter)
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

    # status
    p_status = subparsers.add_parser("status", help="Show card counts and stats")

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

if __name__ == "__main__":
    main()
