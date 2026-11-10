"""Normalisation d'un RawArticle : texte nettoyé, URL canonique, date UTC, source, pertinence."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.schema import NewsSection
from app.core.interfaces import RawArticle
from app.news.cluster import title_tokens
from app.news.sources import SourceResolver
from app.utils.hashing import article_hash

_TAG = re.compile(r"<[^>]+>")


def clean_text(s: str | None, max_len: int = 2000) -> str | None:
    if not s:
        return None
    s = html.unescape(_TAG.sub(" ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] or None


def _hit(terms: list[str], text: str) -> bool:
    return any(re.search(r"\b" + re.escape(t.lower()), text) for t in terms)


def compute_relevance(title: str, summary: str | None, cfg: NewsSection) -> float:
    t, body = title.lower(), (summary or "").lower()
    if _hit(cfg.keywords, t):
        return 1.0
    if _hit(cfg.keywords, body):
        return 0.6
    if _hit(cfg.related_keywords, t):
        return 0.5
    if _hit(cfg.related_keywords, body):
        return 0.3
    return 0.0


@dataclass
class NormalizedArticle:
    title: str
    url: str
    hash: str
    source_key: str
    source_name: str
    published_at: datetime
    author: str | None
    summary: str | None
    content: str | None
    image_url: str | None
    provider: str
    relevance: float
    tokens: frozenset[str]
    reliability: float
    vec: list[float] | None = None


def normalize(
    raw: RawArticle, resolver: SourceResolver, cfg: NewsSection, now: datetime, ignore: set[str]
) -> NormalizedArticle | None:
    title = clean_text(raw.title, 500)
    url = (raw.url or "").strip()
    if not title or not url.lower().startswith(("http://", "https://")):
        return None
    published = raw.published_at or now  # date absente : on suppose « maintenant »
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    published = published.astimezone(timezone.utc)
    summary = clean_text(raw.summary, 1500)
    if summary and summary.lower() == title.lower():
        summary = None
    src = resolver.resolve(raw.source, url)
    return NormalizedArticle(
        title=title,
        url=url,
        hash=article_hash(url, title),
        source_key=src.key,
        source_name=src.display,
        published_at=published,
        author=clean_text(raw.author, 200),
        summary=summary,
        content=clean_text(raw.content, 20000),
        image_url=(raw.image_url or None),
        provider=raw.provider,
        relevance=compute_relevance(title, " ".join(filter(None, [summary, clean_text(raw.content, 3000)])), cfg),
        tokens=title_tokens(title, ignore),
        reliability=src.reliability,
    )
