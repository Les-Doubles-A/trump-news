"""Sous-titres ASS lisibles sur mobile, mot par mot : blocs de 2 à 4 mots, mot en cours surligné,
placés hors des zones masquées par l'interface TikTok / Shorts / Reels. Le même fichier porte les crédits
photo et la mention « illustration générée par IA » (une seule police, aucun chemin à échapper)."""
from __future__ import annotations

import re
from pathlib import Path

from app.video.models import CaptionWord, RenderJob, SubtitleStats
from app.video.motion import shot_bounds

AI_LABEL = {"en": "AI-generated illustration", "fr": "Illustration générée par IA", "es": "Ilustración generada por IA"}
MAX_GAP_S = 0.35        # silence au-delà duquel un nouveau bloc commence
HOLD_S = 0.3            # maintien du dernier bloc après le dernier mot
_BREAK_AFTER = re.compile(r"[.!?;:…]$|[.!?]\W+$")


def chunk_words(words: list[CaptionWord], max_words: int, max_chars: int) -> list[list[CaptionWord]]:
    chunks: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in words:
        if cur:
            prev = cur[-1]
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if (len(cur) >= max_words or chars > max_chars or w.segment != prev.segment
                    or w.start - prev.end > MAX_GAP_S or _BREAK_AFTER.search(prev.text)
                    or (prev.text.endswith(",") and len(cur) >= 2)):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def ass_color(rrggbb: str, alpha: int = 0) -> str:
    """RRGGBB -> &HAABBGGRR (ordre inversé propre au format ASS)."""
    c = rrggbb.strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
        raise ValueError(f"couleur invalide : {rrggbb!r} (attendu RRGGBB)")
    return f"&H{alpha:02X}{c[4:6]}{c[2:4]}{c[0:2]}".upper()


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape(text: str) -> str:
    """Accolades et barres obliques inverses sont des commandes ASS : on les neutralise."""
    return re.sub(r"\s+", " ", text.replace("\\", "/").replace("{", "(").replace("}", ")")).strip()


def _header(job: RenderJob) -> str:
    t, k = job.template, job.width / 1080
    font = job.font_family or t.font
    bold = -1
    caption = (f"Style: Caption,{font},{round(t.font_size * k)},{ass_color(t.text_color)},{ass_color(t.highlight_color)},"
               f"{ass_color(t.outline_color)},{ass_color('000000', 0x60)},{bold},0,0,0,100,100,0,0,1,"
               f"{t.outline * k:.1f},{t.shadow * k:.1f},5,{round(70 * k)},{round(70 * k)},0,1")
    credit = (f"Style: Credit,{font},{round(26 * k)},{ass_color('FFFFFF')},{ass_color('FFFFFF')},{ass_color('000000')},"
              f"{ass_color('000000', 0x70)},0,0,0,0,100,100,0,0,3,{3 * k:.1f},0,7,{round(40 * k)},{round(40 * k)},"
              f"{round(150 * k)},1")
    ai = (f"Style: AI,{font},{round(32 * k)},{ass_color('FFFFFF')},{ass_color('FFFFFF')},{ass_color('000000')},"
          f"{ass_color('000000', 0x40)},{bold},0,0,0,100,100,0,0,3,{6 * k:.1f},0,8,{round(40 * k)},{round(40 * k)},"
          f"{round(215 * k)},1")
    return "\n".join([
        "[Script Info]", "; Généré par Trump News (Phase 7)", "ScriptType: v4.00+", f"PlayResX: {job.width}",
        f"PlayResY: {job.height}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding",
        caption, credit, ai, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def _event(start: float, end: float, style: str, text: str, layer: int = 0) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"


def build_ass(job: RenderJob) -> tuple[str, SubtitleStats]:
    t = job.template
    stats = SubtitleStats(words_total=len(job.words), estimated=any(w.estimated for w in job.words))
    lines = [_header(job)]
    bounds = shot_bounds(job.shots, job.duration)
    hidden = [b for s, b in zip(job.shots, bounds) if s.hide_captions]

    if job.subtitles:
        x, y = job.width // 2, round(t.caption_y * job.height)
        hi = ass_color(t.highlight_color)
        max_words = t.max_words
        chunks = chunk_words(job.words, max_words, t.max_chars)
        for n, chunk in enumerate(chunks):
            mid = (chunk[0].start + chunk[-1].end) / 2
            if any(a <= mid < b for a, b in hidden):
                stats.words_hidden += len(chunk)
                continue
            nxt = chunks[n + 1][0].start if n + 1 < len(chunks) else job.duration
            end_all = min(max(chunk[-1].end + HOLD_S, chunk[-1].end), max(nxt, chunk[-1].end), job.duration)
            texts = [escape(w.text.upper() if t.uppercase else w.text) for w in chunk]
            for i, w in enumerate(chunk):
                a = chunk[0].start if i == 0 else w.start
                b = chunk[i + 1].start if i + 1 < len(chunk) else end_all
                if b - a < 0.02:
                    continue
                body = " ".join(
                    f"{{\\c{hi}\\fscx{t.highlight_scale}\\fscy{t.highlight_scale}}}{txt}{{\\r}}" if j == i else txt
                    for j, txt in enumerate(texts))
                lines.append(_event(a, b, "Caption", f"{{\\an5\\pos({x},{y})}}{body}", layer=1))
                stats.events += 1
            stats.words_shown += len(chunk)

    for shot, (a, b) in zip(job.shots, bounds):
        if shot.credit:
            lines.append(_event(a, b, "Credit", escape(shot.credit), layer=2))
            stats.credit_events += 1
        if shot.ai_generated:
            lines.append(_event(a, b, "AI", escape(AI_LABEL.get(job.language, AI_LABEL["en"])), layer=2))
            stats.ai_events += 1
    return "\n".join(lines) + "\n", stats


def write_ass(job: RenderJob, path: Path | None = None) -> SubtitleStats:
    text, stats = build_ass(job)
    out = Path(path or job.subtitles_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return stats
