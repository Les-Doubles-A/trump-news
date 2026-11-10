"""Montage réel avec FFmpeg (petites vidéos de 4 s). Sauté automatiquement si FFmpeg/ffprobe sont absents."""
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.config.schema import AppConfig
from app.database.models import FactCheck, Narration, Script, Story, Video, VideoReport, VisualPlanRow
from app.database.session import create_db_engine, init_db, session_scope
from app.providers.video.ffmpeg_renderer import FFmpegRenderer
from app.utils.ffmpeg import ffprobe, find_ffprobe
from app.video.service import VideoService
from app.visuals.cards import render_card

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg absent")

SEGMENTS = [
    {"index": 0, "section": "hook", "type": "fact", "text": "A judge blocked the order on Monday.", "claims": ["C1"]},
    {"index": 1, "section": "conclusion", "type": "fact", "text": "Reuters and the BBC reported the ruling.", "claims": ["C1"]},
]
TIMELINE = [
    {"index": 0, "section": "hook", "text": SEGMENTS[0]["text"], "start": 0.0, "end": 1.8, "audio": "",
     "words": [{"text": w, "start": round(i * 0.25, 3), "end": round(i * 0.25 + 0.22, 3)}
               for i, w in enumerate(["A", "judge", "blocked", "the", "order", "on", "Monday"])]},
    {"index": 1, "section": "conclusion", "text": SEGMENTS[1]["text"], "start": 2.1, "end": 4.0, "audio": "", "words": []},
]


def tone_wav(path: Path, seconds: float = 4.0, rate: int = 24000, gaps=((1.8, 2.1),)) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for n in range(int(seconds * rate)):
            t = n / rate
            amp = 0 if any(a <= t < b for a, b in gaps) else 0.25
            frames += struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * 220 * t)))
        w.writeframes(bytes(frames))
    return path


def build_env(tmp: Path, *, verdict="SAFE", status="DRAFT", ai=False, db="t.db"):
    data = tmp / "data"
    for d in ("images", "audio", "videos", "subtitles", "thumbnails"):
        (data / d).mkdir(parents=True, exist_ok=True)
    voice = tone_wav(data / "audio" / "voice.wav")
    card = render_card("headline", SEGMENTS[0]["text"], data / "images" / "card.png")
    photo = data / "images" / "photo.jpg"
    im = Image.new("RGB", (1600, 1000), (180, 40, 40))
    for x in range(0, 1600, 100):
        im.paste((40, 120, 200), (x, 0, x + 50, 1000))
    im.save(photo, "JPEG")
    sources = render_card("sources", "Sources : Reuters · BBC News", data / "images" / "sources.png")
    factory = init_db(create_db_engine(data / db))
    items = [
        {"segment_index": 0, "shot_index": 0, "start": 0.0, "end": 1.8, "role": "primary",
         "asset": {"provider": "cards", "external_id": "c1", "path": str(card), "kind": "card", "width": 1080, "height": 1920}},
        {"segment_index": 1, "shot_index": 0, "start": 2.1, "end": 3.0, "role": "primary",
         "asset": {"provider": "ai" if ai else "wikimedia", "external_id": "p1", "path": str(photo), "kind": "photo",
                   "width": 1600, "height": 1000, "attribution": None if ai else "Photo Judge.jpg / Wikimedia Commons / CC BY 4.0",
                   "ai_generated": ai}},
        {"segment_index": 1, "shot_index": 1, "start": 3.0, "end": 4.0, "role": "sources",
         "asset": {"provider": "cards", "external_id": "s1", "path": str(sources), "kind": "card", "width": 1080, "height": 1920}},
    ]
    with session_scope(factory) as s:
        st = Story(title="Judge blocks order", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", verdict=verdict, data={}))
        sc = Script(story_id=st.id, style="breaking_news", tone="neutral", language="en", duration_target=20, full_script="x",
                    status=status, source_refs=[], claims={"segments": SEGMENTS, "claims": [], "visuals": []})
        s.add(sc)
        s.flush()
        nar = Narration(script_id=sc.id, provider="edge", voice="v", language="en", audio_path=str(voice), duration=4.0,
                        timeline=TIMELINE, input_hash="h")
        s.add(nar)
        s.flush()
        s.add(VisualPlanRow(script_id=sc.id, narration_id=nar.id, ai_disclosure=ai,
                            plan={"script_id": sc.id, "items": items, "credits": ["Photo Judge.jpg / Wikimedia Commons / CC BY 4.0"]}))
    return factory, data


def service(factory, data, tmp, **video):
    cfg = AppConfig.model_validate({"video": video})
    return VideoService(cfg, factory, data, base_dir=tmp)


def probe(path):
    info = ffprobe(path, find_ffprobe(shutil.which("ffmpeg")))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return info, v


def test_renderer_health():
    assert FFmpegRenderer().health_check().ok
    assert not FFmpegRenderer("C:/nope/ffmpeg.exe").health_check().ok


def test_full_render_quality_ready(tmp_path):
    factory, data = build_env(tmp_path)
    o = service(factory, data, tmp_path).render(1)
    info, v = probe(o.path)
    assert (v["width"], v["height"], v["codec_name"], v["pix_fmt"]) == (1080, 1920, "h264", "yuv420p")
    assert abs(float(info["format"]["duration"]) - 4.0) < 0.15
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    crit = {c.name: c for c in o.report.checks if c.critical}
    assert all(c.ok for c in crit.values()), [c for c in crit.values() if not c.ok]
    assert o.status == "READY" and o.report.score >= 0.7
    assert o.thumbnail and o.thumbnail.exists() and o.subtitles.exists()
    ass = o.subtitles.read_text(encoding="utf-8")
    assert "REUTERS" in ass and "Wikimedia Commons" in ass and "JUDGE" not in ass   # hook masqué : texte déjà sur la carte
    assert not (data / "videos" / "work" / "video_0001").exists()                  # dossier de travail nettoyé
    with session_scope(factory) as s:
        row = s.get(Video, o.video_id)
        assert row.status == "READY" and row.video_path == str(o.path) and row.quality_score == o.report.score
        assert s.query(VideoReport).count() == 1
    assert o.path.with_suffix(".json").exists()

    o2 = service(factory, data, tmp_path).check(o.video_id)        # contrôle refait sur le fichier existant
    assert o2.status == "READY"


def test_preview_with_music_and_sfx_never_ready(tmp_path):
    factory, data = build_env(tmp_path, ai=True)
    music, sfx = tmp_path / "assets" / "music", tmp_path / "assets" / "sfx"
    music.mkdir(parents=True)
    sfx.mkdir(parents=True)
    tone_wav(music / "calm.wav", 2.0, gaps=())                      # plus courte que la vidéo : bouclée
    (music / "calm.txt").write_text("licence: CC0\ncredit: Calm by Test\n", encoding="utf-8")
    tone_wav(sfx / "whoosh.wav", 0.3, gaps=())
    (sfx / "whoosh.txt").write_text("licence: CC0\n", encoding="utf-8")
    tone_wav(music / "nolicence.wav", 1.0, gaps=())
    o = service(factory, data, tmp_path, music=True, sfx=True).render(1, preview=True)
    _, v = probe(o.path)
    assert (v["width"], v["height"]) == (540, 960)
    assert o.status == "NEEDS_REVIEW"
    assert not next(c for c in o.report.checks if c.name == "définition finale").ok
    assert next(c for c in o.report.checks if c.name == "mention IA").ok
    assert "Illustration" in o.subtitles.read_text(encoding="utf-8") or "AI-generated" in o.subtitles.read_text(encoding="utf-8")
    assert any("Calm by Test" in c for c in o.credits)
    assert any("nolicence.wav" in w for w in o.warnings)


def test_blocked_fact_check_and_review_script_need_review(tmp_path):
    factory, data = build_env(tmp_path, verdict="BLOCKED", status="NEEDS_REVIEW")
    o = service(factory, data, tmp_path, transitions=False, subtitles=False).render(1, preview=True)
    failed = {c.name for c in o.report.checks if not c.ok}
    assert {"fact-check", "script validé", "sous-titres"} <= failed and o.status == "NEEDS_REVIEW"


def test_cli_render_videos_check(tmp_path, monkeypatch, capsys):
    import app.main as m

    monkeypatch.chdir(tmp_path)
    assert m.main(["init"]) == 0
    build_env(tmp_path, db="trump_news.db")
    assert m.main(["render", "1", "--preview"]) == 0
    out = capsys.readouterr().out
    assert "Vidéo #1" in out and "NEEDS REVIEW" in out and "Crédits" in out
    assert m.main(["videos"]) == 0 and "#1" in capsys.readouterr().out
    assert m.main(["video-check", "1"]) == 0
    assert m.main(["templates"]) == 0 and "breaking_news" in capsys.readouterr().out
    assert m.main(["music"]) == 0
