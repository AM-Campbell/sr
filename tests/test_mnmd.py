"""Tests for the mnmd (Mnemonic Markdown) adapter."""

import pytest

from sr.adapters.mnmd import (
    Adapter, Cloze, _build_text, _find_clozes, _parse_cloze_inner,
    _segment_blocks, _strip_frontmatter,
)


@pytest.fixture
def adapter():
    return Adapter()


# ---------------------------------------------------------------------------
# _parse_cloze_inner — :: disambiguation
# ---------------------------------------------------------------------------

class TestParseClozeInner:
    def test_basic_answer(self):
        assert _parse_cloze_inner("hello") == (None, "hello", None)

    def test_answer_hint(self):
        assert _parse_cloze_inner("answer::hint") == (None, "answer", "hint")

    def test_numeric_id_answer(self):
        assert _parse_cloze_inner("1::answer") == ("1", "answer", None)

    def test_numeric_id_answer_hint(self):
        assert _parse_cloze_inner("1::answer::hint") == ("1", "answer", "hint")

    def test_dotted_id_answer(self):
        assert _parse_cloze_inner("1.1::answer") == ("1.1", "answer", None)

    def test_dotted_id_answer_hint(self):
        assert _parse_cloze_inner("1.1::answer::hint") == ("1.1", "answer", "hint")

    def test_multidigit_numeric_id(self):
        assert _parse_cloze_inner("42::answer") == ("42", "answer", None)

    def test_multidigit_dotted_id(self):
        assert _parse_cloze_inner("10.20::answer") == ("10.20", "answer", None)

    def test_text_first_segment_is_hint(self):
        """Non-numeric first segment → answer::hint."""
        assert _parse_cloze_inner("photosynthesis::a process") == (None, "photosynthesis", "a process")

    def test_whitespace_stripped(self):
        assert _parse_cloze_inner(" answer :: hint ") == (None, "answer", "hint")
        assert _parse_cloze_inner(" 1 :: answer ") == ("1", "answer", None)

    def test_four_segments_ignores_extra(self):
        """Only first three :: segments matter."""
        cid, ans, hint = _parse_cloze_inner("1::ans::hint::extra")
        assert cid == "1"
        assert ans == "ans"
        assert hint == "hint"


# ---------------------------------------------------------------------------
# _strip_frontmatter
# ---------------------------------------------------------------------------

class TestStripFrontmatter:
    def test_with_frontmatter(self):
        text = "---\nsr_adapter: mnmd\ntags: [bio]\n---\nBody here."
        body, start_line = _strip_frontmatter(text)
        assert body.strip() == "Body here."
        # text[:end+4] = "---\nsr_adapter: mnmd\ntags: [bio]\n---" has 3 newlines
        # body_start_line = 3 + 1 = 4 (the \n after closing --- is the first body char)
        assert start_line == 4

    def test_without_frontmatter(self):
        body, start_line = _strip_frontmatter("Just a body.")
        assert body == "Just a body."
        assert start_line == 1

    def test_unclosed_frontmatter(self):
        """Unclosed --- is not treated as frontmatter."""
        text = "---\nno closing marker\nstill going"
        body, start_line = _strip_frontmatter(text)
        assert body == text
        assert start_line == 1

    def test_empty_body_after_frontmatter(self):
        text = "---\nsr_adapter: mnmd\n---\n"
        body, start_line = _strip_frontmatter(text)
        assert body.strip() == ""


# ---------------------------------------------------------------------------
# _segment_blocks
# ---------------------------------------------------------------------------

class TestSegmentBlocks:
    def test_single_paragraph(self):
        blocks = _segment_blocks("Hello world.", 1)
        assert len(blocks) == 1
        assert blocks[0] == ("Hello world.", 1, False)

    def test_two_paragraphs(self):
        blocks = _segment_blocks("Para one.\n\nPara two.", 1)
        assert len(blocks) == 2
        assert blocks[0][0] == "Para one."
        assert blocks[1][0] == "Para two."
        assert blocks[0][2] is False
        assert blocks[1][2] is False

    def test_three_paragraphs(self):
        blocks = _segment_blocks("A.\n\nB.\n\nC.", 1)
        assert len(blocks) == 3

    def test_context_block(self):
        blocks = _segment_blocks("> ?\n> Line one.\n> Line two.", 1)
        assert len(blocks) == 1
        assert blocks[0][2] is True  # is_context_block
        assert blocks[0][0] == "Line one.\nLine two."

    def test_context_block_strips_prefix(self):
        """The > prefix is stripped from content lines."""
        blocks = _segment_blocks("> ?\n> Content here.", 1)
        assert ">" not in blocks[0][0]
        assert blocks[0][0] == "Content here."

    def test_mixed_para_and_context(self):
        body = "Normal para.\n\n> ?\n> Context line.\n\nAnother para."
        blocks = _segment_blocks(body, 1)
        assert len(blocks) == 3
        assert blocks[0] == ("Normal para.", 1, False)
        assert blocks[1][2] is True
        assert blocks[1][0] == "Context line."
        assert blocks[2][0] == "Another para."
        assert blocks[2][2] is False

    def test_consecutive_context_blocks(self):
        """Two > ? blocks with blank line between them."""
        body = "> ?\n> First.\n\n> ?\n> Second."
        blocks = _segment_blocks(body, 1)
        assert len(blocks) == 2
        assert blocks[0][2] is True
        assert blocks[0][0] == "First."
        assert blocks[1][2] is True
        assert blocks[1][0] == "Second."

    def test_multiline_paragraph(self):
        body = "Line one\nline two\nline three."
        blocks = _segment_blocks(body, 1)
        assert len(blocks) == 1
        assert blocks[0][0] == "Line one\nline two\nline three."

    def test_line_numbers(self):
        body = "Para one.\n\nPara two."
        blocks = _segment_blocks(body, 5)  # body starts at line 5
        assert blocks[0][1] == 5   # para one starts at line 5
        assert blocks[1][1] == 7   # blank at 6, para two at 7

    def test_empty_body(self):
        blocks = _segment_blocks("", 1)
        assert len(blocks) == 0

    def test_blank_lines_only(self):
        blocks = _segment_blocks("\n\n\n", 1)
        assert len(blocks) == 0

    def test_context_block_empty_line(self):
        """A bare > inside a context block becomes an empty line."""
        blocks = _segment_blocks("> ?\n> Before.\n>\n> After.", 1)
        assert len(blocks) == 1
        assert blocks[0][0] == "Before.\n\nAfter."


# ---------------------------------------------------------------------------
# _find_clozes
# ---------------------------------------------------------------------------

class TestFindClozes:
    def test_basic(self):
        clozes = _find_clozes("The {{quick}} brown fox.")
        assert len(clozes) == 1
        assert clozes[0].answer == "quick"
        assert clozes[0].id is None
        assert clozes[0].hint is None

    def test_with_hint(self):
        clozes = _find_clozes("The {{answer::hint}}.")
        assert clozes[0].answer == "answer"
        assert clozes[0].hint == "hint"

    def test_with_id(self):
        clozes = _find_clozes("{{1::answer}}")
        assert clozes[0].id == "1"
        assert clozes[0].answer == "answer"

    def test_multiple(self):
        clozes = _find_clozes("{{a}} and {{b}} and {{c}}")
        assert len(clozes) == 3
        assert [c.answer for c in clozes] == ["a", "b", "c"]

    def test_scope_before(self):
        clozes = _find_clozes("{{answer}}[-1]")
        assert clozes[0].scope_before == 1
        assert clozes[0].scope_after is None

    def test_scope_after(self):
        clozes = _find_clozes("{{answer}}[2]")
        assert clozes[0].scope_before is None
        assert clozes[0].scope_after == 2

    def test_scope_both(self):
        clozes = _find_clozes("{{answer}}[-1,2]")
        assert clozes[0].scope_before == 1
        assert clozes[0].scope_after == 2

    def test_scope_comma_only_after(self):
        clozes = _find_clozes("{{answer}}[,3]")
        assert clozes[0].scope_before is None
        assert clozes[0].scope_after == 3

    def test_no_scope(self):
        clozes = _find_clozes("{{answer}}")
        assert clozes[0].scope_before is None
        assert clozes[0].scope_after is None

    def test_match_positions(self):
        text = "AB{{cd}}EF"
        clozes = _find_clozes(text)
        assert text[clozes[0].match_start:clozes[0].match_end] == "{{cd}}"

    def test_no_clozes(self):
        assert _find_clozes("plain text") == []


# ---------------------------------------------------------------------------
# _build_text
# ---------------------------------------------------------------------------

class TestBuildText:
    def _make_clozes(self, text):
        return _find_clozes(text)

    def test_single_active(self):
        text = "{{a}} and {{b}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active={0})
        assert "{{a}}" in result
        assert "{{b}}" not in result
        assert " b" in result

    def test_all_active(self):
        text = "{{a}} and {{b}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active={0, 1})
        assert "{{a}}" in result
        assert "{{b}}" in result

    def test_none_active(self):
        text = "{{a}} and {{b}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active=set())
        assert "{{" not in result
        assert "a and b" == result

    def test_hint_preserved_for_active(self):
        text = "{{ans::hint}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active={0})
        assert result == "{{ans::hint}}"

    def test_hint_stripped_for_inactive(self):
        text = "{{ans::hint}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active=set())
        assert result == "ans"

    def test_scope_modifier_stripped(self):
        text = "{{answer}}[-1,2]"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active={0})
        assert "[-1,2]" not in result
        assert "{{answer}}" in result

    def test_id_stripped_for_active(self):
        """IDs are not included in the stored text — only answer and optional hint."""
        text = "{{1::answer}}"
        clozes = self._make_clozes(text)
        result = _build_text(text, clozes, active={0})
        assert result == "{{answer}}"


# ---------------------------------------------------------------------------
# Card generation — Adapter.parse()
# ---------------------------------------------------------------------------

class TestCardGeneration:
    def test_ungrouped_one_card_per_cloze(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{quick}} brown {{fox}} jumps."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 2

        # Card 0: "quick" active, "fox" plain
        assert "{{quick}}" in cards[0].content["text"]
        assert "{{fox}}" not in cards[0].content["text"]
        assert "fox" in cards[0].content["text"]

        # Card 1: "fox" active, "quick" plain
        assert "{{fox}}" in cards[1].content["text"]
        assert "{{quick}}" not in cards[1].content["text"]
        assert "quick" in cards[1].content["text"]

    def test_grouped_one_card_per_group(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{1::quick}} brown {{1::fox}} jumps."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1
        assert "{{quick}}" in cards[0].content["text"]
        assert "{{fox}}" in cards[0].content["text"]

    def test_grouped_with_hint(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{1::quick::speed}} and {{1::fox::animal}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1
        assert "{{quick::speed}}" in cards[0].content["text"]
        assert "{{fox::animal}}" in cards[0].content["text"]

    def test_sequence_n_cards_progressive_reveal(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nFirst {{1.1::one}} then {{1.2::two}} then {{1.3::three}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 3

        # Step 1: all three blanked (current + future hidden)
        t0 = cards[0].content["text"]
        assert "{{one}}" in t0
        assert "{{two}}" in t0
        assert "{{three}}" in t0

        # Step 2: "one" revealed, "two" + "three" blanked
        t1 = cards[1].content["text"]
        assert "{{one}}" not in t1
        assert "one" in t1
        assert "{{two}}" in t1
        assert "{{three}}" in t1

        # Step 3: "one" and "two" revealed, "three" blanked
        t2 = cards[2].content["text"]
        assert "{{one}}" not in t2
        assert "{{two}}" not in t2
        assert "one" in t2
        assert "two" in t2
        assert "{{three}}" in t2

    def test_sequence_out_of_order_sorted(self, adapter):
        """Steps declared out of order are sorted numerically."""
        text = "---\nsr_adapter: mnmd\n---\n{{1.2::second}} then {{1.1::first}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 2
        # First card should test step 1.1 (first)
        assert cards[0].key == "seq_1_1.1"
        assert cards[1].key == "seq_1_1.2"

    def test_sequence_with_ungrouped_in_same_block(self, adapter):
        """Non-sequence clozes become plain text in sequence cards."""
        text = "---\nsr_adapter: mnmd\n---\n{{1.1::a}} and {{b}} and {{1.2::c}}."
        cards = adapter.parse(text, "/test.md", {})
        # 1 ungrouped card (b) + 2 sequence cards (a, c)
        assert len(cards) == 3
        seq_cards = [c for c in cards if c.key.startswith("seq_")]
        assert len(seq_cards) == 2
        for sc in seq_cards:
            assert "{{b}}" not in sc.content["text"]
            assert "b" in sc.content["text"]

    def test_multiple_sequence_bases(self, adapter):
        """Two independent sequences in one block."""
        text = "---\nsr_adapter: mnmd\n---\n{{1.1::a}} {{2.1::x}} {{1.2::b}} {{2.2::y}}."
        cards = adapter.parse(text, "/test.md", {})
        keys = [c.key for c in cards]
        assert "seq_1_1.1" in keys
        assert "seq_1_1.2" in keys
        assert "seq_2_2.1" in keys
        assert "seq_2_2.2" in keys
        assert len(cards) == 4

    def test_card_keys_prefixes(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{quick}} {{1::grouped}} {{1.1::seq1}} {{1.2::seq2}}."
        cards = adapter.parse(text, "/test.md", {})
        keys = [c.key for c in cards]
        assert any(k.startswith("cloze_L") for k in keys)
        assert any(k.startswith("group_") for k in keys)
        assert any(k.startswith("seq_") for k in keys)

    def test_tags_propagated(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}."
        cards = adapter.parse(text, "/test.md", {"tags": ["bio", "science"]})
        assert cards[0].tags == ["bio", "science"]

    def test_tags_string_config(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}."
        cards = adapter.parse(text, "/test.md", {"tags": "bio, science"})
        assert cards[0].tags == ["bio", "science"]

    def test_source_line_accuracy(self, adapter):
        # "---\nsr_adapter: mnmd\n---" has 2 newlines → body_start_line = 3
        # Body is "\nThe {{answer}} here." — first line (line 3) is empty,
        # "The {{answer}} here." is at line 4
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}} here."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].source_line == 4

    def test_source_line_second_paragraph(self, adapter):
        # body_start_line = 3, body = "\nPara one.\n\nPara two {{b}}."
        # line 3: empty, line 4: "Para one.", line 5: empty, line 6: "Para two {{b}}."
        text = "---\nsr_adapter: mnmd\n---\nPara one.\n\nPara two {{b}}."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].source_line == 6

    def test_multiple_paragraphs(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nPara one {{a}}.\n\nPara two {{b}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 2

    def test_no_clozes_no_cards(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nJust plain text."
        assert adapter.parse(text, "/test.md", {}) == []

    def test_empty_body(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n"
        assert adapter.parse(text, "/test.md", {}) == []

    def test_display_text_is_card_text(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{photosynthesis}} process."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].display_text == "The {{photosynthesis}} process."

    def test_display_text_truncated(self, adapter):
        long_text = "a" * 300
        text = f"---\nsr_adapter: mnmd\n---\n{long_text} {{{{{long_text}}}}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards[0].display_text) == 200

    def test_display_text_grouped(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{1::alpha}} and {{1::beta}}."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].display_text == "{{alpha}} and {{beta}}."

    def test_gradable_default_true(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].gradable is True


# ---------------------------------------------------------------------------
# Content dict structure
# ---------------------------------------------------------------------------

class TestContentDict:
    def test_only_text_key(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}."
        cards = adapter.parse(text, "/test.md", {})
        assert list(cards[0].content.keys()) == ["text"]

    def test_no_source_line_in_content(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}."
        cards = adapter.parse(text, "/test.md", {})
        assert "source_line" not in cards[0].content

    def test_active_cloze_markers_present(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{a}} and {{b}}."
        cards = adapter.parse(text, "/test.md", {})
        # Card 0: {{a}} active, b plain
        assert "{{a}}" in cards[0].content["text"]
        assert "{{b}}" not in cards[0].content["text"]

    def test_hint_preserved_in_active(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer::hint here}}."
        cards = adapter.parse(text, "/test.md", {})
        assert "{{answer::hint here}}" in cards[0].content["text"]

    def test_scope_modifier_not_in_text(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nBefore.\n\nThe {{answer}}[-1]."
        cards = adapter.parse(text, "/test.md", {})
        assert "[-1]" not in cards[0].content["text"]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------

class TestRelations:
    def test_mutually_exclusive_ungrouped_same_block(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{a}} and {{b}} and {{c}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 3

        # Card 0 should have ME relations to card 1 and card 2
        me0 = [r for r in cards[0].relations if r.relation_type == "mutually_exclusive"]
        assert len(me0) == 2
        assert {r.target_key for r in me0} == {cards[1].key, cards[2].key}

        # Card 1 should have ME relation to card 2
        me1 = [r for r in cards[1].relations if r.relation_type == "mutually_exclusive"]
        assert len(me1) == 1
        assert me1[0].target_key == cards[2].key

        # Card 2: no ME (symmetric — only one direction declared)
        me2 = [r for r in cards[2].relations if r.relation_type == "mutually_exclusive"]
        assert len(me2) == 0

    def test_mutually_exclusive_includes_grouped(self, adapter):
        """Grouped cards also get ME with ungrouped in the same block."""
        text = "---\nsr_adapter: mnmd\n---\n{{a}} and {{1::b}} and {{1::c}}."
        cards = adapter.parse(text, "/test.md", {})
        # 1 ungrouped + 1 grouped
        assert len(cards) == 2
        me = [r for r in cards[0].relations if r.relation_type == "mutually_exclusive"]
        assert len(me) == 1

    def test_no_mutually_exclusive_between_blocks(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{a}} here.\n\n{{b}} there."
        cards = adapter.parse(text, "/test.md", {})
        for card in cards:
            assert not any(r.relation_type == "mutually_exclusive" for r in card.relations)

    def test_no_mutually_exclusive_single_cloze(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{a}} alone."
        cards = adapter.parse(text, "/test.md", {})
        assert cards[0].relations == []

    def test_sequence_followed_by(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\n{{1.1::step1}} then {{1.2::step2}} then {{1.3::step3}}."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 3

        # 0 → 1
        fb0 = [r for r in cards[0].relations if r.relation_type == "is_followed_by_on_correct"]
        assert len(fb0) == 1
        assert fb0[0].target_key == cards[1].key

        # 1 → 2
        fb1 = [r for r in cards[1].relations if r.relation_type == "is_followed_by_on_correct"]
        assert len(fb1) == 1
        assert fb1[0].target_key == cards[2].key

        # 2 → nothing
        fb2 = [r for r in cards[2].relations if r.relation_type == "is_followed_by_on_correct"]
        assert len(fb2) == 0

    def test_sequence_not_mutually_exclusive(self, adapter):
        """Sequence cards from the same block should NOT be mutually exclusive."""
        text = "---\nsr_adapter: mnmd\n---\n{{1.1::step1}} then {{1.2::step2}}."
        cards = adapter.parse(text, "/test.md", {})
        for card in cards:
            assert not any(r.relation_type == "mutually_exclusive" for r in card.relations)

    def test_independent_sequences_no_cross_relations(self, adapter):
        """Two sequence bases don't get followed_by across bases."""
        text = "---\nsr_adapter: mnmd\n---\n{{1.1::a}} {{2.1::x}} {{1.2::b}} {{2.2::y}}."
        cards = adapter.parse(text, "/test.md", {})
        for card in cards:
            for rel in card.relations:
                if rel.relation_type == "is_followed_by_on_correct":
                    # 1.x only points to 1.x+1, 2.x only points to 2.x+1
                    if card.key.startswith("seq_1_"):
                        assert rel.target_key.startswith("seq_1_")
                    elif card.key.startswith("seq_2_"):
                        assert rel.target_key.startswith("seq_2_")


# ---------------------------------------------------------------------------
# Scope modifiers
# ---------------------------------------------------------------------------

class TestScopeModifiers:
    def test_scope_before(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nContext paragraph.\n\nThe {{answer}}[-1]."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1
        assert "Context paragraph." in cards[0].content["text"]
        assert "The {{answer}}." in cards[0].content["text"]

    def test_scope_after(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}[2].\n\nAfter one.\n\nAfter two."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1
        assert "After one." in cards[0].content["text"]
        assert "After two." in cards[0].content["text"]

    def test_scope_both(self, adapter):
        text = "---\nsr_adapter: mnmd\n---\nBefore.\n\nThe {{answer}}[-1,1].\n\nAfter."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1
        assert "Before." in cards[0].content["text"]
        assert "After." in cards[0].content["text"]

    def test_scope_clamped_at_boundaries(self, adapter):
        """Requesting more scope than available paragraphs doesn't error."""
        text = "---\nsr_adapter: mnmd\n---\nThe {{answer}}[-5,5]."
        cards = adapter.parse(text, "/test.md", {})
        assert len(cards) == 1  # no crash

    def test_scope_context_with_clozes_in_neighbor(self, adapter):
        """Scope context paragraphs may contain {{}} — they appear as raw text."""
        text = "---\nsr_adapter: mnmd\n---\nContext {{raw}}.\n\nThe {{answer}}[-1]."
        cards = adapter.parse(text, "/test.md", {})
        # The scope context is included as raw block text
        # It's a different block so its clozes are untouched raw text
        answer_card = [c for c in cards if "{{answer}}" in c.content["text"]][0]
        assert "Context {{raw}}." in answer_card.content["text"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRendering:
    def test_front_blanks(self, adapter):
        result = adapter.render_front({"text": "The {{quick}} brown fox."})
        assert "[…]" in result
        assert "quick" not in result

    def test_front_with_hint(self, adapter):
        result = adapter.render_front({"text": "The {{answer::a hint}}."})
        assert "a hint" in result
        assert "answer" not in result

    def test_front_multiple_blanks(self, adapter):
        result = adapter.render_front({"text": "{{a}} and {{b}}."})
        assert result.count("[…]") == 2

    def test_back_highlights(self, adapter):
        result = adapter.render_back({"text": "The {{quick}} brown fox."})
        assert "<mark>quick</mark>" in result

    def test_back_hint_stripped(self, adapter):
        result = adapter.render_back({"text": "The {{answer::hint}}."})
        assert "<mark>answer</mark>" in result
        assert "hint" not in result

    def test_back_multiple_highlights(self, adapter):
        result = adapter.render_back({"text": "{{a}} and {{b}}."})
        assert "<mark>a</mark>" in result
        assert "<mark>b</mark>" in result

    def test_markdown_bold(self, adapter):
        result = adapter.render_front({"text": "The **bold** {{answer}}."})
        assert "<strong>bold</strong>" in result

    def test_markdown_italic(self, adapter):
        result = adapter.render_front({"text": "The *italic* {{answer}}."})
        assert "<em>italic</em>" in result

    def test_markdown_inline_code(self, adapter):
        result = adapter.render_front({"text": "The `code` {{answer}}."})
        assert "<code>code</code>" in result

    def test_html_escaping_surrounding_text(self, adapter):
        """HTML in surrounding text is escaped."""
        result = adapter.render_front({"text": "The <script> {{answer}}."})
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_html_escaping_answer(self, adapter):
        """HTML inside a cloze answer is escaped."""
        result = adapter.render_back({"text": "The {{<b>bold</b>}}."})
        assert "<mark>&lt;b&gt;bold&lt;/b&gt;</mark>" in result

    def test_html_escaping_hint(self, adapter):
        """HTML in a hint is escaped."""
        result = adapter.render_front({"text": "The {{answer::<em>hint</em>}}."})
        assert "&lt;em&gt;hint&lt;/em&gt;" in result
        assert "<em>" not in result

    def test_ampersand_escaped(self, adapter):
        result = adapter.render_front({"text": "A & B {{answer}}."})
        assert "&amp;" in result

    def test_empty_text(self, adapter):
        assert "<div>" in adapter.render_front({"text": ""})
        assert "<div>" in adapter.render_back({"text": ""})

    def test_single_newline_becomes_space(self, adapter):
        result = adapter.render_front({"text": "Line 1\nLine 2"})
        assert "Line 1 Line 2" in result

    def test_double_newline_becomes_br(self, adapter):
        result = adapter.render_front({"text": "Para 1\n\nPara 2"})
        assert "<br>" in result


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_parse_multi_paragraph(self, adapter):
        doc = """---
sr_adapter: mnmd
tags: [biology]
---
Photosynthesis converts {{light energy}} into
{{chemical energy}} stored in {{glucose}}.

The process occurs in {{chloroplasts}}.
"""
        cards = adapter.parse(doc, "/bio.md", {"tags": ["biology"]})
        # 3 clozes in para 1, 1 in para 2 = 4 cards
        assert len(cards) == 4
        for card in cards:
            assert card.tags == ["biology"]
            assert list(card.content.keys()) == ["text"]

    def test_context_block_parse(self, adapter):
        doc = """---
sr_adapter: mnmd
---
> ?
> Photosynthesis converts {{light energy}} into
> {{chemical energy}} stored in {{glucose}}.
"""
        cards = adapter.parse(doc, "/test.md", {})
        assert len(cards) == 3
        for card in cards:
            # > prefix must be stripped
            assert not any(line.startswith("> ") for line in card.content["text"].split("\n"))

    def test_mixed_grouped_and_ungrouped(self, adapter):
        doc = "---\nsr_adapter: mnmd\n---\n{{1::a}} and {{b}} and {{1::c}}."
        cards = adapter.parse(doc, "/test.md", {})
        # 1 ungrouped (b) + 1 grouped (a+c) = 2
        assert len(cards) == 2

    def test_roundtrip_render(self, adapter):
        doc = "---\nsr_adapter: mnmd\n---\nThe {{quick::speed}} brown {{fox}} jumps."
        cards = adapter.parse(doc, "/test.md", {})
        for card in cards:
            front = adapter.render_front(card.content)
            back = adapter.render_back(card.content)
            # Both produce valid HTML wrapping
            assert front.startswith("<div>")
            assert back.startswith("<div>")
            # Front has blanks, back has highlights
            assert "[…]" in front or "[" in front
            assert "<mark>" in back

    def test_no_frontmatter(self, adapter):
        """File without frontmatter still parses if called directly."""
        cards = adapter.parse("The {{answer}}.", "/test.md", {})
        assert len(cards) == 1
        assert "{{answer}}" in cards[0].content["text"]

    def test_multiline_paragraph_cloze(self, adapter):
        doc = "---\nsr_adapter: mnmd\n---\nFirst line with {{a}}\nand second line with {{b}}."
        cards = adapter.parse(doc, "/test.md", {})
        assert len(cards) == 2
        # Both cards should contain the full paragraph text
        for card in cards:
            assert "First line" in card.content["text"]
            assert "second line" in card.content["text"]
