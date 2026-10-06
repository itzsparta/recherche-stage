"""Agent Apprentissage : exploite les résultats réels pour ajuster le système.

Chaque mécanisme produit une proposition. L'orchestrateur applique d'office
les réglages d'exploration (allocation des sources, attribution des
variantes) ; les autres (poids, seuils, règles) attendent `rs bilan --appliquer`.
"""

from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from ..hachage import normaliser
from ..modeles import Dossier, Statut
from .base import Agent, Contexte
from .redaction import ANGLES
from .scoring import score_critere

JOURS_SILENCE = 30
ENVOYEES = (Statut.envoyee, Statut.relancee, Statut.entretien, Statut.refus, Statut.acceptee)
FORMATS = ("", "+mail_seul")
PREFIXE_MOTIF = "motif : "  # décision « rejete » : motif donné par l'humain


def resultat(dossier: Dossier, maintenant) -> str | None:
    """entretien, refus, silence, ou None si trop tôt pour conclure."""
    if dossier.statut in (Statut.entretien, Statut.acceptee) or dossier.suivi.reponse == "entretien":
        return "entretien"
    if dossier.statut == Statut.refus:
        return "refus"
    if dossier.envoi.horodatage and maintenant - dossier.envoi.horodatage > timedelta(days=JOURS_SILENCE):
        return "silence"
    return None


def parts_thompson(succes: dict[str, tuple[int, int]], rng: random.Random, tirages: int = 2000, exploration: float = 0.1) -> dict[str, float]:
    """Part de chaque bras = probabilité d'être le meilleur sous a posteriori
    Beta(1 + succès, 1 + échecs), avec un plancher d'exploration."""
    bras = list(succes)
    if not bras:
        return {}
    victoires = Counter()
    for _ in range(tirages):
        tirage = {b: rng.betavariate(1 + s, 1 + e) for b, (s, e) in succes.items()}
        victoires[max(tirage, key=tirage.get)] += 1
    return {b: round(exploration / len(bras) + (1 - exploration) * victoires[b] / tirages, 4) for b in bras}


class AgentApprentissage(Agent):
    nom = "apprentissage"

    def __init__(self, ctx: Contexte, rng: random.Random | None = None):
        super().__init__(ctx)
        self.rng = rng or random.Random()

    # ------------------------------------------------------------- données

    def donnees_etiquetees(self) -> list[dict]:
        maintenant = self.ctx.horloge()
        lignes = []
        for d in self.ctx.store.lister(ENVOYEES):
            r = resultat(d, maintenant)
            if r is None:
                continue
            delai = None
            if d.suivi.date_reponse and d.envoi.horodatage:
                delai = (d.suivi.date_reponse - d.envoi.horodatage).days
            lignes.append({
                "dossier": d, "source": d.offre.source, "collecteur": d.offre.collecteur, "secteur": d.offre.secteur or "inconnu",
                "score": d.score.valeur, "note_critique": d.critique.note, "variante": d.variante or "aucune",
                "format": d.documents.format, "delai": delai, "resultat": r,
            })
        return lignes

    # --------------------------------------------------------- mécanismes

    def allocation_sources(self, donnees: list[dict]) -> dict[str, float]:
        compte: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for ligne in donnees:
            if ligne["collecteur"]:
                compte[ligne["collecteur"]][0 if ligne["resultat"] == "entretien" else 1] += 1
        # Les sources qui n'ont encore rien produit d'abouti restent explorées.
        for etat in self.ctx.store.cx.execute("SELECT nom FROM sources WHERE coupee = 0"):
            compte.setdefault(etat["nom"], [0, 0])
        return parts_thompson({k: (v[0], v[1]) for k, v in compte.items()}, self.rng)

    def bras_ab(self, donnees: list[dict]) -> dict[str, tuple[int, int]]:
        bras = {f"{a}{f}": [0, 0] for a in ANGLES for f in FORMATS}
        for ligne in donnees:
            if ligne["variante"] in bras:
                bras[ligne["variante"]][0 if ligne["resultat"] == "entretien" else 1] += 1
        return {k: (v[0], v[1]) for k, v in bras.items()}

    def choisir_variante(self) -> str:
        """Thompson : tire un taux plausible par bras et prend le meilleur."""
        bras = self.bras_ab(self.donnees_etiquetees())
        tirage = {b: self.rng.betavariate(1 + s, 1 + e) for b, (s, e) in bras.items()}
        return max(tirage, key=tirage.get)

    def recalibrage_scoring(self, donnees: list[dict]) -> dict | None:
        succes = [x for x in donnees if x["resultat"] == "entretien"]
        if len(donnees) < 10 or len(succes) < 3:
            return None
        echecs = [x for x in donnees if x["resultat"] != "entretien"]
        grille = self.ctx.grille()
        ajustements = {}
        for critere in grille.souples:
            moy_s = statistics.mean(score_critere(critere, x["dossier"]) for x in succes)
            moy_e = statistics.mean(score_critere(critere, x["dossier"]) for x in echecs) if echecs else 0
            facteur = max(0.5, min(1.5, 1 + (moy_s - moy_e)))
            if abs(facteur - 1) >= 0.1:
                ajustements[critere.nom] = round(facteur, 2)
        composantes = {}
        for cle in ("regles", "lexical", "llm"):
            s = [x["dossier"].score.details.get(cle) for x in succes if cle in x["dossier"].score.details]
            e = [x["dossier"].score.details.get(cle) for x in echecs if cle in x["dossier"].score.details]
            if len(s) >= 3 and len(e) >= 3:
                composantes[cle] = round(max(0.5, min(1.5, 1 + statistics.mean(s) - statistics.mean(e))), 2)
        if not ajustements and not composantes:
            return None
        return {"ajustements_poids": ajustements, "facteurs_composantes": composantes,
                "taux_par_secteur": self._taux_par(donnees, "secteur")}

    @staticmethod
    def _taux_par(donnees: list[dict], cle: str) -> dict[str, str]:
        groupes: dict[str, list[str]] = defaultdict(list)
        for ligne in donnees:
            groupes[str(ligne[cle])].append(ligne["resultat"])
        return {g: f"{r.count('entretien')}/{len(r)}" for g, r in groupes.items()}

    def calibration_critique(self) -> dict | None:
        journal = self.ctx.store.journal(limite=100000)
        valides_critique = {j["dossier_id"] for j in journal if j["action"] == "critique:valider"}
        escalades_critique = {j["dossier_id"] for j in journal if j["action"] == "critique:escalader"}
        decisions = self.ctx.store.decisions_humaines()
        envoi = {d["dossier_id"]: d["decision"] for d in decisions if d["decision"] in ("valide_tel_quel", "modifie", "rejete")}
        humain_critique = {d["dossier_id"]: d["decision"] for d in decisions if d["decision"].startswith("critique:")}

        jugees = [i for i in valides_critique if i in envoi]
        faux_negatifs = [i for i in jugees if envoi[i] != "valide_tel_quel"]
        contestees = [i for i in escalades_critique if i in humain_critique]
        faux_positifs = [i for i in contestees if humain_critique[i] == "critique:valide_tel_quel"]

        actuel = self.ctx.store.parametre("seuil_validation_critique", self.ctx.config.critique.seuil_validation)
        nouveau = actuel
        if len(jugees) >= 10 and len(faux_negatifs) / len(jugees) > 0.2:
            nouveau = min(90, actuel + 3)
        elif len(contestees) >= 5 and len(faux_positifs) / len(contestees) > 0.5:
            nouveau = max(70, actuel - 3)
        stats = {"faux_negatifs": f"{len(faux_negatifs)}/{len(jugees)}", "faux_positifs": f"{len(faux_positifs)}/{len(contestees)}"}
        if nouveau == actuel:
            return {"stats": stats}
        return {"stats": stats, "seuil_validation_critique": nouveau, "ancien": actuel}

    def nouvelles_regles(self) -> list[dict]:
        """Erreur récurrente (même extrait dans 3 dossiers ou plus) → contrôle déterministe."""
        occurrences: dict[str, set[str]] = defaultdict(set)
        exemples: dict[str, str] = {}
        for d in self.ctx.store.lister():
            for p in d.critique.problemes:
                if p.type in ("generique", "forme") and 4 <= len(p.extrait) <= 60:
                    cle = normaliser(p.extrait)
                    occurrences[cle].add(d.id)
                    exemples[cle] = p.extrait
        for decision in self.ctx.store.decisions_humaines():
            for ligne in (decision["diff"] or "").splitlines():
                if ligne.startswith("- ") and 4 <= len(ligne) <= 62:
                    cle = normaliser(ligne[2:])
                    occurrences[cle].add(decision["dossier_id"])
                    exemples[cle] = ligne[2:].strip()
        existantes = {r["motif"] for r in self.ctx.store.regles()}
        regles = []
        for cle, dossiers in occurrences.items():
            motif = re.escape(exemples[cle])
            if len(dossiers) >= 3 and motif not in existantes:
                regles.append({"motif": motif, "raison": f"Erreur récurrente ({len(dossiers)} dossiers) : « {exemples[cle]} »"})
        return regles

    def corrections_frequentes(self) -> list[tuple[str, int]]:
        compte = Counter()
        for decision in self.ctx.store.decisions_humaines():
            for ligne in (decision["diff"] or "").splitlines():
                if ligne.startswith(("- ", "+ ")):
                    compte[ligne.strip()] += 1
        return compte.most_common(10)

    def motifs_rejet(self) -> list[tuple[str, int]]:
        """Pourquoi l'humain écarte des candidatures : à lire pour régler la
        zone, les secteurs ou la taille d'entreprise visés."""
        compte = Counter()
        for decision in self.ctx.store.decisions_humaines():
            if decision["decision"].endswith("rejete") and (decision["diff"] or "").startswith(PREFIXE_MOTIF):
                compte[decision["diff"][len(PREFIXE_MOTIF):].strip().lower()] += 1
        return compte.most_common(10)

    def memoire_entreprises(self) -> dict[str, float]:
        delais = {}
        for ligne in self.ctx.store.cx.execute("SELECT entreprise, donnees FROM memoire_entreprises"):
            valeurs = json.loads(ligne["donnees"]).get("delais_jours", [])
            if valeurs:
                delais[ligne["entreprise"]] = statistics.median(valeurs)
        return delais

    # --------------------------------------------------------------- bilan

    def executer(self) -> str:
        donnees = self.donnees_etiquetees()
        store = self.ctx.store
        allocation = self.allocation_sources(donnees)
        bras = self.bras_ab(donnees)
        p_meilleur = parts_thompson(bras, self.rng, exploration=0)
        recalibrage = self.recalibrage_scoring(donnees)
        calibration = self.calibration_critique()
        regles = self.nouvelles_regles()

        store.proposer("allocation_sources", "Veille", {"allocation_sources": allocation})
        if recalibrage:
            store.proposer("recalibrage_scoring", "Scoring", recalibrage)
        if calibration and "seuil_validation_critique" in calibration:
            store.proposer("calibration_critique", "Critique", calibration)
        for regle in regles:
            store.proposer("nouvelles_regles", "Critique", regle)

        resultats = Counter(x["resultat"] for x in donnees)
        lignes = [
            f"# Bilan hebdomadaire — {self.ctx.horloge():%Y-%m-%d}",
            "",
            f"Candidatures abouties : {len(donnees)} (entretiens {resultats['entretien']}, refus {resultats['refus']}, silences {resultats['silence']})",
            f"Appels LLM cette semaine : {store.appels_llm_semaine()}",
            "",
            "## Taux d'entretien par source",
            *[f"- {k} : {v}" for k, v in self._taux_par(donnees, "source").items()],
            "",
            "## Allocation des sources (bandit, appliquée d'office)",
            *[f"- {k} : {v:.0%}" for k, v in sorted(allocation.items(), key=lambda x: -x[1])],
            "",
            "## A/B lettres : probabilité d'être le meilleur bras",
            *[f"- {k} : {bras[k][0]}/{sum(bras[k])} entretiens, P(meilleur) = {v:.0%}" for k, v in sorted(p_meilleur.items(), key=lambda x: -x[1])],
            "",
            "## Calibration du Critique",
            f"- {calibration['stats'] if calibration else 'pas assez de données'}",
            *([f"- Proposition : seuil de validation {calibration['ancien']} → {calibration['seuil_validation_critique']}"] if calibration and "ancien" in calibration else []),
            "",
            "## Recalibrage du scoring",
            f"- {recalibrage if recalibrage else 'pas assez de données (10 résultats dont 3 entretiens requis)'}",
            "",
            "## Nouvelles règles proposées",
            *([f"- `{r['motif']}` : {r['raison']}" for r in regles] or ["- aucune"]),
            "",
            "## Corrections humaines les plus fréquentes",
            *([f"- {c} ({n}×)" for c, n in self.corrections_frequentes()] or ["- aucune"]),
            "",
            "## Motifs de rejet",
            *([f"- {m} ({n}×)" for m, n in self.motifs_rejet()] or ["- aucun"]),
            "",
            "## Délais de réponse médians (jours)",
            *([f"- {e} : {d}" for e, d in self.memoire_entreprises().items()] or ["- aucun"]),
        ]
        bilan = "\n".join(lignes) + "\n"
        dossier = self.ctx.config.dossier_donnees / "bilans"
        dossier.mkdir(parents=True, exist_ok=True)
        (dossier / f"bilan_{self.ctx.horloge():%G-S%V}.md").write_text(bilan, encoding="utf-8")
        store.journaliser(self.nom, "bilan", details={"abouties": len(donnees), "propositions": 1 + bool(recalibrage) + len(regles)})
        return bilan
