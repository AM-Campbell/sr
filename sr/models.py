"""Shared data classes used across sr, adapters, and schedulers."""

from dataclasses import dataclass, field


@dataclass
class Relation:
    target_key: str
    relation_type: str
    target_source: str | None = None


@dataclass
class Card:
    key: str
    content: dict
    display_text: str = ""
    gradable: bool = True
    source_line: int = 1
    tags: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)


@dataclass
class Recommendation:
    card_id: int
    time: str
    precision_seconds: int


@dataclass
class ReviewEvent:
    card_id: int
    timestamp: str
    grade: int
    time_on_front_ms: int
    time_on_card_ms: int
    feedback: str | None
    response: dict | None
