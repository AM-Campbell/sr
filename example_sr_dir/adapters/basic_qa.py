"""basic_qa adapter — parses Q&A pairs from markdown files.

Format: Lines starting with `Q:` begin a question, `A:` begin an answer.
Multi-line content is supported (continuation lines without Q:/A: prefix).
Cards are separated by blank lines between Q/A pairs.

Suspend/unsuspend:
  - Frontmatter `suspended: true` suspends ALL cards in the file.
  - Per-card: prefix the Q line with `!` to suspend that card: `!Q: ...`
  - An external tool can flip these flags; next `sr scan` picks it up.

Example:
    ---
    sr_adapter: basic_qa
    tags: [python, basics]
    ---

    Q: What is a list comprehension?
    A: A concise way to create lists: `[expr for item in iterable]`

    !Q: What does `len()` return?
    A: The number of items in a container.
"""

import dataclasses
import re
import html


@dataclasses.dataclass
class Relation:
    target_key: str
    relation_type: str
    target_source: str | None = None


@dataclasses.dataclass
class Card:
    key: str
    content: dict
    display_text: str = ""
    gradable: bool = True
    suspended: bool = False
    tags: list[str] = dataclasses.field(default_factory=list)
    relations: list[Relation] = dataclasses.field(default_factory=list)


class Adapter:
    def parse(self, text: str, path: str, config: dict) -> list[Card]:
        """Parse Q&A pairs from markdown text."""
        # Strip frontmatter
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                body = text[end + 4:]

        cards = []
        tags = config.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        # File-level suspended flag from frontmatter
        file_suspended = config.get("suspended", False)

        # Parse Q/A pairs
        current_q = None
        current_a = None
        current_suspended = False
        card_index = 0

        for line in body.splitlines() + [""]:
            stripped = line.strip()

            # Check for Q: or !Q: (suspended)
            is_q = False
            is_suspended_q = False
            for prefix in ("!Q:", "!q:"):
                if stripped.startswith(prefix):
                    is_q = True
                    is_suspended_q = True
                    stripped = stripped[len(prefix):].strip()
                    break
            if not is_q:
                for prefix in ("Q:", "q:"):
                    if stripped.startswith(prefix):
                        is_q = True
                        stripped = stripped[len(prefix):].strip()
                        break

            if is_q:
                # Save previous pair if exists
                if current_q is not None and current_a is not None:
                    card_index += 1
                    cards.append(self._make_card(current_q, current_a, card_index,
                                                 tags, file_suspended or current_suspended))
                current_q = stripped
                current_a = None
                current_suspended = is_suspended_q
            elif stripped.startswith("A:") or stripped.startswith("a:"):
                current_a = stripped[2:].strip()
            elif stripped == "":
                # Blank line — finalize pair
                if current_q is not None and current_a is not None:
                    card_index += 1
                    cards.append(self._make_card(current_q, current_a, card_index,
                                                 tags, file_suspended or current_suspended))
                    current_q = None
                    current_a = None
                    current_suspended = False
            else:
                # Continuation line
                if current_a is not None:
                    current_a += "\n" + stripped
                elif current_q is not None:
                    current_q += "\n" + stripped

        # Handle last pair without trailing blank line
        if current_q is not None and current_a is not None:
            card_index += 1
            cards.append(self._make_card(current_q, current_a, card_index,
                                         tags, file_suspended or current_suspended))

        return cards

    def _make_card(self, question: str, answer: str, index: int,
                   tags: list[str], suspended: bool = False) -> Card:
        content = {"question": question, "answer": answer}
        return Card(
            key=f"qa_{index}",
            content=content,
            display_text=question[:80],
            gradable=True,
            suspended=suspended,
            tags=list(tags),
        )

    def render_front(self, card_content: dict) -> str:
        q = card_content.get("question", "")
        return f"<div>{self._md_to_html(q)}</div>"

    def render_back(self, card_content: dict) -> str:
        a = card_content.get("answer", "")
        return f"<div>{self._md_to_html(a)}</div>"

    def _md_to_html(self, text: str) -> str:
        """Minimal markdown to HTML: backticks → code, newlines → br."""
        text = html.escape(text)
        # Code blocks (```)
        text = re.sub(r'```(\w*)\n(.*?)```', self._code_block, text, flags=re.DOTALL)
        # Inline code
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        # Bold
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        # Italic
        text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
        # Newlines
        text = text.replace("\n", "<br>")
        return text

    @staticmethod
    def _code_block(match):
        lang = match.group(1)
        code = match.group(2).strip()
        return f'<pre><code>{code}</code></pre>'
