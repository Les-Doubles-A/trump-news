"""Montage avec FFmpeg, en deux passes :
1. un clip MP4 par prise (cadrage 9:16, fond flouté, Ken Burns), mis en cache ;
2. assemblage : transitions, sous-titres ASS, barre de progression, voix, musique avec ducking, effets, encodage final.
FFmpeg est lancé depuis le dossier de travail : le filtre `ass` n'y voit que des noms relatifs (pas de « C:\\ » à échapper)."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

from app.core.errors import ProviderError
from app.core.interfaces import HealthStatus, IVideoRenderer
from app.utils.ffmpeg import INSTALL_HINT, ffmpeg_filters, filter_script_args, find_ffmpeg, require_ffmpeg, run_ffmpeg
from app.video.models import RenderJob, RenderResult
from app.video.motion import ClipSpec, final_graph, plan_clips, shot_filter
from app.video.subtitles import write_ass

log = logging.getLogger(__name__)
REQUIRED_FILTERS = ("ass", "zoompan", "xfade", "gblur", "sidechaincompress", "loudnorm", "colorchannelmixer")
CACHE_VERSION = "1"
CACHE_MAX_AGE_S = 7 * 86400


class FFmpegRenderer(IVideoRenderer):
    name = "ffmpeg"

    def __init__(self, ffmpeg_path: str | None = None, cache_dir: Path | None = None) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def health_check(self) -> HealthStatus:
        exe = find_ffmpeg(self.ffmpeg_path)
        if not exe:
            return HealthStatus(False, f"FFmpeg introuvable. {INSTALL_HINT}")
        try:
            missing = [f for f in REQUIRED_FILTERS if f not in ffmpeg_filters(exe)]
        except OSError as exc:
            return HealthStatus(False, f"FFmpeg ne démarre pas : {exc}")
        if missing:
            return HealthStatus(False, f"FFmpeg sans les filtres {', '.join(missing)} : installez la version complète "
                                       "(winget install Gyan.FFmpeg).")
        return HealthStatus(True, f"FFmpeg prêt ({exe})")

    def render(self, job: RenderJob, out_path: Path) -> RenderResult:
        t0 = time.monotonic()
        exe = require_ffmpeg(self.ffmpeg_path)
        work = Path(job.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        cache = self.cache_dir or work / "shots"
        cache.mkdir(parents=True, exist_ok=True)
        self._prune(cache)
        warnings: list[str] = []

        plan = plan_clips(job.shots, job.duration, job.fps, job.template.transition_s if job.transitions else 0.0)
        if len(plan.clips) < len(job.shots):
            warnings.append(f"{len(job.shots) - len(plan.clips)} prise(s) trop courte(s) ou après la fin de la voix : ignorée(s).")
        clips = []
        for n, c in enumerate(plan.clips, 1):
            log.info("Prise %d/%d (%d images)", n, len(plan.clips), c.frames)
            clips.append(self._shot(exe, job, c, cache))

        stats = write_ass(job)
        ass_name = fonts_dir = None
        if stats.events or stats.credit_events or stats.ai_events:
            shutil.copyfile(job.subtitles_path, work / "subs.ass")
            ass_name = "subs.ass"
            if job.font_file and Path(job.font_file).exists():
                (work / "fonts").mkdir(exist_ok=True)
                shutil.copyfile(job.font_file, work / "fonts" / Path(job.font_file).name)
                fonts_dir = "fonts"

        inputs: list[str] = []
        for p in clips:
            inputs += ["-i", str(p)]
        voice_idx = len(clips)
        inputs += ["-i", str(Path(job.voice_path).resolve())]
        music_idx = sfx_idx = None
        nxt = voice_idx + 1
        if job.music:
            music_idx, nxt = nxt, nxt + 1
            inputs += ["-stream_loop", "-1", "-i", str(Path(job.music.path).resolve())]
        if job.sfx and plan.transition_frames and len(plan.clips) > 1:
            sfx_idx = nxt
            inputs += ["-i", str(Path(job.sfx.path).resolve())]
        graph = final_graph(job, plan, voice_input=voice_idx, music_input=music_idx, sfx_input=sfx_idx,
                            ass_name=ass_name, fonts_dir=fonts_dir)
        (work / "final_graph.txt").write_text(graph, encoding="utf-8")

        out = Path(out_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.stem + ".part.mp4")
        dur = plan.total_frames / job.fps
        log.info("Assemblage final (%d prises, %.1f s)", len(clips), dur)
        run_ffmpeg([*inputs, *filter_script_args(exe, Path("final_graph.txt")),
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", job.preset, "-crf", str(job.crf), "-profile:v", "high",
                    "-pix_fmt", "yuv420p", "-r", str(job.fps), "-frames:v", str(plan.total_frames),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{dur:.3f}",
                    "-movflags", "+faststart", str(part)], exe, timeout=job.timeout_s, cwd=work)
        part.replace(out)

        thumb = None
        if job.thumbnail_path:
            thumb = Path(job.thumbnail_path).resolve()
            thumb.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_ffmpeg(["-ss", f"{min(1.0, dur / 2):.2f}", "-i", str(out), "-frames:v", "1", "-q:v", "3", str(thumb)],
                           exe, timeout=60)
            except ProviderError as exc:
                warnings.append(f"Vignette non générée : {exc}")
                thumb = None
        if not job.keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return RenderResult(out, dur, stats, Path(job.subtitles_path), thumb, warnings, round(time.monotonic() - t0, 1))

    # ------------------------------------------------------------- internals
    def _shot(self, exe: str, job: RenderJob, clip: ClipSpec, cache: Path) -> Path:
        shot = job.shots[clip.index]
        src = Path(shot.path)
        if not src.exists():
            raise ProviderError(f"Image introuvable : {src} (relancez run.bat visuals).")
        graph = shot_filter(shot, clip.index, clip.frames, job)
        preset, crf = ("ultrafast", "26") if job.preview else ("veryfast", "16")
        st = src.stat()
        key = hashlib.sha256("|".join([CACHE_VERSION, str(src.resolve()), str(st.st_size), str(st.st_mtime_ns), graph,
                                       preset, crf]).encode()).hexdigest()[:24]
        out = cache / f"shot_{key}.mp4"
        if out.exists() and out.stat().st_size > 0:
            os.utime(out)          # prise réutilisée : on repousse son expiration
            return out
        part = out.with_name(out.stem + ".part.mp4")
        run_ffmpeg(["-i", str(src.resolve()), "-filter_complex", graph, "-map", "[v]", "-frames:v", str(clip.frames),
                    "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-r", str(job.fps), str(part)],
                   exe, timeout=job.timeout_s)
        part.replace(out)
        return out

    @staticmethod
    def _prune(cache: Path) -> None:
        now = time.time()
        for f in cache.glob("shot_*.mp4"):
            try:
                if now - f.stat().st_mtime > CACHE_MAX_AGE_S:
                    f.unlink()
            except OSError:
                pass
