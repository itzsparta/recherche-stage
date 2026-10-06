"""Dépôt en présentiel : repère les candidatures spontanées qu'il vaut la peine
d'apporter en main propre (petite entreprise, à distance raisonnable de chez soi).

Les distances sont calculées à vol d'oiseau. Les coordonnées viennent de
l'annuaire des entreprises quand il les donne, sinon de la Base Adresse
Nationale (api-adresse.data.gouv.fr : publique, gratuite, sans clé), avec un
cache dans le store pour n'interroger chaque lieu qu'une fois.
"""

from __future__ import annotations

import math
import re

from .config import ParamsPresentiel
from .modeles import Dossier, Presentiel
from .reseau import ErreurReseau, Http
from .sources.annuaire import TRANCHES

API_ADRESSE = "https://api-adresse.data.gouv.fr/search/"
Point = tuple[float, float]  # (latitude, longitude)


def distance_km(a: Point, b: Point) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def geocoder(http: Http, store, texte: str) -> Point | None:
    """Coordonnées d'une adresse ou d'un lieu « COMMUNE (60290) ». None si
    introuvable ; une panne réseau n'est pas mise en cache (nouvel essai plus tard)."""
    texte = " ".join(texte.split())
    if not texte:
        return None
    cle = f"geo:{texte.lower()}"
    connu = store.parametre(cle) if store is not None else None
    if connu is not None:
        return tuple(connu) if connu else None
    params: dict = {"q": texte, "limit": 1}
    commune = re.fullmatch(r"(.+?)\s*\((\d{5})\)", texte)
    if commune:
        params.update(q=commune.group(1), postcode=commune.group(2), type="municipality")
    try:
        reponse = http.get_json(API_ADRESSE, params)
    except ErreurReseau:
        return None
    entites = (reponse or {}).get("features") or []
    point = None
    if entites:
        lon, lat = entites[0]["geometry"]["coordinates"]
        point = (lat, lon)
    if store is not None:
        store.definir_parametre(cle, list(point) if point else [])
    return point


def effectif_min(dossier: Dossier) -> int | None:
    tranche = dossier.offre.tranche_effectif
    if not tranche:
        trouve = re.search(r"Tranche d'effectif INSEE : (\w+)", dossier.offre.description)
        tranche = trouve.group(1) if trouve else None
    return TRANCHES.get(tranche) if tranche else None


def evaluer(dossier: Dossier, domicile: Point | None, entreprise: Point | None, params: ParamsPresentiel) -> Presentiel:
    o = dossier.offre
    adresse = o.adresse or o.lieu
    if o.type != "spontanee":
        return Presentiel(raison="offre publiée : la candidature passe par le canal indiqué", adresse=adresse)
    effectif = effectif_min(dossier)
    if effectif is not None and effectif >= params.effectif_max:
        return Presentiel(raison=f"grande entreprise ({effectif} salariés et plus) : recrutement rarement décidé sur place",
                          adresse=adresse)
    if domicile is None or entreprise is None:
        return Presentiel(raison="distance inconnue", adresse=adresse)
    km = round(distance_km(domicile, entreprise), 1)
    if km > params.distance_max_km:
        return Presentiel(distance_km=km, raison=f"à {km:.0f} km", adresse=adresse)
    raisons = [f"à {km:.0f} km"]
    if effectif is not None:
        raisons.append("petite entreprise (moins de 50 salariés)" if effectif < 50 else "PME")
    if not (o.contact_email or dossier.fiche_entreprise.contact):
        raisons.append("pas d'adresse mail publique")
    return Presentiel(conseille=True, distance_km=km, raison=", ".join(raisons), adresse=adresse)


def texte_excel(p: Presentiel | None) -> str:
    if p is None or not p.conseille:
        return ""
    return f"Oui : {p.raison}" + (f" — {p.adresse}" if p.adresse else "")
