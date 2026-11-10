"""Voix off d'un script : un fichier par segment (durées réelles), pauses, concaténation, volume, chronologie."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audio.wavtools import concat_wavs, wav_duration
from app.config.schema import AppConfig
from app.core.errors import TrumpNewsError, TTSError, TTSUnavailableError
from app.core.interfaces import ITTSProvider, SynthOptions
from app.core.retry import retry_call
from app.database.models import Narration, Script
from app.database.session import session_scope
from app.providers.tts.factory import build_tts_provider, default_voice
from app.utils.ffmpeg import find_ffmpeg, loudnorm

log = logging.getLogger(__name__)
HASH_VERSION = "1"


@dataclass
class NarrationResult:
    id: int
    script_id: int
    provider: str
    voice: str
    language: str
    audio_path: Path
    duration_s: float
    timeline: list[dict]
    cost_usd: float = 0.0
    chars: int = 0
    fallback_used: bool = False
    from_cache: bool = False
    warnings: list[str] = field(default_factory=list)


class NarrationService:
    def __init__(
        self, cfg: AppConfig, factory: sessionmaker[Session], data_root: Path, *,
        provider_factory: Callable[[str], ITTSProvider] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        normalizer: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self.cfg, self.t, self.factory, self.root = cfg, cfg.tts, factory, Path(data_root)
        self._make = provider_factory or (lambda name: build_tts_provider(name, cfg))
        self._sleep = sleep
        self._normalizer = normalizer

    # ------------------------------------------------------------------ API
    def narrate(self, script_id: int, *, provider: str | None = None, voice: str | None = None,
                speed: float | None = None, force: bool = False) -> NarrationResult:
        segments, language, style = self._load_script(script_id)
        primary = provider or self.t.provider
        chain = [primary]
        if self.t.fallback and self.t.fallback != primary:
            chain.append(self.t.fallback)
        errors: list[str] = []
        for k, name in enumerate(chain):
            try:
                res = self._run(script_id, segments, language, style, name, voice if k == 0 else None, speed, force)
                res.fallback_used = k > 0
                if k > 0:
                    res.warnings.insert(0, f"Fournisseur principal en échec ({errors[0]}) : voix de secours « {name} » utilisée.")
                return res
            except TTSError as exc:
                errors.append(f"{name} : {exc}")
                log.warning("TTS %s en échec : %s", name, exc)
        raise TTSError("Aucun fournisseur vocal n'a réussi. " + " | ".join(errors))

    # ------------------------------------------------------------- internals
    def _load_script(self, script_id: int) -> tuple[list[dict], str, str]:
        with session_scope(self.factory) as s:
            row = s.get(Script, script_id)
            if row is None:
                raise TrumpNewsError(f"Script #{script_id} introuvable. Voir : run.bat scripts")
            segs = [dict(x) for x in (row.claims or {}).get("segments", [])]
            if not segs:
                raise TrumpNewsError(f"Le script #{script_id} ne contient aucun segment.")
            return segs, row.language, row.style

    def _pronounce(self, text: str) -> str:
        for word, spoken in self.t.pronunciations.items():
            text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", spoken, text)
        return text

    def _run(self, script_id, segments, language, style, name, voice_arg, speed_arg, force) -> NarrationResult:
        prov = self._make(name)
        voice = voice_arg or default_voice(name, self.cfg, language)
        if not voice:
            raise TTSError(f"Aucune voix « {language} » configurée pour {name}. Lancez : run.bat voices --provider {name} --lang {language}")
        speed = max(0.7, min(1.3, (speed_arg or self.t.speed) * self.t.style_prosody.get(style, 1.0)))
        h = hashlib.sha256(json.dumps({
            "v": HASH_VERSION, "texts": [s["text"] for s in segments], "provider": name, "voice": voice,
            "speed": round(speed, 3), "pause": self.t.pause_s, "hook_pause": self.t.hook_pause_s,
            "norm": self.t.normalize_loudness, "pron": self.t.pronunciations,
        }, sort_keys=True).encode()).hexdigest()

        if not force and (cached := self._cached(script_id, h)):
            return cached

        work = self.root / "audio" / f"narration_{script_id}_{h[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        files, words_by_seg, chars, cost = [], [], 0, 0.0
        for seg in segments:
            out = work / f"seg_{seg['index']:02d}.wav"
            opts = SynthOptions(voice=voice, language=language, speed=speed)
            res = retry_call(
                lambda seg=seg, out=out, opts=opts: prov.synthesize(self._pronounce(seg["text"]), out, opts),
                attempts=self.t.retries, base_delay=1.0, retry_on=(TTSUnavailableError,), sleep=self._sleep,
                label=f"TTS {name} segment {seg['index'] + 1}",
            )
            files.append(res.path)
            words_by_seg.append(res.words)
            chars += res.chars
            cost += res.cost_usd

        pauses = [self.t.hook_pause_s if s["section"] == "hook" else self.t.pause_s for s in segments]
        pauses[-1] = 0.0
        raw = work / "narration_raw.wav"
        total = concat_wavs(files, pauses, raw)

        warnings: list[str] = []
        final = self.root / "audio" / f"script_{script_id}_{h[:8]}.wav"
        self._finalize_audio(raw, final, warnings)
        duration = wav_duration(final)

        timeline, t0 = [], 0.0
        for seg, f, pause, words in zip(segments, files, pauses, words_by_seg):
            d = wav_duration(f)
            timeline.append({
                "index": seg["index"], "section": seg["section"], "text": seg["text"],
                "start": round(t0, 3), "end": round(t0 + d, 3), "audio": str(f),
                "words": [{"text": w["text"], "start": round(t0 + w["start"], 3), "end": round(t0 + w["end"], 3)} for w in words],
            })
            t0 += d + pause
        with session_scope(self.factory) as s:
            row = Narration(script_id=script_id, provider=name, voice=voice, language=language, audio_path=str(final),
                            duration=duration, timeline=timeline, input_hash=h, cost_usd=cost)
            s.add(row)
            s.flush()
            nid = row.id
        log.info("Voix off #%d : %.1f s via %s/%s (%d caractères)", nid, duration, name, voice, chars)
        return NarrationResult(nid, script_id, name, voice, language, final, duration, timeline, cost, chars, warnings=warnings)

    def _finalize_audio(self, raw: Path, final: Path, warnings: list[str]) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        if self.t.normalize_loudness:
            try:
                if self._normalizer:
                    self._normalizer(raw, final)
                    return
                ffmpeg = find_ffmpeg(self.cfg.video.ffmpeg_path)
                if ffmpeg:
                    with wave.open(str(raw), "rb") as w:
                        rate = w.getframerate()
                    loudnorm(raw, final, ffmpeg, sample_rate=rate)
                    return
                warnings.append("FFmpeg introuvable : volume non normalisé.")
            except Exception as exc:
                warnings.append(f"Normalisation du volume impossible ({exc}) : audio brut conservé.")
        shutil.copyfile(raw, final)

    def _cached(self, script_id: int, h: str) -> NarrationResult | None:
        with session_scope(self.factory) as s:
            row = s.scalar(select(Narration).where(Narration.script_id == script_id, Narration.input_hash == h)
                           .order_by(Narration.id.desc()))
            if row and Path(row.audio_path).exists():
                return NarrationResult(row.id, script_id, row.provider, row.voice, row.language, Path(row.audio_path),
                                       row.duration, row.timeline, row.cost_usd, 0, from_cache=True)
        return None


def latest_narration(factory: sessionmaker[Session], script_id: int) -> Narration | None:
    with session_scope(factory) as s:
        return s.scalar(select(Narration).where(Narration.script_id == script_id).order_by(Narration.id.desc()))
