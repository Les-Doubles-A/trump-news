"""Wikimedia Commons : photos libres (domaine public, CC0, CC BY) avec licence et crédit automatiques."""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path

import httpx

from app.core.errors import ProviderError
from app.core.interfaces import HealthStatus
from app.providers.visuals.base import IVisualProvider
from app.utils.credits import tidy_artist
from app.visuals.imaging import save_jpeg, sha256_file
from app.visuals.models import VisualAsset, VisualQuery

log = logging.getLogger(__name__)
API = "https://commons.wikimedia.org/w/api.php"
_TAG = re.compile(r"<[^>]+>")
_PD = re.compile(r"^(public domain|pd[\s-]|pd$|cc0|cc[\s-]?zero)", re.I)
_BY = re.compile(r"^cc[\s-]by(?![\s-]*(nc|nd|sa))", re.I)
_BY_SA = re.compile(r"^cc[\s-]by[\s-]sa", re.I)
OK_MIME = {"image/jpeg", "image/png", "image/webp"}


def license_rank(name: str, allow_sa: bool) -> int | None:
    """0 = domaine public/CC0 (idéal), 1 = CC BY, 2 = CC BY-SA (si autorisé), None = refusé (NC, ND, inconnue...)."""
    n = (name or "").strip()
    if re.search(r"\b(nc|nd)\b|non[\s-]?commercial|no[\s-]?deriv|fair use|copyrighted", n, re.I):
        return None
    if _PD.match(n):
        return 0
    if _BY.match(n):
        return 1
    if allow_sa and _BY_SA.match(n):
        return 2
    return None


def _clean(v: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", v or ""))).strip()


_SMALL = {"the", "and", "of", "de", "la", "le", "du", "des", "del", "for", "in", "on", "at"}
# mots courants des titres de fichiers, qui ne désignent pas une autre personne
_TITLE_WORDS = {"official", "portrait", "presidential", "president", "photo", "image", "file", "rally", "speech", "speaks",
                "speaking", "house", "white", "office", "oval", "cropped", "jpg", "jpeg", "png", "meeting", "visit", "press",
                "conference", "campaign", "event", "with", "and", "the", "of", "at", "in", "on", "january", "february", "march",
                "april", "may", "june", "july", "august", "september", "october", "november", "december", "us", "u.s."}


def base_key(title: str) -> str:
    """Identité d'une image indépendamment de ses copies : « X.jpg », « X (cropped).jpg », « X (2).jpg » -> « x »."""
    t = title.lower().removeprefix("file:")
    t = re.sub(r"\.\w{3,4}$", "", t)
    t = re.sub(r"[\s_]*\(?\bcropped\b\)?", " ", t)
    t = re.sub(r"\s*\(\d+\)\s*$", "", t)
    return re.sub(r"[\W_]+", " ", t).strip()


def subject_words(term: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[\w'’-]+", term) if len(w) > 2 and w.lower() not in _SMALL}


def crowd(title: str, words: set[str]) -> int:
    """Nombre d'autres noms propres dans le titre (« Sirisena Donald Trump Melania Trump Jayanthi Sirisena » -> 3) :
    une photo où le sujet n'est qu'un personnage parmi d'autres est moins pertinente."""
    names = {w.lower() for w in re.findall(r"[A-Z][a-zà-ÿ'’-]+", title.removeprefix("File:").replace("_", " "))}
    return len(names - words - _TITLE_WORDS)


class WikimediaProvider(IVisualProvider):
    name = "wikimedia"
    kinds = frozenset({"photo"})
    requires_internet = True

    def __init__(self, contact: str = "", allow_share_alike: bool = False, min_width: int = 900, max_candidates: int = 20,
                 client: httpx.Client | None = None) -> None:
        ua = f"TrumpNews/1.0 (personal news-video project{'; ' + contact if contact else ''}) python-httpx"
        self._client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": ua})
        self.allow_sa, self.min_width, self.max_candidates = allow_share_alike, min_width, max_candidates

    def health_check(self) -> HealthStatus:
        try:
            self._client.get(API, params={"action": "query", "meta": "siteinfo", "format": "json"}).raise_for_status()
            return HealthStatus(True, "Wikimedia Commons joignable")
        except Exception as exc:
            return HealthStatus(False, f"Wikimedia Commons injoignable : {exc}")

    def _search(self, term: str) -> list[dict]:
        params = {
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{term} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": self.max_candidates, "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
            "iiextmetadatafilter": "LicenseShortName|Artist|Credit|UsageTerms|ImageDescription",
        }
        try:
            resp = self._client.get(API, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Wikimedia Commons injoignable ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Wikimedia Commons : HTTP {resp.status_code}")
        try:
            pages = (resp.json().get("query") or {}).get("pages") or {}
        except ValueError as exc:
            raise ProviderError("Réponse Wikimedia illisible.") from exc
        return list(pages.values())

    def get(self, query: VisualQuery, dest_dir: Path, exclude: set[str]) -> VisualAsset | None:
        subjects = list(dict.fromkeys(query.entities or query.keywords))[:2]
        # deux sujets connus (ex. Donald Trump + Kentucky) : on cherche d'abord une photo qui montre les deux
        terms = ([" ".join(subjects)] if query.entities and len(subjects) == 2 else []) + subjects
        used = {base_key(x.split(":", 1)[1]) for x in exclude if x.startswith(f"{self.name}:")}
        for term in terms:
            words = subject_words(term)
            ranked = []
            for page in self._search(term):
                info = (page.get("imageinfo") or [{}])[0]
                meta = {k: _clean(v.get("value")) for k, v in (info.get("extmetadata") or {}).items()}
                lic = license_rank(meta.get("LicenseShortName", ""), self.allow_sa)
                title = page.get("title", "")
                w, h = info.get("thumbwidth") or info.get("width") or 0, info.get("thumbheight") or info.get("height") or 0
                if (lic is None or info.get("mime") not in OK_MIME or w < self.min_width or not info.get("thumburl")
                        or base_key(title) in used or not 0.4 <= (w / h if h else 0) <= 2.2):
                    continue
                hay = (title.replace("_", " ") + " " + meta.get("ImageDescription", "")).lower()
                if not all(t in hay for t in words):      # tout le sujet, pas une partie (« Gulf » ≠ « Gulf of Mexico »)
                    continue
                ranked.append((crowd(title, words) >= 2, lic, page.get("index", 99), title, info, meta, w, h))
            if not ranked:
                continue
            ranked.sort(key=lambda r: r[:3])
            _, lic, _, title, info, meta, w, h = ranked[0]
            try:
                img = self._client.get(info["thumburl"])
            except httpx.HTTPError as exc:
                raise ProviderError(f"Téléchargement Wikimedia impossible ({exc.__class__.__name__}).") from exc
            if img.status_code >= 400:
                raise ProviderError(f"Téléchargement Wikimedia : HTTP {img.status_code}")
            dest = dest_dir / "wikimedia" / (re.sub(r"[^\w.-]+", "_", title.removeprefix("File:"))[:80].rsplit(".", 1)[0] + ".jpg")
            w, h = save_jpeg(img.content, dest)
            artist = tidy_artist(meta.get("Artist"), meta.get("Credit"))
            return VisualAsset(
                self.name, title, dest, "photo", w, h, license=meta.get("LicenseShortName"),
                attribution=f"{artist[:120]} / Wikimedia Commons / {meta.get('LicenseShortName', '')}".strip(" /"),
                source_url=info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
                sha256=sha256_file(dest),
            )
        return None
