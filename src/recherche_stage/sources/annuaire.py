"""Cibles de candidature spontanée via l'API Recherche d'entreprises
(recherche-entreprises.api.gouv.fr : publique, gratuite, sans clé).

Seules des données d'entreprise sont conservées : ni dirigeants ni personnes
physiques (RGPD).
"""

from __future__ import annotations

from ..modeles import GrilleCriteres, Offre
from ..reseau import Http

URL = "https://recherche-entreprises.api.gouv.fr/search"


class SourceAnnuaire:
    nom = "annuaire"

    def __init__(self, http: Http, codes_naf: list[str], departements: list[str]):
        self.http = http
        self.codes_naf = codes_naf
        self.departements = departements

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        intitule = f"Stage {grille.type_stage}".strip() if grille.type_stage else "Stage"
        offres: list[Offre] = []
        for naf in self.codes_naf or [None]:
            for departement in self.departements or [None]:
                params = {"per_page": min(25, limite), "etat_administratif": "A"}
                if naf:
                    params["activite_principale"] = naf
                if departement:
                    params["departement"] = departement
                donnees = self.http.get_json(URL, params=params)
                for r in (donnees or {}).get("results", []):
                    siege = r.get("siege") or {}
                    offres.append(
                        Offre(
                            intitule=f"{intitule} (candidature spontanée)",
                            entreprise=r.get("nom_complet") or r.get("nom_raison_sociale", ""),
                            lieu=siege.get("libelle_commune", ""),
                            description=f"Activité principale (NAF) : {r.get('activite_principale', '')}",
                            source="annuaire",
                            type="spontanee",
                            secteur=r.get("activite_principale"),
                            url=f"https://annuaire-entreprises.data.gouv.fr/entreprise/{r.get('siren', '')}",
                        )
                    )
                    if len(offres) >= limite:
                        return offres
        return offres
