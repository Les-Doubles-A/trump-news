from pathlib import Path

import pytest

from app.video.alignment import align_segment, caption_words, estimate_segment, script_tokens
from app.video.models import CaptionWord, RenderJob, Shot
from app.video.subtitles import ass_color, ass_time, build_ass, chunk_words, escape
from app.video.templates import get_template


def words(*items):
    return [{"text": t, "start": a, "end": b} for t, a, b in items]


def test_tokens_keep_punctuation_attached():
    assert script_tokens("Trump — again, said: “No.”") == ["Trump —", "again,", "said:", "“No.”"]


def test_alignment_exact_and_pronunciation_replacement():
    tts = words(("Trump's", 0.0, 0.3), ("Nay", 0.35, 0.5), ("Toe", 0.5, 0.7), ("pledge", 0.75, 1.1))
    out = align_segment("Trump's NATO pledge.", tts, 0.0, 1.2)
    assert [w.text for w in out] == ["Trump's", "NATO", "pledge."]          # orthographe du script, pas de la voix
    assert (out[0].start, out[0].end) == (0.0, 0.3)
    assert out[1].start == 0.35 and out[1].end == pytest.approx(0.7)        # un mot du script = deux mots prononcés
    assert out[2].start == 0.75 and not any(w.estimated for w in out)


def test_alignment_interpolates_unspoken_tokens_and_stays_monotonic():
    tts = words(("Prices", 1.0, 1.4), ("rose", 1.5, 1.8))
    out = align_segment("Prices rose 5% today", tts, 1.0, 3.0)
    assert [w.text for w in out] == ["Prices", "rose", "5%", "today"]
    assert out[2].start >= out[1].end and out[3].end <= 3.0 + 1e-6
    assert all(b.start >= a.start for a, b in zip(out, out[1:]))


def test_estimation_without_word_timings():
    out = estimate_segment("short averyveryverylongword", 2.0, 4.0, segment=3)
    assert out[0].start == 2.0 and out[-1].end == pytest.approx(4.0) and all(w.estimated and w.segment == 3 for w in out)
    assert (out[1].end - out[1].start) > (out[0].end - out[0].start)       # au prorata des caractères
    tl = [{"index": 0, "text": "Hello world", "start": 0, "end": 1, "words": words(("Hello", 0, .4), ("world", .5, .9))},
          {"index": 1, "text": "No timings here", "start": 1.2, "end": 2.5, "words": []}]
    ws, estimated = caption_words(tl)
    assert len(ws) == 5 and estimated and not ws[0].estimated and ws[2].estimated


def cw(text, a, b, seg=0):
    return CaptionWord(text, a, b, seg)


def test_chunking_rules():
    ws = [cw("One", 0, .2), cw("two", .2, .4), cw("three", .4, .6), cw("four.", .6, .8), cw("Five", .8, 1.0),
          cw("six", 1.6, 1.8), cw("seven", 1.8, 2.0, seg=1)]
    chunks = [[w.text for w in c] for c in chunk_words(ws, max_words=3, max_chars=40)]
    # 3 mots max ; coupe après un point ; coupe sur un silence > 0,35 s ; coupe au changement de segment
    assert chunks == [["One", "two", "three"], ["four."], ["Five"], ["six"], ["seven"]]
    long = [cw("extraordinarily", 0, .3), cw("complicated", .3, .6)]
    assert len(chunk_words(long, 3, 16)) == 2                                # limite de caractères


def test_ass_helpers():
    assert ass_color("FFD400") == "&H0000D4FF" and ass_color("#102030", 0x60) == "&H60302010"
    with pytest.raises(ValueError):
        ass_color("yellow")
    assert ass_time(0) == "0:00:00.00" and ass_time(3723.456) == "1:02:03.46"
    assert escape("a {b} c\\N d\n e") == "a (b) c/N d e"


def make_job(tmp_path, shots, words_, **kw):
    return RenderJob(width=1080, height=1920, fps=30, duration=4.0, voice_path=tmp_path / "v.wav", shots=shots, words=words_,
                     template=get_template("breaking_news"), work_dir=tmp_path, subtitles_path=tmp_path / "s.ass", **kw)


def shot(a, b, kind="photo", role="primary", **kw):
    return Shot(Path("x.jpg"), a, b, kind, role, 1600, 1000, **kw)


def test_ass_document_highlight_hidden_cards_credits_and_ai(tmp_path):
    shots = [shot(0, 1.5, kind="card", hide_captions=True), shot(1.5, 3, credit="Photo X / Wikimedia Commons / CC BY 4.0"),
             shot(3, 4, ai_generated=True)]
    ws = [cw("Hidden", .2, .5), cw("words.", .5, .9), cw("Shown", 1.6, 1.9), cw("{here}", 1.9, 2.3), cw("now", 3.1, 3.5)]
    text, stats = build_ass(make_job(tmp_path, shots, ws, language="fr"))
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "HIDDEN" not in text and "SHOWN" in text and "(HERE)" in text     # majuscules du template, accolades neutralisées
    assert "{\\c&H0000D4FF&" not in text and "\\c&H0000D4FF\\fscx110" in text
    assert "\\an5\\pos(540,1229)" in text
    assert "Photo X / Wikimedia Commons" in text and "Illustration générée par IA" in text
    assert (stats.words_total, stats.words_hidden, stats.words_shown) == (5, 2, 3) and stats.coverage == 1.0
    assert stats.credit_events == 1 and stats.ai_events == 1
    caption_lines = [line for line in text.splitlines() if ",Caption," in line]
    assert len(caption_lines) == 3                                           # un événement par mot affiché
    first = caption_lines[0]
    assert first.startswith("Dialogue: 1,0:00:01.60,0:00:01.90,Caption")     # se termine au début du mot suivant


def test_subtitles_disabled_still_writes_credits(tmp_path):
    job = make_job(tmp_path, [shot(0, 4, credit="Photo Y")], [cw("Hello", 0, .5)], subtitles=False)
    text, stats = build_ass(job)
    assert stats.events == 0 and stats.words_shown == 0 and stats.credit_events == 1 and "Photo Y" in text
