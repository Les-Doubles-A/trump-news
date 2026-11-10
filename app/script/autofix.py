"""Corrections déterministes d'un script, quand le LLM n'y arrive pas seul.

Seul cas traité : une attribution manquante. Si les SEULES erreurs bloquantes d'un segment sont « doit être attribué »
et/ou « ne pas l'énoncer comme un fait », on préfixe la phrase par « According to <média> » avec la VRAIE source
de l'affirmation (celle du fact-check) et le segment devient « attributed ». Rien n'est inventé : le média est
celui qui a effectivement rapporté l'information. Le script est ensuite revalidé normalement.
"""
from __future__ import annotations

import re
from typing import Sequence

from app.script.models import ClaimInfo, Violation

FIXABLE = {"attribution", "not_established"}
PREFIX = {"en": "According to {src}, ", "fr": "Selon {src}, ", "es": "Según {src}, "}
_LOWER_FIRST = {  # premier mot banal : passe en minuscule après le préfixe ; un nom propre garde sa majuscule
    "en": {"the", "a", "an", "this", "that", "these", "those", "it", "in", "on", "at", "after", "before", "during", "his",
           "her", "their", "its", "there", "some", "several", "officials", "lawmakers", "advisers", "aides", "prosecutors",
           "judges", "voters", "supporters", "critics", "sources", "investigators"},
    "fr": {"le", "la", "les", "un", "une", "des", "ce", "cette", "ces", "il", "elle", "ils", "elles", "en", "dans", "après",
           "avant", "pendant", "son", "sa", "ses", "leur", "leurs", "plusieurs"},
    "es": {"el", "la", "los", "las", "un", "una", "unos", "unas", "este", "esta", "estos", "estas", "en", "tras", "antes",
           "durante", "su", "sus", "varios", "varias"},
}


AND = {"en": " and ", "fr": " et ", "es": " y "}


def source_names(claim_ids: Sequence[str], claims: dict[str, ClaimInfo]) -> list[str]:
    """Médias à citer (2 au plus) : pour chaque affirmation à attribuer du segment, sa source la plus courte, afin que
    chaque information combinée dans la phrase soit créditée à un média qui l'a rapportée ; une affirmation LIKELY
    seule cite ses deux éditeurs. Vide si rien à attribuer, s'il faudrait plus de 2 médias, ou si une affirmation
    est DISPUTED (une divergence se présente autrement : « les sources divergent »)."""
    needed = [c for c in (claims.get(cid) for cid in claim_ids)
              if c and (c.status in ("REPORTED", "LIKELY", "DISPUTED") or c.kind in ("statement", "allegation", "opinion"))]
    if not needed or any(c.status == "DISPUTED" for c in needed):
        return []
    names: list[str] = []
    for c in needed:
        own = sorted({n for n in c.sources if n}, key=len)
        if not own:
            return []
        if not set(own) & set(names):          # déjà couverte par un média cité : rien à ajouter
            names.append(own[0])
    if len(needed) == 1 and needed[0].status == "LIKELY":
        names = sorted({n for n in needed[0].sources if n}, key=len)[:2]
    return names if len(names) <= 2 else []


def attribute(text: str, sources: list[str], language: str) -> str:
    text = text.strip()
    first = text.split(" ", 1)[0]
    if re.sub(r"\W", "", first).lower() in _LOWER_FIRST.get(language, set()):
        text = text[0].lower() + text[1:]
    src = AND.get(language, AND["en"]).join(sources)
    return PREFIX.get(language, PREFIX["en"]).format(src=src) + text


def fix_attributions(segments: list, violations: list[Violation], claims: dict[str, ClaimInfo],
                     language: str) -> tuple[list, list[str]]:
    """Retourne (segments éventuellement corrigés, notes). `segments` : objets pydantic avec .text .type .claims."""
    by_seg: dict[int, set[str]] = {}
    for v in violations:
        if v.hard and v.segment is not None:
            by_seg.setdefault(v.segment, set()).add(v.code)
    out, notes = list(segments), []
    for i, codes in sorted(by_seg.items()):
        if i >= len(out) or not codes <= FIXABLE:
            continue
        seg = out[i]
        sources = source_names(seg.claims, claims)
        if not sources:
            continue
        update = {"type": "attributed"}
        if "attribution" in codes:
            update["text"] = attribute(seg.text, sources, language)
        out[i] = seg.model_copy(update=update)
        notes.append(f"Segment {i + 1} : attribution ajoutée automatiquement (source réelle : {', '.join(sources)}).")
    return out, notes
