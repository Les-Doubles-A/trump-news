import logging
import threading
import time

from app.core.errors import TrumpNewsError
from app.ui.jobs import JobRunner
from app.ui.log_bridge import QtLogHandler


def test_heavy_jobs_run_one_at_a_time_in_order(qtbot):
    runner = JobRunner()
    active, peak = [0], [0]
    lock = threading.Lock()

    def work(name):
        def fn(h):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            h.progress(1, 1, f"{name} fini")
            with lock:
                active[0] -= 1
            return name
        return fn

    results, busy = [], []
    runner.busy_changed.connect(busy.append)
    for n in ("a", "b", "c"):
        runner.submit(n, work(n), on_done=results.append)
    assert runner.busy and runner.current.title == "a" and [j.title for j in runner.pending()] == ["b", "c"]
    qtbot.waitUntil(lambda: len(results) == 3, timeout=5000)
    assert results == ["a", "b", "c"] and peak[0] == 1               # jamais deux tâches lourdes en même temps
    assert busy == [True, False] and not runner.busy


def test_errors_and_cancellation(qtbot):
    runner = JobRunner()
    errors, finished = [], []
    runner.finished.connect(lambda info, ok, msg: finished.append((info.title, ok, msg)))

    def boom(h):
        raise TrumpNewsError("LM Studio injoignable")

    def bug(h):
        raise ValueError("oups")

    gate = threading.Event()

    def long(h):
        gate.wait(5)
        return "annulée" if h.cancelled else "finie"

    runner.submit("boom", boom, on_error=errors.append)
    runner.submit("bug", bug, on_error=errors.append)
    out = []
    runner.submit("long", long, on_done=out.append)
    qtbot.waitUntil(lambda: runner.current is not None and runner.current.title == "long", timeout=5000)
    runner.cancel_current()
    gate.set()
    qtbot.waitUntil(lambda: bool(out), timeout=5000)
    assert errors == ["LM Studio injoignable", "Erreur inattendue : oups"]
    assert out == ["annulée"] and [f[1] for f in finished] == [False, False, True]


def test_light_jobs_do_not_wait_for_heavy_ones(qtbot):
    runner = JobRunner()
    gate = threading.Event()
    runner.submit("lourde", lambda h: gate.wait(5))
    got = []
    runner.submit("état", lambda h: "ok", on_done=got.append, exclusive=False)
    qtbot.waitUntil(lambda: got == ["ok"], timeout=3000)                # pendant que la tâche lourde attend
    assert runner.busy
    gate.set()
    qtbot.waitUntil(lambda: not runner.busy, timeout=3000)


def test_log_bridge_from_worker_thread(qtbot):
    handler = QtLogHandler()
    lines = []
    handler.bridge.line.connect(lines.append)
    logger = logging.getLogger("test.bridge")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        t = threading.Thread(target=lambda: logger.warning("depuis un fil %d", 2))
        t.start()
        t.join()
        qtbot.waitUntil(lambda: bool(lines), timeout=3000)
    finally:
        logger.removeHandler(handler)
    assert lines[0].level == "WARNING" and lines[0].message == "depuis un fil 2"
