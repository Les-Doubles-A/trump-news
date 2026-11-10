"""GDELT DOC 2.0 : recherche d'articles récents, gratuite et sans clé."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.core.errors import NewsProviderError
from app.core.interfaces import INewsProvider, RawArticle

log = logging.getLogger(__name__)
ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTProvider(INewsProvider):
    def __init__(self, name: str, query: str, language: str = "en", client: httpx.Client | None = None, timeout_s: float = 30) -> None:
        self.name = name
        self.query = query
        self.language = language
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)

    def fetch(self, limit: int = 50) -> list[RawArticle]:
        lang = {"en": "english", "fr": "french", "es": "spanish"}.get(self.language, "english")
        params = {
            "query": f"{self.query} sourcelang:{lang}", "mode": "artlist", "format": "json",
            "maxrecords": min(limit, 250), "timespan": "24h", "sort": "datedesc",
        }
        try:
            resp = self._client.get(ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise NewsProviderError(f"{self.name} : réseau indisponible ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 400:
            raise NewsProviderError(f"{self.name} : HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise NewsProviderError(f"{self.name} : réponse GDELT non-JSON ({resp.text[:120]!r}).") from None
        out: list[RawArticle] = []
        for a in data.get("articles", []) or []:
            if not a.get("url") or not a.get("title"):
                continue
            try:
                published = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
            out.append(RawArticle(
                title=a["title"], url=a["url"], source=a.get("domain"), published_at=published,
                image_url=a.get("socialimage") or None, provider=self.name,
            ))
        log.info("%s : %d articles", self.name, len(out))
        return out
