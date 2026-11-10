"""Vérifications déterministes : une citation doit exister dans la source, un chiffre aussi, et la citation
doit parler du même sujet que l'affirmation (mêmes noms propres, au moins un mot significatif en commun).

Cela prouve que la source dit bien ce que le LLM cite et qu'il ne s'agit pas d'une phrase voisine hors sujet ;
cela ne prouve pas une implication logique complète (d'où la relecture possible du dossier).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})


def normalize(text: str) -> str:
    t = text.translate(_QUOTES).lower()
    t = re.sub(r"[^\w\s$%']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def quote_in_text(quote: str, text: str, *, min_words: int = 4, ratio: float = 0.85) -> bool:
    q = normalize(quote)
    words = q.split()
    if len(words) < min_words:
        return False
    t = normalize(text)
    if q in t:
        return True
    tw = t.split()
    n = len(words)
    qset = set(words)
    for i in range(0, max(1, len(tw) - n + 1)):
        window = tw[i : i + n]
        if len(qset & set(window)) < 0.6 * len(qset):
            continue
        if SequenceMatcher(None, q, " ".join(window)).ratio() >= ratio:
            return True
    return False


_SEP = re.compile(r"(?<=\d)[.,\s\u00a0\u202f'’](?=\d)")


def squash(text: str) -> str:
    """Supprime les séparateurs au milieu d'un nombre : 1,5 / 1.5 / 18 000 / 18,000 -> 15 / 15 / 18000."""
    return _SEP.sub("", text)


def numbers_in(text: str) -> list[str]:
    return re.findall(r"\d+", squash(text))


def numbers_grounded(claim: str, text: str) -> bool:
    """Chaque nombre cité dans l'affirmation doit apparaître dans le texte de la source."""
    haystack = squash(text)
    return all(n in haystack for n in numbers_in(claim))


_CAPWORD = re.compile(r"[A-Z][A-Za-z'’\-]*")
_NOT_NAMES = set(
    "The A An He She They It This That These Those His Her Their Its In On At For From With By Of To And But Or As "
    "If When While After Before During Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March "
    "April May June July August September October November December I We You Mr Mrs Ms Dr".split()
)


def _quote_words(text: str) -> set[str]:
    t = text.translate(_QUOTES).replace(".", "")          # U.S. -> US
    out = set()
    for w in re.findall(r"[\w']+", t.lower()):
        w = w.strip("'").removesuffix("'s")
        out.add(w)
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            out.add(w[:-1])
    return out


_ALIASES = {   # équivalents courants : « US » dans l'affirmation, « United States » dans la citation
    "us": {"united", "america", "american", "usa"}, "usa": {"united", "america", "american", "us"},
    "america": {"us", "usa", "united", "american"}, "united": {"us", "usa", "america", "uk", "britain"},
    "uk": {"britain", "british", "united", "england"}, "britain": {"uk", "british", "united"},
    "eu": {"european", "europe"},
}


def unsupported_terms(claim: str, quote: str, ignore: set[str] = frozenset()) -> list[str]:
    """Noms propres de l'affirmation (lieux, personnes, organisations) absents de la citation, quand ils en
    représentent PLUS DE LA MOITIÉ (sinon liste vide : la citation parle bien du même sujet).

    Une citation retrouvée mot pour mot ne prouve rien si elle parle d'autre chose (ex. affirmation sur le
    « Gulf of America » appuyée par une phrase sur Google). Le sujet principal et l'orateur (`ignore`) sont
    exclus : une citation les désigne souvent par « I » ou « he ». Le premier mot de chaque phrase n'est pas
    considéré comme un nom propre (majuscule de début de phrase)."""
    have = _quote_words(quote)
    names: list[str] = []
    # fin de phrase = ponctuation après une minuscule ou un chiffre (« U.S. » ne coupe pas la phrase)
    for sentence in re.split(r"(?<=[a-z0-9][.!?])\s+", claim.translate(_QUOTES)):
        for w in _CAPWORD.findall(sentence.replace(".", ""))[1:]:
            name = w.strip("'’-").removesuffix("'s")
            key = name.lower()
            if w not in _NOT_NAMES and key not in ignore and len(key) >= 2 and key not in (n.lower() for n in names):
                names.append(name)
    missing = [n for n in names if not _found(n.lower(), have)]
    return missing if len(missing) * 2 > len(names) else []


def _found(key: str, have: set[str]) -> bool:
    """Mot présent, équivalent connu (US/United States) ou de la même famille (Canada/Canadian, Mexico/Mexican)."""
    if key in have or (key.endswith("s") and key[:-1] in have) or _ALIASES.get(key, set()) & have:
        return True
    if len(key) >= 5:
        stem = key[: len(key) - 1]
        return any(w.startswith(stem) for w in have)
    return False


def shares_content(claim: str, quote: str, ignore: set[str] = frozenset()) -> bool:
    """Au moins un mot significatif (hors mots vides et sujet principal) en commun entre l'affirmation et la citation."""
    from app.news.cluster import title_tokens

    return bool(title_tokens(claim, set(ignore)) & title_tokens(quote, set(ignore)))
