"""Extraction robuste de JSON depuis une sortie de LLM."""
from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Retourne le premier objet/tableau JSON trouvé dans `text`.
    Ignore les blocs <think>, les clôtures ```json et le texte autour. Lève ValueError sinon."""
    cleaned = _THINK.sub("", text).strip()
    fence = _FENCE.search(cleaned)
    candidates = [fence.group(1).strip()] if fence else []
    candidates.append(cleaned)
    decoder = json.JSONDecoder()
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = decoder.raw_decode(cand[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
    raise ValueError("Aucun JSON valide trouvé dans la réponse")


def inline_refs(schema: dict) -> dict:
    """Résout les $ref locaux d'un schéma JSON (certains serveurs LLM ne gèrent pas $defs)."""
    defs = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**defs[name], **extra})
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
