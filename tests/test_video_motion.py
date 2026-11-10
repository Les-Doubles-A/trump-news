from pathlib import Path

import pytest

from app.core.interfaces import MusicTrack
from app.video.models import RenderJob, Shot
from app.video.motion import fills_frame, final_graph, plan_clips, shot_bounds, shot_filter, zoompan_expr
from app.video.templates import get_template


def shot(a, b=0.0, kind="photo", w=1600, h=1000):
    return Shot(Path("x.jpg"), a, b, kind, "primary", w, h)


def job(shots, duration=10.0, **kw):
    return RenderJob(width=1080, height=1920, fps=30, duration=duration, voice_path=Path("v.wav"), shots=shots, words=[],
                     template=get_template(kw.pop("template", "breaking_news")), work_dir=Path("w"),
                     subtitles_path=Path("s.ass"), **kw)


def test_shot_bounds_cover_the_whole_voice():
    shots = [shot(0.2), shot(3.0), shot(7.4)]
    assert shot_bounds(shots, 10.0) == [(0.0, 3.0), (3.0, 7.4), (7.4, 10.0)]


def test_clip_plan_keeps_cuts_on_segment_starts_and_total_duration():
    shots = [shot(0), shot(3.0), shot(7.4)]
    p = plan_clips(shots, 10.0, 30, 0.3)
    assert p.total_frames == 300 and p.transition_frames == 8                # 0,3 s -> 9 images -> arrondi pair
    c0, c1, c2 = p.clips
    assert (c0.first, c0.frames) == (0, 94)                                 # 90 + 4 (moitié de la transition)
    assert (c1.first, c1.frames) == (86, 226 - 86)                          # 90-4 .. 222+4
    assert c2.first + c2.frames == 300                                      # se termine avec la voix
    # en enchaînant les fondus (offset = first), la durée finale vaut exactement celle de la voix
    length = c0.frames
    for c in (c1, c2):
        assert length == c.first + p.transition_frames
        length = c.first + c.frames
    assert length == 300


def test_clip_plan_edge_cases():
    p = plan_clips([shot(0)], 4.0, 30, 0.5)
    assert len(p.clips) == 1 and p.transition_frames == 0 and p.clips[0].frames == 120
    p = plan_clips([shot(0), shot(0.01), shot(2), shot(9.5)], 4.0, 30, 0.3)  # prise de 0 image remplacée ; prise après la voix ignorée
    assert [c.index for c in p.clips] == [1, 2]
    p = plan_clips([shot(0), shot(0.1), shot(2)], 4.0, 30, 0.6)             # prise très courte : transition réduite
    assert p.transition_frames <= 0.4 * 3
    with pytest.raises(ValueError):
        plan_clips([], 4.0, 30, 0.3)


def test_framing_and_ken_burns():
    assert fills_frame(1080, 1920, 1080, 1920) and fills_frame(1000, 1800, 1080, 1920)
    assert not fills_frame(1600, 1000, 1080, 1920) and not fills_frame(1200, 1600, 1080, 1920)
    j = job([shot(0)])
    landscape = shot_filter(shot(0), 0, 90, j)
    assert "gblur" in landscape and "overlay=(W-w)/2:(H-h)/2" in landscape and "d=90:s=1080x1920:fps=30" in landscape
    assert "scale=2160:3840" in landscape and "out_range=tv" in landscape
    card = shot_filter(shot(0, kind="card", w=1080, h=1920), 2, 90, j)
    assert "gblur" not in card and "on/89" in card
    assert "scale=1080:1920," in shot_filter(shot(0), 0, 90, job([shot(0)], preview=True))
    for d in ("in", "out", "pan", "in_top"):
        assert all("," not in e for e in zoompan_expr(d, 0.1, 90))            # pas de virgule à échapper
    assert zoompan_expr("in", 0, 90)[0] == "1"


def test_final_graph_video_chain():
    shots = [shot(0), shot(3), shot(7)]
    j = job(shots, progress_bar=True)
    p = plan_clips(shots, 10.0, 30, 0.25)
    g = final_graph(j, p, voice_input=3, music_input=None, sfx_input=None, ass_name="subs.ass", fonts_dir="fonts")
    assert g.count("xfade=transition=slideleft") == 2 and f"offset={p.clips[1].first / 30:.4f}" in g
    assert "ass=subs.ass:fontsdir=fonts" in g and "overlay=x='-w+W*t/10.0000'" in g and "[vout]" in g
    assert "[3:a]aformat" in g and "loudnorm" not in g                    # voix seule déjà normalisée
    cut = final_graph(job(shots, transitions=False), plan_clips(shots, 10.0, 30, 0), voice_input=3, music_input=None,
                      sfx_input=None, ass_name=None, fonts_dir=None)
    assert "concat=n=3:v=1:a=0" in cut and "ass=" not in cut and "xfade" not in cut


def test_final_graph_music_ducking_and_sfx():
    shots = [shot(0), shot(3), shot(7)]
    track = MusicTrack(Path("m.mp3"), "m", "CC0")
    j = job(shots, music=track, music_volume_db=-21, sfx=track)
    p = plan_clips(shots, 10.0, 30, 0.3)
    g = final_graph(j, p, voice_input=3, music_input=4, sfx_input=5, ass_name=None, fonts_dir=None)
    assert "volume=-21dB" in g and "sidechaincompress" in g and "afade=t=out:st=8.500" in g
    assert "asplit=2[s0][s1]" in g and f"adelay={round(p.clips[1].first / 30 * 1000)}:all=1" in g
    assert "amix=inputs=4:duration=first:normalize=0" in g and "loudnorm=I=-16" in g and "aresample=48000[aout]" in g
