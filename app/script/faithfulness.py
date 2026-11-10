"""Seconde passe : un LLM traque toute information ajoutée au-delà des affirmations citées."""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.config.schema import ScriptSection
from app.core.interfaces import ILLMProvider
from app.prompts import render_prompt
from app.script.models import ClaimInfo, Violation

log = logging.getLogger(__name__)


class JudgeSegment(BaseModel):
    index: int
    faithful: bool
    unsupported: str = ""


class Judgement(BaseModel):
    segments: list[JudgeSegment] = Field(default_factory=list)


def judge(llm: ILLMProvider, segments, claims: dict[str, ClaimInfo], cfg: ScriptSection) -> list[Violation]:
    payload = [
        {"index": i, "type": s.type, "text": s.text,
         "claims": [claims[c].__dict__ for c in s.claims if c in claims]}
        for i, s in enumerate(segments)
    ]
    try:
        res = llm.complete_json(render_prompt("script_verify", segments=payload, no_think=cfg.disable_thinking),
                                schema=Judgement, temperature=0.0, max_tokens=cfg.max_output_tokens)
    except Exception as exc:  # la vérification ne doit jamais faire planter la génération
        log.warning("Vérification de fidélité indisponible : %s", exc)
        return [Violation("judge_failed", f"vérification de fidélité indisponible ({exc})", None, hard=False)]
    out = []
    for j in res.segments:
        if 0 <= j.index < len(segments) and not j.faithful and j.unsupported.strip():
            out.append(Violation("unfaithful", f"ajoute une information absente des affirmations citées : « {j.unsupported.strip()[:120]} »", j.index))
    return out
