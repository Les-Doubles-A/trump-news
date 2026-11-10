"""Tâche du Planificateur de tâches Windows qui lance le pilote automatique aux heures choisies.

- Une seule tâche, un déclencheur quotidien par heure, créée pour l'utilisateur courant sans droits administrateur
  (schtasks /Create /XML) : elle ne tourne que si la session Windows est ouverte, aucun mot de passe n'est stocké.
- Lancée par pythonw.exe (pas de fenêtre noire) depuis le dossier du projet.
- Un lancement manqué (PC éteint ou en veille) est rattrapé au démarrage suivant ; jamais deux à la fois ;
  arrêt forcé par Windows au bout de scheduler.max_run_hours.
- Les sorties de schtasks sont traduites selon la langue de Windows : on ne lit que le code de retour.
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from app.core.errors import TrumpNewsError

Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]
ARGUMENTS = "-m app.main scheduled-run"


def _schtasks(args: list[str]) -> "subprocess.CompletedProcess[bytes]":
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["schtasks", *args], capture_output=True, timeout=60, creationflags=flags)


def _text(raw: bytes) -> str:
    return raw.decode("oem" if os.name == "nt" else "utf-8", errors="replace").strip()


def python_for_task(project_dir: Path) -> Path:
    """pythonw.exe du venv (aucune fenêtre), sinon l'interpréteur courant."""
    for name in ("pythonw.exe", "python.exe"):
        p = project_dir / ".venv" / "Scripts" / name
        if p.exists():
            return p
    return Path(sys.executable)


def task_xml(*, times: list[str], command: Path, workdir: Path, user: str, max_hours: int, start: date) -> str:
    triggers = "".join(
        f"""
    <CalendarTrigger>
      <StartBoundary>{start.isoformat()}T{t}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>""" for t in times)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Trump News : actualités, fact-check, montage et publication YouTube automatiques (run.bat schedule).</Description>
  </RegistrationInfo>
  <Triggers>{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT{max_hours}H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


class WindowsTask:
    def __init__(self, name: str, *, runner: Runner = _schtasks) -> None:
        self.name, self._run = name, runner

    def install(self, times: list[str], project_dir: Path, *, max_hours: int = 3, today: date | None = None) -> None:
        if not times:
            raise TrumpNewsError("Aucune heure de lancement (scheduler.times).")
        xml = task_xml(times=sorted(set(times)), command=python_for_task(project_dir), workdir=project_dir,
                       user=current_user(), max_hours=max_hours, start=today or date.today())
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(xml.encode("utf-16"))          # schtasks /XML attend de l'UTF-16 (avec BOM)
            res = self._run(["/Create", "/TN", self.name, "/XML", path, "/F"])
        finally:
            os.unlink(path)
        if res.returncode != 0:
            raise TrumpNewsError(f"Création de la tâche Windows refusée : {_text(res.stderr or res.stdout)}")

    def remove(self) -> bool:
        """Supprime la tâche. False si elle n'existait pas."""
        if not self.exists():
            return False
        res = self._run(["/Delete", "/TN", self.name, "/F"])
        if res.returncode != 0:
            raise TrumpNewsError(f"Suppression de la tâche Windows refusée : {_text(res.stderr or res.stdout)}")
        return True

    def exists(self) -> bool:
        return self._run(["/Query", "/TN", self.name]).returncode == 0

    def run_now(self) -> None:
        res = self._run(["/Run", "/TN", self.name])
        if res.returncode != 0:
            raise TrumpNewsError(f"Lancement de la tâche Windows refusé : {_text(res.stderr or res.stdout)}")
