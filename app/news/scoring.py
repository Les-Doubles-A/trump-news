"""Score d'un sujet (groupe d'articles). Fonction pure : mêmes entrées, même score. Poids configurables."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.config.schema import NewsSection


@dataclass(frozen=True)
class ScoredArticle:
    title: str
    summary: str | None
    source_key: str
    reliability: float
    published_at: datetime
    image_url: str | None
    relevance: float


@dataclass
class ScoreBreakdown:
    total: float
    components: dict[str, float]
    category: str
    n_sources: int
    n_articles: int
    newest: datetime
    best_title: str
    sources: list[str] = field(default_factory=list)


def count_hits(terms: list[str], text: str) -> int:
    return sum(1 for t in terms if re.search(r"\b" + re.escape(t.lower().strip()), text))


def classify(text: str, categories: dict[str, list[str]]) -> str:
    best, best_n = "politics", 0
    for cat, terms in categories.items():
        n = count_hits(terms, text)
        if n > best_n:
            best, best_n = cat, n
    return best


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_story(arts: list[ScoredArticle], cfg: NewsSection, now: datetime) -> ScoreBreakdown:
    sc = cfg.scoring
    n_articles = len(arts)
    keys = {a.source_key for a in arts}
    n_sources = len(keys)
    titles = " . ".join(a.title.lower() for a in arts)
    fulltext = " . ".join(f"{a.title} {a.summary or ''}".lower() for a in arts)
    rep = max(arts, key=lambda a: (a.reliability, a.published_at))
    newest = max(a.published_at for a in arts)
    category = classify(fulltext, cfg.categories)

    breaking = min(3, count_hits(sc.breaking_terms, titles))
    importance = _clamp(0.2 + 0.15 * breaking + (0.2 if category in sc.high_impact_categories else 0.0))
    age_h = max(0.0, (now - newest).total_seconds() / 3600)
    recency = 0.5 ** (age_h / sc.half_life_hours)
    source_count = _clamp(math.log(1 + n_sources) / math.log(1 + 5))
    relevance = max(a.relevance for a in arts)
    video = 0.3
    video += 0.25 if count_hits(sc.video_terms, rep.title.lower()) else 0.0
    video += 0.2 if any(a.image_url for a in arts) else 0.0
    video += 0.15 if 40 <= len(rep.title) <= 110 else 0.0
    video += 0.1 if re.search(r"\d|[\"“”]", rep.title) else 0.0
    video = _clamp(video)

    dup = (n_articles - n_sources) / n_articles
    marked = sum(1 for a in arts if count_hits(sc.uncertainty_terms, f"{a.title} {a.summary or ''}".lower()))
    uncertainty = (marked / n_articles) * (0.5 if n_sources >= 3 else 1.0)
    best_rel = max(a.reliability for a in arts)
    low_quality = _clamp((0.6 - best_rel) / 0.6)

    pos = {
        "importance": importance, "recency": recency, "source_count": source_count,
        "relevance": relevance, "video_potential": video,
    }
    neg = {"duplicates": dup, "uncertainty": uncertainty, "low_quality": low_quality}
    w = {
        "importance": sc.w_importance, "recency": sc.w_recency, "source_count": sc.w_source_count,
        "relevance": sc.w_relevance, "video_potential": sc.w_video_potential,
        "duplicates": sc.w_duplicates, "uncertainty": sc.w_uncertainty, "low_quality": sc.w_low_quality,
    }
    max_pos = sum(w[k] for k in pos) or 1.0
    raw = sum(w[k] * v for k, v in pos.items()) - sum(w[k] * v for k, v in neg.items())
    return ScoreBreakdown(
        total=round(_clamp(raw / max_pos), 4),
        components={**{k: round(v, 3) for k, v in pos.items()}, **{f"-{k}": round(v, 3) for k, v in neg.items()}},
        category=category,
        n_sources=n_sources,
        n_articles=n_articles,
        newest=newest,
        best_title=rep.title,
        sources=sorted(keys),
    )
