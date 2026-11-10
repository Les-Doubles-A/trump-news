"""Pilote automatique : actualités -> choix des sujets -> pipeline complet -> publication YouTube, sans relecture humaine.

Utilisé par le bouton « Tout générer et publier » de l'interface et par `run.bat autopilot` (plus tard par le scheduler).
Personne ne relit : les garde-fous sont donc plus stricts qu'en publication manuelle.
- Seule une vidéo READY est publiée : tous les contrôles bloquants sont passés.
- Un script NEEDS_REVIEW n'est approuvé par le pilote (autopilot.auto_approve_minor) que si TOUS ses avertissements
  sont mineurs (MINOR_WARNINGS : longueur, attribution ajoutée par le code), qu'il ne s'appuie sur aucune
  affirmation DISPUTED et que la vidéo n'échoue à aucun autre contrôle. L'approbation est tracée dans `reviews`
  (relecteur « pilote automatique », sources_checked=False). Tout autre avertissement : sujet écarté, à relire.
- Fact-check SAFE, ou CAUTION si autopilot.allow_caution vaut true ; jamais BLOCKED. Un verdict refusé arrête la
  chaîne dès le fact-check (pas de montage inutile) et le sujet reste candidat si le réglage change.
- Une vidéo récente écartée est réexaminée à chaque lancement (sans LLM ni montage) : un changement de réglage ou
  une approbation manuelle la rend publiable sans la remonter.
- Plafond de publications sur 24 h glissantes (autopilot.max_per_day) et par lancement (autopilot.videos_per_run).
- Un sujet déjà monté (à la main ou par le pilote) n'est jamais repris : pas de doublon.
- Une erreur technique (LM Studio, FFmpeg, réseau) arrête le lancement au lieu de « brûler » les sujets suivants ;
  le sujet en cause sera réessayé au prochain lancement.
- Un envoi refusé pour une raison temporaire (quota YouTube) arrête le lancement ; la vidéo déjà montée est
  renvoyée en priorité au lancement suivant, sans être remontée.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from sqlalchemy import select

from app.core.errors import TrumpNewsError
from app.core.interfaces import IPublisher
from app.database.models import (AutopilotAttempt, FactCheck, Publication, PublicationStatus, Script, Story, Video,
                                 VideoReport, VideoStatus)
from app.database.session import session_scope
from app.news.cluster import ignore_tokens, similarity, title_tokens
from app.news.service import StoryRow, mark_story_used
from app.pipeline.runner import CancelToken, PipelineOptions, PipelineResult, PipelineRunner, Progress
from app.publishing.service import PublishingService
from app.scheduler.lock import run_lock
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)
PLATFORM = "youtube"
REVIEWER = "pilote automatique"
STEPS = 6                     # 5 étapes du pipeline + la publication
DONE = ("PUBLISHED", "PENDING", "SKIPPED", "REJECTED", "RETRY", "RECHECKED")   # sujets à ne plus remonter
# Avertissements sans risque pour l'exactitude : texte plus court ou plus long que la cible, attribution à la vraie
# source ajoutée par le code (autofix), durée réduite faute d'affirmations vérifiées.
MINOR_WARNINGS = ("attribution ajoutée automatiquement", " mots : visez ", "Durée ramenée de ")


@dataclass
class Attempt:
    story_id: int
    title: str
    status: str               # PUBLISHED | PENDING | SKIPPED | REJECTED | RETRY | FAILED | CANCELLED
    message: str
    video_id: int | None = None
    url: str | None = None


@dataclass
class AutopilotReport:
    run_id: str
    attempts: list[Attempt] = field(default_factory=list)
    message: str = ""
    privacy: str = ""

    @property
    def published(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status in ("PUBLISHED", "PENDING")]


class Autopilot:
    def __init__(self, ctx, *, progress: Progress | None = None, token: CancelToken | None = None,
                 pipeline: Callable[[int, Progress], PipelineResult] | None = None,
                 publisher: Callable[[str], IPublisher] | None = None, now=utcnow) -> None:
        self.ctx, self.cfg = ctx, ctx.cfg.autopilot
        self.progress = progress or (lambda i, n, msg: None)
        self.token = token or CancelToken()
        self._now = now
        self._pipeline = pipeline or (lambda sid, prog: PipelineRunner(ctx, progress=prog, token=self.token)
                                      .run(sid, PipelineOptions(allowed_verdicts=self.allowed_verdicts)))
        # Réglages de publication propres au pilote : YouTube seul, visibilité autopilot.youtube_privacy, pas
        # d'approbation humaine (remplacée par les contrôles ci-dessus). config.yaml n'est pas modifié.
        pub_cfg = ctx.cfg.model_copy(deep=True)
        pub_cfg.publishing.require_approval = False
        pub_cfg.publishing.youtube.enabled = True
        pub_cfg.publishing.youtube.privacy = self.cfg.youtube_privacy
        if publisher is None:
            from app.providers.publishers.factory import build_publisher

            def publisher(name: str) -> IPublisher:
                return build_publisher(name, pub_cfg, ctx.factory)
        self._publisher = publisher
        self.publishing = PublishingService(pub_cfg, ctx.factory, publisher)

    # ------------------------------------------------------------------ run
    def run(self) -> AutopilotReport:
        """Un seul lancement à la fois (verrou partagé avec la tâche planifiée) : AlreadyRunning sinon."""
        with run_lock(self.ctx.data_root / "autopilot.lock"):
            return self._run()

    def _run(self) -> AutopilotReport:
        rep = AutopilotReport(uuid.uuid4().hex[:12], privacy=self.cfg.youtube_privacy)
        st = self._publisher(PLATFORM).health_check()
        if not st.ok:      # avant de passer une heure à monter des vidéos impossibles à envoyer
            raise TrumpNewsError(f"YouTube n'est pas prêt : {st.detail}")
        left = self.cfg.max_per_day - self.published_last_24h()
        if left <= 0:
            rep.message = (f"Plafond atteint : {self.cfg.max_per_day} vidéo(s) publiée(s) sur les dernières 24 h "
                           "(autopilot.max_per_day). Rien n'a été lancé.")
            return rep
        target = min(self.cfg.videos_per_run, left)
        try:
            self._retry_pending(rep, target)
            if len(rep.published) < target and self.cfg.fetch_news:
                self._fetch_news()
            candidates = self.candidates() if len(rep.published) < target else []
            k = 0
            for story in candidates:
                if len(rep.published) >= target or k >= self.cfg.max_attempts:
                    break
                near = self.neighbor_of(story.title)    # refait à chaque sujet : compte les vidéos de ce lancement
                if near:
                    log.info("Pilote automatique : sujet #%d écarté, voisin de « %s » déjà publié", story.id, near)
                    continue
                self.token.check()
                k += 1
                aid = self._open(rep.run_id, story.id)  # RUNNING : repris au lancement suivant si celui-ci est coupé
                att = self._try(story, k, min(len(candidates), self.cfg.max_attempts))
                self._record(rep, att, aid)
                if att.status in ("FAILED", "RETRY", "CANCELLED"):
                    rep.message = att.message
                    break
        except TrumpNewsError as exc:   # annulation entre deux sujets, ou échec de la récupération des actualités
            rep.message = str(exc)
        if not rep.message:
            n = len(rep.published)
            rep.message = (f"{n} vidéo(s) publiée(s) sur YouTube ({rep.privacy})." if n else
                           "Aucune vidéo publiée : aucun sujet n'a passé tous les contrôles." if rep.attempts else
                           "Aucun nouveau sujet à traiter. Réessayez après de nouvelles actualités.")
        log.info("Pilote automatique %s : %s", rep.run_id, rep.message)
        return rep

    # ------------------------------------------------------------ sélection
    @property
    def allowed_verdicts(self) -> tuple[str, ...]:
        return ("SAFE", "CAUTION") if self.cfg.allow_caution else ("SAFE",)

    def candidates(self) -> list[StoryRow]:
        """Sujets récents, jamais montés, non BLOCKED, du meilleur score au moins bon."""
        svc, _ = self.ctx.news_service()
        rows = svc.list_stories(limit=100, hours=self.cfg.hours)
        with session_scope(self.ctx.factory) as s:
            taken = set(s.scalars(select(Video.story_id)))
            taken |= set(s.scalars(select(AutopilotAttempt.story_id).where(AutopilotAttempt.status.in_(DONE))))
        return [r for r in rows if r.id not in taken and r.score >= self.cfg.min_story_score
                and r.verdict != "BLOCKED" and (self.cfg.allow_caution or r.verdict != "CAUTION")]

    def neighbor_of(self, title: str) -> str | None:
        """Titre d'une vidéo publiée récemment (autopilot.neighbor_hours) sur la même affaire, sinon None.
        Comparaison des mots significatifs des titres, comme le regroupement des actualités."""
        if not self.cfg.neighbor_hours:
            return None
        since = self._now() - timedelta(hours=self.cfg.neighbor_hours)
        with session_scope(self.ctx.factory) as s:
            titles = list(s.scalars(select(Story.title).join(Video, Video.story_id == Story.id)
                                    .join(Publication, Publication.video_id == Video.id)
                                    .where(Publication.created_at >= since, Publication.status.in_(
                                        [PublicationStatus.PUBLISHED.value, PublicationStatus.PENDING.value]))))
        ignore = ignore_tokens(self.ctx.cfg.news)
        mine = title_tokens(title, ignore)
        for other in dict.fromkeys(titles):
            if similarity(mine, title_tokens(other, ignore)) >= self.cfg.neighbor_similarity:
                return other
        return None

    def published_last_24h(self) -> int:
        since = self._now() - timedelta(hours=24)
        with session_scope(self.ctx.factory) as s:
            return len(list(s.scalars(select(Publication.id).where(
                Publication.platform == PLATFORM, Publication.created_at >= since,
                Publication.status.in_([PublicationStatus.PUBLISHED.value, PublicationStatus.PENDING.value])))))

    # ------------------------------------------------------------ internals
    def _fetch_news(self) -> None:
        self.token.check()
        self.progress(0, STEPS, "Récupération des actualités…")
        svc, providers = self.ctx.news_service()
        if providers:
            svc.run()

    def _try(self, story: StoryRow, k: int, n: int) -> Attempt:
        label = f"Sujet {k}/{n} (#{story.id})"
        res = self._pipeline(story.id, lambda i, total, msg: self.progress(i, STEPS, f"{label} : {msg}"))
        att = Attempt(story.id, story.title, "SKIPPED", res.message, res.video_id)
        if res.status == "CANCELLED":
            att.status, att.message = "CANCELLED", "Pilote automatique annulé par l'utilisateur."
            return att
        if res.status == "FAILED":
            att.status = "FAILED"
            att.message = f"Erreur technique, lancement arrêté (sujet réessayé la prochaine fois) : {res.message}"
            return att
        if res.status == "NOT_ALLOWED":        # repris si le réglage change (statut absent de DONE)
            att.status = "NOT_ALLOWED"
            return att
        if res.status not in ("READY", "NEEDS_REVIEW") or not res.video_id:
            return att
        why = self._review(story.id, res.video_id)
        if why:
            att.status, att.message = "REJECTED", f"Vidéo #{res.video_id} non publiée, à relire dans l'écran Scripts : {why}"
            return att
        self.token.check()
        self.progress(STEPS, STEPS, f"{label} : publication sur YouTube…")
        return self._publish(att)

    def _review(self, story_id: int, video_id: int) -> str:
        """Remplace la relecture humaine par des règles strictes. Retourne la raison du refus, ou "" si publiable."""
        verdict = self._verdict(story_id)
        if verdict not in self.allowed_verdicts:
            return f"fact-check {verdict} (autopilot.allow_caution)."
        with session_scope(self.ctx.factory) as s:
            video = s.get(Video, video_id)
            if video.status == VideoStatus.READY.value:
                return ""
            sc = s.get(Script, video.script_id)
            script_id, script_status, data = sc.id, sc.status, dict(sc.claims or {})
            rep = s.scalar(select(VideoReport).where(VideoReport.video_id == video_id).order_by(VideoReport.id.desc()))
            quality = ((rep.data or {}).get("quality") or {}) if rep else {}
        failed = [f"{c['name']} : {c['detail']}" for c in quality.get("checks", []) if not c["ok"] and c.get("critical")
                  and c["name"] != "script validé"]
        if failed:
            return "contrôle qualité en échec (" + " ; ".join(failed) + ")."
        if quality.get("score", 0) < self.ctx.cfg.quality.min_score:
            return f"score qualité {quality.get('score', 0):.2f} sous le minimum {self.ctx.cfg.quality.min_score}."
        if script_status != "NEEDS_REVIEW":
            return f"contrôle qualité insuffisant (script {script_status})."
        if not self.cfg.auto_approve_minor:
            return "script avec avertissements (autopilot.auto_approve_minor = false)."
        serious = [w for w in data.get("warnings", []) if not any(m in w for m in MINOR_WARNINGS)]
        if serious:
            return "avertissement(s) du script : " + " ; ".join(serious[:3])
        if any(c.get("status") == "DISPUTED" for c in data.get("claims", [])):
            return "le script s'appuie sur une affirmation DISPUTED."
        minor = len(data.get("warnings", []))
        out = self.ctx.reviews_as(REVIEWER).approve_automatically(
            script_id, f"Approbation automatique : fact-check {verdict}, {minor} avertissement(s) mineur(s) seulement.")
        status = out.video_statuses.get(video_id)
        if status != VideoStatus.READY.value:
            return f"contrôle qualité après approbation automatique : {status or 'non refait'}."
        log.info("Pilote automatique : script #%d approuvé automatiquement (avertissements mineurs)", script_id)
        return ""

    def _publish(self, att: Attempt) -> Attempt:
        try:
            _, outcomes = self.publishing.publish(att.video_id, [PLATFORM])
        except TrumpNewsError as exc:          # garde-fou de publication non respecté
            att.status, att.message = "REJECTED", str(exc)
            return att
        if not outcomes:                       # déjà publiée ou en cours
            att.status, att.message = "REJECTED", f"Vidéo #{att.video_id} déjà publiée sur YouTube."
            return att
        o = outcomes[0]
        att.url, att.message = o.url, o.detail
        if o.status in ("PUBLISHED", "PENDING"):
            att.status = o.status
            att.message = o.detail or f"Vidéo #{att.video_id} publiée."
            mark_story_used(self.ctx.factory, att.story_id)
        elif o.retryable:
            att.status = "RETRY"
            att.message = f"Envoi impossible pour l'instant, vidéo #{att.video_id} renvoyée au prochain lancement : {o.detail}"
        else:
            att.status = "REJECTED"
            att.message = f"YouTube a refusé la vidéo #{att.video_id} : {o.detail}"
        return att

    def _retry_pending(self, rep: AutopilotReport, target: int) -> None:
        """Reprend d'abord les vidéos déjà montées, sans les remonter :
        - envoi échoué temporairement (quota) : RETRY ;
        - vidéo récente écartée (SKIPPED, REJECTED) : réexaminée avec les règles et réglages actuels (sans LLM ni
          montage) ; si la raison n'a pas changé, rien n'est réenregistré ;
        - sujet resté RUNNING (lancement précédent coupé) : sa vidéo est reprise si elle existe, sinon le sujet
          redevient candidat (INTERRUPTED)."""
        since = self._now() - timedelta(hours=self.cfg.hours)
        with session_scope(self.ctx.factory) as s:      # sous verrou : tout RUNNING vient d'un lancement coupé
            for a in s.scalars(select(AutopilotAttempt).where(AutopilotAttempt.status == "RUNNING")):
                vid = s.scalar(select(Video.id).where(Video.story_id == a.story_id).order_by(Video.id.desc()))
                if vid:
                    a.status, a.video_id, a.message = "SKIPPED", vid, "Lancement interrompu : vidéo déjà montée, reprise."
                else:
                    a.status, a.message = "INTERRUPTED", "Lancement interrompu avant le montage : sujet repris."
        with session_scope(self.ctx.factory) as s:
            rows = [(a.id, a.status, a.story_id, a.video_id, a.message, s.get(Story, a.story_id).title)
                    for a, v in s.execute(
                        select(AutopilotAttempt, Video).join(Video, Video.id == AutopilotAttempt.video_id).where(
                            (AutopilotAttempt.status == "RETRY")
                            | (AutopilotAttempt.status.in_(["SKIPPED", "REJECTED"]) & (AutopilotAttempt.created_at >= since)
                               & (Video.status != VideoStatus.PUBLISHED.value)))
                        .order_by(AutopilotAttempt.id))]
        for aid, old, sid, vid, old_msg, title in rows:
            if len(rep.published) >= target:
                return
            self.token.check()
            if old != "RETRY" and self.neighbor_of(title):
                continue                                         # même affaire qu'une vidéo publiée récemment
            att = Attempt(sid, title, "", "", vid)            # statut fixé par le réexamen ou par l'envoi
            if old != "RETRY":
                self.progress(STEPS, STEPS, f"Réexamen de la vidéo #{vid}…")
                why = self._review(sid, vid)
                if why:
                    att.status, att.message = "REJECTED", f"Vidéo #{vid} non publiée, à relire dans l'écran Scripts : {why}"
                    if old == "REJECTED" and att.message == old_msg:
                        continue                                 # rien de nouveau
            if att.status != "REJECTED":
                self.progress(STEPS, STEPS, f"Envoi de la vidéo #{vid}…")
                att = self._publish(att)
            with session_scope(self.ctx.factory) as s:
                s.get(AutopilotAttempt, aid).status = "RETRIED" if old == "RETRY" else "RECHECKED"
            self._record(rep, att)
            if att.status == "RETRY":
                raise TrumpNewsError(att.message)

    def _verdict(self, story_id: int) -> str | None:
        with session_scope(self.ctx.factory) as s:
            return s.scalar(select(FactCheck.verdict).where(FactCheck.story_id == story_id).order_by(FactCheck.id.desc()))

    def _open(self, run_id: str, story_id: int) -> int:
        with session_scope(self.ctx.factory) as s:
            row = AutopilotAttempt(run_id=run_id, story_id=story_id, status="RUNNING", message="en cours")
            s.add(row)
            s.flush()
            return row.id

    def _record(self, rep: AutopilotReport, att: Attempt, aid: int | None = None) -> None:
        rep.attempts.append(att)
        log.info("Pilote automatique, sujet #%d : %s - %s", att.story_id, att.status, att.message)
        with session_scope(self.ctx.factory) as s:
            row = s.get(AutopilotAttempt, aid) if aid else None
            if row is None:
                row = AutopilotAttempt(run_id=rep.run_id, story_id=att.story_id)
                s.add(row)
            row.status, row.video_id, row.url, row.message = att.status, att.video_id, att.url, att.message[:2000]
