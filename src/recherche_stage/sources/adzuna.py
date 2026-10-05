"""Agrégateur Adzuna, via son API officielle (developer.adzuna.com, offre
gratuite). Il reprend des offres de nombreux sites d'emploi et de sites
carrière ; la description renvoyée est un extrait.
"""

from __future__ import annotations

from ..modeles import GrilleCriteres, Offre
from ..reseau import Http
from . import est_stage, lire_date, texte_brut

URL = "https://api.adzuna.com/v1/api/jobs/{pays}/search/{page}"


class SourceAdzuna:
    nom = "adzuna"

    def __init__(self, http: Http, app_id: str, app_key: str, pays: str = "fr"):
        self.http = http
        self.app_id = app_id
        self.app_key = app_key
        self.pays = pays

    @staticmethod
    def _vers_offre(r: dict) -> Offre:
        salaire = r.get("salary_min")
        return Offre(
            intitule=texte_brut(r.get("title", "")),
            entreprise=(r.get("company") or {}).get("display_name") or "Entreprise non communiquée",
            lieu=(r.get("location") or {}).get("display_name", ""),
            description=texte_brut(r.get("description", "")),
            url=r.get("redirect_url", ""),
            source="adzuna",
            # Adzuna annonce un salaire annuel
            gratification=round(salaire / 12) if isinstance(salaire, (int, float)) and salaire else None,
            date_publication=lire_date(r.get("created")),
        )

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        offres: list[Offre] = []
        vus: set[str] = set()
        lieux = grille.durs.lieux or [""]
        for requete in grille.requetes or [grille.type_stage or ""]:
            for lieu in lieux:
                params = {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "what": f"stage {requete}".strip(),
                    "results_per_page": 50,
                    "sort_by": "date",
                }
                if lieu:
                    params["where"] = lieu
                donnees = self.http.get_json(URL.format(pays=self.pays, page=1), params=params)
                for r in (donnees or {}).get("results", []):
                    offre = self._vers_offre(r)
                    if offre.url in vus or not est_stage(offre.intitule, offre.description):
                        continue
                    vus.add(offre.url)
                    offres.append(offre)
                if len(offres) >= limite:
                    return offres[:limite]
        return offres[:limite]
