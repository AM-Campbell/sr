"""Deck tree: hierarchical view of card collections by source path."""

import os
import pathlib
import sqlite3


def build_deck_tree(conn: sqlite3.Connection) -> list[dict]:
    """Build a hierarchical deck tree from source paths of gradable cards."""
    rows = conn.execute("""
        SELECT c.source_path, cs.status,
               CASE WHEN r.time IS NOT NULL AND r.time <= datetime('now') THEN 1 ELSE 0 END as is_due
        FROM cards c
        JOIN card_state cs ON c.id = cs.card_id
        LEFT JOIN recommendations r ON c.id = r.card_id
        WHERE c.gradable = 1 AND cs.status IN ('active', 'inactive')
    """).fetchall()

    if not rows:
        return []

    path_stats: dict[str, dict] = {}
    for r in rows:
        sp = r["source_path"]
        if sp not in path_stats:
            path_stats[sp] = {"total": 0, "active": 0, "due": 0}
        path_stats[sp]["total"] += 1
        if r["status"] == "active":
            path_stats[sp]["active"] += 1
            if r["is_due"]:
                path_stats[sp]["due"] += 1

    all_paths = list(path_stats.keys())

    if len(all_paths) == 1:
        common = str(pathlib.Path(all_paths[0]).parent)
    else:
        common = os.path.commonpath(all_paths)
        if common in all_paths:
            common = str(pathlib.Path(common).parent)

    tree: dict = {}
    for sp in all_paths:
        rel = os.path.relpath(sp, common)
        parts = pathlib.PurePosixPath(rel).parts if '/' in rel else rel.split(os.sep)
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
        node["__stats__"] = path_stats[sp]
        node["__full_path__"] = sp

    def collapse(d):
        keys = [k for k in d if not k.startswith("__")]
        if len(keys) == 1 and "__stats__" not in d:
            child_key = keys[0]
            child = d[child_key]
            child_keys = [k for k in child if not k.startswith("__")]
            if child_keys or "__stats__" in child:
                del d[child_key]
                new_key = child_key
                inner = child
                while True:
                    inner_keys = [k for k in inner if not k.startswith("__")]
                    if len(inner_keys) == 1 and "__stats__" not in inner:
                        next_key = inner_keys[0]
                        new_key = new_key + "/" + next_key
                        inner = inner[next_key]
                    else:
                        break
                d[new_key] = inner
        for k in [k for k in d if not k.startswith("__")]:
            collapse(d[k])

    collapse(tree)

    def to_list(d, prefix=""):
        result = []
        children_keys = sorted([k for k in d if not k.startswith("__")])
        for k in children_keys:
            child = d[k]
            full_path = child.get("__full_path__", "")
            is_leaf = "__stats__" in child and not any(
                not ck.startswith("__") for ck in child)
            if is_leaf:
                stats = child["__stats__"]
            else:
                stats = _aggregate_stats(child)
            node_path = os.path.join(prefix, k) if prefix else k
            children = to_list(child, node_path) if not is_leaf else []
            result.append({
                "name": k,
                "path": full_path if is_leaf else os.path.join(common, node_path),
                "children": children,
                "total": stats["total"],
                "active": stats["active"],
                "due": stats["due"],
                "is_leaf": is_leaf,
            })
        return result

    return to_list(tree)


def _aggregate_stats(d: dict) -> dict:
    """Recursively aggregate total/active/due from a tree dict."""
    total = 0
    active = 0
    due = 0
    if "__stats__" in d:
        total += d["__stats__"]["total"]
        active += d["__stats__"]["active"]
        due += d["__stats__"]["due"]
    for k in d:
        if not k.startswith("__"):
            sub = _aggregate_stats(d[k])
            total += sub["total"]
            active += sub["active"]
            due += sub["due"]
    return {"total": total, "active": active, "due": due}
