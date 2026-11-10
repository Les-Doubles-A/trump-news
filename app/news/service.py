"""Ingestion : collecte -> normalisation -> dédoublonnage -> regroupement en sujets -> scoring."""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import NewsSection
from app.core.errors import NewsProviderError
from app.core.interfaces import IEmbedder, INewsProvider
from app.core.retry import retry_call
from app.database.models import Article, FactCheck, Source, Story
from app.database.session import session_scope
from app.news.cluster import cluster_items, cosine, ignore_tokens, similarity, title_tokens, unit
from app.news.normalize import NormalizedArticle, normalize
from app.news.scoring import ScoreBreakdown, ScoredArticle, score_story
from app.news.sources import SourceResolver
from app.utils.timeutil import as_utc, utcnow

log = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    ok: bool
    count: int = 0
    error: str | None = None


@dataclass
class IngestReport:
    fetched: int = 0
    invalid: int = 0
    too_old: int = 0
    irrelevant: int = 0
    duplicates: int = 0
    new_articles: int = 0
    stories_created: int = 0
    stories_updated: int = 0
    semantic: bool = False
    provider_status: dict[str, ProviderResult] = field(default_factory=dict)
    duration_s: float = 0.0


@dataclass
class Sig:
    """Signature d'un titre déjà stocké : mots significatifs + vecteur sémantique éventuel."""

    tokens: frozenset[str]
    vec: list[float] | None = None


@dataclass
class StoryRow:
    id: int
    title: str
    score: float
    category: str | None
    n_sources: int
    n_articles: int
    newest: datetime
    sources: list[str]
    used: bool
    breakdown: ScoreBreakdown
    verdict: str | None = None
    n_text: int = 0


class NewsIngestionService:
    def __init__(
        self,
        cfg: NewsSection,
        providers: list[INewsProvider],
        factory: sessionmaker[Session],
        *,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        embedder: IEmbedder | None = None,
    ) -> None:
        self.cfg = cfg
        self.embedder = embedder if cfg.embeddings.enabled else None
        self._vec_cache: dict[str, list[float]] = {}
        self.providers = providers
        self.factory = factory
        self._now = now
        self._sleep = sleep
        self.resolver = SourceResolver(cfg)
        self._ignore = ignore_tokens(cfg)

    # ------------------------------------------------------------------ run
    def run(self) -> IngestReport:
        t0 = time.perf_counter()
        rep = IngestReport()
        now = self._now()
        raws = []
        for p in self.providers:
            try:
                items = retry_call(
                    lambda p=p: p.fetch(limit=self.cfg.max_per_provider),
                    attempts=3, base_delay=1.0, retry_on=(NewsProviderError,), sleep=self._sleep, label=f"news:{p.name}",
                )
                rep.provider_status[p.name] = ProviderResult(True, len(items))
                raws.extend(items)
            except Exception as exc:  # un fournisseur en panne ne bloque pas les autres
                rep.provider_status[p.name] = ProviderResult(False, 0, str(exc))
                log.error("Fournisseur %s en échec : %s", p.name, exc)
        rep.fetched = len(raws)
        log.info("Retrieved %d articles", rep.fetched)

        kept: list[NormalizedArticle] = []
        seen: set[str] = set()
        cutoff = now - timedelta(hours=self.cfg.max_age_hours)
        for raw in raws:
            n = normalize(raw, self.resolver, self.cfg, now, self._ignore)
            if n is None:
                rep.invalid += 1
            elif n.published_at < cutoff:
                rep.too_old += 1
            elif n.relevance < self.cfg.min_relevance:
                rep.irrelevant += 1
            elif n.hash in seen:
                rep.duplicates += 1
            else:
                seen.add(n.hash)
                kept.append(n)
        kept.sort(key=lambda n: n.published_at, reverse=True)
        self._persist(kept, rep, now)
        rep.duration_s = time.perf_counter() - t0
        log.info(
            "Removed %d duplicates, %d new articles, %d stories created, %d updated",
            rep.duplicates, rep.new_articles, rep.stories_created, rep.stories_updated,
        )
        return rep

    # -------------------------------------------------------------- persist
    def _persist(self, items: list[NormalizedArticle], rep: IngestReport, now: datetime) -> None:
        cfg = self.cfg
        with session_scope(self.factory) as s:
            hashes = [n.hash for n in items]
            existing: set[str] = set()
            for i in range(0, len(hashes), 500):
                existing.update(s.scalars(select(Article.hash).where(Article.hash.in_(hashes[i : i + 500]))))
            fresh = [n for n in items if n.hash not in existing]
            rep.duplicates += len(items) - len(fresh)
            rep.new_articles = len(fresh)

            recent = self._recent_story_tokens(s, now)
            self._attach_vectors(fresh, rep)
            touched_existing: set[int] = set()
            sources: dict[str, Source] = {}
            for group in cluster_items(fresh, self._pair_score, 1.0):
                story, created = self._attach_or_create(s, group, recent)
                if created:
                    rep.stories_created += 1
                else:
                    touched_existing.add(story.id)
                for n in group:
                    s.add(Article(
                        url=n.url[:2000], title=n.title[:1000], source_id=self._source(s, n, sources).id,
                        source_name=n.source_name, author=n.author, published_at=n.published_at,
                        content=n.content, summary=n.summary, image_url=n.image_url, hash=n.hash, story_id=story.id,
                    ))
                    recent.setdefault(story.id, []).append(Sig(n.tokens, n.vec))
            s.flush()
            rep.stories_updated = len(touched_existing)
            self._rescore(s, now)

    # --------------------------------------------------------- similarité
    def _pair_score(self, a, b) -> float:
        """>= 1.0 signifie « même sujet ». Combine mots communs et (si dispo) similarité sémantique."""
        score = similarity(a.tokens, b.tokens) / self.cfg.similarity_threshold
        if a.vec is not None and b.vec is not None:
            score = max(score, cosine(a.vec, b.vec) / self.cfg.embeddings.threshold)
        return score

    def _embed(self, titles: list[str]) -> dict[str, list[float]]:
        missing = [t for t in dict.fromkeys(titles) if t not in self._vec_cache]
        for i in range(0, len(missing), 64):
            batch = missing[i : i + 64]
            vecs = self.embedder.embed(batch, self.cfg.embeddings.model)
            for t, v in zip(batch, vecs):
                self._vec_cache[t] = unit(v)
        return {t: self._vec_cache[t] for t in titles if t in self._vec_cache}

    def _attach_vectors(self, items: list[NormalizedArticle], rep: IngestReport) -> None:
        if self.embedder is None or not items:
            return
        try:
            vecs = self._embed([n.title for n in items])
        except Exception as exc:  # embeddings indisponibles : repli lexical
            log.warning("Embeddings indisponibles, regroupement par mots uniquement : %s", exc)
            return
        for n in items:
            n.vec = vecs.get(n.title)
        rep.semantic = True

    def _recent_story_tokens(self, s: Session, now: datetime) -> dict[int, list[Sig]]:
        cutoff = now - timedelta(days=self.cfg.story_match_days)
        ids = list(s.scalars(select(Story.id).where(Story.created_at >= cutoff)))
        titles: dict[int, list[str]] = {i: [] for i in ids}
        for i in range(0, len(ids), 500):
            for sid, title in s.execute(select(Article.story_id, Article.title).where(Article.story_id.in_(ids[i : i + 500]))):
                titles[sid].append(title)
        vecs: dict[str, list[float]] = {}
        if self.embedder is not None:
            try:
                vecs = self._embed([t for ts in titles.values() for t in ts])
            except Exception as exc:
                log.warning("Embeddings indisponibles pour les sujets récents : %s", exc)
        return {sid: [Sig(title_tokens(t, self._ignore), vecs.get(t)) for t in ts] for sid, ts in titles.items()}

    def _attach_or_create(self, s: Session, group: list[NormalizedArticle], recent) -> tuple[Story, bool]:
        best_id, best_sim = None, 0.0
        for sid, sigs in recent.items():
            for n in group:
                for u in sigs:
                    sim = self._pair_score(n, u)
                    if sim > best_sim:
                        best_id, best_sim = sid, sim
        if best_id is not None and best_sim >= 1.0:
            return s.get(Story, best_id), False
        rep = max(group, key=lambda n: (n.reliability, n.published_at))
        fp = hashlib.sha1(" ".join(sorted(rep.tokens)).encode()).hexdigest()
        story = Story(title=rep.title[:500], status="NEW", score=0.0, fingerprint=fp, created_at=self._now())
        s.add(story)
        s.flush()
        recent[story.id] = []
        return story, True

    @staticmethod
    def _source(s: Session, n: NormalizedArticle, cache: dict[str, Source]) -> Source:
        src = cache.get(n.source_key) or s.scalar(select(Source).where(Source.domain == n.source_key))
        if src is None:
            src = Source(name=n.source_name[:200], domain=n.source_key, reliability=n.reliability)
            s.add(src)
            s.flush()
        cache[n.source_key] = src
        return src

    # -------------------------------------------------------------- scoring
    def scored(self, a: Article) -> ScoredArticle:
        r = self.resolver.resolve(a.source_name, a.url)
        return ScoredArticle(
            title=a.title, summary=a.summary, source_key=r.key, reliability=r.reliability,
            published_at=as_utc(a.published_at) or utcnow(), image_url=a.image_url,
            relevance=_relevance(a, self.cfg),
        )

    def _rescore(self, s: Session, now: datetime) -> None:
        cutoff = now - timedelta(days=self.cfg.rescore_days)
        stories = list(s.scalars(select(Story).where(Story.created_at >= cutoff)))
        for story in stories:
            arts = list(s.scalars(select(Article).where(Article.story_id == story.id)))
            if not arts:
                continue
            bd = score_story([self.scored(a) for a in arts], self.cfg, now)
            story.score, story.category, story.title = bd.total, bd.category, bd.best_title[:500]
            for a in arts:
                a.score = bd.total

    def similar_pairs(self, *, hours: int = 24, limit: int = 30) -> list[tuple[str, str, float, float | None]]:
        """Paires de titres récents les plus proches (mots, cosinus) : sert à régler les seuils."""
        now = self._now()
        with session_scope(self.factory) as s:
            rows = list(s.execute(select(Article.title).where(Article.published_at >= now - timedelta(hours=hours))))
        titles = list(dict.fromkeys(r[0] for r in rows))[:250]
        vecs: dict[str, list[float]] = {}
        if self.embedder is not None:
            try:
                vecs = self._embed(titles)
            except Exception as exc:
                log.warning("Embeddings indisponibles : %s", exc)
        toks = {t: title_tokens(t, self._ignore) for t in titles}
        out = []
        for i, a in enumerate(titles):
            for b in titles[i + 1 :]:
                lex = similarity(toks[a], toks[b])
                cos = cosine(vecs[a], vecs[b]) if a in vecs and b in vecs else None
                if lex > 0.15 or (cos is not None and cos > 0.55):
                    out.append((a, b, lex, cos))
        out.sort(key=lambda x: -(x[3] if x[3] is not None else x[2]))
        return out[:limit]

    # -------------------------------------------------------------- queries
    def list_stories(self, *, limit: int = 20, include_used: bool = False, hours: int | None = None) -> list[StoryRow]:
        now = self._now()
        with session_scope(self.factory) as s:
            q = select(Story).order_by(Story.score.desc())
            if not include_used:
                q = q.where(Story.used_at.is_(None))
            if hours:
                q = q.where(Story.created_at >= now - timedelta(hours=hours))
            rows = []
            for story in s.scalars(q.limit(limit)):
                arts = list(s.scalars(select(Article).where(Article.story_id == story.id)))
                if not arts:
                    continue
                bd = score_story([self.scored(a) for a in arts], self.cfg, now)
                fc = s.scalar(select(FactCheck.verdict).where(FactCheck.story_id == story.id).order_by(FactCheck.id.desc()))
                rows.append(StoryRow(story.id, story.title, story.score, story.category, bd.n_sources,
                                     bd.n_articles, bd.newest, bd.sources, story.used_at is not None, bd, fc,
                                     sum(1 for a in arts if a.summary or a.content)))
            return rows


def _relevance(a: Article, cfg: NewsSection) -> float:
    from app.news.normalize import compute_relevance

    return compute_relevance(a.title, " ".join(filter(None, [a.summary, (a.content or "")[:3000]])), cfg)


def mark_story_used(factory: sessionmaker[Session], story_id: int) -> bool:
    with session_scope(factory) as s:
        story = s.get(Story, story_id)
        if story is None:
            return False
        story.used_at = utcnow()
        story.status = "USED"
        return True
