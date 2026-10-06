"""`rs revue` : la file d'attente en une seule commande interactive.

Chaque élément est affiché en entier (mail, lettre, motif) et se règle d'une
touche : valider, corriger puis valider, rejeter, marquer comme déposé…
La validation humaine avant envoi reste entière : rien ne part sans un « v ».
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from .agents.base import Contexte
from .modeles import Dossier
from .orchestrateur import Orchestrateur


def decrire(ctx: Contexte, dossier: Dossier) -> str:
    o, d = dossier.offre, dossier.documents
    lignes = [
        f"{o.intitule} — {o.entreprise} ({o.lieu}) [{dossier.statut.value}{' / ' + dossier.etape_attente if dossier.etape_attente else ''}]",
        f"Source : {o.source}  URL : {o.url}",
        f"Score {dossier.score.valeur} (confiance {dossier.score.confiance}) : {dossier.score.justification}",
    ]
    if dossier.critique.iteration:
        lignes.append(f"Critique : {dossier.critique.decision} — note {dossier.critique.note} (itération {dossier.critique.iteration})")
        lignes += [f"  - [{p.gravite}/{p.type}] {p.raison} « {p.extrait[:80]} »" for p in dossier.critique.problemes]
    if d.version:
        lignes.append(f"\nObjet : {d.objet_mail}\n\n{d.mail}\n")
        if d.texte_lettre:
            lignes.append(f"--- Lettre ({d.lettre}) ---\n{d.texte_lettre}\n")
        lignes.append(f"Fichiers : {ctx.dossier_fichiers(dossier.id)}")
    if dossier.motif:
        lignes.append(f"Motif : {dossier.motif}")
    return "\n".join(lignes)


def editer(fichiers: list[Path]) -> None:
    """Ouvre les fichiers dans l'éditeur et attend sa fermeture."""
    editeur = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else shutil.which("nano") or "vi")
    for f in fichiers:
        subprocess.run([editeur, str(f)], check=False)


def ouvrir(cible: str) -> None:
    if sys.platform == "win32" and not cible.startswith("http"):
        os.startfile(cible)  # dossier dans l'Explorateur
    else:
        webbrowser.open(cible)


VALIDABLES = ("scoring", "critique", "envoi")


class Revue:
    def __init__(
        self,
        ctx: Contexte,
        orchestrateur: Orchestrateur,
        demander: Callable[[str], str] = input,
        afficher: Callable[[str], None] = print,
        editer: Callable[[list[Path]], None] = editer,
        ouvrir: Callable[[str], None] = ouvrir,
    ):
        self.ctx, self.orch = ctx, orchestrateur
        self.demander, self.afficher, self.editer, self.ouvrir = demander, afficher, editer, ouvrir
        self.bilan: Counter = Counter()

    def _choix(self, invite: str, touches: str) -> str:
        while True:
            reponse = self.demander(invite).strip().lower()[:1]
            if reponse in touches:
                return reponse
            self.afficher(f"Taper une de ces lettres : {', '.join(touches)}")

    def executer(self) -> Counter:
        ouvertes = self.ctx.store.escalades_ouvertes()
        if not ouvertes:
            self.afficher("Rien en attente.")
        traites: set[str] = set()
        for n, e in enumerate(ouvertes, 1):
            if e["dossier_id"] in traites:
                continue  # dossier déjà réglé plus haut (plusieurs alertes)
            self.afficher(f"\n{'=' * 70}\n[{n}/{len(ouvertes)}] {e['type']}\n{e['question']}\n")
            try:
                if e["dossier_id"] is None:
                    suite = self._globale(e)
                else:
                    traites.add(e["dossier_id"])
                    suite = self._dossier(self.ctx.store.get(e["dossier_id"]), e)
            except ValueError as erreur:  # correction refusée (placeholder, fait hors fiche…)
                self.afficher(f"Refusé : {erreur}")
                self.bilan["erreurs"] += 1
                suite = True
            if not suite:
                break
        self.afficher("\nBilan : " + (", ".join(f"{k} {v}" for k, v in sorted(self.bilan.items())) or "rien de changé"))
        return self.bilan

    # ------------------------------------------------------------- éléments

    def _globale(self, e: dict) -> bool:
        r = self._choix("[f]ermer (traité)  [s]auter  [q]uitter : ", "fsq")
        if r == "f":
            self.ctx.store.fermer_escalade(e["id"], "revue")
            self.bilan["fermees"] += 1
        return r != "q"

    def _dossier(self, dossier: Dossier, e: dict) -> bool:
        if dossier.etape_attente == "depot":
            return self._depot(dossier)
        self.afficher(decrire(self.ctx, dossier))
        if dossier.etape_attente not in VALIDABLES and not dossier.etape_attente.startswith("relance:"):
            r = self._choix("[f]ermer (traité)  [r]ejeter le dossier  [s]auter  [q]uitter : ", "frsq")
            if r == "f":
                self.ctx.store.fermer_escalade(e["id"], "revue")
                self.bilan["fermees"] += 1
            elif r == "r":
                self._rejeter(dossier)
            return r != "q"

        reecrivable = bool(dossier.documents.version) and dossier.etape_attente in ("critique", "envoi")
        r = self._choix(
            "[v]alider  [m]odifier puis valider  " + ("[e] réécrire avec une consigne  " if reecrivable else "")
            + "[r]ejeter  [s]auter  [q]uitter : ", "vmrsq" + ("e" if reecrivable else ""))
        if r == "e":
            consigne = self.demander("Que changer ? (ex. « moins sur le TIPE, plus sur mon parcours ») : ").strip()
            if consigne:
                self.orch.reecrire(dossier.id, consigne)
                self.orch.progres = self.afficher
                self.orch.rediger(dossier.id)
                self.bilan["reecrites"] += 1
                nouveau = self.ctx.store.get(dossier.id)
                if nouveau.etape_attente in VALIDABLES:
                    self.afficher("\nNouvelle version :")
                    return self._dossier(nouveau, e)
            return True
        if r in "vm":
            if r == "m":
                d = dossier.documents
                fichiers = [self.ctx.dossier_fichiers(dossier.id) / f"mail_v{d.version}.txt"]
                if d.lettre and Path(d.lettre).exists():
                    fichiers.append(Path(d.lettre))
                self.afficher("Corriger, enregistrer et fermer l'éditeur.")
                self.editer(fichiers)
            self.orch.valider(dossier.id, modifie=r == "m")
            self.bilan["validees"] += 1
            dossier = self.ctx.store.get(dossier.id)
            if dossier.etape_attente == "depot":
                return self._depot(dossier)  # pas d'adresse : dépôt à la main, tout de suite
            if dossier.envoi.canal == "email":
                self.afficher(f"Validé : part par mail à {dossier.envoi.destinataire} au prochain rs cycle (créneau d'envoi).")
        elif r == "r":
            self._rejeter(dossier)
        return r != "q"

    def _depot(self, dossier: Dossier) -> bool:
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        recherche = f"https://duckduckgo.com/?q={quote_plus(dossier.offre.entreprise + ' recrutement stage candidature')}"
        self.afficher(
            f"Dépôt à la main chez {dossier.offre.entreprise} : CV et lettre PDF dans {fichiers}\n"
            f"Trouver la page carrières ou le formulaire de contact : {recherche}"
        )
        while True:
            r = self._choix("[o]uvrir dossier et recherche  [d]éposé  [r]ejeter  [s]auter (plus tard)  [q]uitter : ", "odrsq")
            if r == "o":
                self.ouvrir(str(fichiers))
                self.ouvrir(recherche)
                continue
            if r == "d":
                preuve = self.demander("Où et quand (ex. « site carrières, 06/10 ») : ").strip() or "déposé (revue)"
                self.orch.marquer_envoye(dossier.id, preuve)
                self.bilan["deposees"] += 1
            elif r == "r":
                self._rejeter(dossier)
            return r != "q"

    def _rejeter(self, dossier: Dossier) -> None:
        motif = self.demander("Motif (facultatif) : ").strip()
        self.orch.rejeter(dossier.id, motif)
        self.bilan["rejetees"] += 1
