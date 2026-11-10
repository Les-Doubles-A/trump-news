"""Titre, description et légende d'une vidéo, construits uniquement à partir du script vérifié, de ses sources
et des crédits du montage : rien n'est inventé ni réécrit par le LLM à cette étape."""
from __future__ import annotations

import re

from app.utils.credits import tidy_credit

TITLE_MAX = 100              # YouTube
_ATTRIBUTION = re.compile(r"^(?:according to|selon|según)\s+([^,]{2,60}),\s*(.+)$", re.I)
DESCRIPTION_MAX_BYTES = 5000 # YouTube
CAPTION_MAX = 2200           # TikTok et Instagram
MAX_SOURCES = 10

LABELS = {
    "en": {"sources": "Sources:", "credits": "Image credits:",
           "ai": "Contains AI-generated illustrations (stylized, not real photos)."},
    "fr": {"sources": "Sources :", "credits": "Crédits images :",
           "ai": "Contient des illustrations générées par IA (stylisées, pas de vraies photos)."},
    "es": {"sources": "Fuentes:", "credits": "Créditos de imágenes:",
           "ai": "Contiene ilustraciones generadas por IA (estilizadas, no son fotos reales)."},
}


def clean(text: str) -> str:
    """YouTube refuse « < » et « > » dans le titre et la description."""
    return re.sub(r"\s+", " ", (text or "").replace("<", "").replace(">", "")).strip()


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def make_title(hook: str, fallback: str = "") -> str:
    """Première phrase du hook. « According to BBC, Trump did X. » devient « Trump did X (BBC) » : l'attribution
    reste dans le titre, mais l'information vient en premier (et n'est pas coupée par le nom de la source)."""
    title = clean(hook) or clean(fallback) or "News"
    first = re.split(r"(?<=[.!?])\s+", title)[0]
    m = _ATTRIBUTION.match(first)
    if m and len(m.group(2)) > 15:
        source, claim = m.group(1).strip(), m.group(2).strip().rstrip(".")
        suffix = f" ({source})"
        return _shorten(claim[0].upper() + claim[1:], TITLE_MAX - len(suffix)) + suffix
    return _shorten(title, TITLE_MAX)


def sources_of(source_refs: list[dict] | None) -> list[tuple[str, str]]:
    """(nom, url) des sources réellement citées par le script, sans doublon, dans l'ordre d'apparition."""
    seen, out = set(), []
    for ref in source_refs or []:
        for src in ref.get("sources", []):
            url = (src.get("url") or "").strip()
            key = url or src.get("name")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append((clean(src.get("name") or url), url))
    return out[:MAX_SOURCES]


def _fit_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def build_texts(*, hook: str, full_script: str, source_refs: list[dict] | None, credits: list[str],
                ai_content: bool, hashtags: list[str], language: str) -> tuple[str, str, str, list[str]]:
    """(titre, description longue, légende courte, mots-clés)."""
    lab = LABELS.get(language, LABELS["en"])
    title = make_title(hook, full_script)
    sources = sources_of(source_refs)
    tags = " ".join(hashtags)

    parts = [clean(full_script)]
    if sources:
        parts.append(f"{lab['sources']}\n" + "\n".join(f"- {n}: {u}" if u else f"- {n}" for n, u in sources))
    if credits:
        lines = dict.fromkeys(clean(tidy_credit(c)) for c in credits)      # crédits déjà en cache : nettoyés ici aussi
        parts.append(f"{lab['credits']}\n" + "\n".join(f"- {c}" for c in lines))
    if ai_content:
        parts.append(lab["ai"])
    if tags:
        parts.append(tags)
    description = _fit_bytes("\n\n".join(p for p in parts if p), DESCRIPTION_MAX_BYTES)

    # Légende courte : les liens ne sont pas cliquables sur TikTok et Instagram, on garde le nom des sources.
    head = [title]
    if sources:
        head.append(f"{lab['sources']} " + ", ".join(dict.fromkeys(n for n, _ in sources)))
    if ai_content:
        head.append(lab["ai"])
    tail = tags
    caption = "\n\n".join(head)
    room = CAPTION_MAX - len(tail) - 2
    caption = (caption[:room].rstrip() + "\n\n" + tail) if tail else caption[:CAPTION_MAX]
    keywords = [h.lstrip("#") for h in hashtags]
    return title, description, caption, keywords
