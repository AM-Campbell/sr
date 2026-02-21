"""Tests for sr.models dataclasses."""

from sr.models import Card, Relation, Recommendation, ReviewEvent


def test_card_defaults():
    c = Card(key="k1", content={"q": "hello"})
    assert c.key == "k1"
    assert c.display_text == ""
    assert c.gradable is True
    assert c.tags == []
    assert c.relations == []


def test_card_with_all_fields():
    rel = Relation(target_key="k2", relation_type="depends_on")
    c = Card(key="k1", content={"q": "hi"}, display_text="hi",
             gradable=False, tags=["t1"], relations=[rel])
    assert c.tags == ["t1"]
    assert c.relations[0].target_key == "k2"


def test_relation_defaults():
    r = Relation(target_key="k", relation_type="rt")
    assert r.target_source is None


def test_recommendation():
    r = Recommendation(card_id=1, time="2025-01-01 00:00:00", precision_seconds=60)
    assert r.card_id == 1


def test_review_event():
    e = ReviewEvent(card_id=1, timestamp="2025-01-01", grade=1,
                    time_on_front_ms=100, time_on_card_ms=200,
                    feedback=None, response=None)
    assert e.grade == 1
    assert e.feedback is None
