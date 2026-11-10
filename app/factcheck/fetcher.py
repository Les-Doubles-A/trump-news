"""Téléchargement du texte d'un article, dans le respect de robots.txt. Aucun contournement de paywall
ou de protection : en cas de refus, on retombe sur le résumé du flux."""
from __future__ import annotations

import logging
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)
USER_AGENT = "TrumpNewsBot/1.0 (personal fact-checking; respects robots.txt)"


class ArticleTextFetcher:
    def __init__(self, client: httpx.Client | None = None, timeout_s: float = 15, min_chars: int = 400, max_bytes: int = 2_000_000) -> None:
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.min_chars = min_chars
        self.max_bytes = max_bytes

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                resp = self._client.get(f"{origin}/robots.txt")
                if resp.status_code >= 500:
                    rp = None  # indisponible : prudence, on s'abstient
                elif resp.status_code >= 400:
                    rp.parse([])  # pas de robots.txt : tout est permis
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        return rp is not None and rp.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> str | None:
        host = urlsplit(url).netloc.lower()
        if not url.startswith(("http://", "https://")) or host.endswith("news.google.com"):
            return None
        if not self._allowed(url):
            log.info("Texte non téléchargé (robots.txt ou indisponible) : %s", host)
            return None
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            log.info("Texte non téléchargé (%s) : %s", exc.__class__.__name__, host)
            return None
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html").lower():
            log.info("Texte non téléchargé (HTTP %s) : %s", resp.status_code, host)
            return None
        if len(resp.content) > self.max_bytes:
            return None
        try:
            import trafilatura
        except ImportError:
            log.warning("trafilatura n'est pas installé : extraction du texte impossible.")
            return None
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False, favor_precision=True)
        if not text or len(text) < self.min_chars:
            return None  # paywall, page vide ou extraction ratée
        return text
