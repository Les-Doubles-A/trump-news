import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.autopilot.service import Autopilot, AutopilotReport, Attempt
from app.config.schema import SchedulerSection
from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.core.health import ServiceStatus
from app.database.models import ScheduledRun
from app.database.session import session_scope
from app.scheduler.lock import AlreadyRunning, run_lock
from app.scheduler.service import SchedulerService, next_run
from app.scheduler.windows_task import WindowsTask, task_xml

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeSchtasks:
    def __init__(self, exists=False, fail=None):
        self.calls, self.exists, self.fail, self.xml = [], exists, fail, None

    def __call__(self, args):
        self.calls.append(args)
        verb = args[0]
        if verb == "/Create":
            self.xml = Path(args[args.index("/XML") + 1]).read_bytes()
            if self.fail:
                return subprocess.CompletedProcess(args, 1, b"", self.fail.encode())
            self.exists = True
        elif verb == "/Delete":
            self.exists = False
        elif verb == "/Query":
            return subprocess.CompletedProcess(args, 0 if self.exists else 1, b"", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")


@pytest.fixture()
def ctx(tmp_path):
    return AppContext.load(base_dir=tmp_path, with_logging=False)


def ok(key):
    return lambda c: ServiceStatus(key, key.upper(), True, "ok")


# ------------------------------------------------------------------ tâche Windows
def test_task_xml_is_valid_and_complete(tmp_path):
    xml = task_xml(times=["08:00", "13:00", "18:00"], command=tmp_path / ".venv" / "Scripts" / "pythonw.exe",
                   workdir=tmp_path / "a&b", user="PC\\adam", max_hours=3, start=date(2026, 10, 20))
    root = ET.fromstring(xml.split("?>", 1)[1])
    starts = [e.text for e in root.findall(".//t:CalendarTrigger/t:StartBoundary", NS)]
    assert starts == ["2026-10-20T08:00:00", "2026-10-20T13:00:00", "2026-10-20T18:00:00"]
    assert root.find(".//t:Exec/t:Arguments", NS).text == "-m app.main scheduled-run"
    assert root.find(".//t:Exec/t:WorkingDirectory", NS).text.endswith("a&b")              # échappé puis relu
    assert root.find(".//t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert root.find(".//t:Principal/t:RunLevel", NS).text == "LeastPrivilege"            # sans droits admin
    assert root.find(".//t:Settings/t:StartWhenAvailable", NS).text == "true"
    assert root.find(".//t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find(".//t:Settings/t:ExecutionTimeLimit", NS).text == "PT3H"


def test_windows_task_install_remove_and_errors(tmp_path):
    fake = FakeSchtasks()
    task = WindowsTask("TrumpNews Autopilot", runner=fake)
    assert not task.exists() and task.remove() is False
    task.install(["18:00", "08:00", "08:00"], tmp_path, today=date(2026, 10, 20))
    create = fake.calls[-1]
    assert create[:3] == ["/Create", "/TN", "TrumpNews Autopilot"] and create[-1] == "/F"
    assert fake.xml[:2] == b"\xff\xfe"                                                       # UTF-16 avec BOM
    text = fake.xml.decode("utf-16")
    assert text.count("<CalendarTrigger>") == 2 and text.index("T08:00") < text.index("T18:00")
    assert not Path(create[create.index("/XML") + 1]).exists()                               # fichier temporaire effacé
    assert task.exists() and task.remove() is True and not task.exists()
    with pytest.raises(TrumpNewsError, match="refusée : Access is denied"):
        WindowsTask("x", runner=FakeSchtasks(fail="Access is denied")).install(["08:00"], tmp_path)
    with pytest.raises(TrumpNewsError, match="Aucune heure"):
        task.install([], tmp_path)


def test_next_run_and_times_setting():
    now = datetime(2026, 10, 19, 14, 30)
    assert next_run(["08:00", "13:00", "18:00"], now) == datetime(2026, 10, 19, 18, 0)
    assert next_run(["08:00", "13:00"], now) == datetime(2026, 10, 20, 8, 0)
    assert SchedulerSection(times="08:00,13:00, 18:00").times == ["08:00", "13:00", "18:00"]
    assert SchedulerSection(times=[1080]).times == ["18:00"]                                 # « 18:00 » lu en base 60
    with pytest.raises(ValueError):
        SchedulerSection(times="25:00")


def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "autopilot.lock"
    with run_lock(path):
        with pytest.raises(AlreadyRunning):
            with run_lock(path):
                pass
    with run_lock(path):                                                                     # libéré après usage
        pass


# ------------------------------------------------------------------ lancements
def fake_report(n):
    return AutopilotReport("r1", [Attempt(1, "t", "PUBLISHED", "ok", 1, "https://youtu.be/x")] * n,
                           f"{n} vidéo(s) publiée(s) sur YouTube (public).", "public")


def test_scheduled_run_ok_and_history(ctx, monkeypatch):
    monkeypatch.setattr(Autopilot, "run", lambda self: fake_report(2))
    svc = SchedulerService(ctx, task=WindowsTask("t", runner=FakeSchtasks()), checks=(ok("llm"), ok("ffmpeg")))
    row = svc.run("scheduled")
    assert (row.status, row.published, row.trigger) == ("OK", 2, "scheduled") and row.finished_at
    monkeypatch.setattr(Autopilot, "run", lambda self: fake_report(0))
    assert svc.run("manual").status == "NOTHING"
    assert [r.status for r in svc.history()] == ["NOTHING", "OK"] and svc.last_run().trigger == "manual"


def test_preflight_starts_lmstudio_then_gives_up(ctx, monkeypatch):
    monkeypatch.setattr(Autopilot, "run", lambda self: pytest.fail("ne doit pas tourner"))
    started, answers = [], iter([False, False, True])

    def llm(c):
        return ServiceStatus("llm", "LM Studio", next(answers, False), "injoignable")

    svc = SchedulerService(ctx, task=WindowsTask("t", runner=FakeSchtasks()), checks=(llm, ok("ffmpeg")),
                           start_llm=lambda: started.append(1) or "démarré", sleep=lambda s: None)
    assert all(r.ok for r in svc.preflight()) and started == [1]                             # 3e essai : répond
    svc = SchedulerService(ctx, task=WindowsTask("t", runner=FakeSchtasks()),
                           checks=(lambda c: ServiceStatus("llm", "LM Studio", False, "injoignable"), ok("ffmpeg")),
                           start_llm=lambda: "lms introuvable", sleep=lambda s: None)
    row = svc.run()
    assert row.status == "PREFLIGHT" and "LM Studio (injoignable)" in row.message


def test_interrupted_locked_and_failed_runs(ctx, monkeypatch):
    with session_scope(ctx.factory) as s:
        s.add(ScheduledRun(trigger="scheduled", status="RUNNING"))                           # PC éteint en plein lancement
    svc = SchedulerService(ctx, task=WindowsTask("t", runner=FakeSchtasks()), checks=(ok("llm"),))
    with run_lock(ctx.data_root / "autopilot.lock"):                                         # l'interface tourne déjà
        monkeypatch.setattr(Autopilot, "_run", lambda self: pytest.fail("verrou ignoré"))
        monkeypatch.setattr(Autopilot, "__init__", lambda self, ctx, **kw: setattr(self, "ctx", ctx))
        row = svc.run()
    assert row.status == "LOCKED" and "tourne déjà" in row.message
    with session_scope(ctx.factory) as s:
        assert s.get(ScheduledRun, 1).status == "INTERRUPTED"

    def boom(self):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(Autopilot, "run", boom)
    row = svc.run()
    assert row.status == "FAILED" and "disque plein" in row.message
    with session_scope(ctx.factory) as s:
        assert not list(s.scalars(select(ScheduledRun).where(ScheduledRun.status == "RUNNING")))


def test_status_reports_installation(ctx):
    fake = FakeSchtasks()
    svc = SchedulerService(ctx, task=WindowsTask("t", runner=fake), now=lambda: datetime(2026, 10, 19, 9, 0))
    st = svc.status()
    assert not st.installed and st.next_run is None and st.times == ["08:00", "13:00", "18:00"]
    st = svc.install()
    assert st.installed and st.next_run == datetime(2026, 10, 19, 13, 0)
    assert svc.remove() and not svc.status().installed


def test_cli_schedule(tmp_path, monkeypatch, capsys):
    import app.main as m

    fake = FakeSchtasks()
    monkeypatch.chdir(tmp_path)
    def init(self, name, runner=None):
        self.name, self._run = name, fake

    monkeypatch.setattr(WindowsTask, "__init__", init)
    assert m.main(["schedule"]) == 0 and "INACTIVE" in capsys.readouterr().out
    assert m.main(["schedule", "install"]) == 0 and "08:00, 13:00, 18:00" in capsys.readouterr().out
    assert m.main(["schedule", "status"]) == 0 and "ACTIVE" in capsys.readouterr().out
    monkeypatch.setattr(SchedulerService, "preflight", lambda self: [])
    monkeypatch.setattr(Autopilot, "run", lambda self: fake_report(1))
    assert m.main(["scheduled-run"]) == 0
    assert m.main(["schedule"]) == 0 and "1 publiée(s)" in capsys.readouterr().out
    assert m.main(["schedule", "remove"]) == 0 and "supprimée" in capsys.readouterr().out
