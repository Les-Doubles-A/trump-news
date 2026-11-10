"""Instagram Reels via l'API officielle Instagram Graph (Facebook Login for Business), envoi direct du fichier
(« resumable upload » sur rupload.facebook.com) : aucun hébergement web n'est nécessaire.

Prérequis (une fois) : compte Instagram professionnel relié à une Page Facebook, application Meta avec les permissions
instagram_basic, instagram_content_publish et pages_read_engagement, jeton de Page (secret INSTAGRAM_ACCESS_TOKEN,
enregistré par `run.bat connect instagram`). Limite Meta : 100 publications par 24 h et par compte.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from app.config.schema import InstagramSection
from app.core.errors import PublisherError, PublisherUnavailableError
from app.core.interfaces import HealthStatus, IPublisher, PublishRequest, PublishResult
from app.publishing.oauth import SecretStore

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
TOKEN = "INSTAGRAM_ACCESS_TOKEN"
CAPTION_MAX = 2200
_RATE_CODES = {4, 17, 32, 613}
PENDING_PREFIX = "container:"


class InstagramPublisher(IPublisher):
    name = "instagram"

    def __init__(self, cfg: InstagramSection, user_id: str = "", store: SecretStore | None = None,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 poll_interval: float = 20.0, poll_tries: int = 15) -> None:
        self.cfg, self.user_id, self.store, self._sleep = cfg, user_id or cfg.user_id, store or SecretStore(), sleep
        self.poll_interval, self.poll_tries = poll_interval, poll_tries
        self._client = client or httpx.Client(timeout=httpx.Timeout(60, write=600, read=600))

    @property
    def _base(self) -> str:
        return f"{GRAPH}/{self.cfg.api_version}"

    def _token(self) -> str:
        tok = self.store.get(TOKEN)
        if not tok:
            raise PublisherError("Compte Instagram non connecté : lancez run.bat connect instagram")
        return tok

    def _ig(self) -> str:
        if not self.user_id:
            raise PublisherError("Identifiant du compte Instagram inconnu : lancez run.bat connect instagram")
        return self.user_id

    def _call(self, method: str, path: str, token: str | None = None, **kwargs) -> dict:
        params = {**kwargs.pop("params", {}), "access_token": token or self._token()}
        try:
            resp = self._client.request(method, f"{self._base}/{path}", params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"Instagram injoignable ({exc.__class__.__name__}).") from exc
        data = _json(resp)
        if resp.status_code >= 400:
            _raise(resp.status_code, data)
        return data

    # ------------------------------------------------------------ connexion
    def connect(self, token: str) -> tuple[str, str]:
        """Vérifie un jeton de Page et trouve le compte Instagram relié. Retourne (identifiant, nom du compte)."""
        page = self._call("GET", "me", token, params={"fields": "id,name,instagram_business_account{id,username}"})
        account = page.get("instagram_business_account") or {}
        if not account.get("id"):
            raise PublisherError(f"Aucun compte Instagram professionnel relié à « {page.get('name') or 'ce jeton'} ». "
                                 "Utilisez un jeton de PAGE Facebook dont la Page est reliée au compte Instagram.")
        self.store.set(TOKEN, token)
        self.user_id = str(account["id"])
        return self.user_id, account.get("username") or ""

    def health_check(self) -> HealthStatus:
        try:
            info = self._call("GET", self._ig(), params={"fields": "username"})
            return HealthStatus(True, f"Connecté à @{info.get('username', '?')}")
        except Exception as exc:
            return HealthStatus(False, str(exc))

    # ----------------------------------------------------------- publication
    def publish(self, req: PublishRequest) -> PublishResult:
        token, ig = self._token(), self._ig()
        container = self._call("POST", f"{ig}/media", token, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": (req.caption or req.title)[:CAPTION_MAX],
            "share_to_feed": "true" if self.cfg.share_to_feed else "false"})
        cid = container.get("id")
        if not cid:
            raise PublisherError("Instagram n'a pas créé de conteneur pour la vidéo.")
        data = req.video_path.read_bytes()
        upload_url = container.get("uri") or f"{RUPLOAD}/{self.cfg.api_version}/{cid}"
        try:
            resp = self._client.post(upload_url, content=data, headers={
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"Envoi Instagram interrompu ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 400:
            _raise(resp.status_code, _json(resp))
        log.info("Instagram : vidéo envoyée (conteneur %s), attente du traitement", cid)
        for _ in range(self.poll_tries):
            res = self._finish(cid, token, ig)
            if res.status != "PENDING":
                return res
            self._sleep(self.poll_interval)
        return PublishResult(self.name, "PENDING", PENDING_PREFIX + cid, None,
                             "traitement Instagram en cours : run.bat publications --refresh pour finir la publication")

    def _finish(self, cid: str, token: str, ig: str) -> PublishResult:
        """Publie le conteneur s'il est prêt ; sinon PENDING."""
        code = self._call("GET", cid, token, params={"fields": "status_code"}).get("status_code", "")
        if code in ("ERROR", "EXPIRED"):
            raise PublisherError(f"Instagram a refusé la vidéo (statut {code}).")
        if code != "FINISHED":
            return PublishResult(self.name, "PENDING", PENDING_PREFIX + cid, None, code or "en cours")
        media_id = self._call("POST", f"{ig}/media_publish", token, data={"creation_id": cid}).get("id")
        if not media_id:
            raise PublisherError("Instagram n'a pas confirmé la publication.")
        try:
            url = self._call("GET", str(media_id), token, params={"fields": "permalink"}).get("permalink")
        except PublisherError:
            url = None
        return PublishResult(self.name, "PUBLISHED", str(media_id), url, "Reel publié")

    def status(self, platform_video_id: str) -> PublishResult:
        if not platform_video_id.startswith(PENDING_PREFIX):
            return PublishResult(self.name, "PUBLISHED", platform_video_id)
        return self._finish(platform_video_id[len(PENDING_PREFIX):], self._token(), self._ig())


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _raise(status: int, data: dict) -> None:
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    code, msg = err.get("code"), err.get("message", "")
    if code == 190:
        raise PublisherError("Jeton Instagram expiré ou révoqué : relancez run.bat connect instagram")
    if code in _RATE_CODES or status == 429 or status >= 500:
        raise PublisherUnavailableError(f"Instagram indisponible ou limite atteinte ({code or status}) : réessayez plus tard.")
    raise PublisherError(f"Instagram a refusé la requête ({code or status}) : {msg[:300]}")
