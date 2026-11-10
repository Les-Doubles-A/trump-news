from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.autopilot.service import Autopilot
from app.core.context import AppContext
from app.core.errors import PublisherUnavailableError, TrumpNewsError
from app.core.interfaces import HealthStatus, PublishResult
from app.database.models import (Article, AutopilotAttempt, FactCheck, Publication, Script, Story, Video, VideoReport)
from app.database.session import session_scope
from app.pipeline.runner import CancelToken, PipelineResult
from app.video.quality import QualityContext


class FakeYouTube:
    name = "youtube"

    def __init__(self, ok=True, fail=None):
        self.ok, self.fail, self.sent = ok, fail, []

    def health_check(self):
        return HealthStatus(self.ok, "connecté" if self.ok else "Compte YouTube non connecté")

    def publish(self, req):
        if self.fail:
            raise self.fail
        self.sent.append(req)
        return PublishResult("youtube", "PUBLISHED", f"yt{len(self.sent)}", f"https://www.youtube.com/shorts/yt{len(self.sent)}")


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext.load(base_dir=tmp_path, with_logging=False)
    c.cfg.autopilot.fetch_news = False
    return c


def add_story(ctx, title, score, verdict=None, hours_old=0):
    with session_scope(ctx.factory) as s:
        st = Story(title=title, status="NEW", score=score,
                   created_at=datetime.now(timezone.utc) - timedelta(hours=hours_old))
        s.add(st)
        s.flush()
        s.add(Article(url=f"https://ex.example/{st.id}", title=title, source_name="CBS News", hash=f"h{st.id}",
                      story_id=st.id, summary=title, published_at=datetime.now(timezone.utc)))
        if verdict:
            s.add(FactCheck(story_id=st.id, input_hash="h", verdict=verdict, data={}))
        return st.id


def fake_pipeline(ctx, tmp_path, outcomes):
    """outcomes : story_id -> (statut de la vidéo, verdict, statut du script[, avertissements[, contrôles en échec[, statut des affirmations]]])."""
    calls = []

    def run(story_id, progress):
        calls.append(story_id)
        progress(1, 5, "Fact-check…")
        status, verdict, script_status, *more = outcomes.get(story_id, ("READY", "SAFE", "DRAFT"))
        warnings, failed, claim_status = (list(more) + [[], [], "REPORTED"][len(more):])[:3]
        if status in ("FAILED", "BLOCKED"):
            return PipelineResult(story_id, status, message=f"{status} pour le sujet {story_id}")
        path = tmp_path / f"v{story_id}.mp4"
        path.write_bytes(b"x")
        with session_scope(ctx.factory) as s:
            s.add(FactCheck(story_id=story_id, input_hash="h", verdict=verdict, data={}))
            sc = Script(story_id=story_id, style="neutral_news", tone="neutral", language="en", duration_target=30,
                        hook="Trump waived sanctions.", full_script="Trump waived sanctions.", status=script_status,
                        source_refs=[], claims={"segments": [], "warnings": warnings,
                                                "claims": [{"id": "C1", "claim": "x", "status": claim_status}]})
            s.add(sc)
            s.flush()
            v = Video(story_id=story_id, script_id=sc.id, video_path=str(path), duration=30.0, status=status)
            s.add(v)
            s.flush()
            qctx = QualityContext(1080, 1920, 30, 30.0, factcheck_verdict=verdict, script_status=script_status)
            checks = [{"name": n, "ok": False, "detail": "absente", "critical": True, "weight": 0} for n in failed]
            checks.append({"name": "script validé", "ok": script_status in ("DRAFT", "APPROVED"), "detail": script_status,
                           "critical": True, "weight": 0})
            s.add(VideoReport(video_id=v.id, score=1.0, status=status, data={"context": asdict(qctx), "credits": [],
                                                                            "quality": {"score": 0.9, "checks": checks}}))
            vid = v.id
        return PipelineResult(story_id, status, script_id=sc.id, video_id=vid, message=f"Vidéo #{vid} : {status}")

    run.calls = calls
    return run


def statuses(ctx):
    with session_scope(ctx.factory) as s:
        return {a.story_id: a.status for a in s.scalars(select(AutopilotAttempt))}


def test_publishes_best_stories_public_without_approval(ctx, tmp_path):
    a, b, c, d = (add_story(ctx, f"Story {i}", score) for i, score in enumerate((0.9, 0.8, 0.7, 0.6)))
    ctx.cfg.autopilot.videos_per_run = 2
    pipe = fake_pipeline(ctx, tmp_path, {a: ("NEEDS_REVIEW", "SAFE", "NEEDS_REVIEW", ["segment 2: le dernier segment doit être la conclusion"])})
    yt = FakeYouTube()
    seen = []
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda name: yt, progress=lambda i, n, m: seen.append((i, n, m))).run()
    assert pipe.calls == [a, b, c]                                   # s'arrête dès 2 vidéos publiées
    assert [x.status for x in rep.attempts] == ["REJECTED", "PUBLISHED", "PUBLISHED"]
    assert "à relire" in rep.attempts[0].message and "conclusion" in rep.attempts[0].message and rep.attempts[1].url.endswith("yt1")
    assert len(yt.sent) == 2 and "2 vidéo(s) publiée(s)" in rep.message and rep.privacy == "public"
    assert any(n == 6 and "Sujet 1/" in m for _, n, m in seen)
    with session_scope(ctx.factory) as s:
        assert s.get(Story, b).used_at is not None and s.get(Story, a).used_at is None
        assert {p.status for p in s.scalars(select(Publication))} == {"PUBLISHED"}
    # Deuxième lancement : les sujets essayés ne sont jamais repris
    pipe2 = fake_pipeline(ctx, tmp_path, {})
    Autopilot(ctx, pipeline=pipe2, publisher=lambda name: yt).run()
    assert pipe2.calls == [d]


def test_publisher_uses_autopilot_privacy_and_skips_approval(ctx):
    ctx.cfg.autopilot.youtube_privacy = "unlisted"
    pilot = Autopilot(ctx)
    pub = pilot._publisher("youtube")
    assert pub.cfg.privacy == "unlisted" and pilot.publishing.cfg.publishing.require_approval is False
    assert ctx.cfg.publishing.require_approval is True and ctx.cfg.publishing.youtube.privacy == "private"   # inchangés


def test_candidate_filters(ctx, tmp_path):
    good = add_story(ctx, "Good", 0.5, "SAFE")
    caution = add_story(ctx, "Caution", 0.9, "CAUTION")
    add_story(ctx, "Blocked", 0.95, "BLOCKED")
    add_story(ctx, "Old", 0.99, hours_old=48)
    low = add_story(ctx, "Low", 0.05)
    manual = add_story(ctx, "Manual", 0.97)
    fake_pipeline(ctx, tmp_path, {})(manual, lambda *a: None)       # déjà monté à la main
    pilot = Autopilot(ctx, publisher=lambda n: FakeYouTube())
    assert [r.id for r in pilot.candidates()] == [caution, good, low]
    ctx.cfg.autopilot.allow_caution = False
    ctx.cfg.autopilot.min_story_score = 0.1
    assert [r.id for r in Autopilot(ctx, publisher=lambda n: FakeYouTube()).candidates()] == [good]


def test_caution_refused_after_pipeline_when_not_allowed(ctx, tmp_path):
    sid = add_story(ctx, "New", 0.9)                                  # verdict inconnu avant le pipeline
    ctx.cfg.autopilot.allow_caution = False
    yt = FakeYouTube()
    rep = Autopilot(ctx, pipeline=fake_pipeline(ctx, tmp_path, {sid: ("READY", "CAUTION", "DRAFT")}),
                    publisher=lambda n: yt).run()
    assert rep.attempts[0].status == "REJECTED" and "CAUTION" in rep.attempts[0].message and not yt.sent


def test_daily_cap_and_youtube_not_connected(ctx, tmp_path):
    sid = add_story(ctx, "S", 0.9)
    with pytest.raises(TrumpNewsError, match="YouTube n'est pas prêt"):
        Autopilot(ctx, pipeline=fake_pipeline(ctx, tmp_path, {}), publisher=lambda n: FakeYouTube(ok=False)).run()
    ctx.cfg.autopilot.max_per_day = 1
    other = add_story(ctx, "Published", 0.1)
    vid = fake_pipeline(ctx, tmp_path, {})(other, lambda *a: None).video_id
    with session_scope(ctx.factory) as s:
        s.add(Publication(video_id=vid, platform="youtube", status="PUBLISHED"))
    pipe = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube()).run()
    assert "Plafond atteint" in rep.message and pipe.calls == [] and sid not in statuses(ctx)


def test_technical_error_stops_without_burning_stories(ctx, tmp_path):
    a = add_story(ctx, "A", 0.9)
    b = add_story(ctx, "B", 0.8)
    pipe = fake_pipeline(ctx, tmp_path, {a: ("FAILED", None, None)})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube()).run()
    assert pipe.calls == [a] and rep.attempts[0].status == "FAILED" and "Erreur technique" in rep.message
    pipe2 = fake_pipeline(ctx, tmp_path, {})
    Autopilot(ctx, pipeline=pipe2, publisher=lambda n: FakeYouTube()).run()
    assert pipe2.calls[0] == a and b in pipe2.calls                   # le sujet en erreur est réessayé


def test_quota_error_keeps_video_for_next_run(ctx, tmp_path):
    a = add_story(ctx, "A", 0.9)
    b = add_story(ctx, "B", 0.8)
    pipe = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube(fail=PublisherUnavailableError("Quota YouTube"))).run()
    assert pipe.calls == [a] and rep.attempts[0].status == "RETRY" and "prochain lancement" in rep.message
    ctx.cfg.autopilot.videos_per_run = 1
    yt = FakeYouTube()
    pipe2 = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe2, publisher=lambda n: yt).run()
    assert pipe2.calls == [] and rep.attempts[0].status == "PUBLISHED" and rep.attempts[0].story_id == a  # pas remontée
    assert statuses(ctx)[a] in ("PUBLISHED", "RETRIED") and b not in statuses(ctx)


def test_cancel_between_stories(ctx, tmp_path):
    add_story(ctx, "A", 0.9)
    add_story(ctx, "B", 0.8)
    token = CancelToken()
    inner = fake_pipeline(ctx, tmp_path, {})

    def pipe(sid, progress):
        token.cancel()                                               # demandé pendant le premier sujet
        return inner(sid, progress)

    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube(), token=token).run()
    assert inner.calls == [1] and "annulée" in rep.message


def test_cli_autopilot(tmp_path, monkeypatch, capsys):
    import app.main as m

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Autopilot, "run", lambda self: __import__("app.autopilot.service", fromlist=["x"])
                        .AutopilotReport("r1", message="Aucun nouveau sujet à traiter."))
    assert m.main(["autopilot"]) == 0 and "Aucun nouveau sujet" in capsys.readouterr().out


def fake_recheck(ctx):
    """Le recontrôle réel lance FFmpeg : ici, la vidéo passe READY si son script est approuvé."""
    from types import SimpleNamespace as NS

    def check(vid):
        with session_scope(ctx.factory) as s:
            v = s.get(Video, vid)
            v.status = "READY" if s.get(Script, v.script_id).status == "APPROVED" else "NEEDS_REVIEW"
            old = s.scalar(select(VideoReport).where(VideoReport.video_id == vid).order_by(VideoReport.id.desc()))
            s.add(VideoReport(video_id=vid, score=0.9, status=v.status, data=dict(old.data)))
            return NS(status=v.status)

    ctx.video_service = lambda: NS(check=check)


def test_minor_warnings_are_approved_automatically_and_traced(ctx, tmp_path):
    from app.database.models import Review

    sid = add_story(ctx, "Minor", 0.9)
    fake_recheck(ctx)
    minor = ["Segment 2 : attribution ajoutée automatiquement (source réelle : CBS News Politics).",
             "62 mots : visez 112 (entre 90 et 129) pour 45 s"]
    yt = FakeYouTube()
    rep = Autopilot(ctx, pipeline=fake_pipeline(ctx, tmp_path, {sid: ("NEEDS_REVIEW", "CAUTION", "NEEDS_REVIEW", minor)}),
                    publisher=lambda n: yt).run()
    assert rep.attempts[0].status == "PUBLISHED" and len(yt.sent) == 1
    with session_scope(ctx.factory) as s:
        r = s.scalar(select(Review))
        assert (r.reviewer, r.decision, r.sources_checked, r.factcheck_verdict) == ("pilote automatique", "APPROVED", False, "CAUTION")
        assert "2 avertissement(s) mineur(s)" in r.note


@pytest.mark.parametrize("extra, reason", [
    ({"failed": ["carte des sources"]}, "carte des sources"),
    ({"claim": "DISPUTED"}, "DISPUTED"),
    ({"warnings": ["segment 1: nom propre « Department » absent des affirmations citées"]}, "Department"),
])
def test_serious_problems_are_never_approved(ctx, tmp_path, extra, reason):
    sid = add_story(ctx, "Serious", 0.9)
    fake_recheck(ctx)
    outcome = ("NEEDS_REVIEW", "CAUTION", "NEEDS_REVIEW", extra.get("warnings", []), extra.get("failed", []),
               extra.get("claim", "REPORTED"))
    yt = FakeYouTube()
    rep = Autopilot(ctx, pipeline=fake_pipeline(ctx, tmp_path, {sid: outcome}), publisher=lambda n: yt).run()
    assert rep.attempts[0].status == "REJECTED" and reason in rep.attempts[0].message and not yt.sent
    with session_scope(ctx.factory) as s:
        assert s.scalar(select(Script)).status == "NEEDS_REVIEW"                 # jamais approuvé


def test_auto_approval_can_be_disabled(ctx, tmp_path):
    sid = add_story(ctx, "Minor", 0.9)
    ctx.cfg.autopilot.auto_approve_minor = False
    rep = Autopilot(ctx, pipeline=fake_pipeline(ctx, tmp_path, {sid: ("NEEDS_REVIEW", "SAFE", "NEEDS_REVIEW")}),
                    publisher=lambda n: FakeYouTube()).run()
    assert rep.attempts[0].status == "REJECTED" and "auto_approve_minor" in rep.attempts[0].message


def test_videos_skipped_by_previous_version_are_rechecked_once(ctx, tmp_path):
    sid = add_story(ctx, "Old run", 0.9)
    fake_recheck(ctx)
    res = fake_pipeline(ctx, tmp_path, {sid: ("NEEDS_REVIEW", "CAUTION", "NEEDS_REVIEW",
                                              ["54 mots : visez 112 (entre 90 et 129) pour 45 s"])})(sid, lambda *a: None)
    with session_scope(ctx.factory) as s:
        s.add(AutopilotAttempt(run_id="old", story_id=sid, status="SKIPPED", video_id=res.video_id, message="à relire"))
    yt = FakeYouTube()
    pipe = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: yt).run()
    assert pipe.calls == [] and rep.attempts[0].status == "PUBLISHED" and rep.attempts[0].video_id == res.video_id
    with session_scope(ctx.factory) as s:
        assert sorted(a.status for a in s.scalars(select(AutopilotAttempt))) == ["PUBLISHED", "RECHECKED"]
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: yt).run()
    assert rep.attempts == [] and len(yt.sent) == 1                               # pas de second examen ni doublon

def test_refused_verdict_stops_early_and_setting_change_unlocks_videos(ctx, tmp_path):
    a = add_story(ctx, "Caution video", 0.9)
    b = add_story(ctx, "Caution new", 0.8)
    fake_recheck(ctx)
    ctx.cfg.autopilot.allow_caution = False
    outcome = ("NEEDS_REVIEW", "CAUTION", "NEEDS_REVIEW", ["54 mots : visez 112 (entre 90 et 129) pour 45 s"])
    inner = fake_pipeline(ctx, tmp_path, {a: outcome})

    def pipe(sid, progress):                     # le vrai pipeline s'arrête après le fact-check (allowed_verdicts)
        if sid == b:
            inner.calls.append(sid)
            return PipelineResult(sid, "NOT_ALLOWED", message="Fact-check CAUTION non retenu par les réglages : pas de vidéo.")
        return inner(sid, progress)

    yt = FakeYouTube()
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: yt).run()
    assert [x.status for x in rep.attempts] == ["REJECTED", "NOT_ALLOWED"] and not yt.sent
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: yt).run()     # même raison : rien de réenregistré
    assert [x.story_id for x in rep.attempts] == [b]                       # b reste candidat, a n'est pas remonté
    ctx.cfg.autopilot.allow_caution = True
    ctx.cfg.autopilot.videos_per_run = 1
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: yt).run()
    assert rep.attempts[0].status == "PUBLISHED" and rep.attempts[0].story_id == a and len(yt.sent) == 1
    assert inner.calls.count(a) == 1                                       # publiée sans être remontée

def test_neighbor_of_recent_publication_is_skipped(ctx, tmp_path):
    a = add_story(ctx, "Trump waives Russia sanctions before deadline", 0.9)
    b = add_story(ctx, "Kremlin welcomes Trump decision to waive Russia sanctions", 0.8)
    c = add_story(ctx, "Supreme Court hears tariff case", 0.7)
    ctx.cfg.autopilot.videos_per_run = 2
    pipe = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube()).run()
    assert pipe.calls == [a, c] and [x.story_id for x in rep.published] == [a, c]       # b : même affaire que a
    ctx.cfg.autopilot.neighbor_hours = 0
    assert Autopilot(ctx, publisher=lambda n: FakeYouTube()).neighbor_of("Trump waives Russia sanctions") is None


def test_interrupted_run_is_resumed_without_regenerating(ctx, tmp_path):
    a = add_story(ctx, "A", 0.9)
    b = add_story(ctx, "B", 0.8)
    res = fake_pipeline(ctx, tmp_path, {})(a, lambda *x: None)                     # vidéo montée puis PC éteint
    with session_scope(ctx.factory) as s:
        s.add(AutopilotAttempt(run_id="old", story_id=a, status="RUNNING", message="en cours"))
        s.add(AutopilotAttempt(run_id="old", story_id=b, status="RUNNING", message="en cours"))   # coupé avant montage
    ctx.cfg.autopilot.videos_per_run = 2
    pipe = fake_pipeline(ctx, tmp_path, {})
    rep = Autopilot(ctx, pipeline=pipe, publisher=lambda n: FakeYouTube()).run()
    assert pipe.calls == [b]                                                          # a n'est pas remontée
    assert {x.story_id: x.status for x in rep.attempts} == {a: "PUBLISHED", b: "PUBLISHED"}
    assert rep.attempts[0].video_id == res.video_id
    with session_scope(ctx.factory) as s:
        assert not list(s.scalars(select(AutopilotAttempt).where(AutopilotAttempt.status == "RUNNING")))