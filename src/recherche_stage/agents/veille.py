"""Agent 2 — Veille : collecte les offres et les cibles de candidature
spontanée, déduplique, coupe les sources défaillantes."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from ..hachage import id_dossier, normaliser_entreprise
from ..modeles import Dossier, Offre, Statut
from ..resilience import Disjoncteur, reessayer
from ..sources import Source
from .base import Agent, Contexte


class AgentVeille(Agent):
    nom = "veille"

    def __init__(self, ctx: Contexte, sources: list[Source], dormir: Callable[[float], None] = time.sleep):
        super().__init__(ctx)
        self.sources = sources
        self.dormir = dormir
        self.disjoncteur = Disjoncteur(ctx.store, ctx.config.veille.echecs_avant_coupure)

    def _quotas(self, actives: list[Source]) -> list[tuple[Source, int]]:
        """Répartit le volume de collecte selon l'allocation apprise (bandit),
        à parts égales tant qu'elle n'existe pas."""
        total = self.ctx.config.veille.offres_max_par_cycle
        allocation: dict = self.ctx.store.parametre("allocation_sources", {}) or {}
        parts = {s.nom: float(allocation.get(s.nom, 0)) for s in actives}
        if not any(parts.values()):
            parts = {s.nom: 1.0 for s in actives}
        somme = sum(parts.values())
        ordonnees = sorted(actives, key=lambda s: parts[s.nom], reverse=True)
        return [(s, max(5, round(total * parts[s.nom] / somme))) for s in ordonnees]

    def _due(self, nom: str) -> bool:
        derniere = self.ctx.store.etat_source(nom)["derniere_collecte"]
        if not derniere:
            return True
        intervalle = timedelta(hours=self.ctx.config.veille.intervalle_collecte_heures)
        return self.ctx.horloge() - datetime.fromisoformat(derniere) >= intervalle

    def executer(self, forcer: bool = False) -> list[Dossier]:
        grille = self.ctx.grille()
        actives = [s for s in self.sources if not self.disjoncteur.ouvert(s.nom)]
        for coupee in set(self.sources) - set(actives):
            self.ctx.store.journaliser(self.nom, "source_ignoree_disjoncteur", details={"source": coupee.nom})

        nouveaux: list[Dossier] = []
        reliquat = 0  # quota non consommé par une source en échec, reporté sur la suivante
        for source, quota in self._quotas(actives):
            if not forcer and not getattr(source, "sans_frequence", False) and not self._due(source.nom):
                continue
            limite = quota + reliquat
            try:
                offres = reessayer(
                    lambda s=source, n=limite: s.collecter(grille, n), self.ctx.config.veille.delais_reessai, self.dormir
                )
            except Exception as e:  # noqa: BLE001 - toute panne de source est traitée pareil
                reliquat = limite
                coupee = self.disjoncteur.echec(source.nom)
                self.ctx.store.journaliser(
                    self.nom, "echec_source", details={"source": source.nom, "erreur": str(e), "coupee": coupee}
                )
                if coupee:
                    self.escalader(
                        None,
                        f"source_coupee:{source.nom}",
                        f"Source « {source.nom} » coupée après {self.ctx.config.veille.echecs_avant_coupure} "
                        f"échecs consécutifs (dernier : {e}). Elle sera retentée dans 24 h.",
                    )
                continue
            reliquat = 0
            self.disjoncteur.succes(source.nom)
            etat = self.ctx.store.etat_source(source.nom)
            self.ctx.store.maj_source(
                source.nom,
                derniere_collecte=self.ctx.horloge().isoformat(timespec="seconds"),
                collectes=etat["collectes"] + 1,
            )
            for offre in offres:
                offre.collecteur = source.nom
                dossier = self._integrer(offre, grille.entreprises_prioritaires)
                if dossier:
                    nouveaux.append(dossier)
            self.ctx.store.journaliser(
                self.nom, "collecte", details={"source": source.nom, "offres": len(offres), "limite": limite}
            )
        return nouveaux

    def _integrer(self, offre: Offre, prioritaires: list[str]) -> Dossier | None:
        """Crée le dossier, ou fusionne avec l'existant sur la clé entreprise + intitulé."""
        dossier_id = id_dossier(offre.entreprise, offre.intitule)
        if self.ctx.store.etat_envoi(dossier_id) is not None:
            return None  # déjà candidaté : jamais deux fois, même après purge
        existant = self.ctx.store.get(dossier_id)
        if existant:
            self._fusionner(existant, offre)
            return None
        dossier = Dossier(id=dossier_id, offre=offre, statut=Statut.collectee)
        cles_prioritaires = {normaliser_entreprise(p) for p in prioritaires}
        dossier.prioritaire = normaliser_entreprise(offre.entreprise) in cles_prioritaires
        self.tracer(dossier, "collecte", offre, {"source": offre.source})
        self.ctx.store.save(dossier)
        return dossier

    def _fusionner(self, dossier: Dossier, offre: Offre) -> None:
        o = dossier.offre
        if offre.source in {o.source, *o.sources_fusionnees}:
            return
        o.sources_fusionnees.append(offre.source)
        if dossier.statut in (Statut.collectee, Statut.filtree, Statut.scoree):
            o.contact_email = o.contact_email or offre.contact_email
            o.dates.debut = o.dates.debut or offre.dates.debut
            o.dates.fin = o.dates.fin or offre.dates.fin
            o.gratification = o.gratification or offre.gratification
            o.url = o.url or offre.url
            if len(offre.description) > len(o.description):
                o.description = offre.description
        self.tracer(dossier, "fusion_doublon", offre, {"source": offre.source})
        self.ctx.store.save(dossier)
