"""Relecture humaine des scripts : ce qui permet à une vidéo NEEDS_REVIEW de devenir publiable.

Règles :
- approuver exige d'avoir vérifié les citations et les sources (case à cocher / --verifie) ;
- une note écrite est obligatoire quand le verdict du fact-check est CAUTION ;
- un sujet BLOCKED ne peut jamais être approuvé ;
- l'approbation ne lève QUE le contrôle « script validé » : les contrôles techniques des vidéos restent bloquants ;
- chaque décision est tracée dans la table `reviews`, puis le contrôle qualité des vidéos du script est refait.
"""
from __future__ import annotations

import getpass
import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import TrumpNewsError
from app.database.models import FactCheck, Review, Script, Video, VideoStatus
from app.database.session import session_scope

log = logging.getLogger(__name__)


@dataclass
class ReviewOutcome:
    script_id: int
    decision: str
    video_statuses: dict[int, str] = field(default_factory=dict)   # vidéo -> statut après nouveau contrôle
    warnings: list[str] = field(default_factory=list)


@dataclass
class ScriptDetail:
    """Tout ce qu'il faut voir pour relire un script : phrases, affirmations, citations exactes, sources, historique."""
    id: int
    story_id: int
    status: str
    language: str
    style: str
    duration_target: int
    verdict: str | None
    segments: list[dict]
    claims: dict[str, dict]           # id -> {claim, kind, status, sources: [{name, url, quote}]}
    warnings: list[str]
    reviews: list[dict]


class ReviewService:
    def __init__(self, factory: sessionmaker[Session], *, video_service_factory: Callable | None = None,
                 reviewer: str | None = None) -> None:
        self.factory = factory
        self._videos = video_service_factory
        self.reviewer = reviewer or _whoami()

    # --------------------------------------------------------------- lecture
    def detail(self, script_id: int) -> ScriptDetail:
        with session_scope(self.factory) as s:
            sc = s.get(Script, script_id)
            if sc is None:
                raise TrumpNewsError(f"Script #{script_id} introuvable. Voir : run.bat scripts")
            verdict = s.scalar(select(FactCheck.verdict).where(FactCheck.story_id == sc.story_id).order_by(FactCheck.id.desc()))
            data = sc.claims or {}
            refs = {r["claim_id"]: r for r in (sc.source_refs or [])}
            claims = {c["id"]: {**c, "sources": refs.get(c["id"], {}).get("sources", [])} for c in data.get("claims", [])}
            reviews = [{"decision": r.decision, "note": r.note, "reviewer": r.reviewer, "at": r.created_at,
                        "verdict": r.factcheck_verdict}
                       for r in s.scalars(select(Review).where(Review.script_id == script_id).order_by(Review.id.desc()))]
            return ScriptDetail(sc.id, sc.story_id, sc.status, sc.language, sc.style, sc.duration_target, verdict,
                                data.get("segments", []), claims, data.get("warnings", []), reviews)

    def pending_count(self) -> int:
        with session_scope(self.factory) as s:
            return len(list(s.scalars(select(Script.id).where(Script.status == "NEEDS_REVIEW"))))

    # -------------------------------------------------------------- décisions
    def approve(self, script_id: int, *, sources_checked: bool, note: str = "") -> ReviewOutcome:
        note = (note or "").strip()
        if not sources_checked:
            raise TrumpNewsError("Approbation refusée : confirmez d'abord avoir vérifié les citations et les sources.")
        verdict = self._verdict(script_id)
        if verdict == "BLOCKED":
            raise TrumpNewsError("Approbation impossible : le fact-check de ce sujet est BLOCKED.")
        if verdict in (None, "CAUTION") and not note:
            raise TrumpNewsError("Note obligatoire : le fact-check est "
                                 f"{verdict or 'introuvable'}. Indiquez ce que vous avez vérifié.")
        return self._decide(script_id, "APPROVED", note, sources_checked, verdict)

    def approve_automatically(self, script_id: int, note: str) -> ReviewOutcome:
        """Approbation par le pilote automatique (avertissements mineurs seulement, voir app/autopilot).
        Tracée honnêtement : sources_checked=False, relecteur = celui donné à la construction."""
        verdict = self._verdict(script_id)
        if verdict == "BLOCKED":
            raise TrumpNewsError("Approbation impossible : le fact-check de ce sujet est BLOCKED.")
        return self._decide(script_id, "APPROVED", note, False, verdict)

    def reject(self, script_id: int, note: str = "") -> ReviewOutcome:
        return self._decide(script_id, "REJECTED", (note or "").strip(), False, self._verdict(script_id))

    # ------------------------------------------------------------ internals
    def _verdict(self, script_id: int) -> str | None:
        with session_scope(self.factory) as s:
            sc = s.get(Script, script_id)
            if sc is None:
                raise TrumpNewsError(f"Script #{script_id} introuvable. Voir : run.bat scripts")
            return s.scalar(select(FactCheck.verdict).where(FactCheck.story_id == sc.story_id).order_by(FactCheck.id.desc()))

    def _decide(self, script_id: int, decision: str, note: str, checked: bool, verdict: str | None) -> ReviewOutcome:
        with session_scope(self.factory) as s:
            sc = s.get(Script, script_id)
            sc.status = decision
            s.add(Review(script_id=script_id, decision=decision, note=note or None, reviewer=self.reviewer,
                         sources_checked=checked, factcheck_verdict=verdict))
            videos = [v.id for v in s.scalars(select(Video).where(Video.script_id == script_id, Video.video_path.is_not(None),
                                                                  Video.status != VideoStatus.FAILED.value))]
        log.info("Script #%d %s par %s", script_id, "approuvé" if decision == "APPROVED" else "refusé", self.reviewer)
        out = ReviewOutcome(script_id, decision)
        if self._videos and videos:
            svc = self._videos()
            for vid in videos:
                try:
                    out.video_statuses[vid] = svc.check(vid).status
                except TrumpNewsError as exc:
                    out.warnings.append(f"Vidéo #{vid} non recontrôlée : {exc}")
        return out


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "inconnu"
