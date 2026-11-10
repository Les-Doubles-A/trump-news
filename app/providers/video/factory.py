from __future__ import annotations

from pathlib import Path

from app.config.schema import AppConfig
from app.core.interfaces import IVideoRenderer
from app.core.registry import Registry
from app.providers.video.ffmpeg_renderer import FFmpegRenderer

RENDERERS: Registry[IVideoRenderer] = Registry("vidéo")
RENDERERS.register("ffmpeg", lambda cfg, data_root: FFmpegRenderer(cfg.video.ffmpeg_path, Path(data_root) / "videos" / "cache"))


def build_video_renderer(cfg: AppConfig, data_root: Path, name: str = "ffmpeg") -> IVideoRenderer:
    return RENDERERS.create(name, cfg=cfg, data_root=data_root)
