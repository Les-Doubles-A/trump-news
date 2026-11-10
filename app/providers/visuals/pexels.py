"""Pexels : photos génériques libres de droits (clé API gratuite : PEXELS_API_KEY)."""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from app.config.secrets import get_secret
from app.core.errors import ProviderError
from app.core.interfaces import HealthStatus
from app.providers.visuals.base import IVisualProvider
from app.visuals.imaging import save_jpeg, sha256_file
from app.visuals.models import VisualAsset, VisualQuery

API = "https://api.pexels.com/v1/search"


class PexelsProvider(IVisualProvider):
    name = "pexels"
    kinds = frozenset({"stock", "illustration"})
    requires_internet = True

    def __init__(self, min_width: int = 900, client: httpx.Client | None = None) -> None:
        self.min_width = min_width
        self._client = client or httpx.Client(timeout=30, follow_redirects=True)

    def health_check(self) -> HealthStatus:
        return HealthStatus(bool(get_secret("PEXELS_API_KEY")), "clé PEXELS_API_KEY présente" if get_secret("PEXELS_API_KEY")
                            else "PEXELS_API_KEY absente")

    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        words = re.findall(r"[A-Za-z]{3,}", " ".join(query.keywords) or query.text)[:6]
        if not words:
            return None
        key = get_secret("PEXELS_API_KEY", required=True)
        try:
            resp = self._client.get(API, params={"query": " ".join(words), "orientation": "portrait", "per_page": 15},
                                    headers={"Authorization": key})
        except httpx.HTTPError as exc:
            raise ProviderError(f"Pexels injoignable ({exc.__class__.__name__}).") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("Pexels a refusé la clé API (PEXELS_API_KEY).")
        if resp.status_code >= 400:
            raise ProviderError(f"Pexels : HTTP {resp.status_code}")
        for ph in resp.json().get("photos", []):
            ext = str(ph.get("id"))
            if f"{self.name}:{ext}" in exclude or ph.get("width", 0) < self.min_width:
                continue
            src = (ph.get("src") or {}).get("large2x") or (ph.get("src") or {}).get("portrait")
            if not src:
                continue
            try:
                img = self._client.get(src)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Téléchargement Pexels impossible ({exc.__class__.__name__}).") from exc
            if img.status_code >= 400:
                continue
            dest = dest_dir / "pexels" / f"{ext}.jpg"
            w, h = save_jpeg(img.content, dest)
            return VisualAsset(self.name, ext, dest, "photo", w, h, license="Pexels License",
                               attribution=f"Photo : {ph.get('photographer', 'inconnu')} / Pexels",
                               source_url=ph.get("url"), sha256=sha256_file(dest))
        return None
