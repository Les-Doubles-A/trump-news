from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ScriptBlockedError(Exception):
    """Génération refusée : le dossier de fact-check ne permet pas d'écrire un script fiable."""


SECTIONS = ("hook", "context", "main", "details", "impact", "conclusion")
STYLES = ("breaking_news", "neutral_news", "fast_news", "dramatic", "explainer", "timeline", "fact_check")
DURATIONS = (20, 30, 45, 60, 90)
TONES = ("neutral", "pro_trump", "anti_trump", "balanced")


@dataclass
class ClaimInfo:
    id: str
    text: str
    kind: str
    status: str
    speaker: str | None
    sources: list[str]


@dataclass
class Violation:
    code: str
    message: str
    segment: int | None = None
    hard: bool = True

    def __str__(self) -> str:
        where = f"segment {self.segment + 1}: " if self.segment is not None else ""
        return f"{where}{self.message}"


@dataclass
class GeneratedScript:
    id: int
    story_id: int
    style: str
    tone: str
    language: str
    duration_target: int
    segments: list[dict[str, Any]]
    hook: str
    body: str
    ending: str
    full_script: str
    claims_used: list[dict[str, Any]]
    source_references: list[dict[str, Any]]
    visual_suggestions: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    status: str = "DRAFT"
    word_count: int = 0
    est_seconds: float = 0.0
    attempts: int = 1
