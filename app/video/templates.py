"""Templates de montage : police et couleurs des sous-titres, mouvement, transitions, barre de progression, musique.

Définis en Python (aucun YAML à éditer) ; choix par `run.bat set-option video.template <nom>` ou « auto » (selon le style du script).
Couleurs au format RRGGBB. Tailles exprimées pour une largeur de 1080 px (mises à l'échelle automatiquement).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.core.errors import ConfigError


class VideoTemplate(BaseModel):
    name: str
    label: str
    font: str = "Arial Black"                # famille de police (sous-titres)
    font_size: int = Field(80, ge=30, le=160)
    text_color: str = "FFFFFF"
    highlight_color: str = "FFD400"          # mot en cours de lecture
    outline_color: str = "000000"
    outline: float = 6
    shadow: float = 2
    uppercase: bool = True
    max_words: int = Field(3, ge=1, le=6)    # mots par bloc de sous-titre
    max_chars: int = Field(16, ge=6, le=40)
    caption_y: float = Field(0.64, ge=0.3, le=0.8)   # position verticale (fraction de la hauteur), hors des zones d'interface
    highlight_scale: int = Field(110, ge=100, le=140)
    zoom: float = Field(0.10, ge=0, le=0.3)  # amplitude du Ken Burns sur une prise
    transition: Literal["fade", "fadeblack", "slideleft", "slideup", "wipeleft", "smoothleft", "circleopen"] = "fade"
    transition_s: float = Field(0.3, ge=0, le=1.0)
    progress_bar: bool = False
    progress_color: str = "FFD400"
    music_volume_db: float = Field(-20, ge=-40, le=-6)
    blur_sigma: float = Field(30, ge=0, le=80)        # fond flouté derrière les photos non verticales
    background_dim: float = Field(0.55, gt=0, le=1)   # assombrissement de ce fond (1 = aucun)


TEMPLATES: dict[str, VideoTemplate] = {t.name: t for t in [
    VideoTemplate(name="breaking_news", label="Dernière minute : rythme soutenu, majuscules, barre de progression rouge",
                  highlight_color="FFD400", zoom=0.12, transition="slideleft", transition_s=0.25,
                  progress_bar=True, progress_color="E53935", music_volume_db=-21),
    VideoTemplate(name="news_fast", label="Très rapide : blocs de 2 mots, coupes vives",
                  max_words=2, max_chars=14, font_size=88, zoom=0.14, transition="wipeleft", transition_s=0.2,
                  progress_bar=True, progress_color="FFD400", music_volume_db=-20),
    VideoTemplate(name="minimal", label="Sobre : casse normale, fondus, mouvement léger",
                  font="Arial", uppercase=False, outline=5, shadow=1, max_words=3, max_chars=18,
                  zoom=0.06, transition="fade", transition_s=0.4, music_volume_db=-22),
    VideoTemplate(name="documentary", label="Documentaire : blocs plus longs, fondus lents, Ken Burns marqué",
                  font="Arial", uppercase=False, max_words=4, max_chars=22, font_size=72, highlight_color="FFE082",
                  zoom=0.12, transition="fade", transition_s=0.6, music_volume_db=-23),
    VideoTemplate(name="political", label="Politique : majuscules, glissements, barre de progression bleue",
                  highlight_color="FFD400", zoom=0.10, transition="smoothleft", transition_s=0.35,
                  progress_bar=True, progress_color="1E88E5", music_volume_db=-21),
]}

# style du script (Phase 4) -> template par défaut quand video.template = auto
STYLE_TEMPLATE = {
    "breaking_news": "breaking_news", "fast_news": "news_fast", "neutral_news": "minimal", "explainer": "documentary",
    "timeline": "documentary", "dramatic": "political", "fact_check": "minimal",
}


def get_template(name: str, style: str | None = None) -> VideoTemplate:
    if name == "auto":
        name = STYLE_TEMPLATE.get(style or "", "minimal")
    try:
        return TEMPLATES[name]
    except KeyError:
        raise ConfigError(f"Template vidéo inconnu : '{name}'. Disponibles : auto, {', '.join(TEMPLATES)}.") from None
