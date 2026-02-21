"""Card flag helpers."""

import sqlite3


def add_flag(conn: sqlite3.Connection, card_id: int, flag: str, note: str | None = None):
    conn.execute(
        "INSERT OR REPLACE INTO card_flags (card_id, flag, note) VALUES (?, ?, ?)",
        (card_id, flag, note))
    conn.commit()


def remove_flag(conn: sqlite3.Connection, card_id: int, flag: str):
    conn.execute("DELETE FROM card_flags WHERE card_id=? AND flag=?", (card_id, flag))
    conn.commit()


def get_flags(conn: sqlite3.Connection, card_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT flag, note FROM card_flags WHERE card_id=?", (card_id,))]
