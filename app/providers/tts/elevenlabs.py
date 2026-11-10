"""ElevenLabs : voix de haute qualité via l'API officielle (payante). Clé : variable ELEVENLABS_API_KEY."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from app.audio.wavtools import wav_duration
from app.config.secrets import get_secret
from app.core.errors import TTSError, TTSUnavailableError
from app.core.interfaces import HealthStatus, ITTSProvider, SynthOptions, SynthResult, Voice
from app.utils.ffmpeg import convert_to_wav, require_ffmpeg

log = logging.getLogger(__name__)
API = "https://api.elevenlabs.io/v1"


class ElevenLabsProvider(ITTSProvider):
    name = "elevenlabs"
    requires_internet = True

    def __init__(self, model: str, stability: float, similarity_boost: float, style: float, cost_per_1k_chars: float = 0.0,
                 ffmpeg: str | None = None, client: httpx.Client | None = None,
                 converter: Callable[[Path, Path], None] | None = None) -> None:
        self.model, self.stability, self.similarity, self.style = model, stability, similarity_boost, style
        self.cost_per_1k, self._ffmpeg, self._converter = cost_per_1k_chars, ffmpeg, converter
        self._client = client or httpx.Client(timeout=90)

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": get_secret("ELEVENLABS_API_KEY", required=True) or ""}

    def _check(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise TTSError("ElevenLabs a refusé la clé API (401/403). Vérifiez ELEVENLABS_API_KEY.")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TTSUnavailableError(f"ElevenLabs indisponible ou quota atteint ({resp.status_code}).")
        if resp.status_code >= 400:
            raise TTSError(f"ElevenLabs a refusé la requête ({resp.status_code}) : {resp.text[:200]}")

    def synthesize(self, text: str, out_path: Path, options: SynthOptions) -> SynthResult:
        body = {"text": text, "model_id": self.model, "voice_settings": {
            "stability": self.stability, "similarity_boost": self.similarity, "style": self.style,
            "speed": max(0.7, min(1.2, options.speed))}}
        try:
            resp = self._client.post(f"{API}/text-to-speech/{options.voice}", params={"output_format": "mp3_44100_128"},
                                     headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise TTSUnavailableError(f"ElevenLabs injoignable ({exc.__class__.__name__}).") from exc
        self._check(resp)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "el.mp3"
            raw.write_bytes(resp.content)
            if self._converter:
                self._converter(raw, out_path)
            else:
                convert_to_wav(raw, out_path, require_ffmpeg(self._ffmpeg))
        return SynthResult(out_path, wav_duration(out_path), [], len(text), round(len(text) / 1000 * self.cost_per_1k, 4))

    def list_voices(self, language: str | None = None) -> list[Voice]:
        try:
            resp = self._client.get(f"{API}/voices", headers=self._headers())
        except httpx.HTTPError as exc:
            raise TTSUnavailableError(f"ElevenLabs injoignable ({exc.__class__.__name__}).") from exc
        self._check(resp)
        return [Voice(v["voice_id"], v.get("name", v["voice_id"]), None, (v.get("labels") or {}).get("gender"))
                for v in resp.json().get("voices", [])]

    def health_check(self) -> HealthStatus:
        try:
            n = len(self.list_voices())
            return HealthStatus(True, f"Connecté ({n} voix)")
        except Exception as exc:
            return HealthStatus(False, str(exc))
