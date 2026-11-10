"""Tâches de fond de l'interface, sans jamais figer la fenêtre.

- Les tâches « lourdes » (LLM, voix, montage, écriture en base) passent dans une file à UN seul fil : une à la fois,
  comme LM Studio (un modèle) et SQLite (un écrivain) l'exigent.
- Les tâches « légères » (tests d'état réseau) passent dans un second groupe de fils, en parallèle.
- Les résultats reviennent à l'interface uniquement par des signaux Qt (jamais de widget touché depuis un autre fil).
- Annulation : jeton coopératif, vérifié par la tâche entre deux étapes.
"""
from __future__ import annotations

import itertools
import logging
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from app.core.errors import TrumpNewsError
from app.pipeline.runner import CancelToken

log = logging.getLogger(__name__)


@dataclass
class JobInfo:
    id: int
    title: str
    exclusive: bool


class JobHandle:
    """Donné à la fonction de la tâche : signaler sa progression, savoir si l'utilisateur a annulé."""

    def __init__(self, job_id: int, emitter: "_Emitter") -> None:
        self.id = job_id
        self.token = CancelToken()
        self._emit = emitter

    def progress(self, step: int, total: int, message: str) -> None:
        self._emit.progress.emit(self.id, step, total, message)

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled


class _Emitter(QObject):
    progress = Signal(int, int, int, str)
    done = Signal(int, object)
    failed = Signal(int, str, str)       # id, message lisible, détail technique


class _Worker(QRunnable):
    def __init__(self, fn: Callable[[JobHandle], Any], handle: JobHandle, emitter: _Emitter) -> None:
        super().__init__()
        self.fn, self.handle, self.emitter = fn, handle, emitter
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.fn(self.handle)
        except TrumpNewsError as exc:                    # erreur prévue : message clair pour l'utilisateur
            self.emitter.failed.emit(self.handle.id, str(exc), "")
        except Exception as exc:                         # bug : message + trace dans le journal
            log.exception("Tâche #%d en erreur", self.handle.id)
            self.emitter.failed.emit(self.handle.id, f"Erreur inattendue : {exc}", traceback.format_exc())
        else:
            self.emitter.done.emit(self.handle.id, result)


class JobRunner(QObject):
    started = Signal(object)                 # JobInfo
    progress = Signal(object, int, int, str)  # JobInfo, étape, total, message
    finished = Signal(object, bool, str)     # JobInfo, succès, message d'erreur
    busy_changed = Signal(bool)              # file lourde occupée ou non

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._heavy = QThreadPool(self)
        self._heavy.setMaxThreadCount(1)
        self._light = QThreadPool(self)
        self._light.setMaxThreadCount(4)
        self._ids = itertools.count(1)
        self._jobs: dict[int, tuple[JobInfo, JobHandle, Callable | None, Callable | None]] = {}
        self._queue: list[int] = []          # tâches lourdes en attente ou en cours, dans l'ordre
        self._emitter = _Emitter()
        self._emitter.progress.connect(self._on_progress)
        self._emitter.done.connect(self._on_done)
        self._emitter.failed.connect(self._on_failed)

    # ------------------------------------------------------------------ API
    def submit(self, title: str, fn: Callable[[JobHandle], Any], *, on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[str], None] | None = None, exclusive: bool = True) -> JobInfo:
        info = JobInfo(next(self._ids), title, exclusive)
        handle = JobHandle(info.id, self._emitter)
        self._jobs[info.id] = (info, handle, on_done, on_error)
        if exclusive:
            self._queue.append(info.id)
            if len(self._queue) == 1:
                self.busy_changed.emit(True)
                self.started.emit(info)
        else:
            self.started.emit(info)
        (self._heavy if exclusive else self._light).start(_Worker(fn, handle, self._emitter))
        return info

    @property
    def busy(self) -> bool:
        return bool(self._queue)

    @property
    def current(self) -> JobInfo | None:
        return self._jobs[self._queue[0]][0] if self._queue else None

    def pending(self) -> list[JobInfo]:
        return [self._jobs[i][0] for i in self._queue[1:]]

    def cancel_current(self) -> None:
        if self._queue:
            self._jobs[self._queue[0]][1].token.cancel()

    def wait(self, msecs: int = 30000) -> bool:
        """Attend la fin de toutes les tâches (fermeture de la fenêtre, tests)."""
        return self._heavy.waitForDone(msecs) and self._light.waitForDone(msecs)

    # ------------------------------------------------------------ internals
    @Slot(int, int, int, str)
    def _on_progress(self, job_id: int, step: int, total: int, message: str) -> None:
        if job_id in self._jobs:
            self.progress.emit(self._jobs[job_id][0], step, total, message)

    @Slot(int, object)
    def _on_done(self, job_id: int, result: Any) -> None:
        info, _, on_done, _ = self._finish(job_id)
        if on_done:
            on_done(result)
        self.finished.emit(info, True, "")

    @Slot(int, str, str)
    def _on_failed(self, job_id: int, message: str, detail: str) -> None:
        info, _, _, on_error = self._finish(job_id)
        if on_error:
            on_error(message)
        self.finished.emit(info, False, message)

    def _finish(self, job_id: int):
        entry = self._jobs.pop(job_id)
        info = entry[0]
        if info.exclusive and job_id in self._queue:
            self._queue.remove(job_id)
            if self._queue:
                self.started.emit(self._jobs[self._queue[0]][0])
            else:
                self.busy_changed.emit(False)
        return entry
