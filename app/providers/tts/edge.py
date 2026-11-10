"""Edge TTS : voix neuronales Microsoft gratuites (service en ligne non officiel : pas de garantie de disponibilité)."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Callable

from app.audio.wavtools import wav_duration
from app.core.errors import TTSError, TTSUnavailableError
from app.core.interfaces import HealthStatus, ITTSProvider, SynthOptions, SynthResult, Voice
from app.utils.ffmpeg import convert_to_wav, require_ffmpeg

log = logging.getLogger(__name__)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("edge-tts")
    except Exception:
        return "?"


class EdgeTTSProvider(ITTSProvider):
    name = "edge"
    requires_internet = True

    def __init__(self, pitch: str = "+0Hz", ffmpeg: str | None = None,
                 communicate_factory: Callable | None = None, voices_loader: Callable | None = None,
                 converter: Callable[[Path, Path], None] | None = None) -> None:
        self.pitch, self._ffmpeg = pitch, ffmpeg
        self._communicate, self._voices_loader, self._converter = communicate_factory, voices_loader, converter

    def _make(self, text: str, voice: str, rate: str):
        if self._communicate:
            return self._communicate(text, voice, rate=rate, pitch=self.pitch)
        try:
            import edge_tts
        except ImportError as exc:
            raise TTSError("Le module edge-tts n'est pas installé : pip install edge-tts") from exc
        return edge_tts.Communicate(text, voice, rate=rate, pitch=self.pitch, boundary="WordBoundary")

    def _convert(self, src: Path, dst: Path) -> None:
        if self._converter:
            self._converter(src, dst)
        else:
            convert_to_wav(src, dst, require_ffmpeg(self._ffmpeg))

    async def _stream(self, text: str, voice: str, rate: str) -> tuple[bytes, list[dict]]:
        audio, words = bytearray(), []
        async for chunk in self._make(text, voice, rate).stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"text": chunk["text"], "start": round(start, 3), "end": round(start + chunk["duration"] / 1e7, 3)})
        return bytes(audio), words

    def synthesize(self, text: str, out_path: Path, options: SynthOptions) -> SynthResult:
        rate = f"{round((options.speed - 1.0) * 100):+d}%"
        try:
            audio, words = asyncio.run(self._stream(text, options.voice, rate))
        except TTSError:
            raise
        except Exception as exc:  # réseau, voix refusée, aucun audio reçu...
            if getattr(exc, "status", None) == 403 or " 403" in str(exc)[:40] or "403," in str(exc)[:40]:
                raise TTSError(
                    f"Edge TTS a refusé la connexion (403, edge-tts {_version()}). Causes fréquentes : version trop ancienne "
                    "(pip install -U edge-tts), horloge Windows décalée (synchronisez l'heure), VPN/proxy/pare-feu, "
                    "ou limitation côté Microsoft. Solution de repli fiable : Piper (voix locale) ou ElevenLabs."
                ) from exc
            raise TTSUnavailableError(f"Edge TTS indisponible ({exc.__class__.__name__}: {exc})") from exc
        if not audio:
            raise TTSUnavailableError("Edge TTS n'a renvoyé aucun audio (voix inconnue ou service indisponible).")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "edge.mp3"
            raw.write_bytes(audio)
            self._convert(raw, out_path)
        return SynthResult(out_path, wav_duration(out_path), words, len(text), 0.0)

    def list_voices(self, language: str | None = None) -> list[Voice]:
        try:
            if self._voices_loader:
                raw = self._voices_loader()
            else:
                import edge_tts

                raw = asyncio.run(edge_tts.list_voices())
        except Exception as exc:
            raise TTSUnavailableError(f"Liste des voix Edge indisponible : {exc}") from exc
        voices = [Voice(v["ShortName"], v.get("FriendlyName", v["ShortName"]), v["Locale"].split("-")[0], v.get("Gender"))
                  for v in raw]
        return [v for v in voices if language is None or v.language == language]

    def health_check(self) -> HealthStatus:
        try:
            n = len(self.list_voices("en"))
            return HealthStatus(True, f"Connecté ({n} voix anglaises)")
        except Exception as exc:
            return HealthStatus(False, str(exc))
