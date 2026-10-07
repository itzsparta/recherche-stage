"""Préparation d'un dépôt assisté : valeurs du profil, fichiers et tâche
transmise au processus qui pilote le navigateur."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..agents.base import Contexte
from ..modeles import Dossier

FICHIER_ETAT = "depot_etat.json"
FICHIER_JOURNAL = "depot_journal.txt"
EN_COURS = ("ouverture", "rempli", "connexion", "captcha", "manque")


def _nom(navigateur: str) -> str:
    """« brave » pour « brave » comme pour un chemin Windows « …\\brave.exe »."""
    return re.sub(r"\.exe$", "", re.split(r"[\\/]", navigateur)[-1], flags=re.IGNORECASE)


def chemins_brave() -> list[str]:
    """Emplacements habituels de Brave (Windows, macOS, Linux)."""
    chemins = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            chemins.append(os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
    chemins.append("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
    chemins += [c for c in (shutil.which("brave-browser"), shutil.which("brave")) if c]
    return chemins


def brave() -> str | None:
    return next((c for c in chemins_brave() if os.path.isfile(c)), None)


def choisi(navigateur: str | None) -> str:
    """Navigateur réellement utilisé : celui des Réglages, sinon Brave s'il est
    installé, sinon « » (Edge, puis Chrome)."""
    navigateur = (navigateur or "").strip()
    if navigateur:
        return navigateur
    return "brave" if brave() else ""


def nom_profil(navigateur: str | None) -> str:
    nom = _nom(navigateur or "edge").lower()
    return re.sub(r"[^a-z0-9]+", "-", nom).strip("-") or "edge"


def libelle_navigateur(navigateur: str | None) -> str:
    return {"brave": "Brave", "edge": "Edge", "msedge": "Edge", "chrome": "Chrome"}.get(
        (navigateur or "").strip().lower(), "Edge, sinon Chrome" if not navigateur else _nom(navigateur))


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
    navigateur = choisi(ctx.config.depot.navigateur)
    tache.write_text(json.dumps({
        "url": dossier.fiche_entreprise.page_carriere,
        "valeurs": valeurs(ctx, dossier),
        "fichiers": {"cv": cv, "lettre_fichier": lettre},
        "questions": dossier.documents.reponses_formulaire,
        "etat": str(etat),
        # Un profil par navigateur (deux navigateurs ne partagent pas un même profil) ; les sessions y restent.
        "profil_navigateur": str(ctx.config.dossier_donnees / "navigateur" / nom_profil(navigateur)),
        "navigateur": navigateur,
        # Guide IA : réglages du modèle (jamais de clé : elles restent dans les variables d'environnement).
        "ia": {"llm": ctx.config.llm.model_dump(mode="json"), "base": str(ctx.config.chemin_base),
               "budget": ctx.config.orchestrateur.appels_api_max_par_semaine,
               "max_appels": ctx.config.depot.guide_ia_max_appels},
    }, ensure_ascii=False), encoding="utf-8")
    return tache


def ecrire_etat(ctx: Contexte, dossier_id: str, etat: str, message: str) -> None:
    chemin = ctx.dossier_fichiers(dossier_id) / FICHIER_ETAT
    chemin.write_text(json.dumps({"etat": etat, "message": message, "url": "", "remplis": [], "manquants": []},
                                 ensure_ascii=False), encoding="utf-8")


def derniere_ligne_journal(ctx: Contexte, dossier_id: str) -> str:
    """Dernière ligne utile de la sortie du processus du navigateur (erreur au démarrage)."""
    chemin = ctx.config.dossier_donnees / "dossiers" / dossier_id / FICHIER_JOURNAL
    try:
        lignes = [ligne.strip() for ligne in chemin.read_text(encoding="utf-8", errors="replace").splitlines() if ligne.strip()]
    except OSError:
        return ""
    return lignes[-1][:300] if lignes else ""


def lancer(tache: Path) -> subprocess.Popen:
    # Sortie du processus gardée dans le dossier : en cas d'échec, l'interface en montre la dernière ligne.
    journal = open(tache.parent / FICHIER_JOURNAL, "w", encoding="utf-8")  # noqa: SIM115 (fermé avec le processus)
    # Textes de liens avec accents ou symboles, sous Windows aussi. Clé d'API Claude transmise seulement si le
    # guide IA passe par l'API (`fournisseur: claude`) ; avec Claude Code, c'est l'abonnement qui sert.
    try:
        fournisseur = json.loads(tache.read_text(encoding="utf-8"))["ia"]["llm"]["fournisseur"]
    except (OSError, ValueError, KeyError, TypeError):
        fournisseur = ""
    ecartees = () if fournisseur == "claude" else ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    env = {k: v for k, v in os.environ.items() if k not in ecartees}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([sys.executable, "-m", "recherche_stage.depot", str(tache)], stdout=journal, stderr=subprocess.STDOUT,
                            env=env)
