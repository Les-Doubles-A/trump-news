"""Identification d'un éditeur (clé canonique) et de sa fiabilité par défaut.

Deux articles de Reuters, l'un via Google News, l'autre via le flux RSS, doivent compter pour UNE source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config.schema import NewsSection

_SECOND_LEVEL = {"co", "com", "org", "gov", "ac", "net"}


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def _registrable_label(domain: str) -> str:
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return domain


@dataclass(frozen=True)
class ResolvedSource:
    key: str
    display: str
    domain: str
    reliability: float


class SourceResolver:
    def __init__(self, cfg: NewsSection) -> None:
        self._reliability = {k.lower(): v for k, v in cfg.source_reliability.items()}
        self._aliases = {k.lower(): v.lower() for k, v in cfg.source_aliases.items()}
        self._default = cfg.default_reliability
        patterns = set(self._aliases) | {k for k in self._reliability if len(k) >= 5}
        self._patterns = sorted(patterns, key=len, reverse=True)

    def resolve(self, name: str | None, url: str | None) -> ResolvedSource:
        domain = domain_of(url)
        slug_name = re.sub(r"[^a-z0-9]", "", (name or "").lower())
        slug_domain = domain.replace(".", "")
        key = None
        for pat in self._patterns:
            if pat in slug_name or pat in slug_domain:
                key = self._aliases.get(pat, pat)
                break
        if key is None:
            if slug_name and (not domain or domain.endswith("news.google.com")):
                key = slug_name
            else:
                key = _registrable_label(domain) or slug_name or "unknown"
        display = (name or "").strip() or domain or key
        return ResolvedSource(key, display, domain, self._reliability.get(key, self._default))
