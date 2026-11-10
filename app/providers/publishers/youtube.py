"""YouTube Shorts via l'API officielle YouTube Data v3 (envoi « resumable »).

Prérequis (une fois) : projet Google Cloud, API YouTube Data v3 activée, identifiant OAuth de type « Application de bureau ».
Secrets : YOUTUBE_CLIENT_ID et YOUTUBE_CLIENT_SECRET, puis `run.bat connect youtube` (jeton YOUTUBE_REFRESH_TOKEN).
Une vidéo verticale de moins de 3 minutes est classée Short par YouTube.
"""
from __future__ import annotations

import logging
import time
import webbrowser
from typing import Callable
from urllib.parse import urlencode

import httpx

from app.config.schema import YouTubeSection
from app.core.errors import PublisherError, PublisherUnavailableError
from app.core.interfaces import HealthStatus, IPublisher, PublishRequest, PublishResult
from app.publishing.oauth import SecretStore, pkce_pair, wait_for_code

log = logging.getLogger(__name__)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
REFRESH = "YOUTUBE_REFRESH_TOKEN"
TRANSIENT = (500, 502, 503, 504)


class YouTubePublisher(IPublisher):
    name = "youtube"

    def __init__(self, cfg: YouTubeSection, store: SecretStore | None = None, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = 3) -> None:
        self.cfg, self.store, self._sleep, self.attempts = cfg, store or SecretStore(), sleep, attempts
        self._client = client or httpx.Client(timeout=httpx.Timeout(60, write=600, read=600))

    # ------------------------------------------------------------ connexion
    def _client_ids(self) -> tuple[str, str]:
        cid, secret = self.store.get("YOUTUBE_CLIENT_ID"), self.store.get("YOUTUBE_CLIENT_SECRET")
        if not cid or not secret:
            raise PublisherError("Identifiants OAuth YouTube absents. Enregistrez-les : run.bat set-secret YOUTUBE_CLIENT_ID "
                                 "puis run.bat set-secret YOUTUBE_CLIENT_SECRET (voir README, Phase 9).")
        cid, secret = cid.strip(), secret.strip()
        # L'ID client n'est pas secret (il figure dans l'adresse de la page Google) : on peut l'afficher.
        if not cid.endswith(".apps.googleusercontent.com") or " " in cid:
            raise PublisherError(f"YOUTUBE_CLIENT_ID invalide (« {cid} ») : il doit se terminer par "
                                 ".apps.googleusercontent.com. Recopiez-le : run.bat set-secret YOUTUBE_CLIENT_ID")
        return cid, secret

    def connect(self, opener: Callable[[str], object] = webbrowser.open, timeout_s: float = 300) -> str:
        """Ouvre la page de consentement Google et enregistre le jeton de longue durée. Retourne le nom de la chaîne."""
        cid, secret = self._client_ids()
        verifier, challenge = pkce_pair()

        def build(redirect_uri: str, state: str) -> str:
            return AUTH_URL + "?" + urlencode({
                "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
                "access_type": "offline", "prompt": "select_account consent", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256"})

        code, redirect_uri = wait_for_code(build, opener=opener, timeout_s=timeout_s)
        data = self._token({"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
                            "grant_type": "authorization_code", "code_verifier": verifier})
        if not data.get("refresh_token"):
            raise PublisherError("Google n'a pas fourni de jeton de longue durée : retirez l'accès de l'application dans "
                                 "votre compte Google (Sécurité > Applications tierces) puis relancez run.bat connect youtube.")
        self.store.set(REFRESH, data["refresh_token"])
        return self._channel(data["access_token"])

    def _token(self, form: dict[str, str]) -> dict:
        try:
            resp = self._client.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"Google injoignable ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 500:
            raise PublisherUnavailableError(f"Google indisponible ({resp.status_code}).")
        data = _json(resp)
        if resp.status_code >= 400:
            if data.get("error") == "invalid_grant":
                raise PublisherError("Connexion YouTube expirée ou révoquée : relancez run.bat connect youtube. "
                                     "(Tant que l'application Google est en mode « Test », Google l'expire au bout de 7 jours.)")
            raise PublisherError(f"Google a refusé l'authentification : {data.get('error_description') or data.get('error') or resp.status_code}")
        return data

    def _access_token(self) -> str:
        refresh = self.store.get(REFRESH)
        if not refresh:
            raise PublisherError("Compte YouTube non connecté : lancez run.bat connect youtube")
        cid, secret = self._client_ids()
        return self._token({"client_id": cid, "client_secret": secret, "refresh_token": refresh,
                            "grant_type": "refresh_token"})["access_token"]

    def _channel(self, token: str) -> str:
        resp = self._request("GET", f"{API}/channels", token, params={"part": "snippet", "mine": "true"})
        items = _json(resp).get("items") or []
        if not items:
            raise PublisherError("Ce compte Google n'a pas de chaîne YouTube : créez-la d'abord sur youtube.com.")
        return items[0]["snippet"]["title"]

    def health_check(self) -> HealthStatus:
        try:
            return HealthStatus(True, f"Connecté à la chaîne « {self._channel(self._access_token())} »")
        except Exception as exc:
            return HealthStatus(False, str(exc))

    # ----------------------------------------------------------- publication
    def publish(self, req: PublishRequest) -> PublishResult:
        token = self._access_token()
        size = req.video_path.stat().st_size
        body = {
            "snippet": {"title": req.title, "description": req.description, "tags": req.tags,
                        "categoryId": self.cfg.category_id, "defaultLanguage": req.language,
                        "defaultAudioLanguage": req.language},
            "status": {"privacyStatus": self.cfg.privacy, "selfDeclaredMadeForKids": self.cfg.made_for_kids,
                       "containsSyntheticMedia": req.ai_content},
        }
        resp = self._request("POST", UPLOAD_URL, token, params={"uploadType": "resumable", "part": "snippet,status"},
                             json=body, headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
        session = resp.headers.get("Location")
        if not session:
            raise PublisherError("YouTube n'a pas ouvert de session d'envoi (en-tête Location absent).")
        video = self._upload(session, token, req.video_path.read_bytes())
        vid = video["id"]
        log.info("YouTube : vidéo %s envoyée (%s)", vid, self.cfg.privacy)
        return PublishResult(self.name, "PUBLISHED", vid, f"https://www.youtube.com/shorts/{vid}",
                             f"visibilité {self.cfg.privacy}")

    def _upload(self, session: str, token: str, data: bytes) -> dict:
        """Envoie le fichier ; après une coupure ou une erreur 5xx, demande où l'envoi s'est arrêté et reprend de là."""
        size, start = len(data), 0
        for attempt in range(1, self.attempts + 1):
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}
            if start:
                headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
            try:
                resp = self._client.put(session, content=data[start:], headers=headers)
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code not in TRANSIENT:
                    _raise(resp, "envoi de la vidéo")
            except httpx.HTTPError as exc:
                log.warning("YouTube : envoi interrompu (%s), tentative %d/%d", exc.__class__.__name__, attempt, self.attempts)
            if attempt == self.attempts:
                break
            self._sleep(2 ** attempt)
            start, done = self._resume_offset(session, token, size)
            if done is not None:
                return done
        raise PublisherUnavailableError("Envoi YouTube interrompu à plusieurs reprises : réessayez plus tard.")

    def _resume_offset(self, session: str, token: str, size: int) -> tuple[int, dict | None]:
        """(premier octet à renvoyer, vidéo si l'envoi était en fait terminé)."""
        try:
            resp = self._client.put(session, content=b"", headers={"Authorization": f"Bearer {token}",
                                                                    "Content-Range": f"bytes */{size}"})
        except httpx.HTTPError:
            return 0, None
        if resp.status_code in (200, 201):
            return size, resp.json()
        if resp.status_code == 404:
            raise PublisherError("Session d'envoi YouTube expirée : relancez la publication.")
        rng = resp.headers.get("Range")                 # ex. « bytes=0-999999 » : octets déjà reçus
        return (int(rng.rsplit("-", 1)[1]) + 1 if resp.status_code == 308 and rng else 0), None

    def _request(self, method: str, url: str, token: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            resp = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"YouTube injoignable ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 400:
            _raise(resp, "requête")
        return resp


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _raise(resp: httpx.Response, what: str) -> None:
    err = _json(resp).get("error") or {}
    reasons = {e.get("reason") for e in err.get("errors", []) if isinstance(e, dict)} if isinstance(err, dict) else set()
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    if reasons & {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
        raise PublisherUnavailableError(f"Quota YouTube atteint ({', '.join(sorted(r for r in reasons if r))}) : réessayez demain.")
    if resp.status_code == 401:
        raise PublisherError("Jeton YouTube refusé : relancez run.bat connect youtube")
    if resp.status_code in TRANSIENT or resp.status_code == 429:
        raise PublisherUnavailableError(f"YouTube indisponible ({resp.status_code}) pendant {what}.")
    raise PublisherError(f"YouTube a refusé {what} ({resp.status_code}) : {msg[:300]}")
