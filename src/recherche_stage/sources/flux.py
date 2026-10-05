"""Flux RSS ou Atom publiés par un site carrière ou un site d'offres.

Un flux est fait pour être lu par un programme : c'est un accès prévu. Pour
le flux d'une entreprise, l'entreprise est celle de la configuration ; pour
un agrégateur (`entreprise_fixe: false`), on la lit dans l'article (champ
auteur, ou titre « Poste - Entreprise »).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..config import FluxRSS
from ..modeles import GrilleCriteres, Offre
from ..reseau import Http
from . import est_stage, lire_date, texte_brut

ATOM = "{http://www.w3.org/2005/Atom}"


def _date(valeur: str | None):
    if not valeur:
        return None
    try:
        return parsedate_to_datetime(valeur).date()
    except (TypeError, ValueError):
        return lire_date(valeur)


def _articles(xml: str) -> list[dict]:
    racine = ET.fromstring(xml)
    articles = []
    for item in racine.iter("item"):  # RSS 2.0
        articles.append(
            {
                "titre": item.findtext("title", ""),
                "lien": item.findtext("link", ""),
                "description": item.findtext("description", ""),
                "auteur": item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "",
                "date": _date(item.findtext("pubDate")),
            }
        )
    for entree in racine.iter(f"{ATOM}entry"):  # Atom
        lien = entree.find(f"{ATOM}link[@rel='alternate']")
        if lien is None:
            lien = entree.find(f"{ATOM}link")
        articles.append(
            {
                "titre": entree.findtext(f"{ATOM}title", ""),
                "lien": lien.get("href", "") if lien is not None else "",
                "description": entree.findtext(f"{ATOM}summary") or entree.findtext(f"{ATOM}content") or "",
                "auteur": entree.findtext(f"{ATOM}author/{ATOM}name", ""),
                "date": lire_date(entree.findtext(f"{ATOM}published") or entree.findtext(f"{ATOM}updated")),
            }
        )
    return articles


class SourceFlux:
    def __init__(self, http: Http, flux: FluxRSS):
        self.http = http
        self.flux = flux
        self.nom = f"flux:{flux.nom}"

    def _entreprise(self, article: dict) -> tuple[str, str] | None:
        titre = texte_brut(article["titre"])
        if self.flux.entreprise_fixe:
            return titre, self.flux.nom
        if article["auteur"]:
            return titre, texte_brut(article["auteur"])
        morceaux = re.split(r"\s+[-–|@]\s+", titre, maxsplit=1)
        if len(morceaux) == 2:
            return morceaux[0], morceaux[1]
        return None  # entreprise introuvable : on n'invente pas

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        offres = []
        for article in _articles(self.http.get_texte(self.flux.url)):
            trouve = self._entreprise(article)
            if trouve is None:
                continue
            intitule, entreprise = trouve
            description = texte_brut(article["description"])
            if not est_stage(intitule, description):
                continue
            offres.append(
                Offre(
                    intitule=intitule,
                    entreprise=entreprise,
                    description=description,
                    url=article["lien"].strip(),
                    source=self.nom,
                    date_publication=article["date"],
                )
            )
        return offres[:limite]
