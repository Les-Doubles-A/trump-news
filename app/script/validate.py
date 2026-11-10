"""Contrôle déterministe d'un script : chaque phrase doit être rattachée à une affirmation vérifiée,
et formulée selon le statut de cette affirmation. Fonction pure, entièrement testable."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from app.config.schema import ScriptSection
from app.factcheck.grounding import numbers_grounded, numbers_in
from app.script.markers import ATTRIBUTION, DISAGREE, EDITORIAL, GENERIC_THIRD_PARTY, has_marker
from app.script.models import DURATIONS, SECTIONS, ClaimInfo, Violation

_WORD = re.compile(r"\w+(?:['’-]\w+)*", re.UNICODE)
_CAP = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'’-]{2,}$")
_JUNK = re.compile(r"https?://|www\.|#\w|[\U0001F300-\U0001FAFF\u2600-\u27BF]|\[[^\]]*\]|\*")


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def target_words(duration_s: int, language: str, cfg: ScriptSection) -> int:
    return round(duration_s * cfg.words_per_second.get(language, 2.5))


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(w.lower() for w in _WORD.findall(a)), set(w.lower() for w in _WORD.findall(b))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _norm(word: str) -> str:
    """Forme comparée d'un mot : minuscules, sans possessif (« Trump's » = « Trump »)."""
    return re.sub(r"['’]s$", "", word.lower()).strip("'’-")


def _known_tokens(texts: Iterable[str]) -> set[str]:
    return {_norm(w) for t in texts for w in _WORD.findall(t)}


def validate(
    segments: Sequence,                     # objets avec .section .type .text .claims
    claims: dict[str, ClaimInfo],
    *,
    story_title: str,
    language: str,
    tone: str,
    duration_s: int,
    cfg: ScriptSection,
    recent_scripts: Sequence[str] = (),     # textes de scripts récents d'AUTRES sujets
    claims_language: str = "en",
) -> list[Violation]:
    v: list[Violation] = []
    if not segments:
        return [Violation("empty", "le script est vide")]

    # ---- structure
    order = {s: i for i, s in enumerate(SECTIONS)}
    if segments[0].section != "hook":
        v.append(Violation("structure", "le premier segment doit être le hook", 0))
    if sum(1 for s in segments if s.section == "hook") != 1:
        v.append(Violation("structure", "il faut exactement un segment 'hook'"))
    if not any(s.section == "main" for s in segments):
        v.append(Violation("structure", "il faut au moins un segment 'main'"))
    if segments[-1].section != "conclusion":
        v.append(Violation("structure", "le dernier segment doit être la conclusion", len(segments) - 1))
    last = -1
    for i, s in enumerate(segments):
        if order[s.section] < last:
            v.append(Violation("structure", f"section '{s.section}' dans le mauvais ordre", i))
        last = max(last, order[s.section])

    allow = {a.lower() for a in cfg.entity_allowlist}
    title_tokens = _known_tokens([story_title])
    opinion_count = 0

    for i, s in enumerate(segments):
        text = s.text.strip()
        cited = [claims[c] for c in s.claims if c in claims]
        bad = [c for c in s.claims if c not in claims]
        if bad:
            v.append(Violation("invalid_claim", f"affirmation(s) inexistante(s) citée(s) : {', '.join(bad)}", i))
        if _JUNK.search(text):
            v.append(Violation("format", "le texte de narration ne doit contenir ni URL, ni émoji, ni hashtag, ni crochets", i))
        low = text.lower().replace("’", "'")
        for phrase in cfg.banned_phrases:
            if phrase in low:
                v.append(Violation("clickbait", f"formule racoleuse interdite : « {phrase} »", i))

        if s.type == "context":
            if cited:
                v.append(Violation("context", "un segment 'context' ne cite aucune affirmation (utilisez fact/attributed)", i))
            if numbers_in(text) or count_words(text) > 18:
                v.append(Violation("context", "un segment 'context' est une courte phrase de liaison, sans chiffre", i))
        elif not cited:
            v.append(Violation("uncited", f"segment '{s.type}' sans affirmation valide associée", i))

        # ---- chiffres : aucun chiffre qui ne vienne des affirmations citées
        pool = " ".join(c.text for c in cited) + " " + story_title
        if numbers_in(text) and not numbers_grounded(text, pool):
            v.append(Violation("number", "un chiffre du texte n'apparaît dans aucune affirmation citée", i))

        # ---- noms propres (souple par défaut)
        known = _known_tokens([c.text for c in cited] + [c.speaker or "" for c in cited] + [n for c in cited for n in c.sources]) | title_tokens
        for sentence in (re.split(r"(?<=[.!?…])\s+", text) if language == claims_language else []):  # pas de comparaison inter-langues
            words = _WORD.findall(sentence)
            for w in words[1:]:
                lw = _norm(w)
                if _CAP.match(w) and lw not in known and lw not in allow and not w.isupper():
                    v.append(Violation("entity", f"nom propre « {w} » absent des affirmations citées", i, hard=cfg.strict_entities))
                    break

        # ---- statut -> formulation
        if s.type == "fact":
            for c in cited:
                if c.status != "CONFIRMED" or c.kind != "fact":
                    v.append(Violation("not_established",
                                       f"{c.id} est {c.status}/{c.kind} : ne pas l'énoncer comme un fait (utiliser 'attributed' ou 'disputed')", i))
        disputed = [c for c in cited if c.status == "DISPUTED"]
        if disputed:
            if s.type != "disputed":
                v.append(Violation("disputed_type", f"{disputed[0].id} est DISPUTED : le segment doit être de type 'disputed'", i))
            if not has_marker(text, DISAGREE, language):
                v.append(Violation("disputed_wording", "présenter la divergence des sources (« cependant », « les sources divergent »...)", i))
        needs_attr = [c for c in cited if c.status in ("REPORTED", "LIKELY") or c.kind in ("statement", "allegation", "opinion")]
        if needs_attr:
            who = [x.lower() for c in needs_attr for x in c.sources + ([c.speaker] if c.speaker else [])]
            if not (has_marker(text, ATTRIBUTION, language) or any(w and w in low for w in who)):
                c0 = needs_attr[0]
                v.append(Violation("attribution", f"{c0.id} ({c0.status}/{c0.kind}) doit être attribué "
                                                  "(« selon <média> », « Trump a déclaré », « plusieurs médias rapportent »)", i))

        # ---- opinions
        if s.type == "opinion":
            only_opinion_claims = bool(cited) and all(c.kind == "opinion" for c in cited)
            editorial = tone in ("pro_trump", "anti_trump")
            if editorial and not only_opinion_claims:
                opinion_count += 1
                if not has_marker(text, EDITORIAL, language):
                    v.append(Violation("unmarked_opinion", "opinion éditoriale : l'introduire par une formule explicite (« à notre avis »...)", i))
                if has_marker(text, GENERIC_THIRD_PARTY, language):
                    v.append(Violation("fabricated_attribution", "ne pas attribuer l'opinion à des « analystes/critiques » non sourcés", i))
            elif not only_opinion_claims:
                v.append(Violation("opinion_not_allowed", "ton neutre/équilibré : pas d'opinion propre, seulement celles issues des sources", i))
            elif not has_marker(text, ATTRIBUTION, language):
                v.append(Violation("unmarked_opinion", "attribuer l'opinion à son auteur", i))
        if s.section == "hook":
            if count_words(text) > cfg.hook_max_words:
                v.append(Violation("hook", f"hook trop long ({count_words(text)} mots, max {cfg.hook_max_words})", i))
            if any(w.isupper() and len(w) > 3 for w in _WORD.findall(text)):
                v.append(Violation("hook", "pas de mots en MAJUSCULES dans le hook", i))
            if s.type in ("context", "opinion"):
                v.append(Violation("hook", "le hook doit s'appuyer sur une affirmation vérifiée (fact/attributed/disputed)", i))

    if opinion_count > cfg.max_opinion_segments:
        v.append(Violation("opinion_limit", f"trop d'opinions éditoriales ({opinion_count}, max {cfg.max_opinion_segments})"))

    # ---- longueur
    full = " ".join(s.text for s in segments)
    n, target = count_words(full), target_words(duration_s, language, cfg)
    lo, hi = round(target * cfg.length_low), round(target * cfg.length_high)
    if n < lo or n > hi:
        v.append(Violation("length", f"{n} mots : visez {target} (entre {lo} et {hi}) pour {duration_s} s"))

    # ---- doublon avec un script récent d'un autre sujet
    for other in recent_scripts:
        if _jaccard(full, other) >= cfg.duplicate_similarity:
            v.append(Violation("duplicate", "script trop proche d'un script récent : reformulez et variez la structure"))
            break
    return v


def supported_duration(n_claims: int, language: str, cfg: ScriptSection) -> int:
    """Durée maximale que la matière vérifiée permet de remplir sans inventer (~30 mots par affirmation)."""
    capacity = (20 + 30 * n_claims) * 1.15
    ok = [d for d in DURATIONS if target_words(d, language, cfg) <= capacity]
    return ok[-1] if ok else DURATIONS[0]
