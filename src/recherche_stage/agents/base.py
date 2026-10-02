"""Contexte partagé et traçabilité commune à tous les agents."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..llm import LLM
from ..modeles import Dossier, GrilleCriteres, ProfilStructure
from ..reseau import Http, HttpStandard
from ..store import Store


@dataclass
class Contexte:
    store: Store
    config: Config
    llm: LLM
    http: Http = field(default_factory=HttpStandard)

    @property
    def horloge(self) -> Callable[[], datetime]:
        return self.store.horloge

    def dossier_fichiers(self, dossier_id: str) -> Path:
        chemin = self.config.dossier_donnees / "dossiers" / dossier_id
        chemin.mkdir(parents=True, exist_ok=True)
        return chemin

    def profil(self) -> ProfilStructure:
        brut = self.store.parametre("profil")
        if brut is None:
            raise RuntimeError("Aucun profil : lancez d'abord `rs profil`")
        return ProfilStructure.model_validate(brut)

    def grille(self) -> GrilleCriteres:
        brut = self.store.parametre("grille")
        if brut is None:
            raise RuntimeError("Aucune grille de critères : lancez d'abord `rs profil`")
        return GrilleCriteres.model_validate(brut)


class Agent:
    nom = "agent"

    def __init__(self, ctx: Contexte):
        self.ctx = ctx

    def tracer(self, dossier: Dossier, action: str, entrees: Any, details: dict | None = None) -> str:
        """Ajoute l'action à l'historique du dossier et au journal central, avec
        le hash des entrées (mémorisées pour rejouer la décision)."""
        h = self.ctx.store.memoriser_entrees(entrees)
        dossier.tracer(self.nom, action, h)
        self.ctx.store.journaliser(self.nom, action, dossier.id, h, details)
        return h

    def escalader(self, dossier: Dossier | None, type_: str, question: str) -> None:
        self.ctx.store.escalader(dossier.id if dossier else None, type_, question)
        self.ctx.store.journaliser(self.nom, f"escalade:{type_}", dossier.id if dossier else None, details={"question": question})
