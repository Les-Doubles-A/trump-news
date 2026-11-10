"""Montage d'un script : composition -> rendu -> contrôle qualité -> enregistrement (tables videos et video_reports).
Une vidéo qui échoue à un contrôle bloquant, ou sous quality.min_score, reste NEEDS_REVIEW : elle ne sera jamais publiée."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.errors import ProviderError, TrumpNewsError
from app.core.interfaces import IVideoRenderer
from app.database.models import FactCheck, Script, Video, VideoReport, VideoStatus
from app.database.session import session_scope
from app.providers.video.factory import build_video_renderer
from app.video.composer import compose
from app.video.models import QualityReport
from app.video.quality import FFmpegAnalyzer, IMediaAnalyzer, QualityContext, evaluate

log = logging.getLogger(__name__)


@dataclass
class VideoOutcome:
    video_id: int
    path: Path
    status: str
    report: QualityReport
    duration: float = 0.0
    template: str = ""
    thumbnail: Path | None = None
    subtitles: Path | None = None
    credits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


class VideoService:
    def __init__(self, cfg: AppConfig, factory: sessionmaker[Session], data_root: Path, *,
                 renderer: IVideoRenderer | None = None, analyzer: IMediaAnalyzer | None = None,
                 base_dir: Path | None = None) -> None:
        self.cfg, self.factory, self.root = cfg, factory, Path(data_root)
        self.base = base_dir or Path.cwd()
        self.renderer = renderer or build_video_renderer(cfg, self.root)
        self.analyzer = analyzer or FFmpegAnalyzer(cfg.video.ffmpeg_path)

    def render(self, script_id: int, *, template: str | None = None, music: bool | None = None,
               preview: bool = False, keep_work: bool = False) -> VideoOutcome:
        composed = compose(self.cfg, self.factory, script_id, self.root, base_dir=self.base, template=template,
                           music=music, preview=preview)
        with session_scope(self.factory) as s:
            row = Video(story_id=composed.story_id, script_id=script_id, template=composed.template,
                        status=VideoStatus.RENDERING.value)
            s.add(row)
            s.flush()
            vid = row.id
        name = f"video_{vid:04d}" + ("_preview" if preview else "")
        job = composed.job
        job.work_dir = self.root / "videos" / "work" / name
        job.subtitles_path = self.root / "subtitles" / f"{name}.ass"
        job.thumbnail_path = self.root / "thumbnails" / f"{name}.jpg"
        job.keep_work = job.keep_work or keep_work
        out = self.root / "videos" / f"{name}.mp4"
        log.info("Montage de la vidéo #%d (script #%d, template %s%s)", vid, script_id, composed.template,
                 ", aperçu" if preview else "")
        try:
            result = self.renderer.render(job, out)
        except Exception as exc:
            msg = str(exc)
            with session_scope(self.factory) as s:
                s.get(Video, vid).status = VideoStatus.FAILED.value
                s.add(VideoReport(video_id=vid, score=None, status=VideoStatus.FAILED.value,
                                  data={"error": msg[:2000], "script_id": script_id, "warnings": composed.warnings}))
            log.error("Montage de la vidéo #%d échoué : %s", vid, msg[:300])
            if isinstance(exc, TrumpNewsError):
                raise
            raise ProviderError(f"Montage échoué : {msg[:400]}") from exc

        ctx = composed.context
        ctx.subtitles = {**asdict(result.subtitles), "coverage": round(result.subtitles.coverage, 4)}
        report = evaluate(self.analyzer.analyze(result.path), ctx, self.cfg.quality.min_score)
        warnings = composed.warnings + result.warnings
        data = {
            "script_id": script_id, "story_id": composed.story_id, "narration_id": composed.narration_id,
            "plan_id": composed.plan_id, "template": composed.template, "preview": preview,
            "quality": report.to_dict(), "context": asdict(ctx), "credits": composed.credits, "warnings": warnings,
            "subtitles_path": str(result.subtitles_path) if result.subtitles_path else None,
            "thumbnail_path": str(result.thumbnail_path) if result.thumbnail_path else None,
            "music": {**asdict(job.music), "path": str(job.music.path)} if job.music else None, "elapsed_s": result.elapsed_s,
        }
        self._save(vid, result.path, result.duration, report, data)
        return VideoOutcome(vid, result.path, report.status, report, result.duration, composed.template,
                            result.thumbnail_path, result.subtitles_path, composed.credits, warnings, result.elapsed_s)

    def check(self, video_id: int) -> VideoOutcome:
        """Refait le contrôle qualité d'une vidéo existante (fichier + statut actuel du script et du fact-check)."""
        with session_scope(self.factory) as s:
            row = s.get(Video, video_id)
            if row is None:
                raise TrumpNewsError(f"Vidéo #{video_id} introuvable. Voir : run.bat videos")
            rep = s.scalar(select(VideoReport).where(VideoReport.video_id == video_id).order_by(VideoReport.id.desc()))
            if rep is None or "context" not in rep.data or not row.video_path:
                raise TrumpNewsError(f"La vidéo #{video_id} n'a pas été montée complètement : relancez run.bat render {row.script_id}")
            data = dict(rep.data)
            sc = s.get(Script, row.script_id)
            fc = s.scalar(select(FactCheck).where(FactCheck.story_id == row.story_id).order_by(FactCheck.id.desc()))
            path, duration, published = Path(row.video_path), row.duration or 0.0, row.status == VideoStatus.PUBLISHED.value
            script_status, verdict = (sc.status if sc else "NEEDS_REVIEW"), (fc.verdict if fc else None)
        if not path.exists():
            raise TrumpNewsError(f"Fichier vidéo introuvable : {path}")
        ctx = QualityContext(**{**data["context"], "script_status": script_status, "factcheck_verdict": verdict})
        report = evaluate(self.analyzer.analyze(path), ctx, self.cfg.quality.min_score)
        data.update(quality=report.to_dict(), context=asdict(ctx))
        self._save(video_id, path, duration, report, data, keep_status=published)
        return VideoOutcome(video_id, path, report.status, report, duration, data.get("template", ""),
                            Path(data["thumbnail_path"]) if data.get("thumbnail_path") else None,
                            Path(data["subtitles_path"]) if data.get("subtitles_path") else None,
                            data.get("credits", []), data.get("warnings", []))

    def _save(self, vid: int, path: Path, duration: float, report: QualityReport, data: dict, keep_status: bool = False) -> None:
        with session_scope(self.factory) as s:
            row = s.get(Video, vid)
            row.video_path, row.duration, row.quality_score = str(path), round(duration, 3), report.score
            if not keep_status:
                row.status = report.status
            s.add(VideoReport(video_id=vid, score=report.score, status=report.status, data=data))
        try:
            path.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("Rapport JSON non écrit : %s", exc)


def latest_report(factory: sessionmaker[Session], video_id: int) -> dict | None:
    """Dernier rapport (montage + contrôle qualité) d'une vidéo, tel qu'enregistré dans video_reports."""
    with session_scope(factory) as s:
        rep = s.scalar(select(VideoReport).where(VideoReport.video_id == video_id).order_by(VideoReport.id.desc()))
        return dict(rep.data) if rep else None


def list_videos(factory: sessionmaker[Session], limit: int = 20) -> list[Video]:
    with session_scope(factory) as s:
        return list(s.scalars(select(Video).order_by(Video.id.desc()).limit(limit)))
