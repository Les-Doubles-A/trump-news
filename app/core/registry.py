"""Registre générique de plugins : nom -> fabrique."""
from __future__ import annotations

from typing import Callable, Generic, TypeVar

from app.core.errors import ConfigError

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, name: str, factory: Callable[..., T]) -> None:
        self._factories[name.lower()] = factory

    def names(self) -> list[str]:
        return sorted(self._factories)

    def create(self, name: str, **kwargs) -> T:
        try:
            factory = self._factories[name.lower()]
        except KeyError:
            raise ConfigError(
                f"Fournisseur {self.kind} inconnu : '{name}'. Disponibles : {', '.join(self.names()) or 'aucun'}."
            ) from None
        return factory(**kwargs)
