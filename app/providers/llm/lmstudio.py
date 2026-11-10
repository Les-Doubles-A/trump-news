"""Fournisseur LLM pour LM Studio (serveur local compatible OpenAI)."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import httpx

from app.core.errors import LLMModelLoadError, LLMResponseError, LLMUnavailableError
from app.core.interfaces import ChatMessage, HealthStatus, IEmbedder, ILLMProvider, LLMResponse, LLMUsage
from app.core.retry import retry_call

log = logging.getLogger(__name__)
_EMBEDDING_HINTS = ("embed", "nomic-bert", "rerank")
_VISION = re.compile(r"(?<![a-z])(vl|vision|llava)(?![a-z])")
_TINY = re.compile(r"(?<![\d.])\d{2,3}m(?![a-z])")  # ex. 270m = 270 millions de paramètres
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def is_text_model(model_id: str) -> bool:
    """Modèle adapté à la rédaction : ni vision, ni minuscule, ni utilitaire de titres."""
    m = model_id.lower()
    return not (_VISION.search(m) or _TINY.search(m) or "title-generator" in m)


class LMStudioProvider(ILLMProvider, IEmbedder):
    name = "lmstudio"

    def __init__(
        self,
        endpoint: str = "http://localhost:1234",
        model: str = "auto",
        timeout_s: float = 120,
        retries: int = 3,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self.base_url = base
        self.model = model
        self.retries = retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._sleep = sleep
        self._resolved_model: str | None = None
        self.last_model: str | None = None  # modèle réellement utilisé par la dernière réponse
        self._client = httpx.Client(
            base_url=f"{base}/v1",
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    # -- HTTP --------------------------------------------------------------
    def _request(self, method: str, path: str, *, attempts: int | None = None, **kwargs: Any) -> dict[str, Any]:
        def once() -> dict[str, Any]:
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.ConnectError as exc:
                raise LLMUnavailableError(
                    f"Impossible de joindre LM Studio sur {self.base_url}. Lancez LM Studio, "
                    "chargez un modèle et démarrez le serveur local (onglet Developer)."
                ) from exc
            except httpx.TimeoutException as exc:
                raise LLMUnavailableError(f"LM Studio n'a pas répondu à temps ({exc.__class__.__name__}).") from exc
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(f"Erreur réseau vers LM Studio : {exc}") from exc
            if resp.status_code >= 500:
                raise LLMUnavailableError(f"LM Studio a répondu {resp.status_code} : {resp.text[:200]}")
            if resp.status_code >= 400:
                if "failed to load model" in resp.text.lower():
                    raise LLMModelLoadError(
                        "LM Studio n'a pas pu charger le modèle demandé. Causes fréquentes : VRAM insuffisante "
                        "(essayez un modèle plus petit ou déchargez les autres) ou identifiant incomplet "
                        "(copiez-le tel qu'affiché par `run.bat check-llm`, ex. qwen/qwen3-8b)."
                    )
                raise LLMResponseError(f"Requête refusée par LM Studio ({resp.status_code}) : {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMResponseError("LM Studio a renvoyé une réponse non-JSON.") from exc

        return retry_call(
            once,
            attempts=attempts or self.retries,
            base_delay=1.0,
            retry_on=(LLMUnavailableError,),
            sleep=self._sleep,
            label=f"LM Studio {method} {path}",
        )

    # -- ILLMProvider ------------------------------------------------------
    def list_models(self) -> list[str]:
        data = self._request("GET", "/models")
        ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        return [i for i in ids if not any(h in i.lower() for h in _EMBEDDING_HINTS)]

    def health_check(self) -> HealthStatus:
        try:
            models = self._request("GET", "/models", attempts=1).get("data", [])
            ids = [m["id"] for m in models if isinstance(m, dict) and "id" in m]
            chat = [i for i in ids if not any(h in i.lower() for h in _EMBEDDING_HINTS)]
            if not chat:
                return HealthStatus(False, "LM Studio répond, mais aucun modèle de chat n'est chargé.", [])
            return HealthStatus(True, f"Connecté ({len(chat)} modèle(s) disponible(s)).", chat)
        except Exception as exc:  # health_check ne lève jamais
            return HealthStatus(False, str(exc))

    def embedding_models(self) -> list[str]:
        data = self._request("GET", "/models")
        ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        return [i for i in ids if any(h in i.lower() for h in _EMBEDDING_HINTS)]

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        if not model or model == "auto":
            models = self.embedding_models()
            if not models:
                raise LLMUnavailableError(
                    "Aucun modèle d'embedding dans LM Studio. Téléchargez par exemple "
                    "« nomic-embed-text-v1.5 » (onglet Discover)."
                )
            model = models[0]
        data = self._request("POST", "/embeddings", json={"model": model, "input": texts})
        try:
            rows = sorted(data["data"], key=lambda r: r.get("index", 0))
            vecs = [list(map(float, r["embedding"])) for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError("Réponse d'embedding inattendue de LM Studio.") from exc
        if len(vecs) != len(texts):
            raise LLMResponseError("LM Studio a renvoyé un nombre de vecteurs inattendu.")
        return vecs

    def text_models(self) -> list[str]:
        models = self.list_models()
        return [m for m in models if is_text_model(m)] or models

    def resolve_model(self, requested: str | None = None) -> str:
        wanted = requested or self.model
        if wanted and wanted != "auto":
            return wanted
        if self._resolved_model:
            return self._resolved_model
        models = self.text_models()
        if not models:
            raise LLMUnavailableError("Aucun modèle de chat chargé dans LM Studio.")
        self._resolved_model = models[0]
        log.info("Modèle LM Studio sélectionné automatiquement : %s", self._resolved_model)
        return self._resolved_model

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        wanted = model or self.model
        auto = not wanted or wanted == "auto"
        if auto and self._resolved_model:
            candidates = [self._resolved_model]
        elif auto:
            candidates = self.text_models() or [self.resolve_model()]
        else:
            candidates = [wanted]
        last_exc: LLMModelLoadError | None = None
        tried: set[str] = set()
        i = 0
        while i < len(candidates):
            cand = candidates[i]
            i += 1
            if cand in tried:
                continue
            tried.add(cand)
            try:
                resp = self._complete_with(cand, messages, temperature, max_tokens, json_schema)
                if auto:
                    self._resolved_model = cand
                return resp
            except LLMModelLoadError as exc:
                last_exc = exc
                log.warning("Chargement du modèle %s impossible, essai du suivant.", cand)
                if auto and i == len(candidates):
                    # modèle choisi d'avance (resolve_model) : on élargit aux autres modèles disponibles
                    candidates += [m for m in self.text_models() if m not in tried and m not in candidates]
        assert last_exc is not None
        raise last_exc

    def _complete_with(self, model_id, messages, temperature, max_tokens, json_schema) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [m.as_dict() for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }
        started = time.perf_counter()
        data = self._request("POST", "/chat/completions", json=payload)
        latency = time.perf_counter() - started
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("Réponse LM Studio inattendue (pas de choices[0].message.content).") from exc
        if isinstance(text, str):
            text = _THINK.sub("", text).strip()  # modèles « reasoning » (Qwen3...)
        if not isinstance(text, str) or not text:
            raise LLMResponseError(
                "LM Studio a renvoyé une réponse vide (raisonnement tronqué ? augmentez llm.max_tokens)."
            )
        u = data.get("usage") or {}
        usage = LLMUsage(u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"))
        log.info(
            "LLM %s: %.1fs, tokens=%s",
            model_id,
            latency,
            usage.total_tokens,
            extra={"duration_s": round(latency, 2), "tokens": usage.total_tokens},
        )
        self.last_model = data.get("model", model_id)
        return LLMResponse(text=text, model=self.last_model, usage=usage, latency_s=latency)

    def close(self) -> None:
        self._client.close()
