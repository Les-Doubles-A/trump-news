"""Publication d'une vidéo sur les plateformes activées : garde-fous -> métadonnées -> envoi -> table `publications`.

Une vidéo n'est publiée que si :
- son dernier contrôle qualité est READY et ce n'est pas un aperçu ;
- le fact-check du sujet n'est pas BLOCKED et le script n'a pas été refusé ;
- le script a été approuvé par un humain (run.bat approve), sauf si publishing.require_approval vaut false ;
- elle n'est pas déjà publiée (ou en cours de traitement) sur cette plateforme : jamais de doublon.
Chaque tentative, réussie ou non, est enregistrée ; une plateforme en échec n'empêche pas les autres.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.errors import PublisherError, PublisherUnavailableError, TrumpNewsError
from app.core.interfaces import IPublisher, PublishRequest, PublishResult
from app.database.models import FactCheck, Publication, PublicationStatus, Script, Video, VideoReport, VideoStatus
from app.database.session import session_scope
from app.publishing.metadata import build_texts
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)
PLATFORMS = ("youtube", "tiktok", "instagram")


@dataclass
class PublishPlan:
    """Ce qui serait envoyé, sans rien envoyer (run.bat publish N --dry-run)."""
    video_id: int
    request: PublishRequest | None
    platforms: list[str]
    skipped: dict[str, str] = field(default_factory=dict)    # plateforme -> raison
    problems: list[str] = field(default_factory=list)        # garde-fous non respectés : rien n'est publié

    @property
    def ok(self) -> bool:
        return not self.problems and self.request is not None


@dataclass
class PublicationOutcome:
    platform: str
    status: str                      # PUBLISHED | PENDING | FAILED
    url: str | None = None
    detail: str = ""
    retryable: bool = False


@dataclass
class PublicationRow:
    id: int
    video_id: int
    platform: str
    status: str
    url: str | None
    platform_video_id: str | None
    error: str | None
    created_at: object
    published_at: object


class PublishingService:
    def __init__(self, cfg: AppConfig, factory: sessionmaker[Session], publishers: Callable[[str], IPublisher]) -> None:
        self.cfg, self.factory, self._publishers = cfg, factory, publishers

    def enabled_platforms(self) -> list[str]:
        return [p for p in PLATFORMS if getattr(self.cfg.publishing, p).enabled]

    # -------------------------------------------------------------- contrôle
    def prepare(self, video_id: int, platforms: list[str] | None = None) -> PublishPlan:
        targets = list(dict.fromkeys(platforms or self.enabled_platforms()))
        plan = PublishPlan(video_id, None, [])
        if not targets:
            plan.problems.append("Aucune plateforme activée : run.bat set-option publishing.youtube.enabled true "
                                 "(ou tiktok, instagram), ou précisez --platform.")
        for p in targets:
            if p not in PLATFORMS:
                plan.problems.append(f"Plateforme inconnue : {p} (possibles : {', '.join(PLATFORMS)}).")
        with session_scope(self.factory) as s:
            v = s.get(Video, video_id)
            if v is None:
                raise TrumpNewsError(f"Vidéo #{video_id} introuvable. Voir : run.bat videos")
            rep = s.scalar(select(VideoReport).where(VideoReport.video_id == video_id).order_by(VideoReport.id.desc()))
            sc = s.get(Script, v.script_id)
            fc = s.scalar(select(FactCheck.verdict).where(FactCheck.story_id == v.story_id).order_by(FactCheck.id.desc()))
            data = dict(rep.data) if rep else {}
            if rep is None or rep.status != VideoStatus.READY.value:
                plan.problems.append(f"Contrôle qualité : {rep.status if rep else 'absent'} (READY exigé). "
                                     f"Voir : run.bat video-check {video_id}")
            if data.get("preview"):
                plan.problems.append("C'est un aperçu 540x960 : montez la version finale (run.bat render sans --preview).")
            if not v.video_path or not Path(v.video_path).exists():
                plan.problems.append(f"Fichier vidéo introuvable : {v.video_path or '-'}")
            if fc == "BLOCKED":
                plan.problems.append("Le fact-check du sujet est BLOCKED.")
            if sc is None or sc.status == "REJECTED":
                plan.problems.append("Le script a été refusé à la relecture.")
            elif self.cfg.publishing.require_approval and sc.status != "APPROVED":
                plan.problems.append(f"Script #{sc.id} non approuvé (statut {sc.status}). Relisez-le puis : "
                                     f"run.bat approve {sc.id} --verifie --note \"...\"")
            done = {r.platform: r.status for r in s.scalars(select(Publication).where(
                Publication.video_id == video_id,
                Publication.status.in_([PublicationStatus.PUBLISHED.value, PublicationStatus.PENDING.value])))}
            if sc is not None and v.video_path:
                ctx = data.get("context") or {}
                title, description, caption, tags = build_texts(
                    hook=sc.hook or "", full_script=sc.full_script or "", source_refs=sc.source_refs,
                    credits=data.get("credits") or [], ai_content=bool(ctx.get("ai_shots")),
                    hashtags=self.cfg.publishing.hashtags, language=sc.language)
                plan.request = PublishRequest(Path(v.video_path), title, description, caption, tags, sc.language,
                                              bool(ctx.get("ai_shots")), v.duration or 0.0)
        for p in targets:
            if p in done:
                plan.skipped[p] = "déjà publiée" if done[p] == PublicationStatus.PUBLISHED.value else "publication en cours"
            elif p in PLATFORMS:
                plan.platforms.append(p)
        return plan

    # ------------------------------------------------------------ publication
    def publish(self, video_id: int, platforms: list[str] | None = None) -> tuple[PublishPlan, list[PublicationOutcome]]:
        plan = self.prepare(video_id, platforms)
        if not plan.ok:
            raise TrumpNewsError(f"Vidéo #{video_id} non publiée :\n- " + "\n- ".join(plan.problems))
        outcomes = []
        for p in plan.platforms:
            log.info("Publication de la vidéo #%d sur %s", video_id, p)
            try:
                res = self._publishers(p).publish(plan.request)
            except TrumpNewsError as exc:
                retry = isinstance(exc, PublisherUnavailableError) or not isinstance(exc, PublisherError)
                log.error("Publication de la vidéo #%d sur %s échouée : %s", video_id, p, exc)
                self._record(video_id, PublishResult(p, PublicationStatus.FAILED.value), error=str(exc))
                outcomes.append(PublicationOutcome(p, PublicationStatus.FAILED.value, None, str(exc), retry))
                continue
            self._record(video_id, res)
            outcomes.append(PublicationOutcome(p, res.status, res.url, res.detail))
        return plan, outcomes

    def refresh_pending(self) -> list[PublicationOutcome]:
        """Suit les publications restées en cours de traitement chez la plateforme (TikTok, Instagram)."""
        with session_scope(self.factory) as s:
            rows = [(r.id, r.platform, r.platform_video_id) for r in s.scalars(
                select(Publication).where(Publication.status == PublicationStatus.PENDING.value))]
        out = []
        for pid, platform, ext_id in rows:
            try:
                res = self._publishers(platform).status(ext_id or "")
            except TrumpNewsError as exc:
                if isinstance(exc, PublisherError) and not isinstance(exc, PublisherUnavailableError):
                    self._update(pid, PublishResult(platform, PublicationStatus.FAILED.value, ext_id), error=str(exc))
                    out.append(PublicationOutcome(platform, PublicationStatus.FAILED.value, None, str(exc)))
                else:
                    out.append(PublicationOutcome(platform, PublicationStatus.PENDING.value, None, str(exc), True))
                continue
            self._update(pid, res)
            out.append(PublicationOutcome(platform, res.status, res.url, res.detail))
        return out

    def list(self, limit: int = 30) -> list[PublicationRow]:
        with session_scope(self.factory) as s:
            return [PublicationRow(r.id, r.video_id, r.platform, r.status, r.url, r.platform_video_id, r.error,
                                   r.created_at, r.published_at)
                    for r in s.scalars(select(Publication).order_by(Publication.id.desc()).limit(limit))]

    # ------------------------------------------------------------ internals
    def _record(self, video_id: int, res: PublishResult, error: str | None = None) -> None:
        with session_scope(self.factory) as s:
            row = Publication(video_id=video_id, platform=res.platform)
            s.add(row)
            s.flush()
            pid = row.id
        self._update(pid, res, error)

    def _update(self, pid: int, res: PublishResult, error: str | None = None) -> None:
        with session_scope(self.factory) as s:
            row = s.get(Publication, pid)
            row.status, row.error = res.status, (error or None)
            row.platform_video_id = res.platform_video_id or row.platform_video_id
            row.url = res.url or row.url
            if res.status == PublicationStatus.PUBLISHED.value:
                row.published_at = utcnow()
                video = s.get(Video, row.video_id)
                if video is not None:
                    video.status = VideoStatus.PUBLISHED.value
