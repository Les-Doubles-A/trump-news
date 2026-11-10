"""Fact-check d'un sujet : sources -> LLM (affirmations + citations) -> vérification déterministe -> statuts."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.errors import TrumpNewsError
from app.core.interfaces import IFactChecker, ILLMProvider
from app.database.models import Article, FactCheck, Story
from app.database.session import session_scope
from app.factcheck.assess import assess, verdict_for
from app.factcheck.fetcher import ArticleTextFetcher
from app.factcheck.grounding import numbers_grounded, quote_in_text, shares_content, unsupported_terms
from app.factcheck.models import PHRASING, ClaimResult, Evidence, FactCheckResult, SourceDoc
from app.news.cluster import ignore_tokens, similarity, title_tokens
from app.news.sources import SourceResolver
from app.prompts import prompt_hash, render_prompt
from app.utils.timeutil import as_utc

log = logging.getLogger(__name__)
_LEVEL_RANK = {"full": 2, "summary": 1, "title": 0}


class EvidenceOut(BaseModel):
    source: str
    quote: str


class ClaimOut(BaseModel):
    claim: str
    kind: Literal["fact", "statement", "allegation", "opinion"]
    speaker: str | None = None
    supporting: list[EvidenceOut] = Field(default_factory=list)
    contradicting: list[EvidenceOut] = Field(default_factory=list)
    notes: str | None = None


class Extraction(BaseModel):
    claims: list[ClaimOut]


class LLMFactChecker(IFactChecker):
    def __init__(
        self, cfg: AppConfig, llm: ILLMProvider, factory: sessionmaker[Session], fetcher: ArticleTextFetcher | None = None,
    ) -> None:
        self.cfg, self.fc = cfg, cfg.factcheck
        self.llm, self.factory = llm, factory
        self.fetcher = fetcher if self.fc.fetch_full_text else None
        self.resolver = SourceResolver(cfg.news)
        self._ignore = ignore_tokens(cfg.news)

    # ------------------------------------------------------------------ API
    def check(self, story_id: int, *, force: bool = False) -> FactCheckResult:
        docs, title = self._select_sources(story_id)
        self._enrich_with_full_text(docs)
        resolve = getattr(self.llm, "resolve_model", None)
        model = resolve() if callable(resolve) else getattr(self.llm, "model", None)
        input_hash = self._input_hash(docs, model)
        if not force:
            cached = self._cached(story_id, input_hash)
            if cached:
                return cached

        messages = render_prompt(
            "fact_check", story_title=title, max_claims=self.fc.max_claims, no_think=self.fc.disable_thinking,
            sources=[{"id": d.id, "name": d.name, "title": d.title, "published": d.published, "text": d.text, "level": d.level} for d in docs],
        )
        log.info("Fact-check du sujet #%d (%d sources) via le LLM", story_id, len(docs))
        extraction: Extraction = self.llm.complete_json(
            messages, schema=Extraction, temperature=0.0, max_tokens=self.fc.max_output_tokens
        )
        model = getattr(self.llm, "last_model", None) or model
        result = self._verify(story_id, title, docs, extraction, model)
        self._store(story_id, input_hash, result)
        return result

    # -------------------------------------------------------------- sources
    def _select_sources(self, story_id: int) -> tuple[list[SourceDoc], str]:
        with session_scope(self.factory) as s:
            story = s.get(Story, story_id)
            if story is None:
                raise TrumpNewsError(f"Sujet #{story_id} introuvable. Voir : run.bat news")
            arts = list(s.scalars(select(Article).where(Article.story_id == story_id)))
            title = story.title
        best: dict[str, tuple[tuple, Article, str]] = {}
        for a in arts:
            r = self.resolver.resolve(a.source_name, a.url)
            level = "full" if a.content and len(a.content) > 600 else "summary" if a.summary else "title"
            rank = (_LEVEL_RANK[level], r.reliability, as_utc(a.published_at).timestamp() if a.published_at else 0)
            if r.key not in best or rank > best[r.key][0]:
                best[r.key] = (rank, a, level)
        ordered = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[: self.fc.max_sources]
        docs = []
        for i, (key, (_, a, level)) in enumerate(ordered, 1):
            r = self.resolver.resolve(a.source_name, a.url)
            body = a.content if level == "full" else (a.summary or "")
            docs.append(SourceDoc(
                id=f"S{i}", article_id=a.id, key=key, name=r.display, url=a.url, title=a.title,
                published=f"{as_utc(a.published_at):%Y-%m-%d %H:%M}" if a.published_at else "date inconnue",
                text=body, level=level, reliability=r.reliability,
            ))
        return docs, title

    def _enrich_with_full_text(self, docs: list[SourceDoc]) -> None:
        if self.fetcher is not None:
            for d in docs:
                if d.level != "full":
                    text = self.fetcher.fetch(d.url)
                    if text:
                        d.text, d.level = text, "full"
                        with session_scope(self.factory) as s:  # mis en cache dans la base
                            art = s.get(Article, d.article_id)
                            if art:
                                art.content = text[:20000]
        per_source = min(self.fc.max_chars_per_source, max(300, self.fc.max_input_chars // max(1, len(docs))))
        for d in docs:
            body = d.text[:per_source]
            d.text = f"{d.title}. {body}".strip() if d.level != "full" or not body.startswith(d.title) else body
            d.text = d.text[: per_source + len(d.title) + 2]

    def _input_hash(self, docs: list[SourceDoc], model: str | None) -> str:
        h = hashlib.sha256()
        for d in docs:
            h.update(f"{d.id}|{d.article_id}|{d.level}|{hashlib.sha256(d.text.encode()).hexdigest()}".encode())
        h.update(f"{model}|{prompt_hash('fact_check')}|{self.fc.model_dump_json()}".encode())
        return h.hexdigest()

    # --------------------------------------------------------- verification
    def _verify(self, story_id: int, title: str, docs: list[SourceDoc], ex: Extraction, model: str | None) -> FactCheckResult:
        fc = self.fc
        by_id = {d.id.upper(): d for d in docs}
        warnings: list[str] = []
        dropped: list[dict[str, str]] = []
        kept: list[dict] = []

        def verified(evs: list[EvidenceOut], claim: str, strict: bool, ignore: set[str]) -> list[tuple[SourceDoc, str]]:
            out: dict[str, tuple[SourceDoc, str]] = {}
            for ev in evs:
                d = by_id.get(ev.source.strip().upper())
                if ev.quote.strip().endswith("?"):
                    warnings.append(f"Citation sous forme de question ignorée (une question n'affirme rien) : « {ev.quote[:80]} »")
                elif d is None:
                    warnings.append(f"Source inexistante citée par le LLM : {ev.source!r}")
                elif not quote_in_text(ev.quote, d.text, min_words=fc.min_quote_words, ratio=fc.quote_match_ratio):
                    warnings.append(f"Citation introuvable dans {d.id} ({d.name}) : « {ev.quote[:80]} »")
                elif strict and not numbers_grounded(claim, d.text):
                    warnings.append(f"Chiffre de l'affirmation absent de {d.id} ({d.name}) : « {claim[:80]} »")
                elif strict and fc.require_quote_support and (missing := unsupported_terms(claim, ev.quote, ignore)):
                    warnings.append(f"Citation hors sujet dans {d.id} ({d.name}) : elle ne mentionne pas "
                                    f"{', '.join(missing[:4])} — « {claim[:70]} »")
                elif strict and fc.require_quote_support and not shares_content(claim, ev.quote, ignore):
                    warnings.append(f"Citation hors sujet dans {d.id} ({d.name}) : aucun mot en commun avec « {claim[:70]} »")
                else:
                    out.setdefault(d.id, (d, ev.quote))
            return list(out.values())

        for c in ex.claims[: fc.max_claims * 2]:
            text = c.claim.strip()
            if len(text.split()) < 4:
                dropped.append({"claim": text, "reason": "affirmation trop courte"})
                continue
            # sujet principal (mots-clés du suivi) et orateur : souvent « I » / « he » dans la citation
            ignore = self._ignore | {w.lower() for w in re.findall(r"[\w']+", c.speaker or "")}
            sup = verified(c.supporting, text, True, ignore)
            con = verified(c.contradicting, text, False, ignore)
            if not sup:
                dropped.append({"claim": text, "reason": "aucune source avec citation vérifiable et sur le sujet"})
                continue
            toks = title_tokens(text, self._ignore)
            twin = next((k for k in kept if similarity(toks, k["tokens"]) >= 0.7), None)
            if twin:  # doublon : on fusionne les sources
                twin["sup"] = {**{d.id: (d, q) for d, q in twin["sup"]}, **{d.id: (d, q) for d, q in sup}}.values()
                twin["sup"] = list(twin["sup"])
                twin["con"] = list({**{d.id: (d, q) for d, q in twin["con"]}, **{d.id: (d, q) for d, q in con}}.values())
                continue
            kept.append({"c": c, "text": text, "sup": sup, "con": con, "tokens": toks})

        claims: list[ClaimResult] = []
        for k in kept[: fc.max_claims]:
            status, conf, notes = assess(k["c"].kind, k["sup"], k["con"], fc)
            ev = lambda pairs: [Evidence(d.id, d.key, d.name, d.url, q) for d, q in pairs]  # noqa: E731
            claims.append(ClaimResult(
                claim=k["text"], kind=k["c"].kind, status=status, confidence=conf, speaker=k["c"].speaker,
                supporting=ev(k["sup"]), contradicting=ev(k["con"]), notes=notes, phrasing=PHRASING[status],
            ))
        verdict, conf = verdict_for(claims)
        used = {e.source_id for c in claims for e in c.supporting}
        used_docs = [d for d in docs if d.id in used]
        if verdict != "BLOCKED" and len(used_docs) == 1 and used_docs[0].level == "title":
            verdict = "BLOCKED"
            warnings.append("Une seule source, et seulement son titre : insuffisant pour produire une vidéo.")
        if not claims:
            warnings.append("Aucune affirmation vérifiable : sujet bloqué.")
        if docs and all(d.level == "title" for d in docs):
            warnings.append("Seuls les titres sont disponibles : fiabilité limitée.")
        return FactCheckResult(
            story_id=story_id, story_title=title, verdict=verdict, confidence=conf, model=model, claims=claims,
            sources=[d.public() for d in docs], warnings=warnings, dropped=dropped, llm_claims=len(ex.claims),
        )

    # ---------------------------------------------------------- persistence
    def _cached(self, story_id: int, input_hash: str) -> FactCheckResult | None:
        with session_scope(self.factory) as s:
            row = s.scalar(select(FactCheck).where(FactCheck.story_id == story_id).order_by(FactCheck.id.desc()))
            if row and row.input_hash == input_hash:
                return FactCheckResult.from_dict(row.data, from_cache=True)
        return None

    def _store(self, story_id: int, input_hash: str, result: FactCheckResult) -> None:
        with session_scope(self.factory) as s:
            s.add(FactCheck(
                story_id=story_id, input_hash=input_hash, model=result.model, verdict=result.verdict,
                confidence=result.confidence, data=result.to_dict(),
            ))
            story = s.get(Story, story_id)
            if story and story.status in ("NEW", "FACT_CHECKED"):
                story.status = "FACT_CHECKED"
            for art in s.scalars(select(Article).where(Article.story_id == story_id)):
                art.fact_status = result.verdict


def latest_verdict(factory: sessionmaker[Session], story_id: int) -> tuple[str, float] | None:
    with session_scope(factory) as s:
        row = s.scalar(select(FactCheck).where(FactCheck.story_id == story_id).order_by(FactCheck.id.desc()))
        return (row.verdict, row.confidence) if row else None
