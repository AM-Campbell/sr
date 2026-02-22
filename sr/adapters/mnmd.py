"""mnmd (Mnemonic Markdown) adapter — cloze deletion cards from markdown.

Syntax:
    {{answer}}              — basic cloze
    {{answer::hint}}        — with hint
    {{1::answer}}           — grouped (all blanked together)
    {{1::answer::hint}}     — grouped with hint
    {{1.1::answer}}         — sequence (progressive reveal)
    {{1.1::answer::hint}}   — sequence with hint
    {{answer}}[-1,2]        — scope modifier (1 para before, 2 after)

> ? blocks provide explicit context boundaries:
    > ?
    > Line with {{cloze}}.

:: disambiguation (2 segments): first is numeric/dotted-numeric → id::answer,
else → answer::hint.

Frontmatter: sr_adapter: mnmd required. Tags from frontmatter propagated to cards.
"""

import html
import re
from dataclasses import dataclass

from sr.models import Card, Relation

_CLOZE_RE = re.compile(r'\{\{([^}]+)\}\}')
_CLOZE_WITH_SCOPE_RE = re.compile(r'\{\{([^}]+)\}\}(?:\[(-?\d+)?(?:,(-?\d+))?\])?')
_NUMERIC_RE = re.compile(r'^\d+$')
_DOTTED_RE = re.compile(r'^\d+\.\d+$')


@dataclass
class Cloze:
    """A parsed cloze deletion from a block of text."""
    id: str | None        # None=ungrouped, "1"=grouped, "1.1"=sequence
    answer: str
    hint: str | None
    scope_before: int | None
    scope_after: int | None
    match_start: int      # character offset in block text
    match_end: int        # character offset in block text


def _parse_cloze_inner(inner: str) -> tuple[str | None, str, str | None]:
    """Parse the inside of {{...}} into (id_or_none, answer, hint_or_none).

    :: disambiguation (2 segments):
      - first is numeric/dotted-numeric → id::answer
      - else → answer::hint

    3 segments: id::answer::hint
    1 segment: answer only
    """
    parts = inner.split("::")
    if len(parts) == 1:
        return None, parts[0].strip(), None
    elif len(parts) == 2:
        first = parts[0].strip()
        second = parts[1].strip()
        if _NUMERIC_RE.match(first) or _DOTTED_RE.match(first):
            return first, second, None
        else:
            return None, first, second
    else:
        return parts[0].strip(), parts[1].strip(), parts[2].strip()


def _strip_frontmatter(text: str) -> tuple[str, int]:
    """Strip YAML frontmatter. Returns (body, body_start_line)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
            body_start_line = text[:end + 4].count("\n") + 1
            return body, body_start_line
    return text, 1


def _segment_blocks(body: str, body_start_line: int):
    """Segment body into blocks.

    Returns list of (block_text, block_start_line, is_context_block).
    Paragraphs are split by blank lines. `> ?` blocks are context blocks.
    """
    lines = body.split("\n")
    blocks = []
    current_lines = []
    current_start = body_start_line
    in_context_block = False

    def _flush():
        nonlocal current_lines, in_context_block
        if current_lines:
            text = "\n".join(current_lines)
            if text.strip():
                blocks.append((text, current_start, in_context_block))
            current_lines = []
        in_context_block = False

    for i, line in enumerate(lines):
        abs_line = body_start_line + i
        stripped = line.strip()

        if stripped == "> ?" or stripped == ">?":
            _flush()
            in_context_block = True
            current_start = abs_line
            continue

        if in_context_block:
            if stripped.startswith("> ") or stripped == ">":
                if stripped == ">":
                    current_lines.append("")
                else:
                    current_lines.append(re.sub(r'^>\s?', '', line))
                continue
            else:
                _flush()

        if stripped == "":
            if current_lines:
                _flush()
        else:
            if not current_lines:
                current_start = abs_line
            current_lines.append(line)

    _flush()
    return blocks


def _find_clozes(block_text: str) -> list[Cloze]:
    """Find all clozes in a block."""
    clozes = []
    for m in _CLOZE_WITH_SCOPE_RE.finditer(block_text):
        cloze_id, answer, hint = _parse_cloze_inner(m.group(1))

        scope_before = None
        scope_after = None
        if m.group(2) is not None or m.group(3) is not None:
            if m.group(2) is not None:
                val = int(m.group(2))
                if val < 0:
                    scope_before = abs(val)
                else:
                    scope_after = val
            if m.group(3) is not None:
                scope_after = int(m.group(3))

        clozes.append(Cloze(
            id=cloze_id, answer=answer, hint=hint,
            scope_before=scope_before, scope_after=scope_after,
            match_start=m.start(), match_end=m.end(),
        ))
    return clozes


def _build_text(block_text: str, clozes: list[Cloze], active: set[int]) -> str:
    """Build card text from a block, controlling which clozes are active vs plain.

    - active: cloze indices that keep {{answer}} or {{answer::hint}} markers
      (will be blanked on front, highlighted on back by the renderer).
    - All other clozes become plain text (answer revealed, no markers).

    Scope modifiers are always stripped from the output.
    """
    result = []
    last_end = 0
    for i, cloze in enumerate(clozes):
        result.append(block_text[last_end:cloze.match_start])
        if i in active:
            if cloze.hint:
                result.append("{{" + cloze.answer + "::" + cloze.hint + "}}")
            else:
                result.append("{{" + cloze.answer + "}}")
        else:
            result.append(cloze.answer)
        last_end = cloze.match_end
    result.append(block_text[last_end:])
    return "".join(result)


def _apply_scope(card_text: str, blocks, block_idx: int,
                 scope_before: int | None, scope_after: int | None) -> str:
    """Prepend/append neighboring block text based on scope modifiers."""
    if not scope_before and not scope_after:
        return card_text
    before_parts = []
    after_parts = []
    if scope_before:
        for i in range(max(0, block_idx - scope_before), block_idx):
            before_parts.append(blocks[i][0])
    if scope_after:
        for i in range(block_idx + 1, min(len(blocks), block_idx + 1 + scope_after)):
            after_parts.append(blocks[i][0])
    parts = before_parts + [card_text] + after_parts
    return "\n\n".join(parts)


class Adapter:
    def parse(self, text: str, path: str, config: dict) -> list[Card]:
        """Parse mnmd cloze cards from markdown text."""
        body, body_start_line = _strip_frontmatter(text)
        tags = config.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        blocks = _segment_blocks(body, body_start_line)
        all_cards = []

        for block_idx, (block_text, block_start_line, _is_context) in enumerate(blocks):
            clozes = _find_clozes(block_text)
            if not clozes:
                continue

            # Classify cloze indices by type
            ungrouped = []          # [index, ...]
            groups: dict[str, list[int]] = {}   # group_id -> [index, ...]
            sequences: dict[str, list[tuple[str, int]]] = {}  # base -> [(step_id, index), ...]

            for i, cloze in enumerate(clozes):
                cid = cloze.id
                if cid is None:
                    ungrouped.append(i)
                elif _DOTTED_RE.match(cid):
                    base = cid.split(".")[0]
                    sequences.setdefault(base, []).append((cid, i))
                elif _NUMERIC_RE.match(cid):
                    groups.setdefault(cid, []).append(i)
                else:
                    ungrouped.append(i)

            for base in sequences:
                sequences[base].sort(key=lambda x: list(map(int, x[0].split("."))))

            # Build cards. Track (key -> Card) and card type for relation logic.
            card_by_key: dict[str, Card] = {}
            non_seq_keys: list[str] = []
            seq_keys_by_base: dict[str, list[str]] = {}

            # --- Ungrouped: 1 card per cloze ---
            for idx in ungrouped:
                cloze = clozes[idx]
                card_text = _build_text(block_text, clozes, active={idx})
                card_text = _apply_scope(card_text, blocks, block_idx,
                                         cloze.scope_before, cloze.scope_after)
                key = f"cloze_L{block_start_line}_C{idx}"
                card = Card(
                    key=key, content={"text": card_text},
                    source_line=block_start_line,
                    display_text=card_text[:200],
                    tags=list(tags),
                )
                card_by_key[key] = card
                non_seq_keys.append(key)

            # --- Grouped: 1 card per group ---
            for gid, indices in groups.items():
                card_text = _build_text(block_text, clozes, active=set(indices))
                first = clozes[indices[0]]
                card_text = _apply_scope(card_text, blocks, block_idx,
                                         first.scope_before, first.scope_after)
                answers = [clozes[i].answer for i in indices]
                key = f"group_{gid}"
                card = Card(
                    key=key, content={"text": card_text},
                    source_line=block_start_line,
                    display_text=card_text[:200],
                    tags=list(tags),
                )
                card_by_key[key] = card
                non_seq_keys.append(key)

            # --- Sequence: N cards for N steps ---
            # Step k: reveal steps 0..k-1 as plain text, blank step k,
            # hide steps k+1..N as {{...}} blanks. Progressive reveal.
            for base, steps in sequences.items():
                step_keys = []
                for step_k in range(len(steps)):
                    # Active = current step + all future steps (all shown as blanks)
                    active = {steps[j][1] for j in range(step_k, len(steps))}
                    card_text = _build_text(block_text, clozes, active=active)

                    step_id = steps[step_k][0]
                    cloze_idx = steps[step_k][1]
                    key = f"seq_{base}_{step_id}"
                    card = Card(
                        key=key, content={"text": card_text},
                        source_line=block_start_line,
                        display_text=card_text[:200],
                        tags=list(tags),
                    )
                    card_by_key[key] = card
                    step_keys.append(key)
                seq_keys_by_base[base] = step_keys

            # --- Relations ---
            # mutually_exclusive between non-sequence cards from same block
            for i, key_a in enumerate(non_seq_keys):
                for key_b in non_seq_keys[i + 1:]:
                    card_by_key[key_a].relations.append(Relation(
                        target_key=key_b,
                        relation_type="mutually_exclusive",
                    ))

            # is_followed_by_on_correct between consecutive sequence steps
            for step_keys in seq_keys_by_base.values():
                for i in range(len(step_keys) - 1):
                    card_by_key[step_keys[i]].relations.append(Relation(
                        target_key=step_keys[i + 1],
                        relation_type="is_followed_by_on_correct",
                    ))

            all_cards.extend(card_by_key.values())

        return all_cards

    def render_front(self, card_content: dict) -> str:
        """Render front of card: clozes become blanks.

        Order: markdown → HTML first, then cloze substitution. This ensures
        all text is properly escaped before we inject our own HTML elements.
        """
        text = card_content.get("text", "")
        text = _md_to_html(text)

        def replace_front(m):
            inner = m.group(1)
            _cid, _answer, hint = _parse_cloze_inner(inner)
            if hint:
                # hint is already HTML-escaped by _md_to_html
                return f'<span class="cloze-blank">[{hint}…]</span>'
            return '<span class="cloze-blank">[…]</span>'

        text = _CLOZE_RE.sub(replace_front, text)
        return f"<div>{text}</div>"

    def render_back(self, card_content: dict) -> str:
        """Render back of card: clozes become highlighted answers.

        Order: markdown → HTML first, then cloze substitution. This ensures
        all text is properly escaped before we inject our own HTML elements.
        """
        text = card_content.get("text", "")
        text = _md_to_html(text)

        def replace_back(m):
            inner = m.group(1)
            _cid, answer, _hint = _parse_cloze_inner(inner)
            return f'<mark>{answer}</mark>'

        text = _CLOZE_RE.sub(replace_back, text)
        return f"<div>{text}</div>"


def _md_to_html(text: str) -> str:
    """Minimal markdown to HTML: escape, backticks → code, bold, italic, newlines → br.

    {{ and }} are not affected by html.escape, so cloze markers survive intact.
    """
    text = html.escape(text)
    # Code blocks
    text = re.sub(r'```(\w*)\n(.*?)```', _code_block, text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Double newlines → paragraph break, single newlines → space (standard markdown)
    text = re.sub(r'\n{2,}', '<br><br>', text)
    text = text.replace("\n", " ")
    return text


def _code_block(match):
    code = match.group(2).strip()
    return f'<pre><code>{code}</code></pre>'
