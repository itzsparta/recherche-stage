"""`python -m recherche_stage.depot <tache.json>` : processus à part, lancé par
l'interface, qui pilote le navigateur le temps du dépôt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .remplisseur import Etat, Remplisseur, deposer

import os
import shutil

CANAUX = {"edge": "msedge", "msedge": "msedge", "chrome": "chrome"}


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


def candidats(choix: str) -> list[dict]:
    """Façons de lancer le navigateur choisi, dans l'ordre d'essai."""
    choix = (choix or "").strip()
    if not choix:  # Edge est déjà installé sous Windows : rien à télécharger
        return [{"channel": "msedge"}, {"channel": "chrome"}, {}]
    if choix.lower() in CANAUX:
        return [{"channel": CANAUX[choix.lower()]}]
    if choix.lower() == "brave":
        trouves = [c for c in chemins_brave() if os.path.isfile(c)]
        if not trouves:
            raise RuntimeError("Brave introuvable aux emplacements habituels : dans Réglages, mets le chemin complet de brave.exe "
                               "(clic droit sur le raccourci Brave → Propriétés → Cible).")
        return [{"executable_path": trouves[0]}]
    if os.path.isfile(choix):
        return [{"executable_path": choix}]
    raise RuntimeError(f"Navigateur « {choix} » inconnu : brave, edge, chrome, ou le chemin complet de l'exécutable.")


def ouvrir_navigateur(playwright, profil: str, choix: str = ""):
    erreur: Exception | None = None
    for bac_a_sable in (True, False):  # le bac à sable évite l'avertissement --no-sandbox ; repli si refusé
        for options in candidats(choix):
            try:
                return playwright.chromium.launch_persistent_context(
                    profil, headless=False, chromium_sandbox=bac_a_sable, accept_downloads=False,
                    no_viewport=True, args=["--start-maximized"], **options)
            except Exception as e:  # navigateur absent : on essaie le suivant
                erreur = e
    raise RuntimeError(f"Impossible d'ouvrir le navigateur ({choix or 'Edge, Chrome ou Chromium'}) : {erreur}")


def main(chemin_tache: str) -> int:
    tache_fichier = Path(chemin_tache)
    tache = json.loads(tache_fichier.read_text(encoding="utf-8"))
    tache_fichier.unlink(missing_ok=True)  # contient des données personnelles : pas de copie qui traîne
    chemin_etat = Path(tache["etat"])
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        Etat("erreur", "Playwright n'est pas installé : pip install playwright").ecrire(chemin_etat)
        return 1
    remplisseur = Remplisseur(tache["valeurs"], tache["fichiers"], tache.get("questions"))
    try:
        with sync_playwright() as p:
            contexte = ouvrir_navigateur(p, tache["profil_navigateur"], tache.get("navigateur", ""))
            try:
                deposer(contexte, tache["url"], remplisseur, chemin_etat)
            finally:
                try:
                    contexte.close()
                except Exception:
                    pass
    except Exception as e:
        Etat("erreur", f"Le remplissage s'est arrêté : {type(e).__name__} : {str(e)[:300]}", url=tache["url"]).ecrire(chemin_etat)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
