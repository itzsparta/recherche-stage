"""`python -m recherche_stage.depot <tache.json>` : processus à part, lancé par
l'interface, qui pilote le navigateur le temps du dépôt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .remplisseur import Etat, Remplisseur, deposer

CANAUX = ("msedge", "chrome", None)  # Edge est déjà installé sous Windows : rien à télécharger


def ouvrir_navigateur(playwright, profil: str):
    erreur: Exception | None = None
    for bac_a_sable in (True, False):  # le bac à sable évite l'avertissement --no-sandbox ; repli si refusé
        for canal in CANAUX:
            try:
                return playwright.chromium.launch_persistent_context(
                    profil, channel=canal, headless=False, chromium_sandbox=bac_a_sable, accept_downloads=False,
                    no_viewport=True, args=["--start-maximized"])
            except Exception as e:  # navigateur absent : on essaie le suivant
                erreur = e
    raise RuntimeError(f"Aucun navigateur utilisable (Edge, Chrome ou Chromium de Playwright) : {erreur}")


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
            contexte = ouvrir_navigateur(p, tache["profil_navigateur"])
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
