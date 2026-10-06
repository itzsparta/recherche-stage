"""Vérifications avant un cycle : ce qui ferait échouer le cycle au bout de
quelques minutes (session Claude, classeur ouvert, boîte mail injoignable).

Rien n'est envoyé nulle part : les clés ne sont jamais lues, seulement la
présence de la variable d'environnement ; le réseau n'est sondé que par une
ouverture de connexion TCP vers les serveurs de mail configurés.
"""

from __future__ import annotations

import os
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from .agents.base import Contexte
from .llm import ErreurLLM, NonConnecte, QuotaEpuise

DELAI_RESEAU = 4  # secondes


def _point(cle: str, ok: bool | None, titre: str, detail: str = "") -> dict:
    """ok : True (bon), False (bloquant pour cette partie), None (à surveiller)."""
    return {"cle": cle, "ok": ok, "titre": titre, "detail": detail}


def joignable(hote: str, port: int, delai: float = DELAI_RESEAU) -> bool:
    try:
        with socket.create_connection((hote, port), timeout=delai):
            return True
    except OSError:
        return False


def classeur_ouvert(chemin: Path) -> bool:
    """Excel pose un fichier « ~$nom.xlsx » à côté du classeur ouvert, et le
    verrouille en écriture sous Windows."""
    proprietaires = {chemin.with_name("~$" + chemin.name), chemin.with_name("~$" + chemin.name[2:])}
    if any(p.exists() for p in proprietaires):
        return True
    try:
        with open(chemin, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _llm(ctx: Contexte) -> dict:
    params = ctx.config.llm
    fournisseurs = {params.fournisseur, params.fournisseur_critique or params.fournisseur}
    manques = []
    for f in sorted(fournisseurs):
        if f == "claude_code":
            if not shutil.which(params.claude_code_commande):
                manques.append(f"commande « {params.claude_code_commande} » introuvable : installer Claude Code")
        elif f == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            manques.append("variable ANTHROPIC_API_KEY absente")
        elif f == "gemini" and not os.environ.get(params.gemini_cle_env):
            manques.append(f"variable {params.gemini_cle_env} absente")
    nom = " + ".join(sorted(fournisseurs))
    if manques:
        return _point("llm", False, f"LLM ({nom})", " ; ".join(manques))
    deconnexion = [e for e in ctx.store.escalades_ouvertes() if e["type"] == "quota" and "non connecté" in e["question"]]
    if deconnexion:
        return _point("llm", False, f"LLM ({nom})", "session Claude expirée au dernier cycle : lancer `claude` dans un terminal, taper /login")
    detail = "« Tester » fait un tout petit appel pour vérifier la session" if "claude_code" in fournisseurs else ""
    return _point("llm", True, f"LLM ({nom})", detail)


def verifier(ctx: Contexte) -> list[dict]:
    cfg = ctx.config
    points = []
    points.append(_point("profil", True, "Profil (CV)") if ctx.store.parametre("profil") is not None
                  else _point("profil", False, "Profil (CV)", "aucun profil : lancer `rs profil --cv …`"))
    points.append(_llm(ctx))

    if cfg.suivi.excel:
        chemin = Path(cfg.suivi.excel)
        if not chemin.exists():
            points.append(_point("excel", False, "Tableau Excel", f"{chemin} introuvable"))
        elif classeur_ouvert(chemin):
            points.append(_point("excel", None, "Tableau Excel", f"{chemin.name} est ouvert : le fermer pour qu'il soit mis à jour"))
        else:
            points.append(_point("excel", True, "Tableau Excel", chemin.name))

    serveurs = {}
    if cfg.suivi.imap.hote:
        serveurs["imap"] = ("Lecture des mails", cfg.suivi.imap.hote, cfg.suivi.imap.port)
    if cfg.envoi.smtp.hote:
        serveurs["smtp"] = ("Envoi des mails", cfg.envoi.smtp.hote, cfg.envoi.smtp.port)
    if serveurs:
        with ThreadPoolExecutor(len(serveurs)) as pool:
            etats = dict(zip(serveurs, pool.map(lambda s: joignable(s[1], s[2]), serveurs.values())))
        for cle, (titre, hote, port) in serveurs.items():
            if etats[cle]:
                points.append(_point(cle, True, titre, f"{hote}:{port}"))
            else:
                points.append(_point(cle, False, titre, f"{hote}:{port} injoignable depuis ce réseau (port {port} bloqué ?)"))
    return points


class _Pong(BaseModel):
    ok: bool


def tester_llm(ctx: Contexte) -> tuple[bool, str]:
    """Un appel minuscule, sur demande seulement : il compte dans le budget hebdomadaire."""
    try:
        ctx.llm.structurer("verification", "Réponds uniquement par le JSON demandé, avec ok = true.", "Test de connexion.", _Pong)
    except NonConnecte:
        return False, "Session Claude expirée : dans un terminal, lancer `claude`, taper /login, puis /exit."
    except QuotaEpuise as e:
        return False, f"Limite d'usage atteinte : {e}"
    except (ErreurLLM, RuntimeError, OSError) as e:  # clé absente, commande introuvable…
        return False, f"Le LLM ne répond pas : {e}"
    return True, "Le LLM répond : la session est valide."
