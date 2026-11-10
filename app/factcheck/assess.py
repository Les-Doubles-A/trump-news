"""Statut et confiance d'une affirmation : règles déterministes, jamais décidées par le LLM."""
from __future__ import annotations

from app.config.schema import FactCheckSection
from app.factcheck.grounding import normalize
from app.factcheck.models import ClaimResult, Evidence, SourceDoc


def independent_origins(kind: str, supporters: list[tuple[SourceDoc, str]]) -> int:
    """Nombre d'origines indépendantes. Pour un « fait », une même phrase reprise mot pour mot par
    plusieurs éditeurs (dépêche syndiquée) ne compte qu'une fois."""
    origins = set()
    for doc, quote in supporters:
        nq = normalize(quote)
        if kind == "fact" and len(nq.split()) >= 12:
            origins.add(("quote", nq))
        else:
            origins.add(("source", doc.key))
    return len(origins)


def assess(
    kind: str,
    supporters: list[tuple[SourceDoc, str]],
    contradictors: list[tuple[SourceDoc, str]],
    cfg: FactCheckSection,
) -> tuple[str, float, str]:
    n = independent_origins(kind, supporters)
    keys = sorted({d.key for d, _ in supporters})
    mean_rel = sum(d.reliability for d, _ in supporters) / len(supporters) if supporters else 0.0
    notes: list[str] = []

    if not supporters:
        return "UNKNOWN", 0.1, "Aucune source vérifiable."
    if contradictors:
        status = "DISPUTED"
        notes.append("Contredit par : " + ", ".join(sorted({d.key for d, _ in contradictors})) + ".")
    elif kind in ("allegation", "opinion"):
        status = "REPORTED"
        notes.append("Allégation ou opinion : à attribuer, jamais à présenter comme un fait.")
    elif n >= cfg.confirmed_min_sources and mean_rel >= cfg.confirmed_min_reliability:
        status = "CONFIRMED"
    elif n >= cfg.likely_min_sources:
        status = "LIKELY"
    else:
        status = "REPORTED"
    if len(keys) > n:
        notes.append("Reprises quasi identiques d'une même dépêche : comptées comme une seule origine.")
    notes.append(f"{n} origine(s) indépendante(s) : {', '.join(keys)}.")
    if all(d.level == "title" for d, _ in supporters):
        notes.append("Appuyé uniquement par des titres (texte non disponible).")

    base = {"CONFIRMED": 0.85, "LIKELY": 0.65, "REPORTED": 0.4, "DISPUTED": 0.3, "UNKNOWN": 0.1}[status]
    conf = base + 0.1 * (mean_rel - 0.5) / 0.5
    if kind in ("allegation", "opinion"):
        conf *= 0.85
    if all(d.level == "title" for d, _ in supporters):
        conf *= 0.85
    return status, round(max(0.0, min(1.0, conf)), 2), " ".join(notes)


def verdict_for(claims: list[ClaimResult]) -> tuple[str, float]:
    if not claims:
        return "BLOCKED", 0.0
    conf = round(sum(c.confidence for c in claims[:3]) / len(claims[:3]), 2)
    if any(c.status == "DISPUTED" for c in claims[:3]):
        return "CAUTION", conf
    if any(c.status in ("CONFIRMED", "LIKELY") and c.kind in ("fact", "statement") for c in claims):
        return "SAFE", conf
    return "CAUTION", conf
