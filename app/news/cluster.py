"""Similarité de titres et regroupement d'articles parlant du même sujet."""
from __future__ import annotations

import re
from typing import Callable, Sequence, TypeVar

from app.config.schema import NewsSection

T = TypeVar("T")

_WORD = re.compile(r"[a-z0-9àâçéèêëîïôûùüÿœ']+")
_STOP = set(
    "the and for with that this from after over into about says said say new will amid what how why who when are was has have "
    "his her its not but you than out off can may could would now just more as at by in of on to a an is it be he she they us "
    "their there been were also than then them these those while during against before under between "
    "le la les des une un du de et en pour dans sur avec que qui est au aux ses son sa ce cette par pas plus".split()
)
_GENERIC = {"donald", "president", "white", "house", "administration", "news", "live", "update", "updates"}


def ignore_tokens(cfg: NewsSection) -> set[str]:
    out = set(_GENERIC)
    for kw in cfg.keywords:
        out.update(_WORD.findall(kw.lower()))
    return out


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def title_tokens(title: str, ignore: set[str] | None = None) -> frozenset[str]:
    ignore = ignore or set()
    toks = []
    for w in _WORD.findall(title.lower()):
        w = w.strip("'")
        if w in _STOP or w in ignore or (len(w) < 3 and not w.isdigit()):
            continue
        toks.append(_stem(w))
    return frozenset(toks)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """0..1. Exige des mots significatifs en commun pour éviter de fusionner « Trump says X » et « Trump says Y »."""
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    if len(shared) < 2 and min(len(a), len(b)) > 2:
        return 0.0
    jaccard = len(shared) / len(a | b)
    if len(shared) >= 3:
        return max(jaccard, 0.8 * len(shared) / min(len(a), len(b)))
    return jaccard


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def unit(v: list[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def cluster_items(items: Sequence[T], sim: Callable[[T, T], float], threshold: float) -> list[list[T]]:
    """Regroupement glouton : un item rejoint le groupe dont il est le plus proche (si sim >= threshold)."""
    clusters: list[list[T]] = []
    for it in items:
        best, best_sim = None, 0.0
        for idx, c in enumerate(clusters):
            s = max(sim(it, m) for m in c)
            if s > best_sim:
                best, best_sim = idx, s
        if best is not None and best_sim >= threshold:
            clusters[best].append(it)
        else:
            clusters.append([it])
    return clusters
