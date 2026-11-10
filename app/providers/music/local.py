"""Musique et effets sonores fournis par l'utilisateur, dans un dossier local.

Chaque piste doit être accompagnée d'un fichier texte du même nom déclarant sa licence, par exemple
`assets/music/calm_news.mp3` + `assets/music/calm_news.txt` :

    licence: CC0
    credit: "Calm News" by Jane Doe (https://example.org)

Une piste sans licence déclarée est ignorée (avertissement). Rien n'est jamais téléchargé.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.interfaces import IMusicProvider, MusicTrack

log = logging.getLogger(__name__)
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
_LICENSE_KEYS = {"licence", "license", "licencia"}
_CREDIT_KEYS = {"credit", "crédit", "attribution", "credito", "crédito"}


def read_sidecar(path: Path) -> tuple[str, str, str]:
    """(licence, crédit, titre) lus dans le fichier .txt associé ; chaînes vides si absents."""
    lic = credit = title = ""
    if not path.exists():
        return lic, credit, title
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key in _LICENSE_KEYS:
            lic = value
        elif key in _CREDIT_KEYS:
            credit = value
        elif key in ("title", "titre", "titulo", "título"):
            title = value.strip('"')
    return lic, credit, title


class LocalMusicProvider(IMusicProvider):
    name = "local"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.skipped: list[str] = []

    def list_tracks(self) -> list[MusicTrack]:
        self.skipped = []
        if not self.folder.is_dir():
            return []
        tracks = []
        for p in sorted(self.folder.iterdir(), key=lambda x: x.name.lower()):
            if p.suffix.lower() not in AUDIO_EXT or not p.is_file():
                continue
            lic, credit, title = read_sidecar(p.with_suffix(".txt"))
            if not lic:
                self.skipped.append(p.name)
                log.warning("Piste ignorée (licence non déclarée dans %s) : %s", p.with_suffix(".txt").name, p.name)
                continue
            tracks.append(MusicTrack(p, title or p.stem, lic, credit))
        return tracks

    def pick(self, seed: int, name: str | None = None) -> MusicTrack | None:
        """Piste demandée par son nom de fichier, sinon rotation déterministe (seed = id du script)."""
        tracks = self.list_tracks()
        if name:
            for t in tracks:
                if name.lower() in (t.path.name.lower(), t.path.stem.lower()):
                    return t
            return None
        return tracks[seed % len(tracks)] if tracks else None
