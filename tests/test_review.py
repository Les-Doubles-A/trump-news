import pytest

from app.config.schema import AppConfig
from app.core.errors import TrumpNewsError
from app.database.models import FactCheck, Review, Script, Story, Video, VideoReport
from app.database.session import create_db_engine, init_db, session_scope
from app.review.service import ReviewService
from app.video.quality import MediaAnalysis, QualityContext, evaluate
from app.video.service import VideoService

SEGMENTS = [{"index": 0, "section": "hook", "type": "attributed", "text": "According to CBS News, Trump visited Kentucky.",
             "claims": ["C1"]}]
CLAIMS = [{"id": "C1", "claim": "Trump visited Kentucky.", "kind": "fact", "status": "REPORTED"}]
REFS = [{"claim_id": "C1", "claim": "Trump visited Kentucky.", "status": "REPORTED",
         "sources": [{"name": "CBS News", "url": "https://cbs.example/1", "quote": "Trump visits Kentucky"}]}]


class GoodAnalyzer:
    def analyze(self, path):
        return MediaAnalysis(True, width=1080, height=1920, fps=30.0, vcodec="h264", pix_fmt="yuv420p", duration=10.0,
                             has_audio=True, integrated_lufs=-16.0, true_peak=-2.0)


def make_env(tmp_path, verdict="CAUTION", status="NEEDS_REVIEW", with_video=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    factory = init_db(create_db_engine(tmp_path / "r.db"))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    ctx = QualityContext(1080, 1920, 30, 10.0, hook="According to CBS News, Trump visited Kentucky.",
                         subtitles={"words_total": 7, "words_hidden": 0, "words_shown": 7, "credit_events": 0, "ai_events": 0},
                         has_sources_card=True, factcheck_verdict=verdict, script_status=status)
    from dataclasses import asdict
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        if verdict:
            s.add(FactCheck(story_id=st.id, input_hash="h", verdict=verdict, data={}))
        sc = Script(story_id=st.id, style="neutral_news", tone="neutral", language="en", duration_target=30, full_script="x",
                    status=status, source_refs=REFS, claims={"segments": SEGMENTS, "claims": CLAIMS, "warnings": ["w1"]})
        s.add(sc)
        s.flush()
        if with_video:
            v = Video(story_id=st.id, script_id=sc.id, video_path=str(video), duration=10.0, status="NEEDS_REVIEW")
            s.add(v)
            s.flush()
            s.add(VideoReport(video_id=v.id, score=1.0, status="NEEDS_REVIEW", data={"context": asdict(ctx), "template": "minimal"}))
    videos = lambda: VideoService(AppConfig(), factory, tmp_path, renderer=object(), analyzer=GoodAnalyzer())  # noqa: E731
    return factory, ReviewService(factory, video_service_factory=videos, reviewer="tester")


def test_approve_rules_and_video_becomes_ready(tmp_path):
    factory, svc = make_env(tmp_path)
    with pytest.raises(TrumpNewsError, match="vérifié"):
        svc.approve(1, sources_checked=False, note="ok")
    with pytest.raises(TrumpNewsError, match="Note obligatoire"):
        svc.approve(1, sources_checked=True, note="  ")                    # CAUTION : note exigée
    out = svc.approve(1, sources_checked=True, note="Citations relues sur CBS")
    assert out.decision == "APPROVED" and out.video_statuses == {1: "READY"}
    with session_scope(factory) as s:
        assert s.get(Script, 1).status == "APPROVED" and s.get(Video, 1).status == "READY"
        r = s.query(Review).one()
        assert (r.decision, r.reviewer, r.sources_checked, r.factcheck_verdict, r.note) == \
            ("APPROVED", "tester", True, "CAUTION", "Citations relues sur CBS")
    d = svc.detail(1)
    assert d.status == "APPROVED" and d.claims["C1"]["sources"][0]["quote"] == "Trump visits Kentucky"
    assert d.reviews[0]["decision"] == "APPROVED" and d.warnings == ["w1"]


def test_reject_makes_videos_unpublishable(tmp_path):
    factory, svc = make_env(tmp_path, verdict="SAFE", status="DRAFT")
    assert svc.reject(1, "hors sujet").video_statuses == {1: "NEEDS_REVIEW"}
    with session_scope(factory) as s:
        assert s.get(Script, 1).status == "REJECTED"


def test_safe_needs_no_note_blocked_never_approvable(tmp_path):
    _, svc = make_env(tmp_path / "a", verdict="SAFE", with_video=False)
    assert svc.approve(1, sources_checked=True).decision == "APPROVED" and svc.pending_count() == 0
    _, blocked = make_env(tmp_path / "b", verdict="BLOCKED")
    with pytest.raises(TrumpNewsError, match="BLOCKED"):
        blocked.approve(1, sources_checked=True, note="x")
    with pytest.raises(TrumpNewsError, match="introuvable"):
        blocked.approve(99, sources_checked=True, note="x")


def test_quality_script_status_values():
    base = dict(width=1080, height=1920, fps=30, expected_duration=10.0, has_sources_card=True, factcheck_verdict="SAFE",
                subtitles={"words_total": 3, "words_hidden": 0, "words_shown": 3})
    a = GoodAnalyzer().analyze(None)
    for status, ok in (("DRAFT", True), ("APPROVED", True), ("NEEDS_REVIEW", False), ("REJECTED", False)):
        check = next(c for c in evaluate(a, QualityContext(**base, script_status=status), 0.7).checks if c.name == "script validé")
        assert check.ok is ok, status
