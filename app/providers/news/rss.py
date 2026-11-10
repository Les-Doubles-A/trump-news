"""Fournisseurs RSS/Atom génériques et Google News RSS."""
from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import httpx

from app.core.errors import NewsProviderError
from app.core.interfaces import INewsProvider, RawArticle

log = logging.getLogger(__name__)
USER_AGENT = "TrumpNews/1.0 (personal RSS aggregator)"


def _dt(struct) -> datetime | None:
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(timegm(struct), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    return None


class RSSProvider(INewsProvider):
    def __init__(self, name: str, url: str, client: httpx.Client | None = None, timeout_s: float = 20) -> None:
        self.name = name
        self.url = url
        self._client = client or httpx.Client(
            timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    def _download(self, url: str) -> bytes:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise NewsProviderError(f"{self.name} : réseau indisponible ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 400:
            raise NewsProviderError(f"{self.name} : HTTP {resp.status_code} sur {url}")
        return resp.content

    def _entry_to_raw(self, entry) -> RawArticle | None:
        title, link = entry.get("title"), entry.get("link")
        if not title or not link:
            return None
        content = None
        if entry.get("content"):
            content = entry["content"][0].get("value")
        src = entry.get("source") or {}
        return RawArticle(
            title=title,
            url=link,
            source=(src.get("title") if isinstance(src, dict) else None) or self.name,
            published_at=_dt(entry.get("published_parsed") or entry.get("updated_parsed")),
            author=entry.get("author"),
            summary=entry.get("summary"),
            content=content,
            image_url=_image(entry),
            provider=self.name,
        )

    def fetch(self, limit: int = 50) -> list[RawArticle]:
        data = self._download(self.url)
        feed = feedparser.parse(data)
        if not feed.entries:
            if feed.bozo or not feed.version:
                raise NewsProviderError(f"{self.name} : l'URL ne renvoie pas un flux RSS/Atom valide ({self.url}).")
            return []
        out = [r for r in (self._entry_to_raw(e) for e in feed.entries[:limit]) if r]
        log.info("%s : %d articles", self.name, len(out))
        return out


class GoogleNewsProvider(RSSProvider):
    """Google News RSS (recherche). Les liens sont des redirections Google : on garde le nom de l'éditeur
    (balise <source>) et on retire le suffixe « - Éditeur » du titre."""

    def __init__(
        self, name: str, query: str, language: str = "en", country: str = "US",
        client: httpx.Client | None = None, timeout_s: float = 20,
    ) -> None:
        url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query + ' when:1d')}&hl={language}-{country}&gl={country}&ceid={country}:{language}"
        )
        super().__init__(name, url, client, timeout_s)

    def _entry_to_raw(self, entry) -> RawArticle | None:
        raw = super()._entry_to_raw(entry)
        if raw is None:
            return None
        src = (entry.get("source") or {}).get("title")
        title = raw.title
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        elif " - " in title:
            title, _, tail = title.rpartition(" - ")
            src = src or tail
        raw.title, raw.source = title.strip(), src or raw.source
        raw.summary = None  # le résumé Google n'est qu'une liste de liens
        return raw
