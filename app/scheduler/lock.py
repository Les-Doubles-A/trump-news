"""Verrou de fichier : un seul lancement du pilote à la fois (tâche planifiée, bouton de l'interface, run-now).

Le verrou est tenu par le système d'exploitation sur un fichier ouvert : il disparaît tout seul si le processus
plante ou est tué, donc jamais de verrou « fantôme » à effacer à la main.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.errors import TrumpNewsError


class AlreadyRunning(TrumpNewsError):
    """Un autre lancement du pilote tient déjà le verrou."""


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        try:
            _lock(f.fileno())
        except OSError:
            raise AlreadyRunning("Le pilote automatique tourne déjà (tâche planifiée ou interface) : "
                                 "attendez la fin du lancement en cours.") from None
        try:
            yield
        finally:
            _unlock(f.fileno())
    finally:
        f.close()


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
