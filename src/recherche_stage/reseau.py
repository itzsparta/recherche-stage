"""Client HTTP minimal (bibliothèque standard). Injectable pour les tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

AGENT_UTILISATEUR = "recherche-stage/1.0 (usage personnel, non commercial)"


class ErreurReseau(RuntimeError):
    pass


class Http(Protocol):
    def get_json(self, url: str, params: dict | None = None, entetes: dict | None = None) -> Any: ...

    def get_texte(self, url: str, params: dict | None = None, entetes: dict | None = None) -> str: ...

    def post_form(self, url: str, donnees: dict, entetes: dict | None = None) -> Any: ...

    def accessible(self, url: str) -> bool: ...


class HttpStandard:
    def __init__(self, delai: float = 20):
        self.delai = delai

    def _envoyer(self, requete: urllib.request.Request) -> bytes:
        requete.add_header("User-Agent", AGENT_UTILISATEUR)
        try:
            with urllib.request.urlopen(requete, timeout=self.delai) as reponse:
                return reponse.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ErreurReseau(f"{requete.full_url} : {e}") from e

    def get_json(self, url: str, params: dict | None = None, entetes: dict | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        corps = self._envoyer(urllib.request.Request(url, headers={"Accept": "application/json", **(entetes or {})}))
        return json.loads(corps or b"null")

    def get_texte(self, url: str, params: dict | None = None, entetes: dict | None = None) -> str:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        return self._envoyer(urllib.request.Request(url, headers=entetes or {})).decode("utf-8", errors="replace")

    def post_form(self, url: str, donnees: dict, entetes: dict | None = None) -> Any:
        corps = urllib.parse.urlencode(donnees).encode()
        requete = urllib.request.Request(
            url,
            data=corps,
            headers={"Content-Type": "application/x-www-form-urlencoded", **(entetes or {})},
            method="POST",
        )
        return json.loads(self._envoyer(requete) or b"null")

    def accessible(self, url: str) -> bool:
        """Vérifie qu'une offre est toujours en ligne (avant rédaction)."""
        try:
            self._envoyer(urllib.request.Request(url, method="GET"))
            return True
        except ErreurReseau as e:
            cause = e.__cause__
            # 404 et 410 signalent une offre retirée ; un refus d'accès ou une
            # erreur serveur ne prouvent rien.
            return not (isinstance(cause, urllib.error.HTTPError) and cause.code in (404, 410))
