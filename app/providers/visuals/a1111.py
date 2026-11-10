"""Illustrations IA via Stable Diffusion local (Automatic1111 / Forge, option --api).

Garde-fous : style imposé non photoréaliste, aucun nom de personne dans le prompt, prompt négatif contre les visages
et les photos, et bandeau « illustration générée par IA » incrusté de façon indélébile."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import httpx

from app.core.errors import ProviderError
from app.core.interfaces import HealthStatus
from app.providers.visuals.base import IVisualProvider
from app.visuals.imaging import add_badge, probe, sha256_file
from app.visuals.models import VisualAsset, VisualQuery

NEGATIVE = ("photorealistic, photograph, photo, realistic, real person, human face, celebrity, politician likeness, "
            "portrait, logo, brand, text, letters, watermark, signature, nsfw, gore")
_PERSON_REQUEST = re.compile(r"\b(photo|picture|portrait|headshot|realistic|photorealistic|selfie)\b(\s+of)?", re.I)
BADGES = {"en": "AI-generated illustration", "fr": "Illustration générée par IA", "es": "Ilustración generada por IA"}


def build_prompt(description: str, style: str, names: list[str]) -> str:
    """Retire noms propres de personnes et demandes de ressemblance ; renvoie un prompt symbolique."""
    text = description
    for n in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(n), "a public figure", text, flags=re.I)
    text = re.sub(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b", "a public figure", text)   # noms composés restants
    text = _PERSON_REQUEST.sub("", text)
    text = re.sub(r"[^\w\s,.-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"{style}, {text or 'abstract symbolic composition, charts and map shapes'}"


class A1111Provider(IVisualProvider):
    name = "ai"
    kinds = frozenset({"illustration", "stock"})
    requires_internet = False
    ai_generated = True

    def __init__(self, url: str, style: str, steps: int = 25, cfg_scale: float = 6.5, width: int = 768, height: int = 1344,
                 timeout_s: float = 300, client: httpx.Client | None = None) -> None:
        self.url, self.style, self.steps, self.cfg, self.w, self.h = url.rstrip("/"), style, steps, cfg_scale, width, height
        self._client = client or httpx.Client(timeout=timeout_s)

    def health_check(self) -> HealthStatus:
        try:
            r = self._client.get(f"{self.url}/sdapi/v1/sd-models", timeout=5)
            r.raise_for_status()
            return HealthStatus(True, f"Stable Diffusion prêt ({len(r.json())} modèle(s))")
        except Exception:
            return HealthStatus(False, f"Stable Diffusion injoignable sur {self.url} (lancez Automatic1111/Forge avec --api)")

    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        prompt = build_prompt(query.text, self.style, query.entities)
        pid = hashlib.sha256(f"{prompt}|{self.w}x{self.h}".encode()).hexdigest()[:16]
        if f"{self.name}:{pid}" in exclude:
            return None
        out = dest_dir / "ai" / f"{pid}.png"
        if not out.exists():
            body = {"prompt": prompt, "negative_prompt": NEGATIVE, "steps": self.steps, "cfg_scale": self.cfg,
                    "width": self.w, "height": self.h, "seed": -1, "sampler_name": "Euler a"}
            try:
                resp = self._client.post(f"{self.url}/sdapi/v1/txt2img", json=body)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Stable Diffusion injoignable ({exc.__class__.__name__}). Lancez-le avec --api.") from exc
            if resp.status_code >= 400:
                raise ProviderError(f"Stable Diffusion : HTTP {resp.status_code} {resp.text[:150]}")
            try:
                img = base64.b64decode(resp.json()["images"][0])
            except (KeyError, IndexError, ValueError) as exc:
                raise ProviderError("Réponse Stable Diffusion inattendue.") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(img)
            add_badge(out, BADGES.get(query.language, BADGES["en"]))      # indication de contenu IA : toujours incrustée
        w, h = probe(out)
        return VisualAsset(self.name, pid, out, "illustration", w, h, license="généré par IA",
                           attribution="Illustration générée par IA", ai_generated=True, sha256=sha256_file(out))
