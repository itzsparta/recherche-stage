"""Sources d'offres. Uniquement des accès autorisés : API publiques ou
officielles, flux RSS, alertes mail reçues par l'utilisateur et fichiers
qu'il importe. Aucun scraping de plateforme dont les CGU l'interdisent
(LinkedIn, Indeed, Welcome to the Jungle, JobTeaser) : leurs offres entrent
par leurs alertes mail ou par import de fichier."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Protocol

from ..modeles import GrilleCriteres, Offre


class Source(Protocol):
    nom: str

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]: ...


MOTS_STAGE = re.compile(r"\b(stage|stagiaire|intern|internship|trainee)\b", re.IGNORECASE)


def est_stage(intitule: str, description: str = "") -> bool:
    return bool(MOTS_STAGE.search(intitule) or MOTS_STAGE.search(description[:300]))


def texte_brut(html: str) -> str:
    sans_balises = re.sub(r"<[^>]+>", " ", html)
    for entite, caractere in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        sans_balises = sans_balises.replace(entite, caractere)
    return " ".join(sans_balises.split())


def lire_date(valeur) -> date | None:
    if valeur in (None, ""):
        return None
    if isinstance(valeur, (int, float)):  # millisecondes epoch
        return datetime.fromtimestamp(valeur / 1000).date()
    texte = str(valeur).strip()
    for format_ in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texte[:10], format_).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(texte.replace("Z", "+00:00")).date()
    except ValueError:
        return None
