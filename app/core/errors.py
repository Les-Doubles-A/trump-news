"""Exceptions de l'application. Chaque erreur a un message lisible par l'utilisateur."""


class TrumpNewsError(Exception):
    """Base de toutes les erreurs applicatives."""


class ConfigError(TrumpNewsError):
    """Configuration invalide ou introuvable."""


class SecretNotFoundError(TrumpNewsError):
    """Secret requis absent (variable d'environnement ou trousseau)."""


class ProviderError(TrumpNewsError):
    """Erreur générique d'un fournisseur (LLM, news, TTS...)."""


class LLMUnavailableError(ProviderError):
    """Le LLM est injoignable, en timeout ou répond 5xx. Réessayable."""


class LLMResponseError(ProviderError):
    """Le LLM a répondu, mais la réponse est inutilisable. Non réessayable tel quel."""


class LLMModelLoadError(LLMResponseError):
    """LM Studio n'a pas pu charger le modèle demandé (VRAM insuffisante, identifiant erroné...)."""


class NewsProviderError(ProviderError):
    """Un fournisseur d'actualités a échoué (réseau, HTTP, flux illisible). Réessayable."""


class TTSError(ProviderError):
    """Synthèse vocale impossible (voix introuvable, clé absente, exécutable manquant...). Non réessayable."""


class TTSUnavailableError(TTSError):
    """Service vocal momentanément indisponible (réseau, 5xx, quota). Réessayable."""


class PublisherError(ProviderError):
    """Publication refusée par la plateforme (droits, compte non connecté, vidéo refusée...). Non réessayable telle quelle."""


class PublisherUnavailableError(PublisherError):
    """Plateforme momentanément indisponible (réseau, 5xx, quota). Réessayable plus tard."""
