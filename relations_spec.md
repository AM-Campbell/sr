# Relation Types
The relation type set is append-only — types can be added in future versions but never removed. The scheduler must ignore relation types it does not recognize.

must_be_introduced_before
The upstream note should have been encountered before the downstream note is shown. Does not require mastery, just exposure.

"You should have seen A before we build on it."

The scheduler decides what "introduced" means concretely.

must_be_known_before
The upstream note should be reliably recalled before the downstream note is shown. The downstream note assumes the upstream is internalized.

"You need to actually know A because B depends on it."

The scheduler decides what "known" means concretely (success rate threshold, minimum interval, number of successful reviews, etc.).

is_followed_by_on_correct
When a card from the upstream note is answered correctly, a card from the downstream note should be shown next in the same session. This expresses contiguous ordering within a review session.

Used for note chains that build through steps or reinforce each other when reviewed together.

This relation is used by the parser for sequence cards within a single note (e.g., {{1.1::...}} → {{1.2::...}}).

is_replaced_by
The upstream note is replaced by the downstream note. This tells the scheduler two things:

The downstream note tests the same content as the upstream note, so the scheduler should transfer review state / scheduling momentum from upstream cards to downstream cards.
The upstream note's cards should be retired when the downstream note's cards are promoted.
If the downstream cards are repeatedly failed, the scheduler may demote back to the upstream cards.

This relation covers the card editing case: when a note is rewritten, the author creates a new note and declares old is_replaced_by new. The scheduler transfers state rather than treating the new cards as unseen.

Promotion/demotion thresholds are a scheduler concern, not encoded in the relation.

mutually_exclusive
Cards from the two notes leak information if shown together in the same session. The scheduler should separate them (bury for the session, enforce a gap, etc.).

Common case: two notes that test the same fact from different directions, or notes where seeing one gives away the answer to the other.

This relation is symmetric: A mutually_exclusive B is equivalent to B mutually_exclusive A. Only one direction needs to be declared.
