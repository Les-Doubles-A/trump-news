"""Données du montage : ce que le renderer reçoit (RenderJob) et ce qu'il produit (RenderResult, QualityReport)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.interfaces import MusicTrack
from app.video.templates import VideoTemplate


@dataclass
class CaptionWord:
    text: str           # orthographe du script (pas celle envoyée à la voix)
    start: float
    end: float
    segment: int = 0
    estimated: bool = False


@dataclass
class Shot:
    path: Path
    start: float        # début dans la chronologie de la voix (s)
    end: float
    kind: str           # photo | illustration | card
    role: str           # primary | card | sources
    width: int
    height: int
    provider: str = ""
    credit: str | None = None
    ai_generated: bool = False
    hide_captions: bool = False   # carte qui affiche déjà le texte lu


@dataclass
class RenderJob:
    width: int
    height: int
    fps: int
    duration: float                 # durée de la voix off = durée de la vidéo
    voice_path: Path
    shots: list[Shot]
    words: list[CaptionWord]
    template: VideoTemplate
    work_dir: Path
    subtitles_path: Path
    thumbnail_path: Path | None = None
    language: str = "en"
    subtitles: bool = True
    transitions: bool = True
    progress_bar: bool = False
    music: MusicTrack | None = None
    music_volume_db: float = -20
    sfx: MusicTrack | None = None
    sfx_volume_db: float = -12
    crf: int = 20
    preset: str = "medium"
    preview: bool = False
    font_file: Path | None = None
    font_family: str | None = None
    keep_work: bool = False
    timeout_s: int = 1200


@dataclass
class SubtitleStats:
    words_total: int = 0
    words_hidden: int = 0           # mots lus pendant une carte qui affiche déjà le texte
    words_shown: int = 0
    events: int = 0
    credit_events: int = 0
    ai_events: int = 0
    estimated: bool = False         # temps des mots estimés (moteur vocal sans horodatage)

    @property
    def coverage(self) -> float:
        expected = self.words_total - self.words_hidden
        return 1.0 if expected <= 0 else self.words_shown / expected


@dataclass
class RenderResult:
    path: Path
    duration: float
    subtitles: SubtitleStats
    subtitles_path: Path | None = None
    thumbnail_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


@dataclass
class QualityCheck:
    name: str
    ok: bool
    detail: str
    critical: bool = False
    weight: float = 1.0


@dataclass
class QualityReport:
    score: float
    status: str                     # READY | NEEDS_REVIEW
    checks: list[QualityCheck]

    @property
    def failures(self) -> list[QualityCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "status": self.status, "checks": [asdict(c) for c in self.checks]}
