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
from urllib.parse import quote

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
    cle = _cle(texte)
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


def _cle(texte: str) -> str:
    return f"geo:{' '.join(texte.split()).lower()}"


def point_connu(store, texte: str) -> Point | None:
    """Coordonnées déjà en cache, sans appel réseau (pour l'affichage)."""
    connu = store.parametre(_cle(texte)) if texte and texte.strip() else None
    return tuple(connu) if connu else None


def point_dossier(store, dossier: Dossier) -> Point | None:
    o = dossier.offre
    if o.latitude is not None and o.longitude is not None:
        return (o.latitude, o.longitude)
    return point_connu(store, o.adresse or o.lieu)


def ordonner(depart: Point | None, arrets: list[tuple[object, Point | None]]) -> list:
    """Tournée au plus proche voisin depuis le domicile, raccourcie ensuite en
    décroisant le trajet (2-opt) ; les arrêts sans coordonnées viennent à la
    fin, dans leur ordre d'origine."""
    situes = [(a, p) for a, p in arrets if p is not None]
    ordre, ici = [], depart
    while situes:
        i = min(range(len(situes)), key=lambda k: distance_km(ici, situes[k][1])) if ici else 0
        arret, ici = situes.pop(i)
        ordre.append((arret, ici))
    return [a for a, _ in _decroiser(depart, ordre)] + [a for a, p in arrets if p is None]


def _decroiser(depart: Point | None, ordre: list[tuple[object, Point]]) -> list[tuple[object, Point]]:
    """2-opt sur un trajet ouvert qui part du domicile : retourne un tronçon
    tant que cela raccourcit le total (le plus proche voisin laisse souvent un
    long retour en arrière)."""
    def d(a: Point | None, b: Point | None) -> float:
        return distance_km(a, b) if a is not None and b is not None else 0.0

    n, ameliore = len(ordre), True
    while ameliore:
        ameliore = False
        for i in range(n - 1):
            avant = depart if i == 0 else ordre[i - 1][1]
            for k in range(i + 1, n):
                apres = ordre[k + 1][1] if k + 1 < n else None
                gain = d(avant, ordre[i][1]) + d(ordre[k][1], apres) - d(avant, ordre[k][1]) - d(ordre[i][1], apres)
                if gain > 1e-9:
                    ordre[i:k + 1] = reversed(ordre[i:k + 1])
                    ameliore = True
    return ordre


ARRETS_PAR_ITINERAIRE = 9  # Google Maps : 10 points au plus, départ compris


def itineraires(depart: str, adresses: list[str]) -> list[str]:
    """Liens d'itinéraire (départ du domicile), découpés en tronçons ;
    chaque tronçon repart du dernier arrêt du précédent."""
    liens, origine = [], depart
    for i in range(0, len(adresses), ARRETS_PAR_ITINERAIRE):
        troncon = adresses[i:i + ARRETS_PAR_ITINERAIRE]
        points = ([origine] if origine else []) + troncon
        liens.append("https://www.google.com/maps/dir/" + "/".join(quote(a, safe="") for a in points) + "/")
        origine = troncon[-1]
    return liens


def effectif_min(dossier: Dossier) -> int | None:
    tranche = dossier.offre.tranche_effectif
    if not tranche:
        trouve = re.search(r"Tranche d'effectif INSEE : (\w+)", dossier.offre.description)
        tranche = trouve.group(1) if trouve else None
    return TRANCHES.get(tranche) if tranche else None


def evaluer(dossier: Dossier, domicile: Point | None, entreprise: Point | None, params: ParamsPresentiel) -> Presentiel:
    o = dossier.offre
    adresse = o.adresse or o.lieu
    if o.source == "demarche":  # « se présenter sur place » : demandé par l'entreprise, quelle que soit sa taille
        km = round(distance_km(domicile, entreprise), 1) if domicile and entreprise else None
        return Presentiel(conseille=True, distance_km=km, adresse=adresse,
                          raison="se présenter sur place, d'après ta démarche" + (f", à {km:.0f} km" if km is not None else ""))
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
    if p.depose_le:
        return f"Fait : déposée le {p.depose_le:%d/%m/%Y}" + (f" — {p.adresse}" if p.adresse else "")
    return f"Oui : {p.raison}" + (f" — {p.adresse}" if p.adresse else "")
