from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.core.interfaces import HealthStatus
from app.visuals.models import VisualAsset, VisualQuery


class IVisualProvider(ABC):
    """Fournisseur d'images. `get` retourne un fichier local prêt à l'emploi, ou None s'il n'a rien de pertinent."""

    name: str = "base"
    kinds: frozenset[str] = frozenset()
    requires_internet: bool = False
    ai_generated: bool = False

    @abstractmethod
    def health_check(self) -> HealthStatus:
        ...

    @abstractmethod
    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        """`exclude` : identifiants (« fournisseur:id ») déjà utilisés ; ne jamais les renvoyer."""
