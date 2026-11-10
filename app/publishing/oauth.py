"""Connexion OAuth d'un compte depuis le PC : le navigateur s'ouvre sur la page officielle de la plateforme,
un petit serveur local (127.0.0.1) reçoit le code d'autorisation, puis le jeton est rangé dans le trousseau Windows.

Aucun mot de passe ne transite par l'application : seule la plateforme le voit.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from app.config.secrets import get_secret, set_secret
from app.core.errors import PublisherError

_PAGE = ("<html><head><meta charset='utf-8'><title>Trump News</title></head><body style='font-family:sans-serif'>"
         "<h2>{title}</h2><p>{text}</p></body></html>")


class SecretStore:
    """Lecture/écriture des jetons (trousseau Windows ou .env). Remplaçable dans les tests."""

    def get(self, name: str, *, required: bool = False) -> str | None:
        return get_secret(name, required=required)

    def set(self, name: str, value: str) -> None:
        set_secret(name, value)


def pkce_pair(*, hex_challenge: bool = False) -> tuple[str, str]:
    """(code_verifier, code_challenge) en S256. Google attend du base64url, TikTok (bureau) l'empreinte en hexadécimal."""
    verifier = secrets.token_urlsafe(64)[:96]          # caractères A-Z a-z 0-9 - _ : autorisés par la norme
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = digest.hex() if hex_challenge else base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def wait_for_code(build_url: Callable[[str, str], str], *, port: int = 0, path: str = "/", redirect_host: str = "127.0.0.1",
                  timeout_s: float = 300, opener: Callable[[str], object] = webbrowser.open) -> tuple[str, str]:
    """Ouvre la page d'autorisation et attend le retour du navigateur. Retourne (code, redirect_uri).

    build_url(redirect_uri, state) construit l'adresse de la page d'autorisation."""
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (nom imposé par http.server)
            url = urlparse(self.path)
            if url.path.rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in parse_qs(url.query).items()})
            ok = "code" in received and "error" not in received
            body = _PAGE.format(title="Connexion réussie" if ok else "Connexion refusée",
                                text="Vous pouvez fermer cet onglet et revenir à Trump News." if ok
                                else received.get("error_description") or received.get("error", "erreur inconnue"))
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):   # pas de journal de requêtes (le code d'autorisation y apparaîtrait)
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise PublisherError(f"Port local {port} indisponible pour la connexion ({exc}). Fermez l'autre programme "
                             "ou changez le port dans la configuration.") from exc
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://{redirect_host}:{server.server_address[1]}{path}"
    server.timeout = 0.5
    try:
        opener(build_url(redirect_uri, state))
        deadline = time.monotonic() + timeout_s
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise PublisherError(f"Aucune réponse du navigateur en {timeout_s:.0f} s : connexion annulée.")
    if "error" in received:
        raise PublisherError(f"Connexion refusée par la plateforme : {received.get('error_description') or received['error']}")
    if received.get("state") != state:
        raise PublisherError("Réponse de connexion invalide (state différent) : recommencez.")
    if not received.get("code"):
        raise PublisherError("La plateforme n'a pas renvoyé de code d'autorisation.")
    return received["code"], redirect_uri
