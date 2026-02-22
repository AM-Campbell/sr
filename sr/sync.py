"""Card synchronization: sync scanned cards to the database."""

import json
import pathlib
import sqlite3
import sys

from sr.models import Card, Recommendation
from sr.scanner import content_hash


def sync_cards(conn: sqlite3.Connection,
               scan_results: list[tuple[str, str, list[Card], dict]],
               scheduler=None,
               scanned_paths: list[pathlib.Path] | None = None) -> dict:
    """Sync scanned cards to DB. Returns stats dict."""
    stats = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    scanned_sources: set[str] = set()
    scanned_keys: dict[tuple, Card] = {}
    source_suspended: dict[str, bool] = {}

    for source_path, adapter_name, cards, config in scan_results:
        scanned_sources.add(source_path)
        source_suspended[source_path] = bool(config.get("suspended", False))
        for card in cards:
            scanned_keys[(source_path, card.key, adapter_name)] = card

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

    for key_tuple, card in scanned_keys.items():
        source_path, card_key, adapter_name = key_tuple
        chash = content_hash(card.content)

        if key_tuple in existing_map:
            row = existing_map[key_tuple]
            current_status = row["status"]

            if row["content_hash"] == chash:
                stats["unchanged"] += 1
                conn.execute(
                    "UPDATE cards SET display_text=?, source_line=? WHERE id=?",
                    (card.display_text, card.source_line, row["id"]))
                _sync_tags(conn, row["id"], card.tags)
            else:
                old_id = row["id"]
                new_status = current_status if current_status == "inactive" else "active"
                conn.execute(
                    "UPDATE card_state SET status='deleted', updated_at=datetime('now') WHERE card_id=?",
                    (old_id,))
                conn.execute(
                    "UPDATE cards SET card_key = card_key || '__replaced_' || CAST(id AS TEXT) WHERE id=?",
                    (old_id,))
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

    for key_tuple, row in existing_map.items():
        conn.execute(
            "UPDATE card_state SET status='deleted', updated_at=datetime('now') WHERE card_id=?",
            (row["id"],))
        conn.execute("DELETE FROM recommendations WHERE card_id=?", (row["id"],))
        if scheduler:
            try:
                scheduler.on_card_status_changed(row["id"], "deleted")
            except Exception as e:
                print(f"Warning: scheduler on_card_status_changed failed: {e}", file=sys.stderr)
        stats["deleted"] += 1

    _sync_relations(conn, scan_results, scheduler)
    conn.commit()
    return stats


def _insert_card(conn: sqlite3.Connection, source_path: str, card_key: str,
                 adapter_name: str, card: Card, chash: str,
                 status: str = "active") -> int:
    cur = conn.execute("""
        INSERT INTO cards (source_path, card_key, adapter, content, content_hash, display_text, gradable, source_line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (source_path, card_key, adapter_name,
          json.dumps(card.content, sort_keys=True), chash,
          card.display_text, card.gradable, card.source_line))
    card_id = cur.lastrowid
    conn.execute("INSERT INTO card_state (card_id, status) VALUES (?, ?)", (card_id, status))
    _sync_tags(conn, card_id, card.tags)
    return card_id


def _sync_tags(conn: sqlite3.Connection, card_id: int, tags: list[str]):
    existing = {r["tag"] for r in conn.execute(
        "SELECT tag FROM card_tags WHERE card_id=?", (card_id,))}
    new_tags = set(tags)
    for tag in new_tags - existing:
        conn.execute("INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)",
                     (card_id, tag))
    for tag in existing - new_tags:
        conn.execute("DELETE FROM card_tags WHERE card_id=? AND tag=?", (card_id, tag))


def _sync_relations(conn: sqlite3.Connection, scan_results, scheduler):
    for source_path, adapter_name, cards, _config in scan_results:
        for card in cards:
            if not card.relations:
                continue
            row = conn.execute(
                "SELECT id FROM cards c JOIN card_state cs ON c.id=cs.card_id "
                "WHERE c.source_path=? AND c.card_key=? AND c.adapter=? AND cs.status='active'",
                (source_path, card.key, adapter_name)
            ).fetchone()
            if not row:
                continue
            card_id = row["id"]
            for rel in card.relations:
                target_source = rel.target_source or source_path
                target_row = conn.execute(
                    "SELECT id FROM cards c JOIN card_state cs ON c.id=cs.card_id "
                    "WHERE c.source_path=? AND c.card_key=? AND cs.status='active'",
                    (target_source, rel.target_key)
                ).fetchone()
                if target_row:
                    conn.execute("""
                        INSERT OR IGNORE INTO card_relations
                        (upstream_card_id, downstream_card_id, relation_type)
                        VALUES (?, ?, ?)
                    """, (card_id, target_row["id"], rel.relation_type))


def _upsert_recommendation(conn: sqlite3.Connection, rec: Recommendation, scheduler):
    sched_id = scheduler.scheduler_id
    conn.execute("""
        INSERT OR REPLACE INTO recommendations (card_id, scheduler_id, time, precision_seconds)
        VALUES (?, ?, ?, ?)
    """, (rec.card_id, sched_id, rec.time, rec.precision_seconds))
