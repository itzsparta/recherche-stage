"""Sites carrière des entreprises via les API publiques de leur ATS.

Greenhouse, Lever et SmartRecruiters exposent les offres publiées en JSON,
précisément pour qu'on les affiche ailleurs : c'est l'accès prévu, pas du
scraping. On ne garde que les offres de stage.
"""

from __future__ import annotations

from ..config import EntrepriseATS
from ..modeles import GrilleCriteres, Offre
from ..reseau import Http
from . import est_stage, lire_date, texte_brut


def _greenhouse(http: Http, cible: EntrepriseATS) -> list[Offre]:
    donnees = http.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{cible.identifiant}/jobs", params={"content": "true"}
    )
    return [
        Offre(
            intitule=job.get("title", ""),
            entreprise=cible.nom,
            lieu=(job.get("location") or {}).get("name", ""),
            description=texte_brut(job.get("content", "")),
            url=job.get("absolute_url", ""),
            source="greenhouse",
            date_publication=lire_date(job.get("updated_at")),
        )
        for job in donnees.get("jobs", [])
    ]


def _lever(http: Http, cible: EntrepriseATS) -> list[Offre]:
    donnees = http.get_json(f"https://api.lever.co/v0/postings/{cible.identifiant}", params={"mode": "json"})
    return [
        Offre(
            intitule=job.get("text", ""),
            entreprise=cible.nom,
            lieu=(job.get("categories") or {}).get("location", ""),
            description=job.get("descriptionPlain") or texte_brut(job.get("description", "")),
            url=job.get("hostedUrl", ""),
            source="lever",
            date_publication=lire_date(job.get("createdAt")),
        )
        for job in donnees or []
    ]


def _smartrecruiters(http: Http, cible: EntrepriseATS) -> list[Offre]:
    donnees = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{cible.identifiant}/postings")
    return [
        Offre(
            intitule=job.get("name", ""),
            entreprise=cible.nom,
            lieu=(job.get("location") or {}).get("city", ""),
            url=f"https://jobs.smartrecruiters.com/{cible.identifiant}/{job.get('id', '')}",
            source="smartrecruiters",
            date_publication=lire_date(job.get("releasedDate")),
        )
        for job in donnees.get("content", [])
    ]


LECTEURS = {"greenhouse": _greenhouse, "lever": _lever, "smartrecruiters": _smartrecruiters}


class SourceATS:
    """Une instance par entreprise : le disjoncteur coupe un site sans couper les autres."""

    def __init__(self, http: Http, cible: EntrepriseATS):
        self.http = http
        self.cible = cible
        self.nom = f"ats:{cible.ats}:{cible.identifiant}"

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        lecteur = LECTEURS.get(self.cible.ats)
        if lecteur is None:
            raise ValueError(f"ATS inconnu : {self.cible.ats}")
        offres = [o for o in lecteur(self.http, self.cible) if est_stage(o.intitule, o.description)]
        return offres[:limite]
