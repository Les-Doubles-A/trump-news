import pytest

from app.config.schema import AppConfig
from app.core.errors import ConfigError, TrumpNewsError
from app.database.models import Narration, Script, Story, VisualPlanRow
from app.database.session import create_db_engine, init_db, session_scope
from app.providers.music.local import LocalMusicProvider, read_sidecar
from app.script.models import STYLES
from app.video.composer import compose, resolve_font
from app.video.quality import MediaAnalysis, QualityContext, evaluate, parse_analysis_log, sensationalism
from app.video.templates import STYLE_TEMPLATE, TEMPLATES, get_template

LOG = """
[Parsed_ebur128_1 @ 0x1] t: 0.5 TARGET:-23 LUFS M: -20.1 S:-120.7 I: -19.0 LUFS LRA: 0.0 LU
[Parsed_blackdetect_0 @ 0x2] black_start:0 black_end:0.8 black_duration:0.8
[silencedetect @ 0x3] silence_start: 2.5
[silencedetect @ 0x3] silence_end: 4.6 | silence_duration: 2.1
[silencedetect @ 0x3] silence_start: 9.2
[Parsed_ebur128_1 @ 0x1] Summary:

  Integrated loudness:
    I:         -16.4 LUFS
    Threshold: -26.5 LUFS

  True peak:
    Peak:       -1.7 dBFS
"""


def test_parse_analysis_log():
    lufs, peak, black, silence = parse_analysis_log(LOG, duration=10.0)
    assert lufs == -16.4 and peak == -1.7                     # résumé final, pas les mesures intermédiaires
    assert black == [(0.0, 0.8)] and silence == [(2.5, 4.6), (9.2, 10.0)]
    assert parse_analysis_log("rien") == (None, None, [], [])


def test_sensationalism():
    calm, _ = sensationalism("A federal judge blocked the order on Monday.")
    assert calm == 0
    loud, reasons = sensationalism("SHOCKING: you won't believe what Trump did!!", "en")
    assert loud >= 0.35 and any("shocking" in r for r in reasons) and any("exclamation" in r for r in reasons)
    fr, _ = sensationalism("C'est incroyable !", "fr")
    assert fr >= 0.35


def good_analysis(**kw):
    base = dict(ok=True, width=1080, height=1920, fps=30.0, vcodec="h264", pix_fmt="yuv420p", duration=20.02,
                has_audio=True, integrated_lufs=-16.2, true_peak=-1.6)
    return MediaAnalysis(**{**base, **kw})


def ctx(**kw):
    base = dict(width=1080, height=1920, fps=30, expected_duration=20.0, hook="A judge blocked the order.",
                subtitles={"words_total": 50, "words_hidden": 10, "words_shown": 40, "credit_events": 1, "ai_events": 0},
                shots_needing_credit=1, has_sources_card=True, factcheck_verdict="SAFE", script_status="DRAFT")
    return QualityContext(**{**base, **kw})


def test_quality_ready():
    r = evaluate(good_analysis(), ctx(), 0.7)
    assert r.status == "READY" and r.score == 1.0 and not r.failures


@pytest.mark.parametrize("analysis_kw, ctx_kw, failed", [
    ({"ok": False, "error": "corrompu"}, {}, "fichier lisible"),
    ({"width": 720}, {}, "résolution"),
    ({"fps": 25.0}, {}, "images/s"),
    ({"pix_fmt": "yuvj420p"}, {}, "codec"),
    ({"duration": 21.0}, {}, "durée"),
    ({"has_audio": False}, {}, "audio présent"),
    ({"black": [(3.0, 4.2)]}, {}, "pas d'écran noir"),
    ({}, {"subtitles": {"words_total": 50, "words_hidden": 0, "words_shown": 30, "credit_events": 1}}, "sous-titres"),
    ({}, {"has_sources_card": False}, "carte des sources"),
    ({}, {"shots_needing_credit": 2}, "crédits photo"),
    ({}, {"ai_shots": 1}, "mention IA"),
    ({}, {"factcheck_verdict": "BLOCKED"}, "fact-check"),
    ({}, {"factcheck_verdict": None}, "fact-check"),
    ({}, {"script_status": "NEEDS_REVIEW"}, "script validé"),
    ({}, {"preview": True}, "définition finale"),
])
def test_quality_critical_failures(analysis_kw, ctx_kw, failed):
    r = evaluate(good_analysis(**analysis_kw), ctx(**ctx_kw), 0.7)
    assert r.status == "NEEDS_REVIEW" and failed in {c.name for c in r.failures if c.critical}


def test_quality_score_penalties():
    one = evaluate(good_analysis(true_peak=-0.1), ctx(), 0.7)                 # pénalité légère : reste publiable
    assert one.status == "READY" and 0.7 <= one.score < 1
    two = evaluate(good_analysis(integrated_lufs=-24.0), ctx(hook="SHOCKING bombshell!!"), 0.7)
    assert two.status == "NEEDS_REVIEW" and two.score < 0.7 and all(c.ok for c in two.checks if c.critical)
    off = evaluate(good_analysis(), ctx(subtitles_enabled=False), 0.7)
    assert any(c.name == "sous-titres" and not c.critical and not c.ok for c in off.checks)


def test_templates_and_style_mapping():
    assert set(TEMPLATES) == {"breaking_news", "news_fast", "minimal", "documentary", "political"}
    assert set(STYLE_TEMPLATE) == set(STYLES) and set(STYLE_TEMPLATE.values()) <= set(TEMPLATES)
    assert get_template("auto", "fast_news").name == "news_fast" and get_template("auto", None).name == "minimal"
    assert get_template("documentary").max_words == 4
    with pytest.raises(ConfigError):
        get_template("tiktok_style")
    assert AppConfig.model_validate({"video": {"template": "political"}}).video.template == "political"
    with pytest.raises(Exception):
        AppConfig.model_validate({"video": {"template": "nope"}})


def test_font_resolution(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "seguibl.ttf").write_bytes(b"x")
    assert resolve_font(None, "Arial Black", tmp_path, fonts) == (fonts / "seguibl.ttf", "Segoe UI Black")   # repli
    with pytest.raises(TrumpNewsError):
        resolve_font("assets/fonts/missing.ttf", "Arial", tmp_path, fonts)


def test_local_music_requires_declared_licence(tmp_path):
    (tmp_path / "b.mp3").write_bytes(b"x")
    (tmp_path / "b.txt").write_text("﻿licence: CC BY 4.0\ncredit: \"Song\" by Jane\ntitle: Song\n", encoding="utf-8")
    (tmp_path / "a.wav").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    assert read_sidecar(tmp_path / "b.txt") == ("CC BY 4.0", "\"Song\" by Jane", "Song")
    prov = LocalMusicProvider(tmp_path)
    tracks = prov.list_tracks()
    assert [t.path.name for t in tracks] == ["b.mp3"] and prov.skipped == ["a.wav"]
    assert prov.pick(7).path.name == "b.mp3" and prov.pick(0, "b").title == "Song" and prov.pick(0, "zzz") is None
    assert LocalMusicProvider(tmp_path / "absent").list_tracks() == []


# ------------------------------------------------------------ composition
SEG = [{"index": 0, "section": "hook", "type": "fact", "text": "A judge ruled.", "claims": []}]


@pytest.fixture()
def db(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "c.db"))
    voice = tmp_path / "v.wav"
    from tests.test_video_render import tone_wav
    tone_wav(voice, 1.0, gaps=())
    img = tmp_path / "card.png"
    img.write_bytes(b"png")
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        sc = Script(story_id=st.id, style="explainer", tone="neutral", language="en", duration_target=20, full_script="x",
                    claims={"segments": SEG, "claims": [], "visuals": []})
        s.add(sc)
        s.flush()
    return factory, tmp_path, voice, img


def add_narration(factory, voice):
    with session_scope(factory) as s:
        n = Narration(script_id=1, provider="piper", voice="v", language="en", audio_path=str(voice), duration=1.0,
                      timeline=[{"index": 0, "section": "hook", "text": "A judge ruled.", "start": 0, "end": 1.0, "words": []}],
                      input_hash="h")
        s.add(n)
        s.flush()
        return n.id


def add_plan(factory, nid, path):
    with session_scope(factory) as s:
        s.add(VisualPlanRow(script_id=1, narration_id=nid, plan={"items": [
            {"segment_index": 0, "shot_index": 0, "start": 0, "end": 1, "role": "primary",
             "asset": {"provider": "cards", "external_id": "c", "path": str(path), "kind": "card", "width": 1080, "height": 1920}}],
            "credits": []}))


def test_compose_refuses_incoherent_inputs(db):
    factory, tmp, voice, img = db
    cfg = AppConfig()
    with pytest.raises(TrumpNewsError, match="narrate 1"):
        compose(cfg, factory, 1, tmp, base_dir=tmp)
    nid = add_narration(factory, voice)
    with pytest.raises(TrumpNewsError, match="visuals 1"):
        compose(cfg, factory, 1, tmp, base_dir=tmp)
    add_plan(factory, nid, img)
    add_narration(factory, voice)                          # nouvelle voix après le plan : plan obsolète
    with pytest.raises(TrumpNewsError, match="autre voix off"):
        compose(cfg, factory, 1, tmp, base_dir=tmp)
    with pytest.raises(TrumpNewsError, match="introuvable"):
        compose(cfg, factory, 99, tmp, base_dir=tmp)


def test_compose_job(db):
    factory, tmp, voice, img = db
    add_plan(factory, add_narration(factory, voice), img)
    c = compose(AppConfig.model_validate({"video": {"max_words_per_caption": 2, "music": True}}), factory, 1, tmp,
                base_dir=tmp, preview=True)
    assert c.template == "documentary" and c.job.template.max_words == 2           # auto : style explainer
    assert (c.job.width, c.job.height, c.job.preset) == (540, 960, "ultrafast")
    assert c.job.shots[0].hide_captions and c.context.preview and c.context.factcheck_verdict is None
    assert [w.text for w in c.job.words] == ["A", "judge", "ruled."] and c.job.words[0].estimated
    assert any("Temps des mots estimés" in w for w in c.warnings) and any("aucune piste" in w for w in c.warnings)
    img.unlink()
    with pytest.raises(TrumpNewsError, match="card.png"):
        compose(AppConfig(), factory, 1, tmp, base_dir=tmp)


def test_generated_music_loops_and_is_usable(tmp_path):
    import shutil
    import wave

    from app.audio.music_gen import RATE, make_track, synthesize

    raw = synthesize(tmp_path / "raw.wav", "news", duration_s=10)
    with wave.open(str(raw), "rb") as w:
        assert w.getframerate() == RATE and abs(w.getnframes() / RATE - 10) < 0.1   # 4 mesures de 2,5 s
    if shutil.which("ffmpeg"):
        out = make_track(tmp_path, shutil.which("ffmpeg"), "calm", duration_s=13)
        assert out.exists() and not (tmp_path / ".generated_calm_raw.wav").exists()
        track = LocalMusicProvider(tmp_path).pick(0, "generated_calm")
        assert track and track.license.startswith("CC0") and track.credit == ""     # licence déclarée, pas de crédit


def test_ffmpeg_out_of_memory_is_explained(monkeypatch):
    import subprocess

    from app.core.errors import ProviderError
    from app.utils import ffmpeg as ff

    fake = subprocess.CompletedProcess([], 1, b"", b"x264 [error]: malloc of size 7186688 failed\nError encoding a frame")
    monkeypatch.setattr(ff, "_run", lambda cmd, timeout, cwd: fake)
    with pytest.raises(ProviderError) as e:
        ff.run_ffmpeg(["-i", "x"], "ffmpeg")
    assert "manque de mémoire" in str(e.value) and "fichier de pagination" in str(e.value) and "malloc" in str(e.value)
