"""Cibles de candidature spontanée via l'API Recherche d'entreprises
(recherche-entreprises.api.gouv.fr : publique, gratuite, sans clé).

Seules des données d'entreprise sont conservées : ni dirigeants ni personnes
physiques (RGPD).

Chaque cycle reprend là où le précédent s'est arrêté (page suivante de chaque
couple code NAF × département, entreprises déjà vues écartées), en tournant
entre les couples pour ne pas épuiser un seul secteur.
"""

from __future__ import annotations

from typing import Any

from ..modeles import GrilleCriteres, Offre
from ..reseau import Http

URL = "https://recherche-entreprises.api.gouv.fr/search"
PAR_PAGE = 25
REQUETES_MAX = 30  # par collecte : l'API est publique, on reste sobre

# Tranches d'effectif salarié INSEE -> borne basse
TRANCHES = {
    "01": 1, "02": 3, "03": 6, "11": 10, "12": 20, "21": 50, "22": 100, "31": 200,
    "32": 250, "41": 500, "42": 1000, "51": 2000, "52": 5000, "53": 10000,
}


def tranches_depuis(effectif_min: int) -> list[str]:
    return [code for code, borne in TRANCHES.items() if borne >= effectif_min] if effectif_min > 0 else []


def _nombre(valeur) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def intitule_spontanee(grille: GrilleCriteres) -> str:
    type_stage = (grille.type_stage or "").strip()
    if type_stage.lower().startswith("stage"):
        type_stage = type_stage[5:].strip()
    return f"Stage {type_stage}".strip()


def sites(http: Http, nom: str, departements: list[str]) -> list[dict]:
    """Établissements en activité des sociétés trouvées pour ce nom (« STEF » :
    STEF Transport Lille, STEF Logistique Arras…), dans ces départements. Le
    tri des sociétés qui portent vraiment ce nom est laissé à l'appelant."""
    trouves, vus = [], set()
    for departement in departements or [None]:
        params: dict = {"q": nom, "per_page": PAR_PAGE, "etat_administratif": "A"}
        if departement:
            params["departement"] = departement
        for r in (http.get_json(URL, params=params) or {}).get("results", []):
            for e in r.get("matching_etablissements") or [r.get("siege") or {}]:
                siret, code_postal = e.get("siret"), str(e.get("code_postal") or "")
                if siret in vus or e.get("etat_administratif", "A") != "A":
                    continue
                if departement and not code_postal.startswith(departement):
                    continue
                vus.add(siret)
                trouves.append({
                    "entreprise": r.get("nom_complet") or r.get("nom_raison_sociale", ""),
                    "siren": str(r.get("siren", "")),
                    "lieu": f"{e.get('libelle_commune', '')} ({code_postal})" if code_postal else e.get("libelle_commune", ""),
                    "adresse": e.get("adresse") or "",
                    "latitude": _nombre(e.get("latitude")),
                    "longitude": _nombre(e.get("longitude")),
                    "activite": r.get("activite_principale"),
                    "tranche_effectif": r.get("tranche_effectif_salarie"),
                })
    return trouves


def _etablissement(r: dict, departement: str | None) -> dict:
    """L'établissement du département cherché plutôt que le siège : l'agence
    lilloise d'un groupe parisien est bien dans la zone."""
    if departement:
        for e in r.get("matching_etablissements") or []:
            if str(e.get("code_postal", "")).startswith(departement):
                return e
    return r.get("siege") or {}


class SourceAnnuaire:
    nom = "annuaire"

    def __init__(
        self,
        http: Http,
        codes_naf: list[str],
        departements: list[str],
        store: Any = None,
        effectif_min: int = 0,
        max_par_cycle: int | None = None,
    ):
        self.http = http
        self.codes_naf = codes_naf
        self.departements = departements
        self.store = store
        self.tranches = tranches_depuis(effectif_min)
        self.max_par_cycle = max_par_cycle

    def _memoire(self, cle: str, defaut):
        return self.store.parametre(cle, defaut) if self.store is not None else defaut

    def _vers_offre(self, r: dict, intitule: str, departement: str | None) -> Offre:
        lieu = _etablissement(r, departement)
        commune, code_postal = lieu.get("libelle_commune", ""), lieu.get("code_postal", "")
        description = f"Activité principale (NAF) : {r.get('activite_principale', '')}"
        if r.get("tranche_effectif_salarie"):
            description += f". Tranche d'effectif INSEE : {r['tranche_effectif_salarie']}"
        return Offre(
            intitule=f"{intitule} (candidature spontanée)",
            entreprise=r.get("nom_complet") or r.get("nom_raison_sociale", ""),
            lieu=f"{commune} ({code_postal})" if code_postal else commune,
            description=description,
            source="annuaire",
            type="spontanee",
            secteur=r.get("activite_principale"),
            url=f"https://annuaire-entreprises.data.gouv.fr/entreprise/{r.get('siren', '')}",
            adresse=lieu.get("adresse") or "",
            latitude=_nombre(lieu.get("latitude")),
            longitude=_nombre(lieu.get("longitude")),
            tranche_effectif=r.get("tranche_effectif_salarie"),
        )

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        if self.max_par_cycle:
            limite = min(limite, self.max_par_cycle)
        intitule = intitule_spontanee(grille)

        couples = [(n, d) for n in self.codes_naf or [None] for d in self.departements or [None]]
        curseurs: dict = self._memoire("annuaire_curseurs", {}) or {}
        vus: list[str] = self._memoire("annuaire_vus", []) or []
        deja = set(vus)
        rotation = int(self._memoire("annuaire_rotation", 0) or 0) % len(couples)
        actifs = [c for c in couples[rotation:] + couples[:rotation] if curseurs.get(f"{c[0]}|{c[1]}", 1) > 0]
        part = max(3, -(-limite // max(1, len(actifs))))

        offres: list[Offre] = []
        requetes = 0
        dernier = rotation
        for naf, departement in actifs:
            if len(offres) >= limite or requetes >= REQUETES_MAX:
                break
            cle = f"{naf}|{departement}"
            page = curseurs.get(cle, 1)
            params: dict = {"per_page": PAR_PAGE, "page": page, "etat_administratif": "A"}
            if naf:
                params["activite_principale"] = naf
            if departement:
                params["departement"] = departement
            if self.tranches:
                params["tranche_effectif_salarie"] = ",".join(self.tranches)
            donnees = self.http.get_json(URL, params=params) or {}
            requetes += 1
            dernier = couples.index((naf, departement))
            resultats = donnees.get("results", [])
            pris, page_finie = 0, True
            for r in resultats:
                siren = str(r.get("siren", ""))
                if siren and siren in deja:
                    continue
                if pris >= part or len(offres) >= limite:
                    page_finie = False  # le reste de la page attend le prochain cycle
                    break
                offres.append(self._vers_offre(r, intitule, departement))
                pris += 1
                if siren:
                    deja.add(siren)
                    vus.append(siren)
            if page_finie:
                total_pages = donnees.get("total_pages") or page
                curseurs[cle] = page + 1 if resultats and page < total_pages else -1

        if self.store is not None:
            self.store.definir_parametre("annuaire_curseurs", curseurs)
            self.store.definir_parametre("annuaire_vus", vus[-20000:])
            self.store.definir_parametre("annuaire_rotation", dernier + 1)
        return offres
