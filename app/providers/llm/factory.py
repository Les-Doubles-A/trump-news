"""Création du fournisseur LLM d'après la configuration."""
from __future__ import annotations

from app.config.schema import LLMSection
from app.core.errors import ConfigError
from app.core.interfaces import ILLMProvider
from app.core.registry import Registry
from app.providers.llm.lmstudio import LMStudioProvider

llm_registry: Registry[ILLMProvider] = Registry("LLM")
llm_registry.register("lmstudio", LMStudioProvider)


def build_llm_provider(cfg: LLMSection) -> ILLMProvider:
    if cfg.provider.lower() != "lmstudio":
        raise ConfigError(
            f"Le fournisseur LLM '{cfg.provider}' n'est pas encore implémenté. "
            f"Disponibles : {', '.join(llm_registry.names())}."
        )
    return llm_registry.create(
        cfg.provider,
        endpoint=cfg.endpoint,
        model=cfg.model,
        timeout_s=cfg.timeout_s,
        retries=cfg.retries,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
