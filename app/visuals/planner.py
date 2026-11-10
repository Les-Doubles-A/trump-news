"""Plan visuel d'un script : pour chaque segment, une ou plusieurs prises (photo libre, carte, illustration...)
calées sur la durée réelle de la voix off, sans réutiliser les mêmes images, avec crédits et indication IA."""
from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.errors import ProviderError, TrumpNewsError
from app.core.retry import retry_call
from app.database.models import Narration, Script, VisualAssetRow, VisualPlanRow
from app.database.session import session_scope
from app.providers.visuals.base import IVisualProvider
from app.providers.visuals.factory import build_visual_provider, enabled
from app.visuals.models import PlanItem, VisualAsset, VisualPlan, VisualQuery

log = logging.getLogger(__name__)

STATUS_LABEL = {
    "en": {"LIKELY": "Multiple sources", "REPORTED": "Reported", "DISPUTED": "Disputed"},
    "fr": {"LIKELY": "Plusieurs sources", "REPORTED": "Rapporté", "DISPUTED": "Contesté"},
    "es": {"LIKELY": "Varias fuentes", "REPORTED": "Según medios", "DISPUTED": "Disputado"},
}
SOURCES_WORD = {"en": "Sources", "fr": "Sources", "es": "Fuentes"}
_CAP_STOP = {"The", "A", "An", "According", "Reports", "Report", "Here", "We", "This", "That", "It", "In", "On", "At", "He", "She",
             "They", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
             "April", "May", "June", "July", "August", "September", "October", "November", "December", "However", "But", "And"}
_NUMBER = re.compile(r"[$€£]\s?\d|\d[\d.,]*\s?(?:%|percent|million|billion|milliard|millón|millones)", re.I)
_DESC_STOP = {"the", "and", "with", "from", "showing", "shows", "text", "card", "graphic", "image", "photo", "picture", "display", "map", "of", "for",
              "overlay", "highlighted", "counter", "logo", "logos", "icon", "background", "featuring", "close", "shot"}


def desc_kind(desc: str) -> str:
    if re.search(r"text card|quote card|headline|lower third|title card", desc, re.I):
        return "card"
    if re.search(r"\b(map|chart|graph|diagram|infographic|illustration|icon|timeline)\b", desc, re.I):
        return "illustration"
    return "photo"


def extract_entities(texts: list[str], aliases: dict[str, str]) -> list[str]:
    counts: Counter[str] = Counter()
    for t in texts:
        # noms composés avec liaison : « Gulf of Mexico », « Bank of America », « Lake of the Woods »
        for m in re.finditer(r"\b[A-Z][\w'’.-]*(?:\s+(?:(?:of|of the|de|de la|del|du|des)\s+)?[A-Z][\w'’.-]*)*", t):
            ent = m.group(0).strip(" .'’-")
            words = ent.split()
            at_sentence_start = m.start() == 0 or t[max(0, m.start() - 2):m.start()] in (". ", "! ", "? ")
            if len(words) == 1 and at_sentence_start and words[0] not in aliases:
                continue
            while words and words[0] in _CAP_STOP:
                words.pop(0)
            if not words or len(" ".join(words)) < 3:
                continue
            counts[aliases.get(" ".join(words), " ".join(words))] += 1
    return [e for e, _ in counts.most_common(4)]


class VisualPlanner:
    def __init__(self, cfg: AppConfig, factory: sessionmaker[Session], data_root: Path, *,
                 provider_factory: Callable[[str], IVisualProvider] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg, self.v, self.factory, self.root = cfg, cfg.visuals, factory, Path(data_root)
        self._make = provider_factory or (lambda name: build_visual_provider(name, cfg))
        self._sleep = sleep
        self._providers: dict[str, IVisualProvider] = {}

    # ------------------------------------------------------------------ API
    def plan(self, script_id: int, *, use_ai: bool | None = None) -> VisualPlan:
        script, narration = self._load(script_id)
        lang = script["language"]
        segments, claims, refs = script["segments"], {c["id"]: c for c in script["claims"]}, script["refs"]
        sources_by_claim = {r["claim_id"]: list(dict.fromkeys(s["name"] for s in r["sources"])) for r in refs}
        all_sources = list(dict.fromkeys(n for names in sources_by_claim.values() for n in names))
        suggestions = {}
        for v in script["visuals"]:
            suggestions.setdefault(v.get("segment", 0), v.get("description", ""))
        windows = self._windows(segments, narration, lang)

        order = [n for n in self.v.order if enabled(n, self.cfg) and (use_ai is not False or n != "ai")]
        if use_ai is True and "ai" not in order:
            order.insert(-1 if "cards" in order else len(order), "ai")
        warnings: list[str] = []
        down: set[str] = set()
        used = self._recent_ids()
        used_hashes: set[str] = set()
        items: list[PlanItem] = []

        for seg in segments:
            idx = seg["index"]
            start, end = windows[idx]
            cids = [c for c in seg.get("claims", []) if c in claims]
            texts = [claims[c]["claim"] for c in cids]
            names = list(dict.fromkeys(n for c in cids for n in sources_by_claim.get(c, [])))
            entities = [e for e in extract_entities(texts, self.v.entity_aliases) if not self._is_outlet(e, all_sources)]
            status = next((claims[c]["status"] for c in cids if claims[c]["status"] in STATUS_LABEL[lang]), "")
            n = max(1, min(self.v.max_shots_per_segment, round((end - start) / self.v.target_shot_s)))
            roles = self._roles(seg, n, bool(cids), bool(all_sources))
            desc = suggestions.get(idx, "")
            seg_items: list[PlanItem] = []
            for k, role in enumerate(roles):
                a, b = start + (end - start) * k / len(roles), start + (end - start) * (k + 1) / len(roles)
                chain = self._chain(role, k, seg, desc, entities, names, status, all_sources, lang)
                asset = self._acquire(chain, order, used, used_hashes, down, warnings, lang)
                if asset is None:
                    if seg_items:                      # rien de nouveau à montrer : on prolonge la prise précédente
                        seg_items[-1].end = round(b, 3)
                    else:
                        warnings.append(f"Segment {idx + 1} : aucun visuel disponible (activez « cards » pour un repli garanti).")
                    continue
                if (asset.provider == "cards" and role != "sources" and seg_items
                        and seg_items[-1].asset["provider"] == "cards"):
                    seg_items[-1].end = round(b, 3)    # deux cartes de suite pour un même segment : redondant, on prolonge
                    continue                           # (jamais la carte des sources : elle doit toujours apparaître)
                used.add(f"{asset.provider}:{asset.external_id}")
                if asset.sha256:
                    used_hashes.add(asset.sha256)
                item = PlanItem(idx, k, round(a, 3), round(b, 3), role, asset.to_dict())
                seg_items.append(item)
                items.append(item)

        credits = list(dict.fromkeys(i.asset["attribution"] for i in items
                                     if i.asset.get("attribution") and i.asset["provider"] in ("wikimedia", "pexels")))
        plan = VisualPlan(script_id, items, credits, any(i.asset.get("ai_generated") for i in items), warnings,
                          narration_id=narration["id"] if narration else None)
        if not narration:
            plan.warnings.append("Pas de voix off : durées estimées (lancez run.bat narrate pour caler précisément).")
        self._persist(plan)
        return plan

    # ------------------------------------------------------------- internals
    def _load(self, script_id: int):
        with session_scope(self.factory) as s:
            row = s.get(Script, script_id)
            if row is None:
                raise TrumpNewsError(f"Script #{script_id} introuvable. Voir : run.bat scripts")
            data = row.claims or {}
            if not data.get("segments"):
                raise TrumpNewsError(f"Le script #{script_id} ne contient aucun segment.")
            script = {"language": row.language, "segments": data["segments"], "claims": data.get("claims", []),
                      "visuals": data.get("visuals", []), "refs": row.source_refs or []}
            nar = s.scalar(select(Narration).where(Narration.script_id == script_id).order_by(Narration.id.desc()))
            narration = {"id": nar.id, "timeline": nar.timeline} if nar else None
        return script, narration

    def _windows(self, segments, narration, lang) -> dict[int, tuple[float, float]]:
        if narration:
            return {t["index"]: (t["start"], t["end"]) for t in narration["timeline"]}
        wps = self.cfg.script.words_per_second.get(lang, 2.5)
        out, t = {}, 0.0
        for seg in segments:
            d = max(1.5, len(re.findall(r"\w+", seg["text"])) / wps)
            out[seg["index"]] = (t, t + d)
            t += d + (self.cfg.tts.hook_pause_s if seg["section"] == "hook" else self.cfg.tts.pause_s)
        return out

    @staticmethod
    def _roles(seg, n: int, has_claims: bool, has_sources: bool) -> list[str]:
        if seg["section"] == "conclusion" and has_sources:
            return ["primary"] * (n - 1) + ["sources"]
        if n == 1:
            return ["primary"]
        second = "card" if has_claims else "primary"
        return ([ "primary", second, "primary"] + ["primary"] * 3)[:n]

    def _chain(self, role, k, seg, desc, entities, names, status, all_sources, lang) -> list[VisualQuery]:
        text = seg["text"]
        badge = STATUS_LABEL[lang].get(status, "")
        footer = " · ".join(names[:4])
        cat = "politics"
        if role == "sources":
            return [VisualQuery("sources", f"{SOURCES_WORD[lang]} : {' · '.join(all_sources[:5])}", language=lang)]
        if role == "card":
            kind = "number" if _NUMBER.search(text) else "claim"
            return [VisualQuery(kind, text, footer=footer, badge=badge, category=cat, language=lang)]
        words = [w for w in re.findall(r"[A-Za-z]{3,}", desc) if w.lower() not in _DESC_STOP]
        ents = entities[k // 2:] or entities   # 2e prise « primary » : entité suivante
        dk = desc_kind(desc) if desc else "photo"
        chain: list[VisualQuery] = []
        opening = seg["section"] == "hook" and k == 0
        if opening:   # première image de la vidéo : une photo du sujet principal accroche mieux qu'une carte ou une carte géographique
            subject = ents or ([self.v.hook_entity] if self.v.hook_entity else [])
            if subject:
                chain.append(VisualQuery("photo", desc if dk == "photo" else "", entities=subject[:2],
                                         keywords=words if dk == "photo" else [], language=lang))
        if dk != "card":
            if ents and dk == "photo" and not opening:
                chain.append(VisualQuery("photo", desc, entities=ents[:2], keywords=words, language=lang))
            chain.append(VisualQuery("stock", desc, entities=[], keywords=words or ents, language=lang))
            chain.append(VisualQuery("illustration", desc or text, entities=entities, keywords=words, language=lang))
        chain.append(VisualQuery("headline" if seg["section"] == "hook" else "claim", text, footer=footer, badge=badge,
                                 category=cat, language=lang))
        return chain

    def _is_outlet(self, entity: str, outlets: list[str]) -> bool:
        """Un nom de média (Reuters, BBC News...) n'est pas un sujet de photo."""
        slug = re.sub(r"[^a-z0-9]", "", entity.lower())
        known = {re.sub(r"[^a-z0-9]", "", o.lower()) for o in outlets}
        known |= set(self.cfg.news.source_aliases) | set(self.cfg.news.source_aliases.values()) | set(self.cfg.news.source_reliability)
        return bool(slug) and (slug in known or any(slug in k or (len(k) > 3 and k in slug) for k in known if len(slug) > 3))

    def _provider(self, name: str) -> IVisualProvider:
        if name not in self._providers:
            self._providers[name] = self._make(name)
        return self._providers[name]

    def _acquire(self, chain, order, used, used_hashes, down, warnings, lang) -> VisualAsset | None:
        dest = self.root / "images"
        for q in chain:
            for name in order:
                if name in down:
                    continue
                try:
                    prov = self._provider(name)
                except TrumpNewsError as exc:
                    down.add(name)
                    warnings.append(f"{name} indisponible : {exc}")
                    continue
                if q.kind not in prov.kinds:
                    continue
                try:
                    run = lambda prov=prov, q=q: prov.get(q, dest, set(used))  # noqa: E731
                    asset = retry_call(run, attempts=self.v.retries if prov.requires_internet else 1, base_delay=1.0,
                                       retry_on=(ProviderError,), sleep=self._sleep, label=f"visuels:{name}") \
                        if prov.requires_internet else run()
                except Exception as exc:  # un fournisseur en panne ne bloque pas le plan
                    down.add(name)
                    warnings.append(f"{name} en échec ({exc}) : fournisseur ignoré pour la suite." +
                                    (" Internet requis." if getattr(prov, "requires_internet", False) else ""))
                    continue
                if asset and not (asset.sha256 and asset.sha256 in used_hashes):
                    return asset
        return None

    def _recent_ids(self) -> set[str]:
        n = self.v.avoid_recent_plans
        if n <= 0:
            return set()
        with session_scope(self.factory) as s:
            plans = list(s.scalars(select(VisualPlanRow).order_by(VisualPlanRow.id.desc()).limit(n)))
        return {f"{i['asset']['provider']}:{i['asset']['external_id']}" for p in plans for i in p.plan.get("items", [])
                if i["asset"]["provider"] != "cards"}

    def _persist(self, plan: VisualPlan) -> None:
        with session_scope(self.factory) as s:
            for it in plan.items:
                a = it.asset
                row = s.scalar(select(VisualAssetRow).where(VisualAssetRow.provider == a["provider"],
                                                            VisualAssetRow.external_id == a["external_id"]))
                if row is None:
                    s.add(VisualAssetRow(provider=a["provider"], external_id=a["external_id"], kind=a["kind"], path=a["path"],
                                         width=a["width"], height=a["height"], license=a.get("license"),
                                         attribution=a.get("attribution"), source_url=a.get("source_url"),
                                         ai_generated=bool(a.get("ai_generated")), sha256=a.get("sha256")))
            row = VisualPlanRow(script_id=plan.script_id, narration_id=plan.narration_id, plan=plan.to_dict(),
                                ai_disclosure=plan.ai_disclosure)
            s.add(row)
            s.flush()
            plan.id = row.id


def latest_plan(factory: sessionmaker[Session], script_id: int) -> VisualPlanRow | None:
    with session_scope(factory) as s:
        return s.scalar(select(VisualPlanRow).where(VisualPlanRow.script_id == script_id).order_by(VisualPlanRow.id.desc()))
