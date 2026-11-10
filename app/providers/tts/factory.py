from __future__ import annotations

from pathlib import Path

from app.config.schema import AppConfig
from app.core.errors import ConfigError
from app.core.interfaces import ITTSProvider
from app.providers.tts.edge import EdgeTTSProvider
from app.providers.tts.elevenlabs import ElevenLabsProvider
from app.providers.tts.piper import PiperProvider


def build_tts_provider(name: str, cfg: AppConfig, base_dir: Path | None = None) -> ITTSProvider:
    base = base_dir or Path.cwd()
    t, ffmpeg = cfg.tts, cfg.video.ffmpeg_path
    if name == "edge":
        return EdgeTTSProvider(pitch=t.edge.pitch, ffmpeg=ffmpeg)
    if name == "piper":
        models = t.piper.models_dir
        return PiperProvider(t.piper.executable, models if models.is_absolute() else base / models)
    if name == "elevenlabs":
        e = t.elevenlabs
        return ElevenLabsProvider(e.model, e.stability, e.similarity_boost, e.style, e.cost_per_1k_chars, ffmpeg)
    raise ConfigError(f"Fournisseur TTS inconnu : '{name}'.")


def default_voice(name: str, cfg: AppConfig, language: str) -> str | None:
    table = {"edge": cfg.tts.edge.voices, "piper": cfg.tts.piper.voices, "elevenlabs": cfg.tts.elevenlabs.voices}[name]
    return table.get(language)
