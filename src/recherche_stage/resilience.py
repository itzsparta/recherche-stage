"""Retries avec délai croissant et disjoncteur par source."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar

from .store import Store

R = TypeVar("R")


def reessayer(
    fonction: Callable[[], R],
    delais: list[float],
    dormir: Callable[[float], None] = time.sleep,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> R:
    """Appelle `fonction`, puis la rappelle après chaque délai de `delais`
    tant qu'elle échoue. Lève la dernière erreur si tout échoue."""
    derniere: BaseException | None = None
    for delai in [0.0, *delais]:
        if delai:
            dormir(delai)
        try:
            return fonction()
        except exceptions as e:
            derniere = e
    assert derniere is not None
    raise derniere


class Disjoncteur:
    """Coupe une source après `seuil` échecs consécutifs. Passé `duree`, la
    source est de nouveau tentée une fois (demi-ouverture) : un succès la
    réarme, un échec la recoupe."""

    def __init__(self, store: Store, seuil: int, duree: timedelta = timedelta(hours=24)):
        self.store = store
        self.seuil = seuil
        self.duree = duree

    def ouvert(self, nom: str) -> bool:
        etat = self.store.etat_source(nom)
        if not etat["coupee"]:
            return False
        coupee_le = datetime.fromisoformat(etat["coupee_le"])
        return self.store.horloge() - coupee_le < self.duree

    def succes(self, nom: str) -> None:
        self.store.maj_source(nom, echecs_consecutifs=0, coupee=0, coupee_le=None)

    def echec(self, nom: str) -> bool:
        """Enregistre un échec ; renvoie True si la source vient d'être coupée."""
        etat = self.store.etat_source(nom)
        echecs = etat["echecs_consecutifs"] + 1
        if echecs >= self.seuil:
            self.store.maj_source(
                nom, echecs_consecutifs=echecs, coupee=1, coupee_le=self.store.horloge().isoformat(timespec="seconds")
            )
            return True
        self.store.maj_source(nom, echecs_consecutifs=echecs)
        return False
