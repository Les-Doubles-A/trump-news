"""Interfaces des fournisseurs. Le reste de l'application ne dépend que de ces types.

Phase 1 : ILLMProvider. Les autres interfaces (INewsProvider, ITTSProvider, ...) sont
ajoutées dans la phase qui les implémente, pour ne jamais avoir d'interface sans implémentation.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import LLMResponseError
from app.utils.json_tools import extract_json, inline_refs

if TYPE_CHECKING:
    from app.factcheck.models import FactCheckResult
    from app.video.models import RenderJob, RenderResult

M = TypeVar("M", bound=BaseModel)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system | user | assistant
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_s: float = 0.0


@dataclass
class HealthStatus:
    ok: bool
    detail: str
    models: list[str] = field(default_factory=list)


class ILLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Teste la connexion sans lever d'exception."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Modèles de chat disponibles."""

    @abstractmethod
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Envoie une conversation et retourne la réponse."""

    def complete_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[M] | None = None,
        repair_attempts: int = 1,
        **kwargs: Any,
    ) -> Any:
        """Réponse JSON stricte. Si `schema` (modèle Pydantic) est fourni, la sortie est validée
        et retournée comme instance ; sinon retourne l'objet JSON décodé.
        En cas de JSON invalide, redemande au modèle de corriger (repair_attempts fois)."""
        json_schema = inline_refs(schema.model_json_schema()) if schema is not None else None
        convo = list(messages)
        last_err = "inconnue"
        for attempt in range(repair_attempts + 1):
            resp = self.complete(convo, json_schema=json_schema, **kwargs)
            try:
                data = extract_json(resp.text)
                return schema.model_validate(data) if schema is not None else data
            except (ValueError, ValidationError) as exc:
                last_err = str(exc)
                log.warning("JSON invalide du LLM (tentative %d): %s", attempt + 1, last_err[:300])
                convo = convo + [
                    ChatMessage("assistant", resp.text),
                    ChatMessage(
                        "user",
                        "Ta réponse n'est pas un JSON valide conforme au schéma. Erreur : "
                        f"{last_err[:500]}\nRéponds uniquement avec le JSON corrigé, sans texte autour.",
                    ),
                ]
        raise LLMResponseError(f"Le LLM n'a pas produit de JSON valide après correction : {last_err[:300]}")


@dataclass
class RawArticle:
    """Article tel que renvoyé par un fournisseur, avant normalisation."""

    title: str
    url: str
    source: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    summary: str | None = None
    content: str | None = None
    image_url: str | None = None
    provider: str = ""


class INewsProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, limit: int = 50) -> list[RawArticle]:
        """Récupère les articles récents. Lève NewsProviderError en cas d'échec."""


class IEmbedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Vecteurs sémantiques (un par texte, dans l'ordre)."""


class IFactChecker(ABC):
    @abstractmethod
    def check(self, story_id: int, *, force: bool = False) -> "FactCheckResult":
        """Vérifie les affirmations d'un sujet et retourne un résultat structuré et sourcé."""


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str | None = None
    gender: str | None = None


@dataclass
class SynthOptions:
    voice: str
    language: str = "en"
    speed: float = 1.0


@dataclass
class SynthResult:
    path: Path
    duration_s: float
    words: list[dict] = field(default_factory=list)   # [{"text", "start", "end"}] si le moteur les fournit
    chars: int = 0
    cost_usd: float = 0.0


class ITTSProvider(ABC):
    name: str = "base"
    requires_internet: bool = False

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Teste la disponibilité sans lever d'exception."""

    @abstractmethod
    def list_voices(self, language: str | None = None) -> list[Voice]:
        ...

    @abstractmethod
    def synthesize(self, text: str, out_path: Path, options: SynthOptions) -> SynthResult:
        """Écrit un fichier WAV dans out_path. Lève TTSUnavailableError (réessayable) ou TTSError."""


class IVideoRenderer(ABC):
    """Monteur vidéo. Reçoit un RenderJob complet (aucun accès à la base) et écrit un MP4."""

    name: str = "base"

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Teste la disponibilité (exécutable, filtres nécessaires) sans lever d'exception."""

    @abstractmethod
    def render(self, job: "RenderJob", out_path: Path) -> "RenderResult":
        """Monte la vidéo dans out_path. Lève ProviderError en cas d'échec."""


@dataclass(frozen=True)
class MusicTrack:
    path: Path
    title: str
    license: str
    credit: str = ""


class IMusicProvider(ABC):
    """Pistes audio (musique, effets) dont la licence est connue. Jamais de téléchargement automatique."""

    name: str = "base"

    @abstractmethod
    def list_tracks(self) -> list[MusicTrack]:
        """Pistes utilisables, licence déclarée."""


@dataclass
class PublishRequest:
    """Tout ce qu'une plateforme reçoit : le fichier et des métadonnées déjà construites et vérifiées (aucun accès à la base)."""

    video_path: Path
    title: str
    description: str                   # texte long (YouTube) : narration, sources avec liens, crédits
    caption: str = ""                  # légende courte (TikTok, Instagram : 2 200 caractères au plus)
    tags: list[str] = field(default_factory=list)
    language: str = "en"
    ai_content: bool = False           # images IA dans la vidéo : signalé à la plateforme quand elle le permet
    duration_s: float = 0.0


@dataclass
class PublishResult:
    platform: str
    status: str                        # PUBLISHED | PENDING (traitement en cours côté plateforme)
    platform_video_id: str | None = None
    url: str | None = None
    detail: str = ""


class IPublisher(ABC):
    """Publication sur une plateforme, uniquement par son API officielle."""

    name: str = "base"

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Vérifie que le compte est connecté (jeton valide) sans publier ni lever d'exception."""

    @abstractmethod
    def publish(self, req: PublishRequest) -> PublishResult:
        """Envoie la vidéo. Lève PublisherError (refus) ou PublisherUnavailableError (réessayable)."""

    def status(self, platform_video_id: str) -> PublishResult:
        """Suit une publication restée PENDING. Par défaut, rien à suivre."""
        return PublishResult(self.name, "PUBLISHED", platform_video_id)
