from __future__ import annotations

from pathlib import Path

from app.core.interfaces import HealthStatus
from app.providers.visuals.base import IVisualProvider
from app.visuals.cards import H, W, card_id, render_card
from app.visuals.imaging import sha256_file
from app.visuals.models import VisualAsset, VisualQuery


class CardsProvider(IVisualProvider):
    name = "cards"
    kinds = frozenset({"headline", "claim", "number", "sources"})

    def __init__(self, font_path: str | None = None) -> None:
        self.font_path = font_path

    def health_check(self) -> HealthStatus:
        return HealthStatus(True, "Cartes générées localement")

    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        if query.kind not in self.kinds or not query.text.strip():
            return None
        kind = "headline" if query.kind == "sources" else query.kind
        cid = card_id(query.kind, query.text, query.footer, query.badge, query.category)
        out = dest_dir / "cards" / f"{cid}.png"
        if not out.exists():
            render_card(kind, query.text, out, footer=query.footer, badge=query.badge, category=query.category,
                        font_path=self.font_path)
        return VisualAsset(self.name, cid, out, "card", W, H, license="généré", source_url=None, sha256=sha256_file(out))
