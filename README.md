# sr — Spaced Repetition

A Unix-philosophy spaced repetition CLI. Cards live in text files. The filesystem is the organizational structure. Review happens in the browser.

## Quick Start

### 1. Install

sr is a single Python file with no dependencies beyond Python 3.12+ and sqlite3.

```sh
# Clone or copy sr.py somewhere
git clone <repo> ~/local/sr

# Add to PATH (pick one)
ln -s ~/local/sr/sr.py ~/.local/bin/sr
# or
echo '#!/bin/sh\nexec python ~/local/sr/sr.py "$@"' > ~/.local/bin/sr && chmod +x ~/.local/bin/sr
```

### 2. Set up your SR directory

The SR directory holds your adapters, schedulers, database, and settings.

```sh
mkdir -p ~/sr/adapters ~/sr/schedulers/sm2

# Point sr at it
mkdir -p ~/.config/sr
echo "DIR=$HOME/sr" > ~/.config/sr/config

# Copy the bundled adapter and scheduler
cp ~/local/sr/example_sr_dir/adapters/basic_qa.py ~/sr/adapters/
cp ~/local/sr/example_sr_dir/schedulers/sm2/sm2.py ~/sr/schedulers/sm2/
cp ~/local/sr/example_sr_dir/settings.toml ~/sr/
```

### 3. Create cards

Create a markdown file anywhere on your filesystem:

```markdown
---
sr_adapter: basic_qa
tags: [python, basics]
---

Q: What is a list comprehension?
A: A concise way to create lists: `[expr for item in iterable if condition]`

Q: What is the GIL?
A: The Global Interpreter Lock — a mutex in CPython that allows only one
thread to execute Python bytecode at a time.
```

The `sr_adapter: basic_qa` line in the frontmatter tells sr which adapter to use. Files without it are ignored.

### 4. Scan and review

```sh
# Scan a file or directory
sr scan ~/notes/python.md
sr scan ~/notes/

# Start a review session (scans first, then opens browser)
sr review ~/notes/

# Check your stats
sr status
```

The review server opens at `http://127.0.0.1:8791`. Press Space to flip, 1 for wrong, 2 for correct. Ctrl+C to end the session.

## Usage

```
sr scan [PATH ...]            Scan files, sync cards to database.
sr review [PATH ...]          Scan, then start review in browser.
  --tag TAG                   Filter by tag.
  --flag FLAG                 Filter by flag (e.g. edit_later).
sr status                     Show card counts and due cards.
sr browse [--port PORT]       Browse and manage cards in browser.
```

Paths can be files or directories. Defaults to current directory if omitted.

## Card Sources

sr finds cards in two ways:

**Markdown files** — Any `.md` file with `sr_adapter: <name>` in its YAML frontmatter. The adapter parses the file contents into cards.

**Directories with `.sr.config`** — A directory containing a `.sr.config` file. All files in that directory are parsed by the named adapter.

```toml
# .sr.config
adapter = "basic_qa"
```

## The basic_qa Adapter

The bundled adapter parses Q&A pairs from markdown:

```markdown
---
sr_adapter: basic_qa
tags: [geography]
---

Q: What is the capital of France?
A: Paris

Q: What is the capital of Japan?
A: Tokyo
```

Multi-line answers work — continuation lines are included until the next blank line.

## Adapters

An adapter is a Python file in `SR_DIR/adapters/` that knows how to parse a file format into cards and render them as HTML for review.

```python
class Adapter:
    def parse(self, text: str, path: str, config: dict) -> list[Card]:
        """Parse file text into cards. config is the frontmatter dict."""

    def render_front(self, card_content: dict) -> str:
        """Return HTML for the front of the card."""

    def render_back(self, card_content: dict) -> str:
        """Return HTML for the back of the card."""
```

Each card returned by `parse()` has:

| Field          | Type       | Description                                    |
|----------------|------------|------------------------------------------------|
| `key`          | str        | Stable ID within this source file.             |
| `content`      | dict       | Arbitrary data. Only the adapter interprets it.|
| `display_text` | str        | Short preview for logs/status.                 |
| `gradable`     | bool       | False → "Next" button instead of grading.      |
| `tags`         | list[str]  | Tags for filtering.                            |
| `relations`    | list[Relation] | Relations to other cards.                  |

### Autograde

Adapters can include JavaScript in rendered HTML that auto-grades the card:

```python
def render_front(self, content):
    return '''
    <div>What is the capital of France?</div>
    <input id="ans" type="text">
    <button onclick="
        var a = document.getElementById('ans').value.toLowerCase();
        window.srAutoGrade(a === 'paris' ? 1 : 0, {given: a});
    ">Submit</button>
    '''
```

`window.srAutoGrade(grade, response)` flips the card, shows a correct/wrong indicator, and lets the user read the back before clicking Next.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full adapter and scheduler interface reference.

## Schedulers

The default SM-2 scheduler ships in `SR_DIR/schedulers/sm2/`. It implements the SuperMemo 2 algorithm with feedback-adjusted ease factors.

Schedulers are pluggable — set `scheduler = "myscheduler"` in `settings.toml` and place your module at `SR_DIR/schedulers/myscheduler/myscheduler.py`.

## Settings

### `~/.config/sr/config`

```
DIR=/home/user/sr
```

Points sr at your SR directory. Default: `~/.local/share/sr`.

### `SR_DIR/settings.toml`

```toml
scheduler = "sm2"
review_port = 8791
```

## Review Keyboard Shortcuts

| Key     | Action                        |
|---------|-------------------------------|
| Space   | Flip card                     |
| 1       | Wrong                         |
| 2       | Correct                       |
| Enter   | Next (autograde)              |
| f       | Toggle flag (`edit_later`)    |
| e       | Open source file in editor    |
| s       | Suspend card and advance      |
| z / u   | Undo                          |

## Flagging Cards

During review, press `f` to flag a card for later editing. Flagged cards can be reviewed separately with `sr review --flag edit_later`. Flags are managed in bulk via `sr browse`.

## Editing Source Files

Press `e` during review to open the card's source file in your editor at the correct line. Configure the editor command in `settings.toml`:

```toml
edit_command = "kitty -e vim +{line} {file}"
```

If not configured, sr auto-detects a terminal emulator and uses `$EDITOR`.

## Suspending Cards

Press `s` during review to suspend the current card (`active` → `inactive`). The card is removed from the review queue. Reactivate cards via `sr browse`.

## Browsing Cards

`sr browse` opens a web UI for managing all cards. Filter by status, tag, or flag. Click a card to view details, toggle status, or manage flags.
