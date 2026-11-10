from __future__ import annotations

from pathlib import Path

from app.config.schema import AppConfig
from app.core.errors import ConfigError
from app.providers.visuals.a1111 import A1111Provider
from app.providers.visuals.base import IVisualProvider
from app.providers.visuals.cards import CardsProvider
from app.providers.visuals.pexels import PexelsProvider
from app.providers.visuals.user import UserImagesProvider
from app.providers.visuals.wikimedia import WikimediaProvider


def enabled(name: str, cfg: AppConfig) -> bool:
    v = cfg.visuals
    return {"user": True, "wikimedia": v.wikimedia.enabled, "pexels": v.pexels.enabled, "ai": v.ai.enabled,
            "cards": v.cards.enabled}[name]


def build_visual_provider(name: str, cfg: AppConfig, base_dir: Path | None = None) -> IVisualProvider:
    base, v = base_dir or Path.cwd(), cfg.visuals
    if name == "user":
        d = v.user_dir
        return UserImagesProvider(d if d.is_absolute() else base / d)
    if name == "wikimedia":
        w = v.wikimedia
        return WikimediaProvider(w.contact, w.allow_share_alike, w.min_width, w.max_candidates)
    if name == "pexels":
        return PexelsProvider(v.pexels.min_width)
    if name == "ai":
        a = v.ai
        return A1111Provider(a.url, a.style_prompt, a.steps, a.cfg_scale, a.width, a.height, a.timeout_s)
    if name == "cards":
        return CardsProvider(v.cards.font_path)
    raise ConfigError(f"Fournisseur visuel inconnu : '{name}'.")
