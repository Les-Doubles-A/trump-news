"""Nettoyage des noms d'auteurs des crédits d'images (métadonnées Wikimedia souvent mal formées)."""
from __future__ import annotations

import re

UNKNOWN = "Unknown author"
_DOUBLED = re.compile(r"^(.{3,}?)\s*\1$")
_URL = re.compile(r"^(?:https?://|www\.)\S+$", re.I)


def tidy_artist(name: str | None, fallback: str | None = None) -> str:
    """« Unknown authorUnknown author » -> « Unknown author » ; une adresse web n'est pas un nom d'auteur."""
    for candidate in (name, fallback):
        s = re.sub(r"\s+", " ", candidate or "").strip()
        m = _DOUBLED.match(s)
        if m:
            s = m.group(1).strip()
        if s and not _URL.match(s):
            return s
    return UNKNOWN


def tidy_credit(credit: str) -> str:
    """Crédit « auteur / Wikimedia Commons / licence » : seul l'auteur est nettoyé."""
    parts = credit.split(" / ")
    if len(parts) >= 2:
        parts[0] = tidy_artist(parts[0])
    return " / ".join(parts)
