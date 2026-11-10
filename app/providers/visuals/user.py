"""Images fournies par l'utilisateur : les mots du nom de fichier servent de mots-clés."""
from __future__ import annotations

import re
from pathlib import Path

from app.core.interfaces import HealthStatus
from app.providers.visuals.base import IVisualProvider
from app.visuals.imaging import probe, sha256_file
from app.visuals.models import VisualAsset, VisualQuery

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_STOP = {"the", "and", "for", "with", "from", "into", "that", "this", "image", "photo", "picture", "img", "of", "in", "on", "a"}


def _tokens(text: str) -> set[str]:
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w
            for w in re.findall(r"[a-zà-ÿ0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


class UserImagesProvider(IVisualProvider):
    name = "user"
    kinds = frozenset({"photo", "stock", "illustration"})

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _files(self) -> list[Path]:
        return sorted(p for p in self.dir.rglob("*") if p.suffix.lower() in EXTS) if self.dir.exists() else []

    def health_check(self) -> HealthStatus:
        n = len(self._files())
        return HealthStatus(n > 0, f"{n} image(s) dans {self.dir}" if n else f"Aucune image dans {self.dir}")

    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        wanted = _tokens(" ".join([query.text, *query.entities, *query.keywords]))
        best, best_score = None, 0
        for f in self._files():
            ext_id = f.relative_to(self.dir).as_posix()
            if f"{self.name}:{ext_id}" in exclude:
                continue
            score = len(wanted & _tokens(f.stem.replace("_", " ").replace("-", " ")))
            if score > best_score:
                best, best_score = f, score
        if best is None:
            return None
        w, h = probe(best)
        return VisualAsset(self.name, best.relative_to(self.dir).as_posix(), best, "photo", w, h,
                           license="fournie par l'utilisateur", sha256=sha256_file(best))
