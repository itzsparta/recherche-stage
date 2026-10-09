"""Démarches faites en dehors de l'outil : forum, recommandation, candidature
envoyée à la main, contact rencontré sur un stand. Elles sont écrites dans
`demarches.yaml` (à côté de config.yaml, jamais versionné : il contient des
noms et des adresses de contacts).

Elles servent à trois choses :
- le tableau Excel de suivi en reçoit une ligne chacune ;
- jamais de candidature spontanée automatique en double chez une entreprise où
  une démarche est en cours (une offre de cette entreprise reste proposée,
  marquée prioritaire, avec la démarche rappelée) ;
- à la date de relance indiquée, un rappel apparaît dans la file d'attente.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from .excel import _coeur, _departement

# envoyee : candidature faite ; en_attente : on attend leur retour ; a_faire : une action reste à faire de ton côté ;
# contact : simple échange (stand visité), qui n'empêche rien.
STATUTS = ("envoyee", "en_attente", "a_faire", "contact")


class Demarche(BaseModel):
    entreprise: str
    statut: Literal["envoyee", "en_attente", "a_faire", "contact"] = "contact"
    date: dt.date | None = None  # du contact, ou de l'envoi pour une candidature
    canal: str = ""  # « Forum AgoraJob », « Recommandation d'un salarié »…
    contact: str = ""
    lieu: str = ""  # « Grande-Synthe (59) » : limite la correspondance à ce département
    activite: str = ""
    notes: str = ""
    relance: dt.date | None = None  # date du rappel dans la file d'attente

    @property
    def bloque_spontanee(self) -> bool:
        """Une candidature spontanée automatique ferait doublon avec la démarche."""
        return self.statut != "contact"

    def resume(self) -> str:
        quand = f" le {self.date:%d/%m/%Y}" if self.date else ""
        avec = f", contact : {self.contact}" if self.contact else ""
        return f"{self.canal or 'démarche directe'}{quand}{avec}" + (f" — {self.notes}" if self.notes else "")


class ErreurDemarches(ValueError):
    pass


def charger(chemin: str | Path | None) -> list[Demarche]:
    """Démarches du fichier ; liste vide s'il n'existe pas."""
    if not chemin or not Path(chemin).exists():
        return []
    try:
        contenu = yaml.safe_load(Path(chemin).read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        raise ErreurDemarches(f"{Path(chemin).name} illisible : {e}") from e
    brutes = contenu.get("demarches", []) if isinstance(contenu, dict) else contenu
    demarches = []
    for i, brute in enumerate(brutes or [], 1):
        try:
            demarches.append(Demarche.model_validate(brute))
        except ValidationError as e:
            nom = brute.get("entreprise", f"n° {i}") if isinstance(brute, dict) else f"n° {i}"
            erreur = e.errors()[0]
            raise ErreurDemarches(f"{Path(chemin).name}, démarche {nom} : {'.'.join(map(str, erreur['loc']))} "
                                  f"— {erreur['msg']} (statut : {', '.join(STATUTS)} ; dates : 2026-10-08)") from e
    return demarches


_CACHE: dict[str, tuple[float, list[Demarche]]] = {}


def charger_si_change(chemin: str | Path | None) -> list[Demarche]:
    """Comme `charger`, relu seulement si le fichier a changé ; un fichier
    invalide donne une liste vide (l'erreur est signalée par le cycle)."""
    if not chemin or not Path(chemin).exists():
        return []
    cle, modifie = str(chemin), Path(chemin).stat().st_mtime
    if cle not in _CACHE or _CACHE[cle][0] != modifie:
        try:
            _CACHE[cle] = (modifie, charger(chemin))
        except ErreurDemarches:
            _CACHE[cle] = (modifie, [])
    return _CACHE[cle][1]


def correspondance(demarches: list[Demarche], entreprise: str, lieu: str = "") -> Demarche | None:
    """Démarche qui concerne cette entreprise : tous les mots du nom de la démarche
    se retrouvent dans le sien (« STEF » = « STEF TRANSPORT LILLE »), et même
    département quand les deux sont connus. La plus avancée l'emporte."""
    mots = set(_coeur(entreprise).split())
    departement = _departement(lieu)
    trouvees = []
    for d in demarches:
        cible = set(_coeur(re.sub(r"[/&+]", " ", d.entreprise)).split())
        if not cible or not cible <= mots:
            continue
        if departement and _departement(d.lieu) and departement != _departement(d.lieu):
            continue
        trouvees.append(d)
    return min(trouvees, key=lambda d: STATUTS.index(d.statut), default=None)
