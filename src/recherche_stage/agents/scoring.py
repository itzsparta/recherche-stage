"""Agent 3 — Scoring : élimine sur critères durs, écarte les offres suspectes
ou expirées, calcule un score de compatibilité et un niveau de confiance.

Score = règles (critères souples pondérés) + proximité lexicale + adéquation
évaluée par le LLM exigence par exigence. Le LLM n'est appelé que pour les
offres qui ont une chance d'atteindre le seuil.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from ..hachage import normaliser, normaliser_entreprise
from ..modeles import CritereSouple, Dossier, GrilleCriteres, ProfilStructure, Score, Statut
from .base import Agent

TOLERANCE_DATES = timedelta(days=14)

# ----------------------------------------------------------------- suspicion

# Motifs cherchés dans le texte normalisé (minuscules, sans accents ni apostrophes).
MOTIFS_SUSPECTS = [
    (r"\b(frais|droits?) (d inscription|de dossier|de formation)\b", "frais demandés au candidat"),
    (r"\b(whatsapp|telegram)\b", "contact par messagerie instantanée"),
    (r"\b(western union|mandat cash|cryptomonnaie|bitcoin)\b", "transaction financière"),
    (r"\b(gagnez|revenus? garantis?|argent facile)\b", "promesse de gain"),
    (r"\b(envoyez|transmettez|fournir|joindre)\b.{0,40}\b(coordonnees bancaires|rib|numero de securite sociale)\b", "données sensibles demandées"),
]
DOMAINES_GRATUITS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "live.", "laposte.net", "orange.fr", "free.fr")


def raisons_suspicion(dossier: Dossier) -> tuple[list[str], list[str]]:
    """Renvoie (signaux forts, signaux faibles). Un signal fort ou deux faibles excluent l'offre."""
    o = dossier.offre
    texte = normaliser(f"{o.intitule} {o.description}")
    fortes = [raison for motif, raison in MOTIFS_SUSPECTS if re.search(motif, texte)]
    if o.gratification and o.gratification > 4000:
        fortes.append("gratification irréaliste pour un stage")
    faibles = []
    if o.type == "offre" and o.contact_email and o.contact_email.split("@")[-1].startswith(DOMAINES_GRATUITS):
        faibles.append("adresse de contact personnelle")
    if o.type == "offre" and len(o.description) < 80:
        faibles.append("description quasi vide")
    return fortes, faibles


# ------------------------------------------------------------- critères durs


def _duree_mois(debut: date, fin: date) -> float:
    return (fin - debut).days / 30.44


def _lieu_compatible(lieu: str, lieux: list[str]) -> bool | None:
    if not lieu:
        return None
    texte = normaliser(lieu)
    jetons = set(texte.split())
    for voulu in lieux:
        v = normaliser(voulu)
        if not v:
            continue
        if v.isdigit():  # département : « 59 », ou code postal « 59000 »
            if v in jetons or any(j.isdigit() and len(j) == 5 and j.startswith(v) for j in jetons):
                return True
        elif re.search(rf"\b{re.escape(v)}\b", texte):
            return True
    return False


def violations_criteres_durs(dossier: Dossier, grille: GrilleCriteres) -> tuple[list[str], list[str]]:
    """Renvoie (violations, informations manquantes)."""
    o, d = dossier.offre, grille.durs
    violations: list[str] = []
    manquantes: list[str] = []

    if o.dates.debut and d.disponible_du and o.dates.debut < d.disponible_du - TOLERANCE_DATES:
        violations.append(f"début {o.dates.debut} avant la disponibilité ({d.disponible_du})")
    if o.dates.debut and d.disponible_au and o.dates.debut > d.disponible_au:
        violations.append(f"début {o.dates.debut} après la fin de disponibilité ({d.disponible_au})")
    if o.dates.fin and d.disponible_au and o.dates.fin > d.disponible_au + TOLERANCE_DATES:
        violations.append(f"fin {o.dates.fin} après la fin de disponibilité ({d.disponible_au})")
    if not o.dates.debut and o.type == "offre":
        manquantes.append("dates")

    duree = o.duree_mois
    if duree is None and o.dates.debut and o.dates.fin:
        duree = _duree_mois(o.dates.debut, o.dates.fin)
    if duree is not None:
        if d.duree_min_mois and duree < d.duree_min_mois - 0.5:
            violations.append(f"durée {duree:.1f} mois < minimum {d.duree_min_mois}")
        if d.duree_max_mois and duree > d.duree_max_mois + 0.5:
            violations.append(f"durée {duree:.1f} mois > maximum {d.duree_max_mois}")

    teletravail_complet = o.teletravail is True and re.search(
        r"\b(full remote|100 (teletravail|remote)|teletravail (complet|total|integral))\b", normaliser(o.description)
    )
    if d.lieux and not (teletravail_complet and d.accepte_teletravail_complet):
        compatible = _lieu_compatible(o.lieu, d.lieux)
        if compatible is False:
            violations.append(f"lieu « {o.lieu} » hors zone")
        elif compatible is None:
            manquantes.append("lieu")

    if d.gratification_min and o.gratification is not None and o.gratification < d.gratification_min:
        violations.append(f"gratification {o.gratification} € < minimum {d.gratification_min} €")

    exclues = {normaliser_entreprise(e) for e in d.entreprises_exclues}
    if normaliser_entreprise(o.entreprise) in exclues:
        violations.append("entreprise exclue")
    intitule = normaliser(o.intitule)
    for mot in d.mots_exclus:
        if re.search(rf"\b{re.escape(normaliser(mot))}\b", intitule):
            violations.append(f"mot exclu « {mot} » dans l'intitulé")
    return violations, manquantes


# --------------------------------------------------------- score par règles


def score_critere(critere: CritereSouple, dossier: Dossier) -> float:
    o = dossier.offre
    texte = normaliser(f"{o.intitule} {o.description} {o.secteur or ''}")
    valeurs = [normaliser(v) for v in critere.valeurs if normaliser(v)]
    if critere.type in ("mots_cles", "secteur"):
        if not valeurs:
            return 0.0
        trouves = sum(1 for v in valeurs if re.search(rf"\b{re.escape(v)}\b", texte))
        return min(1.0, trouves / min(3, len(valeurs))) if critere.type == "mots_cles" else float(trouves > 0)
    if critere.type == "lieu_prefere":
        return float(bool(_lieu_compatible(o.lieu, critere.valeurs)))
    if critere.type == "teletravail":
        return 1.0 if o.teletravail else 0.0
    if critere.type == "gratification":
        cible = float(critere.valeurs[0]) if critere.valeurs else 0
        if o.gratification is None or not cible:
            return 0.5
        return min(1.0, o.gratification / cible)
    return 0.0


# Une cible de candidature spontanée n'a ni description ni conditions : seuls
# ces critères s'appliquent à elle.
CRITERES_SPONTANEE = ("secteur", "lieu_prefere")


def score_regles(dossier: Dossier, grille: GrilleCriteres) -> float:
    criteres = grille.souples
    if dossier.offre.type == "spontanee":
        criteres = [c for c in criteres if c.type in CRITERES_SPONTANEE]
    total = sum(c.poids for c in criteres)
    if not total:
        return 0.5  # aucun critère applicable : neutre
    return sum(c.poids * score_critere(c, dossier) for c in criteres) / total


# ------------------------------------------------------ proximité lexicale

MOTS_VIDES = set(
    "a au aux avec ce ces dans de des du elle en et eux il je la le les leur lui ma mais me meme mes moi mon ne nos "
    "notre nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une vos votre vous c d j l "
    "m n s t y est sont etre avoir sera vos votre ainsi tres plus au sein afin cette cet the and of to in for with "
    "stage stagiaire mission missions profil poste entreprise equipe".split()
)


def _jetons(texte: str) -> Counter:
    # Racinisation grossière : les 6 premiers caractères suffisent à rapprocher
    # « maintenance » / « maintenir » ou « industriel » / « industrielle ».
    return Counter(m[:6] for m in normaliser(texte).split() if len(m) > 2 and m not in MOTS_VIDES)


def similarite_lexicale(profil: ProfilStructure, dossier: Dossier) -> float:
    texte_profil = " ".join(
        [c.nom for c in profil.competences]
        + [f"{e.intitule} {' '.join(e.missions)}" for e in profil.experiences]
        + [f"{f.diplome} {f.details}" for f in profil.formations]
        + [f"{p.titre} {p.description}" for p in profil.projets]
    )
    a, b = _jetons(texte_profil), _jetons(f"{dossier.offre.intitule} {dossier.offre.description}")
    if not a or not b:
        return 0.0
    produit = sum(a[t] * b[t] for t in a.keys() & b.keys())
    cosinus = produit / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))
    return min(1.0, cosinus / 0.35)  # 0,35 de cosinus correspond déjà à une très bonne proximité


# ------------------------------------------------------------- avis du LLM

SYSTEME = """Tu évalues l'adéquation entre une offre de stage et le profil d'un candidat.

Pour chaque exigence explicite de l'offre (compétence, formation, langue, outil, savoir-être demandé) :
- `couverte` vaut « oui » seulement si un élément précis du profil la satisfait ; « partiel » si un élément s'en approche ; « non » sinon.
- `preuve` est l'identifiant exact de cet élément du profil (ex. exp1, comp_python) ; chaîne vide si « non ».
Ne suppose aucune compétence absente du profil.
`suspecte` : vrai si l'offre ressemble à une arnaque, un emploi déguisé ou une offre sans rapport avec un stage.
`certitude` : ta certitude globale ; « basse » si l'offre est trop vague pour juger."""


class Exigence(BaseModel):
    exigence: str
    obligatoire: bool
    couverte: Literal["oui", "partiel", "non"]
    preuve: str


class EvaluationOffre(BaseModel):
    exigences: list[Exigence]
    suspecte: bool
    raison_suspecte: str
    informations_manquantes: list[str]
    certitude: Literal["haute", "moyenne", "basse"]
    justification: str


def couverture(evaluation: EvaluationOffre, profil: ProfilStructure) -> tuple[float, int]:
    """Taux de couverture pondéré (obligatoire = 2) et nombre d'exigences couvertes.
    Une exigence « couverte » par un identifiant inexistant compte comme non couverte."""
    ids = profil.identifiants()
    points = poids_total = 0.0
    couvertes = 0
    for e in evaluation.exigences:
        poids = 2.0 if e.obligatoire else 1.0
        poids_total += poids
        if e.preuve and e.preuve not in ids:
            continue
        valeur = {"oui": 1.0, "partiel": 0.5, "non": 0.0}[e.couverte]
        couvertes += valeur == 1.0
        points += poids * valeur
    return (points / poids_total if poids_total else 0.0), couvertes


# ------------------------------------------------------------------- agent


class AgentScoring(Agent):
    nom = "scoring"

    def poids(self) -> tuple[float, float, float]:
        p = self.ctx.config.scoring
        ajuste = self.ctx.store.parametre("poids_scoring", {}) or {}
        return ajuste.get("regles", p.poids_regles), ajuste.get("lexical", p.poids_lexical), ajuste.get("llm", p.poids_llm)

    def executer(self, dossiers: list[Dossier]) -> list[Dossier]:
        profil, grille = self.ctx.profil(), self.ctx.grille()
        retenus = []
        for dossier in dossiers:
            self.evaluer(dossier, profil, grille)
            self.ctx.store.save(dossier)
            if dossier.statut == Statut.scoree:
                retenus.append(dossier)
        return sorted(retenus, key=lambda d: d.score.valeur, reverse=True)

    def _abandonner(self, dossier: Dossier, motif: str, entrees) -> None:
        dossier.statut = Statut.abandonnee
        dossier.motif = motif
        self.tracer(dossier, "elimination", entrees, {"motif": motif})

    def _scorer_spontanee(self, dossier: Dossier, grille: GrilleCriteres, entrees) -> None:
        """Cible issue de l'annuaire, déjà filtrée par secteur et zone : le score
        ne porte que sur les critères applicables, sans évaluation sémantique
        (il n'y a pas d'offre à comparer au profil)."""
        regles = score_regles(dossier, grille)
        dossier.score = Score(
            valeur=round(100 * regles, 1),
            confiance=0.7,
            justification="Candidature spontanée : score sur les critères secteur et lieu uniquement.",
            details={"regles": round(regles, 3)},
        )
        dossier.statut = Statut.scoree
        self.tracer(dossier, "score", entrees, {"valeur": dossier.score.valeur, "spontanee": True})

    def evaluer(self, dossier: Dossier, profil: ProfilStructure, grille: GrilleCriteres) -> None:
        entrees = {"offre": dossier.offre, "grille": grille}
        aujourd_hui = self.ctx.horloge().date()

        fortes, faibles = raisons_suspicion(dossier)
        if fortes or len(faibles) >= 2:
            return self._abandonner(dossier, "suspecte : " + "; ".join(fortes + faibles), entrees)

        o = dossier.offre
        expiration = timedelta(days=self.ctx.config.scoring.jours_avant_expiration)
        if o.type == "offre" and o.dates.debut and o.dates.debut < aujourd_hui:
            return self._abandonner(dossier, f"expiree : début {o.dates.debut} passé", entrees)
        if o.type == "offre" and o.date_publication and aujourd_hui - o.date_publication > expiration:
            return self._abandonner(dossier, f"expiree : publiée le {o.date_publication}", entrees)

        violations, manquantes = violations_criteres_durs(dossier, grille)
        if violations:
            return self._abandonner(dossier, "critere_dur : " + "; ".join(violations), entrees)
        dossier.statut = Statut.filtree
        self.tracer(dossier, "filtre_criteres_durs", entrees, {"manquantes": manquantes})

        if o.type == "spontanee":
            return self._scorer_spontanee(dossier, grille, entrees)

        w_r, w_l, w_m = self.poids()
        regles = score_regles(dossier, grille)
        lexical = similarite_lexicale(profil, dossier)
        details = {"regles": round(regles, 3), "lexical": round(lexical, 3)}
        pre_score = 100 * (w_r * regles + w_l * lexical) / ((w_r + w_l) or 1)

        seuil = self.ctx.config.orchestrateur.seuil_score_minimum
        evaluation = None
        if pre_score >= seuil - 30 or dossier.prioritaire:
            evaluation = self.ctx.llm.structurer(
                self.nom,
                SYSTEME,
                f"PROFIL :\n{profil.model_dump_json(exclude={'cv_pdf'})}\n\nOFFRE :\n{o.model_dump_json()}",
                EvaluationOffre,
            )
            if evaluation.suspecte:
                return self._abandonner(dossier, f"suspecte : {evaluation.raison_suspecte}", entrees)

        if evaluation is None:
            valeur = pre_score
            certitude = 0.6  # sans avis sémantique, on reste prudent
            justification = "Pré-score insuffisant, évaluation sémantique non lancée."
        else:
            taux, couvertes = couverture(evaluation, profil)
            details |= {"llm": round(taux, 3), "exigences_couvertes": couvertes}
            valeur = 100 * (w_r * regles + w_l * lexical + w_m * taux) / ((w_r + w_l + w_m) or 1)
            certitude = {"haute": 1.0, "moyenne": 0.8, "basse": 0.5}[evaluation.certitude]
            certitude -= 0.5 * abs(regles - taux)  # règles et sémantique en désaccord
            manquantes = sorted(set(manquantes) | set(evaluation.informations_manquantes))
            justification = evaluation.justification

        if len(o.description) < 300:
            manquantes.append("description courte")
        manquantes += faibles
        confiance = max(0.0, min(1.0, certitude - 0.12 * len(manquantes)))
        dossier.score = Score(
            valeur=round(valeur, 1),
            confiance=round(confiance, 2),
            justification=justification + (f" Informations manquantes : {', '.join(manquantes)}." if manquantes else ""),
            details=details,
        )
        dossier.statut = Statut.scoree
        self.tracer(dossier, "score", entrees | {"evaluation": evaluation}, {"valeur": dossier.score.valeur, "confiance": confiance})

        if valeur >= seuil and confiance < self.ctx.config.orchestrateur.seuil_confiance:
            dossier.statut = Statut.en_attente_humain
            dossier.etape_attente = "scoring"
            dossier.motif = "confiance_faible"
            self.escalader(
                dossier,
                "confiance_faible",
                f"« {o.intitule} » chez {o.entreprise} : score {valeur:.0f} mais confiance {confiance:.2f} "
                f"({', '.join(manquantes) or 'avis divergents'}). Poursuivre ? (rs valider / rs rejeter)",
            )
