"""Musique de fond synthétisée localement (aucun téléchargement, aucun droit tiers : publiée en CC0).

Nappes d'accords douces (+ pulsation de basse discrète pour l'ambiance « news »), en boucle sans coupure,
puis finition FFmpeg (filtre passe-bas, écho léger, volume -14 LUFS comme une piste du commerce).
"""
from __future__ import annotations

import math
import random
import wave
from array import array
from pathlib import Path

from app.utils.ffmpeg import run_ffmpeg

RATE = 22050
MOODS = {
    # tempo, progression (notes MIDI de chaque accord), basse pulsée
    "news": (96, [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)], True),    # Am F C G
    "calm": (72, [(48, 52, 55), (57, 60, 64), (53, 57, 60), (55, 59, 62)], False),   # C Am F G
}


def _freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _bar(chord: tuple[int, ...], bar_s: float, tail_s: float, pulse: bool, rng: random.Random) -> list[float]:
    n = int((bar_s + tail_s) * RATE)
    attack, release = 0.5 * RATE, (tail_s + 0.5) * RATE
    phases = [rng.random() * 2 * math.pi for _ in chord]
    freqs = [_freq(m) for m in chord]
    root = _freq(chord[0] - 12)
    eighth = bar_s / 8
    out = []
    for i in range(n):
        t = i / RATE
        if i < attack:
            env = 0.5 - 0.5 * math.cos(math.pi * i / attack)
        elif i > n - release:
            env = 0.5 + 0.5 * math.cos(math.pi * (i - (n - release)) / release)
        else:
            env = 1.0
        s = 0.0
        for f, ph in zip(freqs, phases):   # deux oscillateurs légèrement désaccordés : effet « nappe »
            w = 2 * math.pi * f * t + ph
            s += math.sin(w) + 0.6 * math.sin(w * 1.004) + 0.15 * math.sin(2 * w)
        v = 0.16 * env * s / len(freqs)
        if pulse and t < bar_s:
            k = t % eighth
            v += 0.22 * math.exp(-k / 0.11) * (math.sin(2 * math.pi * root * t) + 0.3 * math.sin(4 * math.pi * root * t))
        out.append(v)
    return out


def synthesize(path: Path, mood: str = "news", duration_s: float = 60.0, seed: int = 0) -> Path:
    """Écrit un WAV mono 22 050 Hz qui boucle sans coupure (la traîne du dernier accord revient au début)."""
    tempo, chords, pulse = MOODS[mood]
    bar_s = 4 * 60 / tempo
    n_bars = max(len(chords), round(duration_s / bar_s / len(chords)) * len(chords))
    total = int(n_bars * bar_s * RATE)
    tail_s = 1.0
    rng = random.Random(seed)
    buf = [0.0] * total
    cache: dict[int, list[float]] = {}
    for b in range(n_bars):
        idx = b % len(chords)
        if idx not in cache:
            cache[idx] = _bar(chords[idx], bar_s, tail_s, pulse, rng)
        start = int(b * bar_s * RATE)
        for i, v in enumerate(cache[idx]):
            buf[(start + i) % total] += v
    peak = max(abs(x) for x in buf) or 1.0
    pcm = array("h", (int(32767 * 0.8 * x / peak) for x in buf))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def make_track(folder: Path, ffmpeg: str, mood: str = "news", duration_s: float = 60.0, seed: int = 0) -> Path:
    """Piste finale stéréo 48 kHz + fichier de licence, prête pour assets/music."""
    raw = folder / f".generated_{mood}_raw.wav"
    out = folder / f"generated_{mood}.wav"
    synthesize(raw, mood, duration_s, seed)
    try:
        run_ffmpeg(["-i", str(raw), "-af", "lowpass=f=3500,aecho=0.8:0.6:70|140:0.22|0.12,"
                    "loudnorm=I=-14:TP=-2:LRA=7", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(out)], ffmpeg)
    finally:
        raw.unlink(missing_ok=True)
    out.with_suffix(".txt").write_text(
        f"licence: CC0 (générée localement par Trump News, aucun crédit requis)\ntitle: Generated {mood} bed\n",
        encoding="utf-8")
    return out
