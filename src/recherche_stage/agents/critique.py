"""Agent 6 — Critique : contrôle qualité indépendant. Juge, ne réécrit pas.

Couche 1 : contrôles déterministes (module `controles`).
Couches 2 à 4 : un appel LLM avec un prompt distinct de celui du rédacteur,
qui ne répond qu'à des questions fermées par critère. La note n'est pas
demandée au modèle : elle est calculée ici à partir de ces réponses, ce qui
rend la grille stable d'un dossier à l'autre.

Note sur la température : la spécification demande 0,1. Les modèles Claude
actuels n'acceptent plus de paramètre d'échantillonnage ; la stabilité vient
des questions fermées et du calcul déterministe de la note.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from pydantic import BaseModel

from .. import controles
from ..modeles import Critique, Dossier, Probleme, ProfilStructure, Statut
from .base import Agent

POIDS = {
    "exactitude_factuelle": 0.35,
    "controles_deterministes": 0.20,
    "adequation_offre": 0.25,
    "specificite": 0.10,
    "forme": 0.10,
}

SYSTEME = """Tu es relecteur qualité de candidatures de stage, indépendant du rédacteur.
Tu ne réécris rien : tu réponds aux questions fermées, avec l'extrait exact du document en appui.

Couche « ancrage factuel » :
- Pour chaque affirmation déclarée, `ancree` : est-elle entièrement justifiée par les éléments du profil ou de la fiche qu'elle cite ?
  `exageree` : le niveau, l'ampleur ou la responsabilité dépassent-ils ce que dit la source ?
- `phrases_factuelles_non_declarees` : phrases factuelles du document (sur le candidat ou l'entreprise) absentes de la liste d'affirmations.
- `infos_entreprise_hors_fiche` : informations sur l'entreprise qui ne viennent ni de l'offre ni de la fiche.
Couche « adéquation à l'offre » :
- `exigences_cles` : les exigences principales de l'offre (5 au plus) ; `adressee` vrai si le document y répond avec un élément du profil.
- `substituable` : la lettre resterait-elle valable si l'on remplaçait le nom de l'entreprise par celui d'une autre ?
- `accroche_concrete` : la première phrase s'appuie-t-elle sur un fait précis (projet, mission, exigence) plutôt qu'une généralité ?
- `profil_trop_eloigne` : le profil est-il si éloigné de l'offre qu'aucune rédaction honnête ne peut convaincre ?
Couche « forme » :
- `fautes` : fautes d'orthographe, de grammaire ou de typographie, avec correction.
- `registre_adapte`, `coherent_cv_lettre` (le titre et l'accroche du CV concordent-ils avec la lettre ?), `formules_creuses`.
- `cas_ambigu` : vrai seulement si une décision humaine est indispensable (ex. information contradictoire entre offre et profil) ; formule alors `question_ambigu` précise."""


class VerifAffirmation(BaseModel):
    affirmation: str
    ancree: bool
    exageree: bool
    commentaire: str


class VerifExigence(BaseModel):
    exigence: str
    adressee: bool
    extrait: str


class Faute(BaseModel):
    extrait: str
    correction: str


class EvaluationCritique(BaseModel):
    affirmations: list[VerifAffirmation]
    phrases_factuelles_non_declarees: list[str]
    infos_entreprise_hors_fiche: list[str]
    exigences_cles: list[VerifExigence]
    substituable: bool
    accroche_concrete: bool
    profil_trop_eloigne: bool
    fautes: list[Faute]
    registre_adapte: bool
    coherent_cv_lettre: bool
    formules_creuses: list[str]
    cas_ambigu: bool
    question_ambigu: str


def _borne(x: float) -> float:
    return max(0.0, min(1.0, x))


def noter(evaluation: EvaluationCritique, ctrl: controles.ResultatControles, dossier: Dossier, fautes_max: int, fautes_correcteur: list[Faute]) -> tuple[dict[str, float], list[Probleme]]:
    """Calcule la note de chaque critère et les problèmes qui en découlent."""
    problemes: list[Probleme] = list(ctrl.problemes)

    # Exactitude factuelle
    affirmations = evaluation.affirmations
    justes = sum(1 for a in affirmations if a.ancree and not a.exageree)
    exactitude = justes / len(affirmations) if affirmations else 0.5
    exactitude -= 0.1 * len(evaluation.phrases_factuelles_non_declarees) + 0.2 * len(evaluation.infos_entreprise_hors_fiche)
    for a in affirmations:
        if not a.ancree:
            problemes.append(Probleme(type="invente", gravite="bloquant", extrait=a.affirmation[:120], raison=a.commentaire or "Affirmation non justifiée par le profil.", correction="Supprimer ou reformuler à partir du profil."))
        elif a.exageree:
            problemes.append(Probleme(type="exagere", gravite="majeur", extrait=a.affirmation[:120], raison=a.commentaire or "Niveau surévalué.", correction="Aligner le niveau sur celui du profil."))
    for phrase in evaluation.phrases_factuelles_non_declarees:
        problemes.append(Probleme(type="invente", gravite="majeur", extrait=phrase[:120], raison="Phrase factuelle sans source déclarée.", correction="La relier à un élément du profil ou la supprimer."))
    for info in evaluation.infos_entreprise_hors_fiche:
        problemes.append(Probleme(type="invente", gravite="bloquant", extrait=info[:120], raison="Information sur l'entreprise absente de la fiche sourcée.", correction="Supprimer cette information."))

    # Adéquation à l'offre
    exigences = evaluation.exigences_cles
    adressees = sum(1 for e in exigences if e.adressee)
    taux = adressees / len(exigences) if exigences else 0.0
    adequation = 0.6 * taux + 0.2 * (not evaluation.substituable) + 0.2 * evaluation.accroche_concrete
    if len(exigences) >= 2 and adressees < 2:
        manquees = ", ".join(e.exigence for e in exigences if not e.adressee)
        problemes.append(Probleme(type="generique", gravite="bloquant", extrait="", raison=f"Moins de 2 exigences clés adressées ({adressees}/{len(exigences)}).", correction=f"Répondre explicitement à : {manquees}."))
    if evaluation.substituable:
        problemes.append(Probleme(type="generique", gravite="majeur", extrait="", raison="Test de substitution échoué : la lettre vaut pour n'importe quelle entreprise.", correction="Ancrer l'accroche et un paragraphe dans l'offre ou la fiche entreprise."))
    if not evaluation.accroche_concrete:
        problemes.append(Probleme(type="generique", gravite="mineur", extrait="", raison="Accroche générale.", correction="Ouvrir sur un fait précis de l'offre ou de l'entreprise."))

    # Spécificité
    refs_fiche = sum(1 for a in dossier.documents.affirmations for s in a.sources if s.startswith("fiche:"))
    specificite = 0.5 * (not evaluation.substituable) + 0.25 * evaluation.accroche_concrete + 0.25 * (refs_fiche > 0 or dossier.fiche_entreprise.minimale)

    # Forme
    fautes = evaluation.fautes + [f for f in fautes_correcteur if f.extrait not in {g.extrait for g in evaluation.fautes}]
    creuses = set(evaluation.formules_creuses) | {p.extrait for p in ctrl.problemes if p.type == "generique" and p.gravite == "mineur"}
    hors_longueur = any(p.type == "forme" and "Longueur" in p.raison for p in ctrl.problemes)
    forme = 1 - 0.15 * len(fautes) - 0.1 * len(creuses) - 0.2 * (not evaluation.registre_adapte) - 0.2 * (not evaluation.coherent_cv_lettre) - 0.1 * hors_longueur
    for f in fautes:
        problemes.append(Probleme(type="forme", gravite="bloquant" if len(fautes) > fautes_max else "mineur", extrait=f.extrait, raison="Faute.", correction=f.correction))
    if not evaluation.registre_adapte:
        problemes.append(Probleme(type="forme", gravite="majeur", extrait="", raison="Registre inadapté.", correction="Adopter un registre soutenu et sobre."))
    if not evaluation.coherent_cv_lettre:
        problemes.append(Probleme(type="incoherence", gravite="majeur", extrait="", raison="CV et lettre incohérents.", correction="Aligner le titre et l'accroche du CV sur la lettre."))
    for formule in evaluation.formules_creuses:
        problemes.append(Probleme(type="generique", gravite="mineur", extrait=formule, raison="Formule creuse.", correction="Remplacer par un fait précis."))

    notes = {
        "exactitude_factuelle": _borne(exactitude),
        "controles_deterministes": _borne(ctrl.score),
        "adequation_offre": _borne(adequation),
        "specificite": _borne(specificite),
        "forme": _borne(forme),
    }
    return notes, problemes


def corriger_avec_languagetool(url: str, texte: str) -> list[Faute]:
    """Correcteur orthographique optionnel. Utiliser un serveur LanguageTool
    local : le texte contient des données personnelles (RGPD)."""
    if not url or not texte:
        return []
    corps = urllib.parse.urlencode({"text": texte, "language": "fr"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{url.rstrip('/')}/v2/check", data=corps), timeout=20) as r:
            resultats = json.loads(r.read())
    except OSError:
        return []
    fautes = []
    for m in resultats.get("matches", []):
        if m.get("rule", {}).get("issueType") in ("misspelling", "grammar", "typographical"):
            debut, longueur = m["offset"], m["length"]
            suggestion = (m.get("replacements") or [{}])[0].get("value", "")
            fautes.append(Faute(extrait=texte[debut : debut + longueur], correction=suggestion))
    return fautes


class AgentCritique(Agent):
    nom = "critique"

    def decider(self, note: float, problemes: list[Probleme], evaluation: EvaluationCritique, iteration: int, non_corrigibles: list[str]) -> tuple[str, str]:
        """Renvoie (décision, question pour l'humain)."""
        p = self.ctx.config.critique
        seuil_validation = self.ctx.store.parametre("seuil_validation_critique", p.seuil_validation)
        bloquants = [x for x in problemes if x.gravite == "bloquant"]
        if evaluation.profil_trop_eloigne:
            return "abandonner", ""
        if evaluation.cas_ambigu:
            return "escalader", evaluation.question_ambigu or "Cas ambigu signalé par le relecteur."
        if note >= seuil_validation and not bloquants and not non_corrigibles:
            return "valider", ""
        if non_corrigibles:
            return "escalader", "Problème hors de portée du rédacteur : " + "; ".join(non_corrigibles)
        principaux = "; ".join(f"{x.raison} ({x.extrait[:60]})" if x.extrait else x.raison for x in (bloquants or problemes)[:3])
        if iteration > self.ctx.config.orchestrateur.iterations_critique_max:
            return "escalader", f"Échec après {iteration - 1} réécritures (note {note:.0f}). Points restants : {principaux}. Corriger à la main, valider en l'état ou abandonner ?"
        if note < p.seuil_renvoi:
            return "escalader", f"Note {note:.0f}/100, trop basse pour une réécriture automatique. Problèmes : {principaux}. Faut-il poursuivre cette candidature ?"
        return "renvoyer", ""

    def executer(self, dossier: Dossier) -> None:
        profil: ProfilStructure = self.ctx.profil()
        grille = self.ctx.grille()
        dossier.statut = Statut.en_critique
        iteration = dossier.critique.iteration + 1
        precedents = dossier.critique.problemes

        autres = sorted({d.offre.entreprise for d in self.ctx.store.lister() if d.id != dossier.id and d.documents.version})
        ctrl = controles.executer(
            dossier, profil, self.ctx.config.critique, grille.durs.disponible_du, grille.durs.disponible_au,
            autres, self.ctx.store.regles(), self.ctx.horloge().date(),
        )

        doc = dossier.documents
        contenu = "\n\n".join(
            [
                f"ITÉRATION : {iteration}",
                "REMARQUES PRÉCÉDENTES :\n" + ("\n".join(f"- {p.raison} ({p.extrait[:60]})" for p in precedents) or "aucune"),
                f"PROFIL SOURCE :\n{profil.model_dump_json(exclude={'cv_pdf'})}",
                f"OFFRE :\n{dossier.offre.model_dump_json()}",
                "FICHE ENTREPRISE :\n" + dossier.fiche_entreprise.model_dump_json(),
                f"DOCUMENTS :\nObjet : {doc.objet_mail}\nMail :\n{doc.mail}\n\nLettre :\n{doc.texte_lettre or '(aucune)'}\n\n"
                f"Réponses formulaire : {json.dumps(doc.reponses_formulaire, ensure_ascii=False)}",
                "AFFIRMATIONS DÉCLARÉES :\n" + "\n".join(f"- {a.texte} ← {', '.join(a.sources)}" for a in doc.affirmations),
            ]
        )
        evaluation = self.ctx.llm.structurer(self.nom, SYSTEME, contenu, EvaluationCritique, critique=True)
        fautes_lt = corriger_avec_languagetool(self.ctx.config.critique.languagetool_url or "", f"{doc.mail}\n\n{doc.texte_lettre}")

        notes, problemes = noter(evaluation, ctrl, dossier, self.ctx.config.critique.fautes_max, fautes_lt)
        note = round(100 * sum(POIDS[c] * v for c, v in notes.items()), 1)
        decision, question = self.decider(note, problemes, evaluation, iteration, ctrl.non_corrigibles)

        dossier.critique = Critique(note=note, decision=decision, iteration=iteration, problemes=problemes, details=notes, question_humain=question)
        self.tracer(
            dossier,
            f"critique:{decision}",
            {"documents": doc, "profil": profil, "offre": dossier.offre, "fiche": dossier.fiche_entreprise, "iteration": iteration, "precedents": precedents},
            {"note": note, "problemes": len(problemes)},
        )
        if decision == "valider":
            dossier.statut = Statut.validee
        elif decision == "renvoyer":
            dossier.statut = Statut.enquetee  # retour à la Rédaction avec les corrections
        elif decision == "abandonner":
            dossier.statut = Statut.abandonnee
            dossier.motif = "trop_eloigne (critique) : retour au Scoring"
            self.ctx.store.journaliser(self.nom, "retour_scoring", dossier.id, details={"score": dossier.score.valeur, "note": note})
        else:
            dossier.statut = Statut.en_attente_humain
            dossier.etape_attente = "critique"
            dossier.motif = "escalade_critique"
            self.escalader(dossier, "critique", f"« {dossier.offre.intitule} » chez {dossier.offre.entreprise} : {question}")
