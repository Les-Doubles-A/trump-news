"""Phase 10 : lancement planifié du pilote automatique (FULL AUTO, publication YouTube).

`run.bat schedule install` crée la tâche Windows ; à chaque heure prévue, Windows exécute `scheduled-run` :
verrou -> vérification des services (LM Studio démarré si besoin) -> pilote automatique -> compte rendu
dans la table `scheduled_runs` et dans data/logs/scheduled/. Un lancement interrompu (PC éteint, plantage) est
noté INTERRUPTED au lancement suivant ; le pilote reprend alors les vidéos déjà montées sans les remonter.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from app.core import health
from app.core.errors import TrumpNewsError
from app.core.health import ServiceStatus
from app.database.models import ScheduledRun
from app.database.session import session_scope
from app.pipeline.runner import CancelToken, Progress
from app.scheduler.lock import AlreadyRunning
from app.scheduler.windows_task import WindowsTask
from app.utils.timeutil import utcnow

log = logging.getLogger(__name__)
PREFLIGHT = (health.check_llm, health.check_ffmpeg, health.check_tts)


@dataclass
class RunRow:
    id: int
    trigger: str
    status: str
    published: int
    message: str
    started_at: datetime
    finished_at: datetime | None


@dataclass
class ScheduleStatus:
    installed: bool
    times: list[str]
    next_run: datetime | None          # heure locale du PC
    last: RunRow | None


def next_run(times: list[str], now: datetime) -> datetime | None:
    """Prochaine heure de lancement (heure locale), à partir de l'heure locale `now`."""
    slots = []
    for t in times:
        h, m = (int(x) for x in t.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        slots.append(slot if slot > now else slot + timedelta(days=1))
    return min(slots) if slots else None


def find_lms() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    default = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    return str(default) if default.exists() else None


def start_lmstudio() -> str:
    """Démarre le serveur local de LM Studio (lms server start). Retourne un message lisible."""
    lms = find_lms()
    if lms is None:
        return "outil « lms » introuvable : ouvrez LM Studio une fois, ou démarrez son serveur à la main"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run([lms, "server", "start"], capture_output=True, timeout=120, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"lms server start a échoué : {exc}"
    return "serveur LM Studio démarré" if res.returncode == 0 else \
        "lms server start a échoué : " + (res.stderr or res.stdout).decode("utf-8", "replace").strip()[:200]


class SchedulerService:
    def __init__(self, ctx, *, task: WindowsTask | None = None, checks=PREFLIGHT,
                 start_llm: Callable[[], str] = start_lmstudio, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.ctx, self.cfg = ctx, ctx.cfg.scheduler
        self.task = task or WindowsTask(self.cfg.task_name)
        self._checks, self._start_llm, self._sleep, self._now = checks, start_llm, sleep, now

    # ----------------------------------------------------------- tâche Windows
    def install(self) -> ScheduleStatus:
        self.task.install(self.cfg.times, self.ctx.base_dir, max_hours=self.cfg.max_run_hours)
        log.info("Planification installée : %s (tâche « %s »)", ", ".join(self.cfg.times), self.cfg.task_name)
        return self.status()

    def remove(self) -> bool:
        removed = self.task.remove()
        log.info("Planification %s", "supprimée" if removed else "absente : rien à supprimer")
        return removed

    def status(self) -> ScheduleStatus:
        installed = self.task.exists()
        return ScheduleStatus(installed, list(self.cfg.times),
                              next_run(self.cfg.times, self._now()) if installed else None, self.last_run())

    def last_run(self) -> RunRow | None:
        rows = self.history(1)
        return rows[0] if rows else None

    def history(self, limit: int = 20) -> list[RunRow]:
        with session_scope(self.ctx.factory) as s:
            return [RunRow(r.id, r.trigger, r.status, r.published, r.message or "", r.started_at, r.finished_at)
                    for r in s.scalars(select(ScheduledRun).order_by(ScheduledRun.id.desc()).limit(limit))]

    # ------------------------------------------------------------- lancement
    def preflight(self) -> list[ServiceStatus]:
        """État des services indispensables ; LM Studio est démarré s'il ne répond pas (scheduler.start_lmstudio)."""
        results = [c(self.ctx) for c in self._checks]
        i = next((k for k, r in enumerate(results) if r.key == "llm"), None)
        if i is not None and not results[i].ok and self.cfg.start_lmstudio:
            log.info("LM Studio ne répond pas : %s", self._start_llm())
            for _ in range(6):                     # le serveur met quelques secondes à répondre
                self._sleep(5)
                results[i] = self._checks[i](self.ctx)
                if results[i].ok:
                    break
        return results

    def run(self, trigger: str = "scheduled", progress: Progress | None = None,
            token: CancelToken | None = None) -> RunRow:
        self._close_interrupted()
        rid = self._start(trigger)
        try:
            bad = [r for r in self.preflight() if not r.ok]
            if bad:
                return self._finish(rid, "PREFLIGHT", 0, None,
                                    "Services indisponibles : " + " ; ".join(f"{r.label} ({r.detail})" for r in bad))
            rep = self.ctx.autopilot(progress=progress, token=token).run()
        except AlreadyRunning as exc:
            return self._finish(rid, "LOCKED", 0, None, str(exc))
        except TrumpNewsError as exc:
            return self._finish(rid, "FAILED", 0, None, str(exc))
        except Exception as exc:              # un lancement planifié ne doit jamais rester RUNNING
            log.exception("Lancement planifié en erreur")
            return self._finish(rid, "FAILED", 0, None, f"Erreur inattendue : {exc}")
        n = len(rep.published)
        failed = any(a.status == "FAILED" for a in rep.attempts)
        return self._finish(rid, "FAILED" if failed else ("OK" if n else "NOTHING"), n, rep.run_id, rep.message)

    # ------------------------------------------------------------ internals
    def _close_interrupted(self) -> None:
        """Un RUNNING restant vient d'un lancement coupé (PC éteint, processus tué, plantage)."""
        with session_scope(self.ctx.factory) as s:
            for r in s.scalars(select(ScheduledRun).where(ScheduledRun.status == "RUNNING")):
                r.status, r.finished_at = "INTERRUPTED", utcnow()
                r.message = (r.message or "") + "Lancement interrompu (PC éteint, mise en veille ou plantage)."

    def _start(self, trigger: str) -> int:
        with session_scope(self.ctx.factory) as s:
            row = ScheduledRun(trigger=trigger, status="RUNNING")
            s.add(row)
            s.flush()
            return row.id

    def _finish(self, rid: int, status: str, published: int, run_id: str | None, message: str) -> RunRow:
        with session_scope(self.ctx.factory) as s:
            row = s.get(ScheduledRun, rid)
            row.status, row.published, row.autopilot_run_id = status, published, run_id
            row.message, row.finished_at = message[:4000], utcnow()
            out = RunRow(row.id, row.trigger, row.status, row.published, row.message, row.started_at, row.finished_at)
        log.info("Lancement %s #%d : %s - %s", out.trigger, out.id, out.status, out.message)
        return out
