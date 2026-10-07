"""Préparation d'un dépôt assisté : valeurs du profil, fichiers et tâche
transmise au processus qui pilote le navigateur."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from ..agents.base import Contexte
from ..modeles import Dossier

FICHIER_ETAT = "depot_etat.json"
EN_COURS = ("ouverture", "rempli", "connexion", "captcha", "manque")


def disponible() -> bool:
    return importlib.util.find_spec("playwright") is not None


def valeurs(ctx: Contexte, dossier: Dossier) -> dict[str, str]:
    profil = ctx.profil()
    i = profil.identite
    formation = profil.formations[0] if profil.formations else None
    return {
        "prenom": i.prenom, "nom": i.nom, "nom_complet": f"{i.prenom} {i.nom}".strip(),
        "email": i.email, "telephone": i.telephone, "ville": i.ville,
        "ecole": formation.etablissement if formation else "", "diplome": formation.diplome if formation else "",
        "lettre_texte": dossier.documents.texte_lettre,
    }


def lire_etat(ctx: Contexte, dossier_id: str) -> dict | None:
    chemin = ctx.config.dossier_donnees / "dossiers" / dossier_id / FICHIER_ETAT
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def preparer(ctx: Contexte, dossier: Dossier, pieces: list[Path]) -> Path:
    """Écrit la tâche du processus de dépôt et renvoie son chemin."""
    fichiers = ctx.dossier_fichiers(dossier.id)
    cv = next((str(p) for p in pieces if not p.name.lower().startswith("lettre")), "")
    lettre = next((str(p) for p in pieces if p.name.lower().startswith("lettre")), "")
    etat = fichiers / FICHIER_ETAT
    etat.write_text(json.dumps({"etat": "ouverture", "message": "Ouverture du navigateur…", "url": dossier.fiche_entreprise.page_carriere,
                                "remplis": [], "manquants": []}, ensure_ascii=False), encoding="utf-8")
    tache = fichiers / "depot_tache.json"
    tache.write_text(json.dumps({
        "url": dossier.fiche_entreprise.page_carriere,
        "valeurs": valeurs(ctx, dossier),
        "fichiers": {"cv": cv, "lettre_fichier": lettre},
        "questions": dossier.documents.reponses_formulaire,
        "etat": str(etat),
        "profil_navigateur": str(ctx.config.dossier_donnees / "navigateur"),
    }, ensure_ascii=False), encoding="utf-8")
    return tache


def lancer(tache: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "recherche_stage.depot", str(tache)])
