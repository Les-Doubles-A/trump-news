"""TikTok via l'API officielle Content Posting (publication directe, envoi du fichier par morceaux).

Prérequis (une fois) : application sur developers.tiktok.com avec Login Kit (bureau) et Content Posting API (Direct Post),
adresse de retour http://localhost:<port>/callback/ déclarée. Secrets : TIKTOK_CLIENT_KEY et TIKTOK_CLIENT_SECRET,
puis `run.bat connect tiktok` (jeton TIKTOK_REFRESH_TOKEN, valable un an).
Tant que TikTok n'a pas audité l'application, toute publication reste privée (SELF_ONLY) : c'est une règle de TikTok.
"""
from __future__ import annotations

import logging
import time
import webbrowser
from typing import Callable
from urllib.parse import urlencode

import httpx

from app.config.schema import TikTokSection
from app.core.errors import PublisherError, PublisherUnavailableError
from app.core.interfaces import HealthStatus, IPublisher, PublishRequest, PublishResult
from app.publishing.oauth import SecretStore, pkce_pair, wait_for_code

log = logging.getLogger(__name__)
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"
REFRESH = "TIKTOK_REFRESH_TOKEN"
MIN_CHUNK, MAX_CHUNK, BIG_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024, 10 * 1024 * 1024
TITLE_MAX = 2200

_MESSAGES = {
    "access_token_invalid": "Jeton TikTok expiré ou révoqué : relancez run.bat connect tiktok",
    "scope_not_authorized": "TikTok : l'autorisation « video.publish » n'a pas été accordée. Relancez run.bat connect tiktok",
    "unaudited_client_can_only_post_to_private_accounts":
        "TikTok n'a pas encore audité votre application : elle ne peut publier que sur un compte PRIVÉ. Passez le compte "
        "en privé dans l'application TikTok, ou demandez l'audit sur developers.tiktok.com.",
    "privacy_level_option_mismatch": "Visibilité TikTok non autorisée pour ce compte : changez publishing.tiktok.privacy.",
    "spam_risk_user_banned_from_posting": "TikTok interdit actuellement la publication sur ce compte.",
}
_TRANSIENT = {"rate_limit_exceeded", "spam_risk_too_many_posts", "reached_active_user_cap"}


def chunk_plan(size: int) -> tuple[int, int]:
    """(taille d'un morceau, nombre de morceaux) selon les règles TikTok : 5 à 64 Mo par morceau,
    le dernier absorbe le reste ; un fichier de moins de 64 Mo part en un seul morceau."""
    if size <= MAX_CHUNK:
        return size, 1
    return BIG_CHUNK, size // BIG_CHUNK


class TikTokPublisher(IPublisher):
    name = "tiktok"

    def __init__(self, cfg: TikTokSection, store: SecretStore | None = None, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_interval: float = 5.0, poll_tries: int = 24) -> None:
        self.cfg, self.store, self._sleep = cfg, store or SecretStore(), sleep
        self.poll_interval, self.poll_tries = poll_interval, poll_tries
        self._client = client or httpx.Client(timeout=httpx.Timeout(60, write=600, read=600))
        self._token: str | None = None

    # ------------------------------------------------------------ connexion
    def _client_ids(self) -> tuple[str, str]:
        key, secret = self.store.get("TIKTOK_CLIENT_KEY"), self.store.get("TIKTOK_CLIENT_SECRET")
        if not key or not secret:
            raise PublisherError("Identifiants TikTok absents. Enregistrez-les : run.bat set-secret TIKTOK_CLIENT_KEY "
                                 "puis run.bat set-secret TIKTOK_CLIENT_SECRET (voir README, Phase 9).")
        return key, secret

    def connect(self, opener: Callable[[str], object] = webbrowser.open, timeout_s: float = 300) -> str:
        key, secret = self._client_ids()
        verifier, challenge = pkce_pair(hex_challenge=True)

        def build(redirect_uri: str, state: str) -> str:
            return AUTH_URL + "?" + urlencode({
                "client_key": key, "response_type": "code", "scope": SCOPES, "redirect_uri": redirect_uri,
                "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})

        code, redirect_uri = wait_for_code(build, port=self.cfg.redirect_port, path="/callback/", redirect_host="localhost",
                                           opener=opener, timeout_s=timeout_s)
        self._save(self._oauth({"client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code",
                                "redirect_uri": redirect_uri, "code_verifier": verifier}))
        return self.creator_info().get("creator_nickname") or "compte TikTok"

    def _oauth(self, form: dict[str, str]) -> dict:
        try:
            resp = self._client.post(f"{API}/oauth/token/", data=form)
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"TikTok injoignable ({exc.__class__.__name__}).") from exc
        if resp.status_code >= 500:
            raise PublisherUnavailableError(f"TikTok indisponible ({resp.status_code}).")
        data = _json(resp)
        if resp.status_code >= 400 or not data.get("access_token"):
            err = data.get("error_description") or data.get("error") or resp.status_code
            raise PublisherError(f"TikTok a refusé l'authentification ({err}) : relancez run.bat connect tiktok")
        return data

    def _save(self, data: dict) -> None:
        self._token = data["access_token"]
        if data.get("refresh_token"):            # TikTok peut renvoyer un nouveau jeton de renouvellement : on garde le dernier
            self.store.set(REFRESH, data["refresh_token"])

    def _access_token(self) -> str:
        if self._token is None:
            refresh = self.store.get(REFRESH)
            if not refresh:
                raise PublisherError("Compte TikTok non connecté : lancez run.bat connect tiktok")
            key, secret = self._client_ids()
            self._save(self._oauth({"client_key": key, "client_secret": secret, "grant_type": "refresh_token",
                                    "refresh_token": refresh}))
        return self._token or ""

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._client.post(f"{API}{path}", json=body, headers={
                "Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json; charset=UTF-8"})
        except httpx.HTTPError as exc:
            raise PublisherUnavailableError(f"TikTok injoignable ({exc.__class__.__name__}).") from exc
        data = _json(resp)
        err = data.get("error") or {}
        code = err.get("code", "") if isinstance(err, dict) else ""
        if resp.status_code >= 500:
            raise PublisherUnavailableError(f"TikTok indisponible ({resp.status_code}).")
        if code in _TRANSIENT or resp.status_code == 429:
            raise PublisherUnavailableError(f"TikTok : limite atteinte ({code or resp.status_code}), réessayez plus tard.")
        if resp.status_code >= 400 or (code and code != "ok"):
            raise PublisherError(_MESSAGES.get(code) or f"TikTok a refusé la requête ({code or resp.status_code}) : "
                                 f"{(err.get('message') if isinstance(err, dict) else '') or ''}"[:300])
        return data.get("data") or {}

    def creator_info(self) -> dict:
        return self._post("/post/publish/creator_info/query/", {})

    def health_check(self) -> HealthStatus:
        try:
            info = self.creator_info()
            return HealthStatus(True, f"Connecté à « {info.get('creator_nickname') or info.get('creator_username')} » "
                                      f"(visibilités permises : {', '.join(info.get('privacy_level_options', []))})")
        except Exception as exc:
            return HealthStatus(False, str(exc))

    # ----------------------------------------------------------- publication
    def publish(self, req: PublishRequest) -> PublishResult:
        info = self.creator_info()
        options = info.get("privacy_level_options") or []
        if self.cfg.privacy not in options:
            raise PublisherError(f"Visibilité TikTok « {self.cfg.privacy} » non permise pour ce compte. Possibles : "
                                 f"{', '.join(options) or 'aucune'} (run.bat set-option publishing.tiktok.privacy ...).")
        max_s = info.get("max_video_post_duration_sec")
        if max_s and req.duration_s > max_s:
            raise PublisherError(f"Vidéo trop longue pour ce compte TikTok ({req.duration_s:.0f} s > {max_s} s).")
        size = req.video_path.stat().st_size
        chunk, count = chunk_plan(size)
        init = self._post("/post/publish/video/init/", {
            "post_info": {
                "title": (req.caption or req.title)[:TITLE_MAX], "privacy_level": self.cfg.privacy,
                "disable_comment": self.cfg.disable_comment or bool(info.get("comment_disabled")),
                "disable_duet": self.cfg.disable_duet or bool(info.get("duet_disabled")),
                "disable_stitch": self.cfg.disable_stitch or bool(info.get("stitch_disabled")),
                "video_cover_timestamp_ms": 1000, "brand_content_toggle": False, "brand_organic_toggle": False,
                "is_aigc": req.ai_content,
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
        })
        publish_id, upload_url = init.get("publish_id"), init.get("upload_url")
        if not publish_id or not upload_url:
            raise PublisherError("TikTok n'a pas fourni d'adresse d'envoi.")
        self._upload(upload_url, req.video_path.read_bytes(), chunk, count)
        log.info("TikTok : vidéo envoyée (publish_id %s), attente du traitement", publish_id)
        username = info.get("creator_username")
        for _ in range(self.poll_tries):
            res = self._status(publish_id, username)
            if res.status != "PENDING":
                return res
            self._sleep(self.poll_interval)
        return PublishResult(self.name, "PENDING", publish_id, None,
                             "traitement TikTok en cours : run.bat publications --refresh pour suivre")

    def _upload(self, url: str, data: bytes, chunk: int, count: int) -> None:
        size = len(data)
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            try:
                resp = self._client.put(url, content=data[start:end + 1], headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"})
            except httpx.HTTPError as exc:
                raise PublisherUnavailableError(f"Envoi TikTok interrompu ({exc.__class__.__name__}).") from exc
            if resp.status_code >= 500:
                raise PublisherUnavailableError(f"TikTok indisponible pendant l'envoi ({resp.status_code}).")
            if resp.status_code not in (200, 201, 206):
                raise PublisherError(f"TikTok a refusé le morceau {i + 1}/{count} ({resp.status_code}) : {resp.text[:200]}")

    def _status(self, publish_id: str, username: str | None = None) -> PublishResult:
        data = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
        st = data.get("status", "")
        if st == "FAILED":
            raise PublisherError(f"TikTok a refusé la vidéo : {data.get('fail_reason') or 'raison non précisée'}")
        if st != "PUBLISH_COMPLETE":
            return PublishResult(self.name, "PENDING", publish_id, None, st or "en cours")
        ids = data.get("publicaly_available_post_id") or []       # orthographe de l'API TikTok
        url = f"https://www.tiktok.com/@{username}/video/{ids[0]}" if ids and username else None
        return PublishResult(self.name, "PUBLISHED", str(ids[0]) if ids else publish_id, url,
                             f"visibilité {self.cfg.privacy}" + ("" if ids else " (lien public disponible après modération)"))

    def status(self, platform_video_id: str) -> PublishResult:
        return self._status(platform_video_id, self.creator_info().get("creator_username"))


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
