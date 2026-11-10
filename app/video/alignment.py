"""Temps de chaque mot affiché en sous-titre.

Le texte affiché est toujours celui du script ; les mots horodatés du moteur vocal (Edge) peuvent différer
(prononciations remplacées, ponctuation absente, nombres découpés). On aligne donc les deux suites de mots
(difflib) ; sans horodatage (Piper, ElevenLabs), les temps sont estimés au prorata des caractères dans le segment.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.video.models import CaptionWord


def norm(token: str) -> str:
    return re.sub(r"[\W_]+", "", token.lower())


def script_tokens(text: str) -> list[str]:
    """Mots du script ; la ponctuation isolée (« — », « - ») est rattachée au mot précédent."""
    out: list[str] = []
    for tok in text.split():
        if not norm(tok) and out:
            out[-1] += " " + tok
        elif not norm(tok):
            continue
        else:
            out.append(tok)
    return out


def _weight(token: str) -> float:
    return len(norm(token)) + 1.0


def _spread(tokens: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """Répartit [start, end] entre les mots, au prorata de leur longueur."""
    total = sum(_weight(t) for t in tokens) or 1.0
    out, t = [], start
    for tok in tokens:
        d = (end - start) * _weight(tok) / total
        out.append((t, t + d))
        t += d
    return out


def estimate_segment(text: str, start: float, end: float, segment: int = 0) -> list[CaptionWord]:
    tokens = script_tokens(text)
    return [CaptionWord(tok, round(a, 3), round(b, 3), segment, estimated=True)
            for tok, (a, b) in zip(tokens, _spread(tokens, start, end))]


def align_segment(text: str, tts_words: list[dict], start: float, end: float, segment: int = 0) -> list[CaptionWord]:
    tokens = script_tokens(text)
    if not tokens:
        return []
    words = [w for w in tts_words if norm(str(w.get("text", "")))]
    if not words:
        return estimate_segment(text, start, end, segment)
    a, b = [norm(t) for t in tokens], [norm(w["text"]) for w in words]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (float(words[j1 + k]["start"]), float(words[j1 + k]["end"]))
        elif tag == "replace":   # ex. « NATO » lu « Nay Toe » : les mots du script se partagent la durée prononcée
            for k, span in enumerate(_spread(tokens[i1:i2], float(words[j1]["start"]), float(words[j2 - 1]["end"]))):
                times[i1 + k] = span
    _fill_gaps(tokens, times, start, end)
    out, prev = [], start
    for tok, (s, e) in zip(tokens, times):  # type: ignore[misc]
        s = max(s, prev)
        e = max(e, s + 0.04)
        out.append(CaptionWord(tok, round(s, 3), round(e, 3), segment))
        prev = s
    return out


def _fill_gaps(tokens: list[str], times: list, start: float, end: float) -> None:
    """Mots du script absents de l'horodatage : temps interpolés entre les voisins connus."""
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        lo = times[i - 1][1] if i > 0 else start
        hi = times[j][0] if j < len(times) else end
        if hi - lo < 0.08 * (j - i):          # pas de place : on se cale sur la fin du mot précédent
            hi = lo + 0.08 * (j - i)
        for k, span in enumerate(_spread(tokens[i:j], lo, hi)):
            times[i + k] = span
        i = j


def caption_words(timeline: list[dict]) -> tuple[list[CaptionWord], bool]:
    """Mots horodatés de toute la voix off, à partir de la chronologie de la narration (Phase 5).
    Retourne (mots, estimé) ; « estimé » est vrai si au moins un segment n'avait pas d'horodatage."""
    out: list[CaptionWord] = []
    estimated = False
    for seg in timeline:
        words = align_segment(seg.get("text", ""), seg.get("words") or [], float(seg["start"]), float(seg["end"]),
                              int(seg.get("index", 0)))
        estimated = estimated or any(w.estimated for w in words)
        out.extend(words)
    return out, estimated
