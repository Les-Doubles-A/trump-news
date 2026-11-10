"""Hash de déduplication des articles."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|ocid$)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p.split("=")[0])))
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{query}" if query else "")


def article_hash(url: str, title: str) -> str:
    norm_title = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha256(f"{normalize_url(url)}|{norm_title}".encode()).hexdigest()
