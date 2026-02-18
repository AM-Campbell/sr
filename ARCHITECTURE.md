# sr — Architecture

Single-file spaced repetition CLI. Cards come from text files. Adapters parse and render them. A scheduler decides when to show them. Review happens in the browser.

## File Layout

```
~/.config/sr/config              # points to SR directory
SR_DIR/
  settings.toml                  # scheduler name, review port
  sr.db                          # core SQLite database
  adapters/                      # adapter modules (Python files)
    basic_qa.py
  schedulers/
    sm2/
      sm2.py                     # scheduler module
      sm2.db                     # scheduler's private database
```

## Config

### `~/.config/sr/config`

```
DIR=/home/user/Dropbox/sr
```

One key: `DIR` — absolute path to the SR directory. Default if missing: `~/.local/share/sr`.

### `settings.toml`

Flat key-value TOML in the SR directory root.

```toml
scheduler = "sm2"
review_port = 8791
```

| Key           | Type | Default | Description              |
|---------------|------|---------|--------------------------|
| `scheduler`   | str  | `"sm2"` | Scheduler module name    |
| `review_port` | int  | `8791`  | Review server port       |

## Source Discovery

`sr scan [PATH ...]` walks the given paths (default: cwd) looking for card sources.

### Markdown files (`.md`)

A `.md` file is a card source if its YAML frontmatter contains `sr_adapter`:

```yaml
---
sr_adapter: basic_qa
tags: [python, memory]
suspended: false
---

(card content here)
```

The full frontmatter dict is passed to the adapter as `config`. Recognized keys:

| Key          | Type       | Description                              |
|--------------|------------|------------------------------------------|
| `sr_adapter` | str        | **Required.** Adapter name.              |
| `tags`       | list[str]  | Tags applied to all cards in this file.  |
| `suspended`  | bool       | Suspend all cards in this file.          |
| *(other)*    | any        | Passed through to adapter as config.     |

Files without `sr_adapter` in frontmatter are silently skipped.

### Directories with `.sr.config`

A directory containing `.sr.config` is treated as a card source. Every file in the directory (except `.sr.config` itself) is parsed by the named adapter. Subdirectories are **not** recursed into.

```toml
adapter = "basic_qa"
```

### Recursive scanning

Directories without `.sr.config` are recursed into. At each level, sr looks for `.md` files with frontmatter and subdirectories with `.sr.config`. Directories starting with `.` are skipped. Permission errors are silently skipped.

## Card Sync

On scan, sr matches source cards to the database by `(source_path, card_key, adapter)`:

| Source state        | DB state             | Action                                                                 |
|---------------------|----------------------|------------------------------------------------------------------------|
| Present, same hash  | active               | No-op. Sync tags.                                                      |
| Present, same hash  | active, now suspended| Status → `inactive`. Remove recommendations.                          |
| Present, same hash  | inactive, now unsuspended | Status → `active`. Scheduler `on_card_created`.                  |
| Present, new hash   | active or inactive   | Old card → `deleted` + key renamed. New card inserted. `is_replaced_by` relation. Scheduler `on_card_replaced`. |
| Present             | not in DB            | New card inserted. Scheduler `on_card_created`. Status set by `suspended` flag.  |
| Missing from source | active or inactive   | Status → `deleted`. Recommendations removed.                          |

Only cards whose `source_path` falls under the scanned paths are considered for deletion. Cards from unscanned paths are untouched.

## Database Schema

SQLite with WAL mode and foreign keys enabled.

### `cards`

Append-only content table. Old versions are kept with `deleted` status.

| Column         | Type    | Description                                    |
|----------------|---------|------------------------------------------------|
| `id`           | INTEGER | Primary key, autoincrement.                    |
| `source_path`  | TEXT    | Absolute path to source file.                  |
| `card_key`     | TEXT    | Adapter-assigned key (unique within source).   |
| `adapter`      | TEXT    | Adapter name.                                  |
| `content`      | JSON    | Card content dict. Opaque to core.             |
| `content_hash` | TEXT    | SHA-256 of `json.dumps(content, sort_keys=True)`. |
| `display_text` | TEXT    | Short preview text.                            |
| `gradable`     | BOOLEAN | Whether the card can be graded.                |
| `created_at`   | TEXT    | ISO timestamp.                                 |

**Constraint:** `UNIQUE(source_path, card_key, adapter)`. When a card is replaced, the old card's key is renamed to `{key}__replaced_{id}` to free the slot.

### `card_state`

Mutable lifecycle state, separate from immutable content.

| Column       | Type    | Description                                          |
|--------------|---------|------------------------------------------------------|
| `card_id`    | INTEGER | Primary key, references `cards(id)`.                 |
| `status`     | TEXT    | `active`, `inactive` (suspended), or `deleted`.      |
| `updated_at` | TEXT    | Last status change timestamp.                        |

### `card_relations`

Typed directed graph between cards.

| Column               | Type    | Description                 |
|----------------------|---------|-----------------------------|
| `upstream_card_id`   | INTEGER | Source card.                |
| `downstream_card_id` | INTEGER | Target card.                |
| `relation_type`      | TEXT    | e.g. `is_replaced_by`.     |

**Primary key:** `(upstream_card_id, downstream_card_id, relation_type)`.

### `card_tags`

| Column    | Type    | Description                    |
|-----------|---------|--------------------------------|
| `card_id` | INTEGER | References `cards(id)`.        |
| `tag`     | TEXT    | Tag string.                    |

**Primary key:** `(card_id, tag)`.

### `review_log`

Append-only. Every review is recorded, including re-reviews after undo.

| Column            | Type    | Description                                           |
|-------------------|---------|-------------------------------------------------------|
| `id`              | INTEGER | Primary key, autoincrement.                           |
| `card_id`         | INTEGER | Card reviewed.                                        |
| `session_id`      | TEXT    | UUID grouping reviews in one session.                 |
| `timestamp`       | TEXT    | UTC timestamp.                                        |
| `grade`           | INTEGER | `0` (wrong) or `1` (correct).                        |
| `time_on_front_ms`| INTEGER | Milliseconds on front before flip.                   |
| `time_on_card_ms` | INTEGER | Milliseconds total on card.                           |
| `feedback`        | TEXT    | `too_hard`, `just_right`, `too_easy`, or NULL.        |
| `response`        | JSON    | Adapter-specific response data, or NULL.              |

### `recommendations`

Scheduler's output. One row per card per scheduler.

| Column              | Type    | Description                               |
|---------------------|---------|-------------------------------------------|
| `card_id`           | INTEGER | Card to review.                           |
| `scheduler_id`      | TEXT    | Scheduler name.                           |
| `time`              | TEXT    | Ideal next review time (UTC).             |
| `precision_seconds` | INTEGER | Tolerance window (seconds).               |

**Primary key:** `(card_id, scheduler_id)`.

## Adapter Interface

An adapter is a Python file in `SR_DIR/adapters/` containing a class named `Adapter`.

```python
class Adapter:
    def parse(self, text: str, path: str, config: dict) -> list[Card]:
        """Parse source file into cards.

        text:   Full file contents (including frontmatter).
        path:   Absolute path to source file.
        config: Parsed frontmatter dict (for .md files) or
                .sr.config dict (for directory sources).
        """

    def render_front(self, card_content: dict) -> str:
        """Render the front of a card. Returns an HTML fragment."""

    def render_back(self, card_content: dict) -> str:
        """Render the back of a card. Returns an HTML fragment."""
```

The adapter owns both parsing and rendering. The `content` dict is opaque to the core — only the adapter interprets it.

### Card dataclass

Returned by `parse()`:

```python
@dataclass
class Card:
    key: str                # Stable ID within this source file.
    content: dict           # Stored as JSON. Only the adapter reads it.
    display_text: str = ""  # Short preview (for sr status, logs).
    gradable: bool = True   # False → no grade buttons, "Next" instead.
    suspended: bool = False # True → card inserted as inactive.
    tags: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
```

### Relation dataclass

```python
@dataclass
class Relation:
    target_key: str                   # card_key of related card.
    relation_type: str                # e.g. "mutually_exclusive".
    target_source: str | None = None  # source_path. None = same source.
```

### Suspension

The adapter controls suspension. Two patterns:

**File-level** (frontmatter): `suspended: true` — adapter reads from config, sets `card.suspended = True` on all cards.

**Per-card** (adapter convention): The `basic_qa` adapter uses `!Q:` prefix to suspend individual cards. Other adapters can use whatever convention makes sense for their format.

An external tool modifies the source file (flipping the flag), then `sr scan` picks up the change.

### Autograde

Adapters can emit JavaScript in their rendered HTML that calls a global hook:

```javascript
window.srAutoGrade(grade, response)
```

- `grade`: `0` or `1`.
- `response`: object or `null` — stored in `review_log.response`.

When called:

1. The card flips automatically (shows back).
2. A result indicator appears: green "Correct" or red "Wrong".
3. Grade buttons are replaced with a "Next" button.
4. Feedback buttons (too_hard / just_right / too_easy) remain available.
5. User reads the back, optionally selects feedback, clicks Next.
6. Grade + feedback + response are submitted together.

For non-gradable cards (`gradable=False`), the core shows a "Next" button after flip. No grade is logged.

### Error handling

If `render_front` or `render_back` raises, the core shows an error card with the card ID and error message. If `parse` raises, the file is skipped with a warning.

## Scheduler Interface

A scheduler is a Python file at `SR_DIR/schedulers/<name>/<name>.py` containing a class named `Scheduler`. The scheduler gets its own directory for private state (its own SQLite database, config, etc.).

```python
class Scheduler:
    scheduler_id = "sm2"  # Used as key in recommendations table.

    def __init__(self, db_dir: str, core_db_path: str):
        """
        db_dir:        Path to scheduler's directory (for private DB).
        core_db_path:  Path to sr.db (read-only access to cards, review_log).
        """

    def on_review(self, card_id: int, event: ReviewEvent) -> list[Recommendation]:
        """Called after a review. Return updated recommendations."""

    def on_card_created(self, card_id: int) -> Recommendation | None:
        """New active card (or reactivated from inactive). Return initial recommendation."""

    def on_card_replaced(self, old_card_id: int, new_card_id: int) -> Recommendation | None:
        """Card content changed. Migrate state from old to new."""

    def on_card_status_changed(self, card_id: int, status: str) -> None:
        """Card became active/inactive/deleted. Clean up or create state."""

    def on_relations_changed(self, card_ids: list[int]) -> list[Recommendation]:
        """Relations changed for these cards. Adjust if needed."""

    def compute_all(self, active_card_ids: list[int]) -> list[Recommendation]:
        """Full recompute of all recommendations."""
```

### ReviewEvent

```python
@dataclass
class ReviewEvent:
    card_id: int
    timestamp: str          # "YYYY-MM-DD HH:MM:SS" UTC
    grade: int              # 0 or 1
    time_on_front_ms: int
    time_on_card_ms: int
    feedback: str | None    # "too_hard", "just_right", "too_easy"
    response: dict | None
```

### Recommendation

```python
@dataclass
class Recommendation:
    card_id: int
    time: str               # "YYYY-MM-DD HH:MM:SS" UTC
    precision_seconds: int  # Tolerance window.
```

Written to the `recommendations` table. The review server reads this table to order cards: due cards first (ordered by `time ASC`), cards with no recommendation last.

### Error handling

If any scheduler method raises, the core logs a warning and continues. Card selection falls back to card ID order for cards without recommendations.

## CLI

```
sr scan [PATH ...]         Scan sources, sync cards to DB.
sr review [PATH ...]       Scan, then start review server.
  --tag TAG                Filter review to cards with this tag.
sr status                  Show card counts and due counts.
```

Paths default to cwd if omitted.

## Review Server

`sr review` starts an HTTP server on localhost. A session token (UUID) is generated on first request and required on all API calls via `X-Session-Token` header.

### Endpoints

| Method | Path          | Description                                    |
|--------|---------------|------------------------------------------------|
| GET    | `/`           | Review UI (single HTML page).                  |
| GET    | `/api/session`| Get session token.                             |
| GET    | `/api/next`   | Next card: `{id, gradable, front_html, session_stats}` or `{done: true}`. |
| POST   | `/api/flip`   | Flip card: `{back_html}`.                      |
| POST   | `/api/grade`  | Submit grade: `{grade, feedback?, response?}` → `{ok}`. |
| POST   | `/api/skip`   | Skip non-gradable card (no grade logged) → `{ok}`. |
| POST   | `/api/undo`   | Re-present previous card → `{ok, front_html, back_html}`. |
| GET    | `/api/status` | Session stats: `{reviewed, remaining}`.        |

### Timing

Server-side. Timer starts on `/api/next` response. `time_on_front_ms` is measured to `/api/flip`. `time_on_card_ms` is measured to `/api/grade`.

### Card selection

Reads from `recommendations` table, joins `card_state` (active only, gradable only). Orders by `time ASC`, with NULL recommendations last. Cards already reviewed in the current session are excluded.

### Keyboard shortcuts

| Key     | Action                              |
|---------|-------------------------------------|
| Space   | Flip card.                          |
| 1       | Grade wrong.                        |
| 2       | Grade correct.                      |
| Enter   | Next (autograde / non-gradable).    |
| z / u   | Undo.                               |

### Undo

Re-presents the previous card already flipped (front + back visible). The original review stays in `review_log` (append-only). The re-review is logged as a new event. The scheduler sees it as a normal `on_review`.

## SM-2 Scheduler

Ships at `SR_DIR/schedulers/sm2/sm2.py`. Implements the SuperMemo 2 algorithm.

### Private state (`sm2.db`)

| Column          | Type    | Description                  |
|-----------------|---------|------------------------------|
| `card_id`       | INTEGER | Primary key.                 |
| `ease_factor`   | REAL    | EF (default 2.5, min 1.3).  |
| `interval_days` | REAL    | Current interval in days.    |
| `repetitions`   | INTEGER | Consecutive correct count.   |
| `last_review`   | TEXT    | Timestamp of last review.    |
| `next_review`   | TEXT    | Computed next review time.   |

### Algorithm

**Correct (grade=1):**
- Rep 1 → interval = 1 day.
- Rep 2 → interval = 6 days.
- Rep 3+ → interval = previous interval * EF.
- Feedback: `too_easy` → EF + 0.15 (max 3.0). `too_hard` → EF - 0.15 (min 1.3).

**Incorrect (grade=0):**
- Reset reps to 0.
- Interval → ~15 minutes.
- EF - 0.2 (min 1.3).

**On card replaced:** Migrates EF, reduces interval by 30%, decrements reps by 1.

**On card created:** Schedules for immediate review.
