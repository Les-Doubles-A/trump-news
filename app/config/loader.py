"""Chargement de la configuration : YAML + .env + surcharges TRUMPNEWS__SECTION__CLE."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from app.config.schema import AppConfig
from app.core.errors import ConfigError

ENV_PREFIX = "TRUMPNEWS__"
CONFIG_ENV_VAR = "TRUMPNEWS_CONFIG"


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not path:
            continue
        try:
            value = yaml.safe_load(raw)  # "true" -> True, "30" -> 30, "[a, b]" -> list
        except yaml.YAMLError:
            value = raw
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"Surcharge d'environnement invalide : {key}")
        node[path[-1]] = value


def load_config(path: str | Path | None = None, *, base_dir: Path | None = None, environ: dict[str, str] | None = None) -> AppConfig:
    base = base_dir or Path.cwd()
    if environ is None:
        load_dotenv(base / ".env", override=False)
        environ = dict(os.environ)

    explicit = path or environ.get(CONFIG_ENV_VAR)
    cfg_path = Path(explicit) if explicit else base / "config.yaml"
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Fichier de configuration illisible ({cfg_path}) : {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{cfg_path} doit contenir un mapping YAML.")
    elif explicit:
        raise ConfigError(f"Fichier de configuration introuvable : {cfg_path}")

    _apply_env_overrides(data, environ)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        lines = [f"  - {'.'.join(str(p) for p in e['loc'])} : {e['msg']}" for e in exc.errors()]
        raise ConfigError("Configuration invalide :\n" + "\n".join(lines)) from exc
