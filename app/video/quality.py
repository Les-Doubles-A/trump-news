"""Contrôle qualité déterministe d'une vidéo montée.

Contrôles bloquants (un seul échec => NEEDS_REVIEW, jamais publiée) : fichier lisible, résolution, fps, codec,
durée, audio, écran noir, sous-titres, carte des sources, crédits, mention IA, fact-check, statut du script, aperçu.
Contrôles de qualité pondérés (score entre 0 et 1, comparé à quality.min_score) : volume, crêtes, silences,
sensationnalisme du hook, horodatage réel des mots.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.utils.ffmpeg import ffmpeg_log, ffprobe, find_ffprobe, require_ffmpeg
from app.core.errors import ProviderError
from app.video.models import QualityCheck, QualityReport

TARGET_LUFS = -16.0
LUFS_TOLERANCE = 2.0
MAX_TRUE_PEAK = -0.5
DURATION_TOLERANCE_S = 0.3
MAX_DURATION_S = 91.0
SILENCE_S = 1.5
SENSATIONAL_MAX = 0.35

CLICKBAIT = {
    "en": ["shocking", "you won't believe", "unbelievable", "insane", "jaw-dropping", "bombshell", "destroyed", "destroys",
           "slams", "epic", "mind-blowing", "outrageous", "explosive", "must see", "must-see", "goes viral", "meltdown"],
    "fr": ["choquant", "incroyable", "vous ne croirez pas", "hallucinant", "explosif", "détruit", "humilie", "pète un câble",
           "scandaleux", "à couper le souffle", "fou furieux", "énorme scandale"],
    "es": ["impactante", "increíble", "no creerás", "no vas a creer", "brutal", "destroza", "humilla", "escandaloso",
           "explosivo", "alucinante"],
}


@dataclass
class MediaAnalysis:
    ok: bool
    error: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    pix_fmt: str = ""
    duration: float = 0.0
    has_audio: bool = False
    integrated_lufs: float | None = None
    true_peak: float | None = None
    black: list[tuple[float, float]] = field(default_factory=list)
    silence: list[tuple[float, float]] = field(default_factory=list)


class IMediaAnalyzer(Protocol):
    def analyze(self, path: Path) -> MediaAnalysis: ...


def _num(v: str) -> float | None:
    return None if "inf" in v else float(v)


def parse_analysis_log(text: str, duration: float = 0.0) -> tuple[float | None, float | None, list, list]:
    """(loudness intégrée LUFS, true peak dBFS, écrans noirs, silences) depuis le journal de blackdetect/ebur128/silencedetect."""
    lufs = re.findall(r"^\s*I:\s+(-?[\d.]+|-inf)\s+LUFS", text, re.M)       # résumé final (pas les lignes « t: »)
    peaks = re.findall(r"^\s*Peak:\s+(-?[\d.]+|-inf)\s+dBFS", text, re.M)
    black = [(float(a), float(b)) for a, b in re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", text)]
    silence, start = [], None
    for kind, val in re.findall(r"silence_(start|end):\s*(-?[\d.]+)", text):
        if kind == "start":
            start = max(0.0, float(val))
        elif start is not None:
            silence.append((start, float(val)))
            start = None
    if start is not None:
        silence.append((start, max(start, duration)))
    return (_num(lufs[-1]) if lufs else None, _num(peaks[-1]) if peaks else None, black, silence)


class FFmpegAnalyzer:
    """ffprobe (format) + une passe de décodage complète (lisibilité, noir, volume, silences)."""

    def __init__(self, ffmpeg_path: str | None = None) -> None:
        self.ffmpeg_path = ffmpeg_path

    def analyze(self, path: Path) -> MediaAnalysis:
        exe = require_ffmpeg(self.ffmpeg_path)
        probe_exe = find_ffprobe(exe)
        if not probe_exe:
            return MediaAnalysis(False, "ffprobe introuvable (il est livré avec FFmpeg : winget install Gyan.FFmpeg).")
        try:
            info = ffprobe(Path(path), probe_exe)
        except ProviderError as exc:
            return MediaAnalysis(False, str(exc))
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if v is None:
            return MediaAnalysis(False, "aucun flux vidéo", has_audio=has_audio)
        num, _, den = str(v.get("avg_frame_rate", "0/1")).partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
        graph = "[0:v]blackdetect=d=0.5:pix_th=0.06[v]"
        maps = ["-map", "[v]"]
        if has_audio:
            graph += f";[0:a]ebur128=peak=true,silencedetect=n=-45dB:d={SILENCE_S}[a]"
            maps += ["-map", "[a]"]
        rc, log = ffmpeg_log(["-i", str(path), "-filter_complex", graph, *maps, "-f", "null", "-"], exe)
        lufs, peak, black, silence = parse_analysis_log(log, duration)
        decode_error = re.search(r"(error while decoding|invalid data found|corrupt)", log, re.I)
        ok = rc == 0 and not decode_error
        return MediaAnalysis(ok, "" if ok else f"décodage en erreur : {log.strip().splitlines()[-1][:200] if log.strip() else rc}",
                             int(v.get("width", 0)), int(v.get("height", 0)), round(fps, 3), v.get("codec_name", ""),
                             v.get("pix_fmt", ""), duration, has_audio, lufs, peak, black, silence)


def sensationalism(text: str, language: str = "en") -> tuple[float, list[str]]:
    """Score 0-1 de racolage (termes « clickbait », points d'exclamation, mots en capitales). Heuristique : pénalise, ne bloque pas."""
    low = text.lower()
    reasons = [f"« {t} »" for t in CLICKBAIT.get(language, CLICKBAIT["en"]) if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low)]
    excl = text.count("!")
    caps = [w for w in re.findall(r"[^\W\d_]{5,}", text) if w.isupper()]
    score = 0.35 * len(reasons) + (0.25 if excl >= 1 else 0) + (0.2 if excl >= 2 else 0) + 0.2 * min(len(caps), 2)
    if excl:
        reasons.append(f"{excl} point(s) d'exclamation")
    if caps:
        reasons.append("capitales : " + ", ".join(caps[:3]))
    return min(1.0, round(score, 2)), reasons


@dataclass
class QualityContext:
    """Ce que la vidéo devrait être (enregistré avec le rapport pour pouvoir refaire le contrôle : run.bat video-check)."""
    width: int
    height: int
    fps: int
    expected_duration: float
    language: str = "en"
    hook: str = ""
    subtitles_enabled: bool = True
    subtitles: dict = field(default_factory=dict)     # SubtitleStats sous forme de dict
    shots_needing_credit: int = 0
    ai_shots: int = 0
    has_sources_card: bool = False
    factcheck_verdict: str | None = None
    script_status: str = "DRAFT"
    preview: bool = False


def evaluate(a: MediaAnalysis, ctx: QualityContext, min_score: float) -> QualityReport:
    checks: list[QualityCheck] = []

    def crit(name: str, ok: bool, detail: str) -> None:
        checks.append(QualityCheck(name, bool(ok), detail, critical=True, weight=0))

    def score(name: str, ok: bool, detail: str, weight: float) -> None:
        checks.append(QualityCheck(name, bool(ok), detail, critical=False, weight=weight))

    crit("fichier lisible", a.ok, "décodage complet sans erreur" if a.ok else a.error or "illisible")
    crit("résolution", (a.width, a.height) == (ctx.width, ctx.height), f"{a.width}x{a.height} (attendu {ctx.width}x{ctx.height})")
    crit("images/s", abs(a.fps - ctx.fps) <= 0.5, f"{a.fps:g} (attendu {ctx.fps})")
    crit("codec", a.vcodec == "h264" and a.pix_fmt == "yuv420p", f"{a.vcodec or '?'} {a.pix_fmt or '?'} (attendu h264 yuv420p)")
    crit("durée", abs(a.duration - ctx.expected_duration) <= DURATION_TOLERANCE_S and a.duration <= MAX_DURATION_S,
         f"{a.duration:.2f} s (voix : {ctx.expected_duration:.2f} s, maximum {MAX_DURATION_S:g} s)")
    crit("audio présent", a.has_audio, "piste audio présente" if a.has_audio else "aucune piste audio")
    long_black = [(s, e) for s, e in a.black if e - s >= 0.5]
    crit("pas d'écran noir", a.ok and not long_black,
         "aucun" if not long_black else ", ".join(f"{s:.1f}-{e:.1f} s" for s, e in long_black[:3]))

    st = ctx.subtitles or {}
    total, hidden, shown = st.get("words_total", 0), st.get("words_hidden", 0), st.get("words_shown", 0)
    expected = total - hidden
    coverage = 1.0 if expected <= 0 and total else (shown / expected if expected > 0 else 0.0)
    if ctx.subtitles_enabled:
        crit("sous-titres", total > 0 and coverage >= 0.95,
             f"{shown}/{expected} mots affichés ({coverage:.0%}), {hidden} déjà écrits sur une carte" if total else "aucun mot")
    else:
        score("sous-titres", False, "désactivés (video.subtitles = false) : peu lisible sans le son", 1)
    crit("carte des sources", ctx.has_sources_card, "présente en conclusion" if ctx.has_sources_card else "absente")
    credits = st.get("credit_events", 0)
    crit("crédits photo", credits >= ctx.shots_needing_credit,
         f"{credits}/{ctx.shots_needing_credit} image(s) sous licence créditée(s) à l'écran")
    ai = st.get("ai_events", 0)
    crit("mention IA", ai >= ctx.ai_shots, "aucune image IA" if not ctx.ai_shots else f"{ai}/{ctx.ai_shots} image(s) IA signalée(s)")
    verdict = ctx.factcheck_verdict
    crit("fact-check", verdict is not None and verdict != "BLOCKED", f"verdict {verdict or 'introuvable'}")
    script_hint = {"NEEDS_REVIEW": " : relecture humaine requise (écran Scripts > Relire, ou run.bat approve)",
                   "REJECTED": " : script refusé à la relecture", "APPROVED": " : approuvé à la relecture"}
    crit("script validé", ctx.script_status in ("DRAFT", "APPROVED"),
         f"statut {ctx.script_status}" + script_hint.get(ctx.script_status, ""))
    crit("définition finale", not ctx.preview, "aperçu basse définition : ne pas publier" if ctx.preview else "pleine définition")

    if a.integrated_lufs is None:
        score("volume", False, "volume non mesuré", 2)
    else:
        score("volume", abs(a.integrated_lufs - TARGET_LUFS) <= LUFS_TOLERANCE,
              f"{a.integrated_lufs:.1f} LUFS (cible {TARGET_LUFS:g} ± {LUFS_TOLERANCE:g})", 2)
    score("crêtes", a.true_peak is not None and a.true_peak <= MAX_TRUE_PEAK,
          f"{a.true_peak:.1f} dBTP (max {MAX_TRUE_PEAK:g})" if a.true_peak is not None else "non mesuré", 1)
    long_sil = [(s, e) for s, e in a.silence if e - s >= SILENCE_S]
    score("silences", a.has_audio and not long_sil,
          "aucun silence long" if not long_sil else ", ".join(f"{s:.1f}-{e:.1f} s" for s, e in long_sil[:3]), 1)
    sens, reasons = sensationalism(ctx.hook, ctx.language)
    score("sensationnalisme du hook", sens < SENSATIONAL_MAX, f"score {sens:.2f}" + (f" ({'; '.join(reasons)})" if reasons else ""), 2)
    score("synchronisation des mots", not st.get("estimated", False),
          "temps estimés (moteur vocal sans horodatage)" if st.get("estimated") else "temps réels du moteur vocal", 0.5)

    weighted = [c for c in checks if not c.critical]
    total_w = sum(c.weight for c in weighted) or 1.0
    q = round(sum(c.weight for c in weighted if c.ok) / total_w, 3)
    ready = all(c.ok for c in checks if c.critical) and q >= min_score
    return QualityReport(q, "READY" if ready else "NEEDS_REVIEW", checks)
