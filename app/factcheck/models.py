"""Structures du fact-check, sérialisables en JSON (stockées dans fact_checks.data)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STATUSES = ("CONFIRMED", "LIKELY", "REPORTED", "DISPUTED", "UNKNOWN")
KINDS = ("fact", "statement", "allegation", "opinion")

PHRASING = {
    "CONFIRMED": "Peut être énoncé comme un fait ; citer au moins une source.",
    "LIKELY": "Formuler : « Plusieurs médias rapportent que… ».",
    "REPORTED": "Attribuer obligatoirement : « Selon <média>… » ou « Les médias rapportent… ».",
    "DISPUTED": "Ne pas affirmer : « Les sources divergent : … ». Présenter les deux versions.",
    "UNKNOWN": "Ne pas utiliser dans le script.",
}


@dataclass
class SourceDoc:
    id: str                 # "S1"
    article_id: int
    key: str                # éditeur canonique
    name: str
    url: str
    title: str
    published: str
    text: str               # texte soumis au LLM, et contre lequel les citations sont vérifiées
    level: str              # full | summary | title
    reliability: float

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["chars"] = len(self.text)
        d.pop("text")
        return d


@dataclass
class Evidence:
    source_id: str
    source_key: str
    source_name: str
    url: str
    quote: str


@dataclass
class ClaimResult:
    claim: str
    kind: str
    status: str
    confidence: float
    speaker: str | None = None
    supporting: list[Evidence] = field(default_factory=list)
    contradicting: list[Evidence] = field(default_factory=list)
    notes: str = ""
    phrasing: str = ""


@dataclass
class FactCheckResult:
    story_id: int
    story_title: str
    verdict: str                     # SAFE | CAUTION | BLOCKED
    confidence: float
    model: str | None
    claims: list[ClaimResult]
    sources: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    llm_claims: int = 0
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("from_cache")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, from_cache: bool = False) -> "FactCheckResult":
        claims = [
            ClaimResult(
                **{**c, "supporting": [Evidence(**e) for e in c["supporting"]],
                   "contradicting": [Evidence(**e) for e in c["contradicting"]]}
            )
            for c in d["claims"]
        ]
        return cls(**{**d, "claims": claims, "from_cache": from_cache})
