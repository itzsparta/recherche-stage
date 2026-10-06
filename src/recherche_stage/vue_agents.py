"""Données de la vue « Agents » de l'interface : ce que fait chaque agent, l'état
des sources de la Veille, les propositions de l'Apprentissage en attente et le
bilan des résultats.

Tout est lu dans la base locale ; rien n'appelle le LLM.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta

from .agents.apprentissage import parts_thompson
from .modeles import Statut
from .orchestrateur import Orchestrateur
from .resilience import Disjoncteur
from .store import debut_semaine

# (nom dans le journal, libellé, rôle) — dans l'ordre du pipeline.
AGENTS = [
    ("profil", "Profil", "Lit ton CV et en tire tes critères (rs profil)"),
    ("veille", "Veille", "Collecte les offres et les entreprises de l'annuaire"),
    ("scoring", "Scoring", "Note chaque piste selon ton profil"),
    ("enquete", "Enquête", "Cherche des faits sourcés et le bon contact"),
    ("redaction", "Rédaction", "Écrit la lettre et le mail, sans rien inventer"),
    ("critique", "Critique", "Relit et contrôle, sans réécrire"),
    ("envoi", "Envoi", "Une seule fois par entreprise, après ta validation"),
    ("suivi", "Suivi", "Lit les réponses, prépare les relances"),
    ("apprentissage", "Apprentissage", "Tire les leçons des résultats"),
]

COMPOSANTES = {"regles": "critères de ta grille", "lexical": "mots communs avec ton profil", "llm": "avis du LLM"}
VARIANTES = {
    "actualite_entreprise": "ouverture sur l'actualité de l'entreprise",
    "competence_cle": "ouverture sur la compétence clé",
    "experience_terrain": "ouverture sur une expérience concrète",
    "projet_formation": "ouverture sur un projet de formation",
}


def _en_attente(orch: Orchestrateur) -> dict[str, int]:
    """Dossiers qui attendent chaque agent."""
    seuil = orch.ctx.config.orchestrateur.seuil_score_minimum
    compte = Counter(d.statut for d in orch.store.lister())
    return {
        "scoring": compte[Statut.collectee] + compte[Statut.filtree],
        "enquete": sum(1 for d in orch.store.lister([Statut.scoree]) if d.score.valeur >= seuil),
        "redaction": compte[Statut.enquetee],
        "critique": compte[Statut.redigee] + compte[Statut.en_critique],
        "envoi": compte[Statut.validee],
        "suivi": compte[Statut.envoyee] + compte[Statut.relancee],
    }


def agents(orch: Orchestrateur) -> list[dict]:
    store, maintenant = orch.store, orch.ctx.horloge()
    activite = store.activite_agents(maintenant - timedelta(days=7))
    llm = store.appels_llm_par_agent(debut_semaine(maintenant))
    attente = _en_attente(orch)
    erreurs: dict[str, str] = {}
    for ligne in store.dernieres_actions("erreur", 100):
        if ligne["horodatage"] >= (maintenant - timedelta(days=7)).isoformat(timespec="seconds"):
            erreurs.setdefault(ligne["agent"], ligne["details"].get("erreur", ""))
    liste = []
    for nom, libelle, role in AGENTS:
        a = activite.get(nom, {})
        liste.append({
            "nom": nom, "libelle": libelle, "role": role,
            "dernier": a.get("dernier"), "actions": a.get("actions", 0), "erreurs": a.get("erreurs", 0),
            "derniere_erreur": erreurs.get(nom, ""),
            "appels_llm": llm.get(nom, {}).get("appels", 0), "tokens": llm.get(nom, {}).get("tokens", 0),
            "en_attente": attente.get(nom),
        })
    return liste


# ------------------------------------------------------------------ sources


def sources(orch: Orchestrateur) -> list[dict]:
    """Lecture seule (la page interroge pendant qu'un cycle écrit dans la base)."""
    store, cfg = orch.store, orch.ctx.config.veille
    duree = Disjoncteur(store, cfg.echecs_avant_coupure).duree
    lignes = {ligne["nom"]: dict(ligne) for ligne in store.cx.execute("SELECT * FROM sources")}
    allocation = store.parametre("allocation_sources", {}) or {}
    erreurs: dict[str, str] = {}
    for ligne in store.dernieres_actions("echec_source"):
        erreurs.setdefault(ligne["details"].get("source", ""), ligne["details"].get("erreur", ""))
    liste = []
    for source in orch.veille.sources:
        etat = lignes.get(source.nom) or {"echecs_consecutifs": 0, "coupee": 0, "coupee_le": None, "derniere_collecte": None, "collectes": 0}
        reprise = None
        if etat["coupee"]:
            fin = datetime.fromisoformat(etat["coupee_le"]) + duree
            code = "coupee" if orch.ctx.horloge() < fin else "a_retenter"
            reprise = fin.isoformat(timespec="minutes")
        elif etat["echecs_consecutifs"]:
            code = "fragile"
        elif not etat["derniere_collecte"]:
            code = "jamais"
        else:
            code = "ok"
        liste.append({
            "nom": source.nom, "etat": code, "echecs": etat["echecs_consecutifs"], "collectes": etat["collectes"],
            "derniere_collecte": etat["derniere_collecte"], "reprise": reprise,
            "erreur": erreurs.get(source.nom, "") if etat["echecs_consecutifs"] or etat["coupee"] else "",
            "part": allocation.get(source.nom),
        })
    return liste


def reactiver_source(orch: Orchestrateur, nom: str) -> None:
    if nom not in {s.nom for s in orch.veille.sources}:
        raise KeyError(nom)
    Disjoncteur(orch.store, orch.ctx.config.veille.echecs_avant_coupure).succes(nom)
    orch.store.fermer_escalades_globales((f"source_coupee:{nom}",), "réactivée depuis l'interface")
    orch.store.journaliser("humain", "source_reactivee", details={"source": nom})


# ------------------------------------------------------------- propositions


def _decrire(p: dict) -> tuple[str, list[str]]:
    c = p["contenu"]
    if p["mecanisme"] == "recalibrage_scoring":
        details = [f"« {nom} » compte {'plus' if f > 1 else 'moins'} dans la note (×{f})" for nom, f in c.get("ajustements_poids", {}).items()]
        details += [f"Part « {COMPOSANTES.get(k, k)} » de la note : ×{f}" for k, f in c.get("facteurs_composantes", {}).items()]
        if c.get("taux_par_secteur"):
            details.append("Entretiens par secteur : " + ", ".join(f"{s} {t}" for s, t in c["taux_par_secteur"].items()))
        return "Scoring : ajuster la note d'après les entretiens obtenus", details
    if p["mecanisme"] == "calibration_critique":
        stats = c.get("stats", {})
        sens = "plus exigeant" if c["seuil_validation_critique"] > c["ancien"] else "moins exigeant"
        return (f"Critique : seuil de validation {c['ancien']} → {c['seuil_validation_critique']} ({sens})", [
            f"Lettres validées par le Critique que tu as corrigées ou rejetées : {stats.get('faux_negatifs', '—')}",
            f"Lettres bloquées par le Critique que tu as validées telles quelles : {stats.get('faux_positifs', '—')}",
        ])
    if p["mecanisme"] == "nouvelles_regles":
        return "Critique : nouveau contrôle automatique", [c["raison"], "Le Critique signalera cette formulation dans les prochaines lettres."]
    if p["mecanisme"] == "allocation_sources":
        parts = sorted(c["allocation_sources"].items(), key=lambda x: -x[1])
        return "Veille : nouvelle répartition de la collecte", [", ".join(f"{s} {v:.0%}" for s, v in parts)]
    return f"{p['cible']} : {p['mecanisme']}", []


def propositions(orch: Orchestrateur) -> list[dict]:
    liste = []
    for p in orch.store.propositions():
        titre, details = _decrire(p)
        liste.append({"id": p["id"], "cree": p["cree"], "cible": p["cible"], "mecanisme": p["mecanisme"], "titre": titre, "details": details})
    return liste


def decider_proposition(orch: Orchestrateur, proposition_id: int, appliquer: bool) -> str:
    p = orch.store.proposition(proposition_id)
    if p is None or p["etat"] != "proposee":
        raise ValueError("Proposition déjà traitée.")
    titre, _ = _decrire(p)
    if appliquer:
        orch.appliquer_propositions(ids={proposition_id})
        return f"Appliqué : {titre}."
    orch.store.marquer_proposition(proposition_id, "refusee")
    orch.store.journaliser("humain", "proposition_refusee", details={"id": proposition_id, "mecanisme": p["mecanisme"]})
    return f"Refusé : {titre}. Elle ne sera pas reproposée telle quelle."


# -------------------------------------------------------------------- bilan


def bilan(orch: Orchestrateur) -> dict:
    app = orch.apprentissage
    donnees = app.donnees_etiquetees()
    resultats = Counter(x["resultat"] for x in donnees)
    bras = app.bras_ab(donnees)
    # Générateur à part : afficher le bilan ne doit pas changer le tirage des variantes.
    p_meilleur = parts_thompson(bras, random.Random(0), exploration=0) if any(sum(v) for v in bras.values()) else {}
    variantes = []
    for nom, (succes, echecs) in sorted(bras.items(), key=lambda x: -p_meilleur.get(x[0], 0)):
        if succes + echecs:
            angle, _, format_ = nom.partition("+")
            libelle = VARIANTES.get(angle, angle) + (" (mail seul)" if format_ else "")
            variantes.append({"nom": libelle, "entretiens": succes, "total": succes + echecs, "p_meilleur": p_meilleur.get(nom, 0)})
    return {
        "dernier": orch.store.parametre("dernier_bilan"),
        "abouties": len(donnees),
        "entretiens": resultats["entretien"], "refus": resultats["refus"], "silences": resultats["silence"],
        "par_source": [{"nom": k, "taux": v} for k, v in app._taux_par(donnees, "source").items()],
        "motifs_rejet": [{"texte": m, "n": n} for m, n in app.motifs_rejet()],
        "corrections": [{"texte": c, "n": n} for c, n in app.corrections_frequentes()],
        "variantes": variantes,
        "delais": [{"entreprise": e, "jours": d} for e, d in sorted(app.memoire_entreprises().items(), key=lambda x: x[1])][:10],
    }


def refaire_bilan(orch: Orchestrateur) -> str:
    orch.apprentissage.executer()
    orch.appliquer_propositions(auto_seulement=True)
    orch.store.definir_parametre("dernier_bilan", orch.ctx.horloge().isoformat(timespec="seconds"))
    n = len(orch.store.propositions())
    return "Bilan refait." + (f" {n} proposition(s) attendent ta décision." if n else " Aucune proposition en attente.")


def resume(orch: Orchestrateur) -> dict:
    """Pastille de l'onglet : ce qui demande ton attention."""
    coupees = sum(1 for s in sources(orch) if s["etat"] in ("coupee", "a_retenter"))
    return {"propositions": len(orch.store.propositions()), "sources_coupees": coupees}


def tout(orch: Orchestrateur) -> dict:
    return {"agents": agents(orch), "sources": sources(orch), "propositions": propositions(orch), "bilan": bilan(orch)}
