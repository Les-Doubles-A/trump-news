"""Formules par langue que le contrôle automatique exige selon le statut d'une affirmation."""
from __future__ import annotations

import re
from functools import lru_cache

ATTRIBUTION = {
    "en": ["according to", "said", "says", "told", "reported", "reports", "report that", "reportedly", "claimed", "claims",
           "announced", "announces", "stated", "declared", "sources say", "outlets report", "media report", "alleged",
           "allegedly", "accused", "accuses", "warned", "warns", "wrote", "posted"],
    "fr": ["selon", "d'après", "a déclaré", "déclare", "a dit", "dit", "affirme", "a affirmé", "rapporte", "rapportent",
           "annonce", "a annoncé", "aurait", "auraient", "accuse", "prétend", "indique", "a indiqué", "explique",
           "a expliqué", "révèle", "les médias", "a averti", "a écrit"],
    "es": ["según", "dijo", "dice", "afirmó", "afirma", "declaró", "declara", "informa", "informan", "anunció",
           "anuncia", "habría", "acusa", "señaló", "indicó", "los medios", "advirtió", "escribió"],
}
DISAGREE = {
    "en": ["however", "but", "disputed", "contradict", "contradicts", "differ", "differs", "disagree", "while", "conflicting",
           "denied", "denies", "contest", "contests", "whereas", "yet"],
    "fr": ["cependant", "mais", "contesté", "contredit", "divergent", "divergentes", "en revanche", "alors que", "dément",
           "nie", "pourtant", "diffèrent"],
    "es": ["sin embargo", "pero", "disputado", "contradice", "difieren", "mientras que", "niega", "desmiente", "en cambio"],
}
# Opinion assumée par la rédaction (tons pro/anti) : jamais attribuée à un tiers inventé.
EDITORIAL = {
    "en": ["in our view", "from a critical perspective", "from a supportive perspective", "our take", "from our perspective"],
    "fr": ["à notre avis", "selon nous", "d'un point de vue critique", "d'un point de vue favorable", "de notre point de vue"],
    "es": ["en nuestra opinión", "desde una perspectiva crítica", "desde una perspectiva favorable", "a nuestro juicio"],
}
# Tiers génériques : interdits sauf si une affirmation d'opinion issue des sources les justifie.
GENERIC_THIRD_PARTY = {
    "en": ["analysts", "experts", "critics", "supporters", "observers", "commentators", "many say", "some say", "people say", "opponents"],
    "fr": ["analystes", "experts", "critiques", "partisans", "observateurs", "commentateurs", "certains estiment", "opposants"],
    "es": ["analistas", "expertos", "críticos", "partidarios", "observadores", "comentaristas", "algunos dicen", "opositores"],
}


@lru_cache(maxsize=64)
def _regex(terms: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)


def has_marker(text: str, table: dict[str, list[str]], lang: str) -> bool:
    return bool(_regex(tuple(table.get(lang, table["en"]))).search(text.replace("’", "'")))
