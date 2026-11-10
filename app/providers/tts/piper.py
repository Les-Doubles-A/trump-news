"""Piper : synthèse vocale 100 % locale et gratuite (exécutable `piper` + modèles .onnx)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import httpx

from app.audio.wavtools import wav_duration
from app.core.errors import TTSError
from app.core.interfaces import HealthStatus, ITTSProvider, SynthOptions, SynthResult, Voice

log = logging.getLogger(__name__)
_NAME = re.compile(r"^([a-z]{2,3})_([A-Z]{2})-(.+)-(x_low|low|medium|high)$")
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def voice_urls(name: str) -> list[str]:
    m = _NAME.match(name)
    if not m:
        raise TTSError(f"Nom de voix Piper invalide : '{name}' (attendu : en_US-lessac-medium).")
    lang, region, voice, quality = m.groups()
    base = f"{HF_BASE}/{lang}/{lang}_{region}/{voice}/{quality}/{name}"
    return [f"{base}.onnx", f"{base}.onnx.json"]


def download_voice(name: str, dest_dir: Path, client: httpx.Client | None = None) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=True)
    out: list[Path] = []
    try:
        for url in voice_urls(name):
            target = dest_dir / url.rsplit("/", 1)[1]
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise TTSError(f"Téléchargement impossible ({resp.status_code}) : {url}")
                    with open(target, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
            except httpx.HTTPError as exc:
                raise TTSError(f"Téléchargement impossible : {exc}") from exc
            out.append(target)
    finally:
        if own:
            client.close()
    return out


class PiperProvider(ITTSProvider):
    name = "piper"
    requires_internet = False

    def __init__(self, executable: str, models_dir: Path, runner: Callable = subprocess.run) -> None:
        self.executable, self.models_dir, self._run = executable, Path(models_dir), runner

    def _exe(self) -> str | None:
        return self.executable if Path(self.executable).exists() else shutil.which(self.executable)

    def list_voices(self, language: str | None = None) -> list[Voice]:
        voices = []
        for f in sorted(self.models_dir.glob("*.onnx")):
            lang = f.stem.split("_")[0]
            if language is None or lang == language:
                voices.append(Voice(f.stem, f.stem, lang))
        return voices

    def health_check(self) -> HealthStatus:
        if not self._exe():
            return HealthStatus(False, f"Piper introuvable ('{self.executable}'). Installez-le : pip install piper-tts")
        voices = self.list_voices()
        if not voices:
            return HealthStatus(False, f"Aucune voix dans {self.models_dir}. Lancez : run.bat download-voice en_US-lessac-medium")
        return HealthStatus(True, f"{len(voices)} voix locale(s)", [v.id for v in voices])

    def synthesize(self, text: str, out_path: Path, options: SynthOptions) -> SynthResult:
        exe = self._exe()
        if not exe:
            raise TTSError(f"Piper introuvable ('{self.executable}'). Installez-le : pip install piper-tts")
        model = Path(options.voice) if Path(options.voice).suffix == ".onnx" else self.models_dir / f"{options.voice}.onnx"
        if not model.exists():
            raise TTSError(f"Voix Piper absente : {model.name}. Lancez : run.bat download-voice {options.voice}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "--model", str(model), "--output_file", str(out_path), "--length_scale", f"{1.0 / options.speed:.3f}"]
        try:
            proc = self._run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=180,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise TTSError("Piper a dépassé le délai imparti.") from exc
        if proc.returncode != 0 or not out_path.exists():
            raise TTSError(f"Piper a échoué : {proc.stderr.decode('utf-8', 'replace')[-300:]}")
        return SynthResult(out_path, wav_duration(out_path), [], len(text), 0.0)
