from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VisualQuery:
    kind: str                       # photo | stock | illustration | headline | claim | number | sources
    text: str = ""                  # description libre (suggestion du script) ou texte de la carte
    entities: list[str] = field(default_factory=list)   # noms propres issus des affirmations (Donald Trump, ...)
    keywords: list[str] = field(default_factory=list)
    footer: str = ""                # cartes : sources / attribution
    badge: str = ""                 # cartes : étiquette de statut
    category: str = "politics"
    language: str = "en"


@dataclass
class VisualAsset:
    provider: str
    external_id: str
    path: Path
    kind: str                       # photo | illustration | card
    width: int
    height: int
    license: str | None = None
    attribution: str | None = None
    source_url: str | None = None
    ai_generated: bool = False
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


@dataclass
class PlanItem:
    segment_index: int
    shot_index: int
    start: float
    end: float
    role: str                       # primary | card | sources
    asset: dict[str, Any]


@dataclass
class VisualPlan:
    script_id: int
    items: list[PlanItem]
    credits: list[str] = field(default_factory=list)
    ai_disclosure: bool = False
    warnings: list[str] = field(default_factory=list)
    id: int = 0
    narration_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id")
        return d
