"""Chaîne complète pour un sujet : fact-check -> script -> voix off -> plan visuel -> montage.

Utilisée par `run.bat pipeline N`, par l'interface (Phase 8) et plus tard par le scheduler (Phase 10).
Chaque étape rapporte sa progression ; l'annulation est vérifiée entre deux étapes (une étape commencée va au bout).
Une étape refusée par les garde-fous (sujet BLOCKED, script bloqué) arrête proprement la chaîne : ce n'est pas un plantage.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.script.models import ScriptBlockedError

log = logging.getLogger(__name__)
STEPS = ("Fact-check", "Script", "Voix off", "Visuels", "Montage")


class Cancelled(TrumpNewsError):
    """Chaîne annulée par l'utilisateur (entre deux étapes)."""


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled("Chaîne annulée.")


@dataclass
class PipelineOptions:
    duration_s: int | None = None
    style: str | None = None
    tone: str | None = None
    language: str | None = None
    template: str | None = None
    preview: bool = False
    force_factcheck: bool = False
    allowed_verdicts: tuple[str, ...] | None = None   # pilote automatique : arrêt dès le fact-check si verdict refusé


@dataclass
class StepReport:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass
class PipelineResult:
    story_id: int
    status: str                       # READY | NEEDS_REVIEW | BLOCKED | NOT_ALLOWED | SCRIPT_BLOCKED | FAILED | CANCELLED
    steps: list[StepReport] = field(default_factory=list)
    script_id: int | None = None
    video_id: int | None = None
    video_path: str | None = None
    message: str = ""


Progress = Callable[[int, int, str], None]   # (étape 1-based, total, message)


class PipelineRunner:
    def __init__(self, ctx: AppContext, *, progress: Progress | None = None, token: CancelToken | None = None) -> None:
        self.ctx = ctx
        self.progress = progress or (lambda i, n, msg: None)
        self.token = token or CancelToken()

    def run(self, story_id: int, opts: PipelineOptions | None = None) -> PipelineResult:
        opts = opts or PipelineOptions()
        res = PipelineResult(story_id, "FAILED")
        try:
            fc = self._step(res, 1, lambda: self.ctx.fact_checker().check(story_id, force=opts.force_factcheck),
                            lambda r: f"verdict {r.verdict}, {len(r.claims)} affirmation(s) retenue(s)")
            if fc.verdict == "BLOCKED":
                return self._stop(res, "BLOCKED", "Sujet BLOCKED par le fact-check : pas de vidéo (sources insuffisantes).")
            if opts.allowed_verdicts is not None and fc.verdict not in opts.allowed_verdicts:
                return self._stop(res, "NOT_ALLOWED", f"Fact-check {fc.verdict} non retenu par les réglages : pas de vidéo.")
            try:
                gs = self._step(res, 2, lambda: self.ctx.script_generator().generate(
                    story_id, style=opts.style, tone=opts.tone, duration_s=opts.duration_s, language=opts.language),
                    lambda g: f"script #{g.id}, {g.status}, {g.word_count} mots")
            except ScriptBlockedError as exc:
                return self._stop(res, "SCRIPT_BLOCKED", f"Script bloqué par les garde-fous : {exc}")
            res.script_id = gs.id
            self._step(res, 3, lambda: self.ctx.narration().narrate(gs.id), lambda n: f"{n.duration_s:.1f} s, voix {n.voice}")
            self._step(res, 4, lambda: self.ctx.visual_planner().plan(gs.id), lambda p: f"{len(p.items)} prise(s)")
            video = self._step(res, 5, lambda: self.ctx.video_service().render(gs.id, template=opts.template, preview=opts.preview),
                               lambda o: f"vidéo #{o.video_id}, {o.status}, qualité {o.report.score:.2f}")
            res.video_id, res.video_path, res.status = video.video_id, str(video.path), video.status
            res.message = f"Vidéo #{video.video_id} : {video.status}"
            return res
        except Cancelled:
            return self._stop(res, "CANCELLED", "Chaîne annulée par l'utilisateur.")
        except TrumpNewsError as exc:
            return self._stop(res, "FAILED", f"Échec à l'étape « {STEPS[len(res.steps) - 1]} » : {exc}")

    def produce(self, script_id: int, story_id: int, opts: PipelineOptions | None = None) -> PipelineResult:
        """Étapes 3 à 5 pour un script existant : voix off, plan visuel, montage."""
        opts = opts or PipelineOptions()
        res = PipelineResult(story_id, "FAILED", script_id=script_id)
        res.steps += [StepReport(STEPS[0], True, "déjà fait"), StepReport(STEPS[1], True, f"script #{script_id}")]
        try:
            self._step(res, 3, lambda: self.ctx.narration().narrate(script_id), lambda n: f"{n.duration_s:.1f} s, voix {n.voice}")
            self._step(res, 4, lambda: self.ctx.visual_planner().plan(script_id), lambda p: f"{len(p.items)} prise(s)")
            video = self._step(res, 5, lambda: self.ctx.video_service().render(script_id, template=opts.template,
                                                                               preview=opts.preview),
                               lambda o: f"vidéo #{o.video_id}, {o.status}, qualité {o.report.score:.2f}")
        except Cancelled:
            return self._stop(res, "CANCELLED", "Chaîne annulée par l'utilisateur.")
        except TrumpNewsError as exc:
            return self._stop(res, "FAILED", f"Échec à l'étape « {res.steps[-1].name} » : {exc}")
        res.video_id, res.video_path, res.status = video.video_id, str(video.path), video.status
        res.message = f"Vidéo #{video.video_id} : {video.status}"
        return res

    # ------------------------------------------------------------ internals
    def _step(self, res: PipelineResult, i: int, fn: Callable[[], Any], describe: Callable[[Any], str]) -> Any:
        self.token.check()
        name = STEPS[i - 1]
        self.progress(i, len(STEPS), f"{name}…")
        log.info("Pipeline sujet #%d : %s", res.story_id, name)
        t0 = time.monotonic()
        res.steps.append(StepReport(name, False, "en cours"))
        try:
            out = fn()
        except (TrumpNewsError, ScriptBlockedError) as exc:   # garde-fou ou échec : étape notée, chaîne arrêtée
            res.steps[-1] = StepReport(name, False, str(exc)[:300], round(time.monotonic() - t0, 1))
            raise
        res.steps[-1] = StepReport(name, True, describe(out), round(time.monotonic() - t0, 1))
        self.progress(i, len(STEPS), f"{name} : {res.steps[-1].detail}")
        return out

    def _stop(self, res: PipelineResult, status: str, message: str) -> PipelineResult:
        res.status, res.message = status, message
        log.info("Pipeline sujet #%d arrêté : %s", res.story_id, message)
        return res
