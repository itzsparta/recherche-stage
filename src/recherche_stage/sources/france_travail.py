"""API officielle « Offres d'emploi v2 » de France Travail (francetravail.io).

Nécessite un compte développeur et une application abonnée à l'API ; les
identifiants sont lus dans les variables d'environnement nommées dans la
configuration.
"""

from __future__ import annotations

from ..modeles import GrilleCriteres, Offre
from ..reseau import Http
from . import est_stage, lire_date

URL_JETON = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
URL_RECHERCHE = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"


class SourceFranceTravail:
    nom = "france_travail"

    def __init__(self, http: Http, client_id: str, secret: str):
        self.http = http
        self.client_id = client_id
        self.secret = secret

    def _jeton(self) -> str:
        reponse = self.http.post_form(
            f"{URL_JETON}?realm=%2Fpartenaire",
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.secret,
                "scope": "api_offresdemploiv2 o2dsoffre",
            },
        )
        return reponse["access_token"]

    @staticmethod
    def _vers_offre(r: dict) -> Offre:
        contact = r.get("contact") or {}
        return Offre(
            intitule=r.get("intitule", ""),
            entreprise=(r.get("entreprise") or {}).get("nom") or "Entreprise non communiquée",
            lieu=(r.get("lieuTravail") or {}).get("libelle", ""),
            description=r.get("description", ""),
            url=(r.get("origineOffre") or {}).get("urlOrigine")
            or f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id', '')}",
            source="france_travail",
            contact_email=contact.get("courriel"),
            secteur=r.get("secteurActiviteLibelle"),
            date_publication=lire_date(r.get("dateCreation")),
        )

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        entetes = {"Authorization": f"Bearer {self._jeton()}"}
        offres: list[Offre] = []
        for requete in grille.requetes or [grille.type_stage or "stage"]:
            donnees = self.http.get_json(
                URL_RECHERCHE,
                params={"motsCles": f"stage {requete}".strip(), "range": f"0-{min(limite, 149)}"},
                entetes=entetes,
            )
            for r in (donnees or {}).get("resultats", []):
                offre = self._vers_offre(r)
                if est_stage(offre.intitule, offre.description):
                    offres.append(offre)
            if len(offres) >= limite:
                break
        return offres[:limite]
