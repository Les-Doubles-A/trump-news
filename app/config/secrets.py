"""Accès aux secrets : variable d'environnement d'abord, puis Windows Credential Manager (keyring)."""
from __future__ import annotations

import os

from app.core.errors import SecretNotFoundError
from app.utils.logging_setup import register_secret

SERVICE = "trump-news"


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:  # keyring absent ou backend indisponible
        return None


def get_secret(name: str, *, required: bool = False) -> str | None:
    value = os.environ.get(name)
    if not value:
        kr = _keyring()
        if kr is not None:
            try:
                value = kr.get_password(SERVICE, name)
            except Exception:
                value = None
    if value:
        register_secret(value)
        return value
    if required:
        raise SecretNotFoundError(
            f"Secret '{name}' introuvable. Définissez la variable d'environnement {name} "
            f"(fichier .env) ou enregistrez-le : python -m app.main set-secret {name}"
        )
    return None


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr is None:
        raise SecretNotFoundError("Le module 'keyring' est indisponible ; utilisez le fichier .env.")
    kr.set_password(SERVICE, name, value)
    register_secret(value)
