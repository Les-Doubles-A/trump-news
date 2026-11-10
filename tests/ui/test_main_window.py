import logging
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.core.health import ServiceStatus
from app.database.models import Article, FactCheck, Script, Story, Video, VideoReport
from app.database.session import session_scope
from app.pipeline.runner import PipelineResult, PipelineRunner, StepReport
from app.review.service import ScriptDetail
from app.ui.dialogs import PipelineDialog, ReviewDialog
from app.ui.log_bridge import QtLogHandler
from app.ui.main_window import MainWindow
from app.ui.widgets import Notifier, selected_key
from app.video.quality import QualityContext

REFS = [{"claim_id": "C1", "claim": "Trump visited Kentucky.", "status": "REPORTED",
         "sources": [{"name": "CBS News", "url": "https://cbs.example/1", "quote": "Trump visits Kentucky for 13th rally"}]}]


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext.load(base_dir=tmp_path, with_logging=False)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    qctx = QualityContext(1080, 1920, 30, 10.0, factcheck_verdict="CAUTION", script_status="NEEDS_REVIEW")
    with session_scope(c.factory) as s:
        st = Story(title="Trump visits Kentucky for 13th rally", status="NEW", score=0.8)
        s.add(st)
        s.flush()
        s.add(Article(url="https://cbs.example/1", title=st.title, source_name="CBS News", hash="a1", story_id=st.id,
                      summary="Trump visits Kentucky for 13th rally in the last month.",
                      published_at=datetime.now(timezone.utc)))
        s.add(FactCheck(story_id=st.id, input_hash="h", verdict="CAUTION", data={}))
        sc = Script(story_id=st.id, style="neutral_news", tone="neutral", language="en", duration_target=30, full_script="x",
                    hook="According to CBS News, Trump visited Kentucky.", status="NEEDS_REVIEW", source_refs=REFS,
                    claims={"segments": [{"index": 0, "section": "hook", "type": "attributed",
                                          "text": "According to CBS News, Trump visited Kentucky.", "claims": ["C1"]}],
                            "claims": [{"id": "C1", "claim": "Trump visited Kentucky.", "kind": "fact", "status": "REPORTED"}]})
        s.add(sc)
        s.flush()
        v = Video(story_id=st.id, script_id=sc.id, video_path=str(video), duration=10.0, status="NEEDS_REVIEW",
                  template="minimal", quality_score=1.0)
        s.add(v)
        s.flush()
        s.add(VideoReport(video_id=v.id, score=1.0, status="NEEDS_REVIEW", data={
            "context": asdict(qctx), "template": "minimal", "credits": ["Photo X / Wikimedia Commons / CC0"],
            "quality": {"score": 1.0, "status": "NEEDS_REVIEW", "checks": [
                {"name": "script validé", "ok": False, "detail": "statut NEEDS_REVIEW", "critical": True, "weight": 0}]}}))
    return c


class FakeScheduler:
    installed = False

    def status(self):
        from app.scheduler.service import ScheduleStatus

        nxt = datetime(2026, 10, 20, 8, 0) if FakeScheduler.installed else None
        return ScheduleStatus(FakeScheduler.installed, ["08:00", "13:00", "18:00"], nxt, None)

    def install(self):
        FakeScheduler.installed = True
        return self.status()

    def remove(self):
        FakeScheduler.installed = False
        return True


def fake_health(key, ok=True, warning=False):
    return lambda c: ServiceStatus(key, key.upper(), ok, f"{key} testé", warning)


@pytest.fixture()
def win(qtbot, ctx):
    FakeScheduler.installed = False
    w = MainWindow(ctx, notify=Notifier(quiet=True), player=False, scheduler=FakeScheduler,
                   health_fns={k: fake_health(k, ok=k != "wikimedia", warning=k == "memory")
                               for k in ("llm", "ffmpeg", "tts", "wikimedia", "memory")})
    qtbot.addWidget(w)
    w.start()
    yield w
    w.runner.wait(5000)


def test_dashboard_counters_health_and_nav_badge(qtbot, win):
    dash = win.pages["dashboard"]
    assert dash.counters["to_review"].text() == "1" and dash.counters["videos_review"].text() == "1"
    qtbot.waitUntil(lambda: dash.dots["memory"].state != "muted", timeout=5000)
    qtbot.waitUntil(lambda: dash.dots["wikimedia"].state == "error", timeout=5000)
    assert dash.dots["llm"].state == "ok" and dash.dots["memory"].state == "warn"
    assert win.nav.item(2).text() == "Scripts (1 à relire)"


def test_news_scripts_videos_pages(qtbot, win):
    win.go("news")
    news = win.pages["news"]
    assert news.table.rowCount() == 1 and news.table.item(0, 6).text() == "CAUTION"
    news.table.selectRow(0)
    assert "CBS News" in news.detail.toPlainText() or "cbs" in news.detail.toPlainText().lower()

    win.go("scripts", 1)
    sp = win.pages["scripts"]
    assert selected_key(sp.table) == 1
    html = sp.detail.toPlainText()
    assert "Trump visits Kentucky for 13th rally" in html and "CBS News" in html     # citation exacte + source

    win.go("videos", 1)
    vp = win.pages["videos"]
    assert vp.table.rowCount() == 1 and "script validé" in vp.report.toPlainText() and "Wikimedia" in vp.report.toPlainText()


def test_review_dialog_rules(qtbot, ctx):
    detail = ctx.reviews().detail(1)
    dlg = ReviewDialog(detail)
    qtbot.addWidget(dlg)
    dlg._approve()
    assert dlg.decision is None and "Cochez" in dlg.error.text()
    dlg.checked.setChecked(True)
    dlg._approve()
    assert dlg.decision is None and "Note obligatoire" in dlg.error.text()          # CAUTION
    dlg.note.setPlainText("Citation relue sur CBS")
    dlg._approve()
    assert dlg.decision == ReviewDialog.APPROVE
    blocked = ReviewDialog(ScriptDetail(9, 1, "NEEDS_REVIEW", "en", "s", 30, "BLOCKED", [], {}, [], []))
    qtbot.addWidget(blocked)
    assert not blocked.approve_btn.isEnabled()


def test_approve_from_scripts_page(qtbot, win, ctx):
    class Approving:
        def __init__(self, detail, parent):
            self.decision = ReviewDialog.APPROVE
            self.checked = type("C", (), {"isChecked": lambda self: True})()

        def exec(self):
            return True

        def note_text(self):
            return "relu"

    win.go("scripts", 1)
    win.pages["scripts"].open_review(dialog_factory=Approving)
    qtbot.waitUntil(lambda: not win.runner.busy, timeout=15000)
    with session_scope(ctx.factory) as s:
        assert s.get(Script, 1).status == "APPROVED"
    assert win.nav.item(2).text() == "Scripts"


def test_pipeline_job_reports_and_navigates(qtbot, win, monkeypatch):
    def fake_run(self, story_id, opts):
        self.progress(1, 5, "Fact-check…")
        return PipelineResult(story_id, "NEEDS_REVIEW", [StepReport("Fact-check", True, "ok")], script_id=1, video_id=1,
                              message="Vidéo #1 : NEEDS_REVIEW")

    monkeypatch.setattr(PipelineRunner, "run", fake_run)
    progress = []
    win.runner.progress.connect(lambda info, i, n, m: progress.append(m))
    win.run_pipeline(1, PipelineDialog([], None).options())
    qtbot.waitUntil(lambda: win.last_html is not None, timeout=5000)
    title, html = win.last_html
    assert "Pipeline du sujet #1" in title and "Relisez le script #1" in html and progress == ["Fact-check…"]
    assert win.current_page() is win.pages["videos"] and not win.cancel_btn.isVisible()


def test_autopilot_button_runs_and_reports(qtbot, win, monkeypatch):
    from app.autopilot.service import Attempt, Autopilot, AutopilotReport

    def fake_run(self):
        self.progress(1, 6, "Sujet 1/1 (#1) : Fact-check…")
        return AutopilotReport("r1", [Attempt(1, "Trump visits Kentucky", "PUBLISHED", "Vidéo #1 publiée.", 1,
                                              "https://www.youtube.com/shorts/abc")],
                               "1 vidéo(s) publiée(s) sur YouTube (public).", "public")

    monkeypatch.setattr(Autopilot, "run", fake_run)
    win.pages["dashboard"].autopilot_btn.click()
    qtbot.waitUntil(lambda: win.last_html is not None, timeout=5000)
    title, html = win.last_html
    assert title == "Pilote automatique" and "Publiée" in html and "shorts/abc" in html and "(public)" in html
    assert win.pages["dashboard"].autopilot_btn.isEnabled()


def test_schedule_card_enable_disable(qtbot, win):
    dash = win.pages["dashboard"]
    qtbot.waitUntil(lambda: "inactive" in dash.schedule_label.text(), timeout=5000)
    assert dash.schedule_on.isEnabled() and not dash.schedule_off.isEnabled()
    dash.schedule_on.click()
    qtbot.waitUntil(lambda: "active</b>" in dash.schedule_label.text() and "20/10 à 08:00" in dash.schedule_label.text(),
                    timeout=5000)
    assert not dash.schedule_on.isEnabled() and dash.schedule_off.isEnabled()
    dash.schedule_off.click()
    qtbot.waitUntil(lambda: "inactive" in dash.schedule_label.text(), timeout=5000)


def test_errors_are_reported_not_raised(qtbot, win, monkeypatch):
    def boom(self):
        raise TrumpNewsError("LM Studio injoignable")

    monkeypatch.setattr(AppContext, "fact_checker", boom)
    win.run_fact_check(1)
    qtbot.waitUntil(lambda: any(h[0] == "error" for h in win.notify.history), timeout=5000)
    assert ("error", "Fact-check", "LM Studio injoignable") in win.notify.history
    assert win.job_label.text().startswith("Échec")


def test_live_logs(qtbot, ctx):
    handler = QtLogHandler()
    w = MainWindow(ctx, notify=Notifier(quiet=True), player=False, log_handler=handler, scheduler=FakeScheduler,
                   health_fns={k: fake_health(k) for k in ("llm", "ffmpeg", "tts", "wikimedia", "memory")})
    qtbot.addWidget(w)
    lg = logging.getLogger("test.ui")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        lg.warning("montage terminé")
        qtbot.waitUntil(lambda: "montage terminé" in w.pages["logs"].view.toPlainText(), timeout=3000)
        w.pages["logs"].level.setCurrentIndex(2)                       # erreurs seulement
        assert "montage terminé" not in w.pages["logs"].view.toPlainText()
    finally:
        lg.removeHandler(handler)


def test_publish_button_blocked_by_guardrails(qtbot, win):
    win.go("videos", 1)
    vp = win.pages["videos"]
    assert vp.publish_btn.isEnabled()
    vp.publish_btn.click()                                   # contrôle qualité NEEDS_REVIEW : rien n'est envoyé
    kind, title, msg = win.notify.history[-1]
    assert kind == "error" and title == "Publication impossible" and "READY exigé" in msg
    assert not win.runner.busy


def test_publish_button_publishes_after_confirmation(qtbot, win, monkeypatch):
    from app.core.interfaces import PublishRequest
    from app.publishing.service import PublicationOutcome, PublishPlan, PublishingService

    req = PublishRequest(__import__("pathlib").Path("v.mp4"), "Titre", "desc", "legende", [], "en", False, 10.0)
    sent = []
    monkeypatch.setattr(PublishingService, "prepare", lambda self, vid, platforms=None: PublishPlan(vid, req, ["youtube"]))

    def fake_publish(self, vid, platforms=None):
        sent.append((vid, platforms))
        return PublishPlan(vid, req, platforms), [PublicationOutcome("youtube", "PUBLISHED", "https://youtu.be/abc")]

    monkeypatch.setattr(PublishingService, "publish", fake_publish)
    win.go("videos", 1)
    win.pages["videos"].publish_btn.click()
    qtbot.waitUntil(lambda: win.last_html is not None, timeout=5000)
    assert sent == [(1, ["youtube"])]
    assert "Publication de la vidéo #1" in win.last_html[0] and "https://youtu.be/abc" in win.last_html[1]
    assert any(k == "confirm" for k, _, _ in win.notify.history)
