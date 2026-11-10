"""Appels FFmpeg : détection, conversion en WAV, normalisation du volume, analyse (ffprobe)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from app.core.errors import ConfigError, ProviderError

log = logging.getLogger(__name__)
INSTALL_HINT = "Installez FFmpeg (Windows : `winget install Gyan.FFmpeg`, puis rouvrez le terminal) ou renseignez video.ffmpeg_path."


def find_ffmpeg(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    return shutil.which("ffmpeg")


def require_ffmpeg(configured: str | None = None) -> str:
    exe = find_ffmpeg(configured)
    if not exe:
        raise ConfigError(f"FFmpeg introuvable. {INSTALL_HINT}")
    return exe


def run_ffmpeg(args: list[str], ffmpeg: str, timeout: int = 180, cwd: Path | None = None) -> None:
    proc = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args], timeout, cwd)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if _OUT_OF_MEMORY.search(err):
            raise ProviderError(MEMORY_HINT + f"\nDétail FFmpeg : {err.strip().splitlines()[0][:200]}")
        raise ProviderError(f"FFmpeg a échoué : {err[-600:]}")


_OUT_OF_MEMORY = re.compile(r"malloc of size \d+ failed|cannot allocate memory|out of memory|ENOMEM", re.I)
MEMORY_HINT = ("FFmpeg manque de mémoire : Windows ne peut plus en réserver (RAM + fichier de pagination). "
               "Causes habituelles : modèle LM Studio chargé, WSL/Docker ouverts, aucun fichier de pagination. "
               "Solution durable : activer le fichier de pagination (sysdm.cpl > Avancé > Performances > Mémoire virtuelle) ; "
               "sinon fermez WSL (wsl --shutdown) ou éjectez le modèle dans LM Studio avant le montage.")


def ffmpeg_log(args: list[str], ffmpeg: str, timeout: int = 300) -> tuple[int, str]:
    """Exécute FFmpeg au niveau « info » et retourne (code de sortie, journal stderr) : sert aux analyses."""
    proc = _run([ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", *args], timeout, None)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def _run(cmd: list[str], timeout: int, cwd: Path | None) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags,
                              cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"FFmpeg a dépassé le délai imparti ({timeout} s).") from exc


def ffmpeg_version(ffmpeg: str) -> tuple[int, int]:
    """(majeure, mineure) ; (0, 0) si illisible (versions « git » : considérées récentes par l'appelant)."""
    proc = _run([ffmpeg, "-hide_banner", "-version"], 30, None)
    m = re.search(r"version\s+n?(\d+)\.(\d+)", proc.stdout.decode("utf-8", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def ffmpeg_filters(ffmpeg: str) -> set[str]:
    """Noms des filtres disponibles dans cette version de FFmpeg."""
    out = _run([ffmpeg, "-hide_banner", "-filters"], 30, None).stdout.decode("utf-8", "replace")
    return {m.group(1) for m in re.finditer(r"^\s*[A-Z.|]{2,3}\s+(\w+)\s", out, re.M)}


def filter_script_args(ffmpeg: str, script: Path) -> list[str]:
    """Graphe de filtres lu dans un fichier (évite la limite de 32 767 caractères de la ligne de commande Windows).
    FFmpeg >= 7 : « -/filter_complex fichier » ; « -filter_complex_script » n'existe plus dans les versions récentes."""
    major, _ = ffmpeg_version(ffmpeg)
    return ["-filter_complex_script", str(script)] if 0 < major < 7 else ["-/filter_complex", str(script)]


def find_ffprobe(ffmpeg: str | None) -> str | None:
    if ffmpeg:
        p = Path(ffmpeg)
        sibling = p.with_name("ffprobe" + p.suffix)
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe")


def ffprobe(path: Path, ffprobe_exe: str, timeout: int = 60) -> dict:
    """Flux et format d'un fichier média (JSON de ffprobe). Lève ProviderError si le fichier est illisible."""
    proc = _run([ffprobe_exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout, None)
    if proc.returncode != 0:
        raise ProviderError(f"ffprobe ne peut pas lire {Path(path).name} : {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")


def convert_to_wav(src: Path, dst: Path, ffmpeg: str) -> None:
    """Décode n'importe quel audio en WAV PCM 16 bits mono (fréquence d'origine conservée)."""
    run_ffmpeg(["-i", str(src), "-ac", "1", "-c:a", "pcm_s16le", str(dst)], ffmpeg)


def loudnorm(src: Path, dst: Path, ffmpeg: str, target_lufs: float = -16.0, sample_rate: int | None = None) -> None:
    """Normalise le volume. `sample_rate` est obligatoire en pratique : loudnorm sur-échantillonne sinon à 192 kHz."""
    rate = ["-ar", str(sample_rate)] if sample_rate else []
    run_ffmpeg(["-i", str(src), "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11", *rate, "-c:a", "pcm_s16le", str(dst)], ffmpeg)
