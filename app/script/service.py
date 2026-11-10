"""Génération du script : dossier de fact-check -> LLM (segments rattachés aux affirmations) -> contrôle -> correction."""
from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.errors import TrumpNewsError
from app.core.interfaces import ChatMessage, ILLMProvider
from app.database.models import FactCheck, Script
from app.database.session import session_scope
from app.factcheck.models import FactCheckResult
from app.prompts import render_prompt
from app.script.models import STYLES, TONES, ClaimInfo, GeneratedScript, ScriptBlockedError, Violation
from app.script.autofix import fix_attributions
from app.script.faithfulness import judge
from app.script.validate import count_words, supported_duration, target_words, validate

log = logging.getLogger(__name__)
LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish"}
OPINION_LABEL = {"en": "In our view", "fr": "À notre avis", "es": "En nuestra opinión"}


class SegmentOut(BaseModel):
    section: Literal["hook", "context", "main", "details", "impact", "conclusion"]
    type: Literal["fact", "attributed", "disputed", "opinion", "context"]
    text: str
    claims: list[str] = Field(default_factory=list)


class VisualOut(BaseModel):
    segment: int = 0
    description: str


class ScriptOut(BaseModel):
    segments: list[SegmentOut]
    visual_suggestions: list[VisualOut] = Field(default_factory=list)


class ScriptGenerator:
    def __init__(self, cfg: AppConfig, llm: ILLMProvider, factory: sessionmaker[Session]) -> None:
        self.cfg, self.sc = cfg, cfg.script
        self.llm, self.factory = llm, factory

    # ------------------------------------------------------------------ API
    def generate(
        self, story_id: int, *, style: str | None = None, tone: str | None = None,
        duration_s: int | None = None, language: str | None = None,
    ) -> GeneratedScript:
        style = style or self.cfg.editorial.style
        tone = tone or self.cfg.editorial.tone
        duration_s = duration_s or self.cfg.editorial.duration_s
        language = language or self.cfg.app.language
        if style not in STYLES or tone not in TONES or language not in LANGUAGE_NAMES:
            raise TrumpNewsError(f"Paramètres invalides (style={style}, ton={tone}, langue={language}).")

        fc = self._load_fact_check(story_id)
        claims = self._claims(fc)
        notes: list[str] = []
        if self.sc.adapt_duration:
            allowed = supported_duration(len(claims), language, self.sc)
            if duration_s > allowed:
                notes.append(f"Durée ramenée de {duration_s} s à {allowed} s : seulement {len(claims)} affirmation(s) vérifiée(s), "
                             "pas assez de matière pour davantage sans inventer.")
                duration_s = allowed
        target = target_words(duration_s, language, self.sc)
        recent_hooks, recent_scripts = self._recent(story_id)

        base = render_prompt(
            "script_generation", language_name=LANGUAGE_NAMES[language], style=style, tone=tone, duration_s=duration_s,
            target_words=target, min_words=round(target * self.sc.length_low), max_words=round(target * self.sc.length_high),
            hook_max_words=self.sc.hook_max_words, max_opinion=self.sc.max_opinion_segments,
            opinion_label=OPINION_LABEL[language], recent_hooks=recent_hooks, story_title=fc.story_title,
            claims=[c.__dict__ for c in claims.values()], no_think=self.sc.disable_thinking,
        )
        convo, out, violations, attempts = list(base), None, [], 0
        auto_notes: list[str] = []
        for attempts in range(1, self.sc.max_attempts + 1):
            out = self.llm.complete_json(convo, schema=ScriptOut, temperature=self.sc.temperature,
                                         max_tokens=self.sc.max_output_tokens)
            violations = validate(out.segments, claims, story_title=fc.story_title, language=language, tone=tone,
                                  duration_s=duration_s, cfg=self.sc, recent_scripts=recent_scripts)
            hard = [v for v in violations if v.hard]
            auto_notes = []
            if hard and self.sc.auto_attribution:
                fixed, fix_notes = fix_attributions(list(out.segments), violations, claims, language)
                if fix_notes:
                    candidate = out.model_copy(update={"segments": fixed})
                    again = validate(candidate.segments, claims, story_title=fc.story_title, language=language, tone=tone,
                                     duration_s=duration_s, cfg=self.sc, recent_scripts=recent_scripts)
                    if sum(v.hard for v in again) < len(hard):      # on ne garde la correction que si elle améliore
                        out, violations = candidate, again
                        hard = [v for v in violations if v.hard]
                        auto_notes = fix_notes
            if not hard and self.sc.llm_verify:
                verdicts = judge(self.llm, out.segments, claims, self.sc)
                violations += verdicts
                hard = [v for v in verdicts if v.hard]
            log.info("Script tentative %d/%d : %d erreur(s) bloquante(s), %d avertissement(s)",
                     attempts, self.sc.max_attempts, len(hard), len(violations) - len(hard))
            if not hard:
                break
            convo = list(base) + [
                ChatMessage("assistant", json.dumps(out.model_dump(), ensure_ascii=False)),
                ChatMessage("user", "Your script was REJECTED by the checker. Fix ALL of these problems and return the complete "
                                    "corrected JSON, nothing else:\n" + "\n".join(f"- {v}" for v in hard[:12])),
            ]
        assert out is not None
        return self._finalize(story_id, fc, claims, out, violations, attempts, style, tone, duration_s, language,
                              recent_scripts, notes + auto_notes)

    # ----------------------------------------------------------- internals
    def _load_fact_check(self, story_id: int) -> FactCheckResult:
        with session_scope(self.factory) as s:
            row = s.scalar(select(FactCheck).where(FactCheck.story_id == story_id).order_by(FactCheck.id.desc()))
            if row is None:
                raise TrumpNewsError(f"Aucun fact-check pour le sujet #{story_id}. Lancez d'abord : run.bat fact-check {story_id}")
            fc = FactCheckResult.from_dict(row.data)
        if fc.verdict == "BLOCKED":
            raise ScriptBlockedError(f"Sujet #{story_id} bloqué par le fact-check : aucune affirmation vérifiable.")
        return fc

    def _claims(self, fc: FactCheckResult) -> dict[str, ClaimInfo]:
        usable = [c for c in fc.claims if c.status != "UNKNOWN"]
        if not usable:
            raise ScriptBlockedError("Aucune affirmation exploitable pour écrire un script.")
        return {
            f"C{i}": ClaimInfo(f"C{i}", c.claim, c.kind, c.status, c.speaker,
                               list(dict.fromkeys(e.source_name for e in c.supporting + c.contradicting)))
            for i, c in enumerate(usable, 1)
        }

    def _recent(self, story_id: int) -> tuple[list[str], list[str]]:
        with session_scope(self.factory) as s:
            rows = list(s.execute(select(Script.story_id, Script.hook, Script.full_script).order_by(Script.id.desc()).limit(40)))
        hooks = [h for _, h, _ in rows if h][: self.sc.recent_hooks_to_avoid]
        others = [t for sid, _, t in rows if sid != story_id and t]
        return hooks, others

    def _finalize(self, story_id, fc, claims, out, violations, attempts, style, tone, duration_s, language, recent_scripts, notes=()):
        segments = list(out.segments)
        pruned = 0
        hard_idx = {v.segment for v in violations if v.hard and v.segment is not None}
        if any(v.hard for v in violations):
            if 0 in hard_idx:
                raise ScriptBlockedError(
                    "Le hook n'a pas pu être rendu conforme après plusieurs essais : " +
                    "; ".join(str(v) for v in violations if v.hard)[:400])
            keep = [i for i, _ in enumerate(segments) if i not in hard_idx]
            pruned = len(segments) - len(keep)
            segments = [segments[i] for i in keep]
            remap = {old: new for new, old in enumerate(keep)}
            out.visual_suggestions = [VisualOut(segment=remap[v.segment], description=v.description)
                                      for v in out.visual_suggestions if v.segment in remap]
            if not any(s.section == "main" for s in segments):
                raise ScriptBlockedError("Plus aucun segment principal valide après correction automatique.")
            violations = validate(segments, claims, story_title=fc.story_title, language=language, tone=tone,
                                  duration_s=duration_s, cfg=self.sc, recent_scripts=recent_scripts)
        warnings = list(notes) + [str(v) for v in violations]
        if pruned:
            warnings.insert(0, f"{pruned} segment(s) non conforme(s) supprimé(s) automatiquement.")

        def joined(sections):
            return " ".join(s.text.strip() for s in segments if s.section in sections)

        hook, ending = joined({"hook"}), joined({"conclusion"})
        body = joined({"context", "main", "details", "impact"})
        full = " ".join(s.text.strip() for s in segments)
        used_ids = list(dict.fromkeys(c for s in segments for c in s.claims if c in claims))
        by_text = {c.claim: c for c in fc.claims}
        refs, used = [], []
        for cid in used_ids:
            info = claims[cid]
            full_claim = by_text.get(info.text)
            used.append({"id": cid, "claim": info.text, "kind": info.kind, "status": info.status})
            refs.append({"claim_id": cid, "claim": info.text, "status": info.status, "sources": [
                {"name": e.source_name, "url": e.url, "quote": e.quote} for e in (full_claim.supporting if full_claim else [])]})
        needs_review = bool(pruned) or fc.verdict != "SAFE" or bool(violations) or any(c["status"] == "DISPUTED" for c in used)
        words = count_words(full)
        gs = GeneratedScript(
            id=0, story_id=story_id, style=style, tone=tone, language=language, duration_target=duration_s,
            segments=[{"index": i, **s.model_dump()} for i, s in enumerate(segments)], hook=hook, body=body, ending=ending,
            full_script=full, claims_used=used, source_references=refs,
            visual_suggestions=[v.model_dump() for v in out.visual_suggestions],
            warnings=warnings, status="NEEDS_REVIEW" if needs_review else "DRAFT", word_count=words,
            est_seconds=round(words / self.sc.words_per_second.get(language, 2.5), 1), attempts=attempts,
        )
        with session_scope(self.factory) as s:
            row = Script(
                story_id=story_id, style=style, tone=tone, language=language, duration_target=duration_s, hook=hook, body=body,
                ending=ending, full_script=full, status=gs.status, source_refs=refs,
                claims={"segments": gs.segments, "claims": used, "visuals": gs.visual_suggestions, "warnings": warnings},
            )
            s.add(row)
            s.flush()
            gs.id = row.id
        return gs


def list_scripts(factory: sessionmaker[Session], limit: int = 20) -> list[Script]:
    with session_scope(factory) as s:
        return list(s.scalars(select(Script).order_by(Script.id.desc()).limit(limit)))


def get_script(factory: sessionmaker[Session], script_id: int) -> Script | None:
    with session_scope(factory) as s:
        return s.get(Script, script_id)
