"""Manipulation de fichiers WAV en Python pur (aucune dépendance) : durée, silence, concaténation."""
from __future__ import annotations

import wave
from pathlib import Path

from app.core.errors import ProviderError


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def concat_wavs(paths: list[Path], pauses_s: list[float], out: Path) -> float:
    """Concatène des WAV de mêmes paramètres, avec `pauses_s[i]` secondes de silence APRÈS chaque fichier
    (len(pauses_s) == len(paths)). Retourne la durée totale en secondes."""
    if not paths or len(paths) != len(pauses_s):
        raise ValueError("paths et pauses_s doivent avoir la même longueur (non nulle)")
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(paths[0]), "rb") as first:
        params = first.getparams()
    frames_total = 0
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(params.nchannels)
        dst.setsampwidth(params.sampwidth)
        dst.setframerate(params.framerate)
        for path, pause in zip(paths, pauses_s):
            with wave.open(str(path), "rb") as src:
                p = src.getparams()
                if (p.nchannels, p.sampwidth, p.framerate) != (params.nchannels, params.sampwidth, params.framerate):
                    raise ProviderError(f"Paramètres audio incompatibles : {path.name} ({p.framerate} Hz) "
                                        f"vs {params.framerate} Hz.")
                data = src.readframes(src.getnframes())
                dst.writeframes(data)
                frames_total += src.getnframes()
            n = int(round(pause * params.framerate))
            if n > 0:
                dst.writeframes(b"\x00" * n * params.nchannels * params.sampwidth)
                frames_total += n
    return frames_total / float(params.framerate)
