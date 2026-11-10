from types import SimpleNamespace as NS

from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.core.health import check_memory
from app.pipeline.runner import CancelToken, PipelineOptions, PipelineResult, PipelineRunner
from app.script.models import ScriptBlockedError
from app.utils.sysmem import MemoryStatus


class FakeCtx:
    """Contexte minimal : chaque fabrique renvoie un faux service qui enregistre ses appels."""

    def __init__(self, verdict="CAUTION", script_error=None, render_error=None, on_narrate=None):
        self.calls = []
        self.verdict, self.script_error, self.render_error, self.on_narrate = verdict, script_error, render_error, on_narrate

    def fact_checker(self):
        return NS(check=lambda sid, force=False: self._log("fc", sid, force) or NS(verdict=self.verdict, claims=[1, 2]))

    def script_generator(self):
        def gen(sid, **kw):
            self._log("script", sid, kw["duration_s"])
            if self.script_error:
                raise self.script_error
            return NS(id=7, status="NEEDS_REVIEW", word_count=80)
        return NS(generate=gen)

    def narration(self):
        def narrate(sid):
            self._log("narrate", sid)
            if self.on_narrate:
                self.on_narrate()
            return NS(duration_s=30.0, voice="v")
        return NS(narrate=narrate)

    def visual_planner(self):
        return NS(plan=lambda sid: self._log("visuals", sid) or NS(items=[1, 2, 3]))

    def video_service(self):
        def render(sid, template=None, preview=False):
            self._log("render", sid, template, preview)
            if self.render_error:
                raise self.render_error
            return NS(video_id=3, path="v.mp4", status="NEEDS_REVIEW", report=NS(score=0.9))
        return NS(render=render)

    def _log(self, *a):
        self.calls.append(a)


def test_full_chain_with_progress():
    ctx, seen = FakeCtx(), []
    res = PipelineRunner(ctx, progress=lambda i, n, m: seen.append((i, n))).run(
        5, PipelineOptions(duration_s=30, template="minimal", preview=True, force_factcheck=True))
    assert [c[0] for c in ctx.calls] == ["fc", "script", "narrate", "visuals", "render"]
    assert ctx.calls[0] == ("fc", 5, True) and ctx.calls[1] == ("script", 5, 30) and ctx.calls[-1] == ("render", 7, "minimal", True)
    assert (res.status, res.script_id, res.video_id) == ("NEEDS_REVIEW", 7, 3) and all(s.ok for s in res.steps)
    assert seen[0] == (1, 5) and seen[-1] == (5, 5)


def test_guardrails_stop_the_chain_cleanly():
    ctx = FakeCtx(verdict="BLOCKED")
    res = PipelineRunner(ctx).run(5)
    assert res.status == "BLOCKED" and len(ctx.calls) == 1
    ctx = FakeCtx(verdict="CAUTION")
    res = PipelineRunner(ctx).run(5, PipelineOptions(allowed_verdicts=("SAFE",)))
    assert res.status == "NOT_ALLOWED" and len(ctx.calls) == 1 and "CAUTION" in res.message
    ctx = FakeCtx(script_error=ScriptBlockedError("hook non conforme"))
    res = PipelineRunner(ctx).run(5)
    assert res.status == "SCRIPT_BLOCKED" and not res.steps[-1].ok and "hook" in res.steps[-1].detail
    ctx = FakeCtx(render_error=TrumpNewsError("FFmpeg introuvable"))
    res = PipelineRunner(ctx).run(5)
    assert res.status == "FAILED" and "Montage" in res.message and "FFmpeg" in res.message


def test_cancel_between_steps_and_produce():
    token = CancelToken()
    ctx = FakeCtx(on_narrate=token.cancel)                     # annulation demandée pendant la voix off
    res = PipelineRunner(ctx, token=token).run(5)
    assert res.status == "CANCELLED" and [c[0] for c in ctx.calls] == ["fc", "script", "narrate"]   # l'étape va au bout
    ctx = FakeCtx()
    res = PipelineRunner(ctx).produce(7, 5, PipelineOptions(preview=True))
    assert [c[0] for c in ctx.calls] == ["narrate", "visuals", "render"] and res.video_id == 3 and res.status == "NEEDS_REVIEW"


def test_memory_warning():
    ok = check_memory(lambda: MemoryStatus(32, 10, 48, 20))
    assert ok.ok and not ok.warning
    low = check_memory(lambda: MemoryStatus(31.8, 5, 31.8, 3.3))
    assert low.warning and "fichier de pagination" in low.detail
    assert check_memory(lambda: None).detail.startswith("non mesurée")
    assert check_memory(lambda: 1 / 0).ok is False                      # ne lève jamais


def test_app_context_builds_services_and_reloads(tmp_path, monkeypatch):
    from app.audio.narration import NarrationService
    from app.review.service import ReviewService
    from app.video.service import VideoService
    from app.visuals.planner import VisualPlanner

    ctx = AppContext.load(base_dir=tmp_path, with_logging=False)
    assert (tmp_path / "data" / "trump_news.db").exists() and ctx.data_root == tmp_path / "data"
    assert isinstance(ctx.narration(), NarrationService) and isinstance(ctx.visual_planner(), VisualPlanner)
    assert isinstance(ctx.video_service(), VideoService) and isinstance(ctx.reviews(), ReviewService)
    assert ctx.llm() is ctx.llm()                                       # un seul client LLM
    monkeypatch.setenv("TRUMPNEWS__VIDEO__TEMPLATE", "political")
    assert ctx.reload_config().video.template == "political"


def test_cli_approve_reject_pipeline(tmp_path, monkeypatch, capsys):
    import app.main as m
    from app.database.models import FactCheck, Script, Story
    from app.database.session import create_db_engine, init_db, session_scope

    monkeypatch.chdir(tmp_path)
    assert m.main(["init"]) == 0
    factory = init_db(create_db_engine(tmp_path / "data" / "trump_news.db"))
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", verdict="CAUTION", data={}))
        s.add(Script(story_id=st.id, style="s", tone="neutral", language="en", duration_target=30, full_script="x",
                     status="NEEDS_REVIEW", claims={"segments": [], "claims": []}))
    assert m.main(["approve", "1"]) == 2 and "--verifie" in capsys.readouterr().err
    assert m.main(["approve", "1", "--verifie"]) == 2 and "Note obligatoire" in capsys.readouterr().err
    assert m.main(["approve", "1", "--verifie", "--note", "relu"]) == 0 and "APPROUVÉ" in capsys.readouterr().out
    assert m.main(["reject", "1", "--note", "non"]) == 0 and "REFUSÉ" in capsys.readouterr().out
    monkeypatch.setattr(PipelineRunner, "run", lambda self, sid, opts: PipelineResult(sid, "BLOCKED", message="sources insuffisantes"))
    assert m.main(["pipeline", "1"]) == 1 and "BLOCKED" in capsys.readouterr().out
