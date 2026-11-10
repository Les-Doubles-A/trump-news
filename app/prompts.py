"""Chargement des prompts (dossier /prompts, modifiables sans toucher au code) et rendu Jinja2."""
from __future__ import annotations

import hashlib
from pathlib import Path

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from app.core.errors import ConfigError
from app.core.interfaces import ChatMessage

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SEPARATOR = "=== USER ==="
_env = SandboxedEnvironment(undefined=StrictUndefined, trim_blocks=True, keep_trailing_newline=False)


def prompt_path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.txt"


def prompt_hash(name: str) -> str:
    return hashlib.sha256(prompt_path(name).read_bytes()).hexdigest()


def render_prompt(name: str, **variables) -> list[ChatMessage]:
    path = prompt_path(name)
    if not path.exists():
        raise ConfigError(f"Prompt introuvable : {path}")
    raw = path.read_text(encoding="utf-8")
    system, _, user = raw.partition(SEPARATOR)
    try:
        messages = [ChatMessage("system", _env.from_string(system.strip()).render(**variables).strip())]
        if user.strip():
            messages.append(ChatMessage("user", _env.from_string(user.strip()).render(**variables).strip()))
    except TemplateError as exc:
        raise ConfigError(f"Erreur dans le prompt {path.name} : {exc}") from exc
    return messages
