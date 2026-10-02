"""Couche 1 du Critique : contrôles déterministes, sans LLM.

Chaque contrôle renvoie une liste de problèmes ; la proportion de contrôles
passés donne la note du critère `controles_deterministes`. Les règles apprises
(agent Apprentissage) s'ajoutent sous forme d'expressions régulières.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import ParamsCritique
from .hachage import normaliser, normaliser_entreprise
from .modeles import Dossier, Probleme, ProfilStructure

PLACEHOLDERS = [
    r"\[[^\]\n]{0,60}\]",
    r"\{\{?[^}\n]{0,60}\}?\}",
    r"<[^<>\n]{1,40}>",
    r"\bX{3,}\b",
    r"\b(TODO|TBD|A COMPLETER|À COMPLÉTER|lorem ipsum)\b",
    r"\((nom|adresse|date|intitul[eé]) (de |du |de la |d'|de l')?(l'entreprise|entreprise|poste|destinataire|recruteur|contact)\)",
    r"_{4,}",
]

FORMULES_CREUSES = [
    "dynamique et motive",
    "je me permets",
    "vif interet",
    "n hesitez pas",
    "force de proposition",
    "rigoureux et organise",
    "rigoureuse et organisee",
    "motive et serieux",
    "motivee et serieuse",
    "dans l attente de votre retour",
]

MOIS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}


@dataclass
class ResultatControles:
    problemes: list[Probleme] = field(default_factory=list)
    passes: int = 0
    total: int = 0
    non_corrigibles: list[str] = field(default_factory=list)  # hors de portée du rédacteur

    @property
    def score(self) -> float:
        return self.passes / self.total if self.total else 1.0

    def verifier(self, nom: str, problemes: list[Probleme]) -> None:
        self.total += 1
        if problemes:
            self.problemes.extend(problemes)
        else:
            self.passes += 1


def mots(texte: str) -> int:
    return len(re.findall(r"\b\w+\b", texte))


def textes(dossier: Dossier) -> dict[str, str]:
    d = dossier.documents
    t = {"objet": d.objet_mail, "mail": d.mail, "lettre": d.texte_lettre}
    t.update({f"formulaire:{q}": r for q, r in d.reponses_formulaire.items()})
    return {k: v for k, v in t.items() if v}


def _sans_accents(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()


def _present(aiguille: str, texte: str) -> bool:
    a = normaliser(aiguille)
    return bool(a) and re.search(rf"\b{re.escape(a)}\b", normaliser(texte)) is not None


def controle_entreprise(dossier: Dossier, autres_entreprises: list[str]) -> list[Probleme]:
    nom = dossier.offre.entreprise
    variantes = {nom, normaliser_entreprise(nom)}
    t = textes(dossier)
    problemes = []
    for cle in ("lettre", "mail"):
        if cle in t and not any(_present(v, t[cle]) for v in variantes):
            problemes.append(
                Probleme(type="incoherence", gravite="bloquant", extrait=t[cle][:80],
                         raison=f"Le nom « {nom} » n'apparaît pas dans {cle}.", correction=f"Citer « {nom} » exactement.")
            )
    cible = normaliser_entreprise(nom)
    tout = " ".join(t.values())
    for autre in autres_entreprises:
        cle_autre = normaliser_entreprise(autre)
        if not cle_autre or len(cle_autre) <= 3 or cle_autre == cible or cle_autre in cible:
            continue
        # Un nom d'un seul mot (« Total », « Orange ») est aussi un mot courant :
        # on ne le cherche qu'avec sa casse d'origine.
        trouve = _present(cle_autre, tout) if " " in cle_autre else re.search(rf"\b{re.escape(_sans_accents(autre.split()[0]))}\b", _sans_accents(tout))
        if trouve:
            problemes.append(
                Probleme(type="incoherence", gravite="bloquant", extrait=autre,
                         raison=f"Nom d'une autre entreprise candidatée (« {autre} ») présent dans le document.",
                         correction=f"Supprimer toute mention de « {autre} ».")
            )
    return problemes


def controle_intitule(dossier: Dossier) -> list[Probleme]:
    if dossier.offre.type == "spontanee":
        return []
    intitule = re.sub(r"\(?\b[hfm]\s*/\s*[hfm](\s*/\s*x)?\b\)?", "", dossier.offre.intitule, flags=re.IGNORECASE).strip(" -–")
    t = textes(dossier)
    if any(_present(intitule, t.get(cle, "")) for cle in ("objet", "mail", "lettre")):
        return []
    return [Probleme(type="incoherence", gravite="majeur", extrait=t.get("objet", "")[:80],
                     raison=f"L'intitulé exact « {intitule} » n'est repris ni dans l'objet, ni dans le mail, ni dans la lettre.",
                     correction=f"Reprendre l'intitulé « {intitule} » à l'identique, au moins dans l'objet.")]


def controle_placeholders(dossier: Dossier) -> list[Probleme]:
    problemes = []
    for cle, texte in textes(dossier).items():
        for motif in PLACEHOLDERS:
            for m in re.finditer(motif, texte, flags=re.IGNORECASE):
                problemes.append(
                    Probleme(type="placeholder", gravite="bloquant", extrait=m.group(0),
                             raison=f"Champ à compléter dans {cle}.", correction="Remplacer par l'information réelle ou supprimer.")
                )
    return problemes


def dates_citees(texte: str) -> list[tuple[date, bool]]:
    """Dates trouvées dans le texte ; le booléen indique une date au mois près."""
    trouvees: list[tuple[date, bool]] = []
    for j, m, a in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", texte):
        try:
            trouvees.append((date(int(a), int(m), int(j)), False))
        except ValueError:
            continue
    t = normaliser(texte)
    mois = "|".join(MOIS)
    for j, m, a in re.findall(rf"\b(\d{{1,2}})(?:er)? ({mois}) (\d{{4}})\b", t):
        try:
            trouvees.append((date(int(a), MOIS[m], int(j)), False))
        except ValueError:
            continue
    for m, a in re.findall(rf"(?<!\d )\b({mois}) (\d{{4}})\b", t):
        trouvees.append((date(int(a), MOIS[m], 1), True))
    return trouvees


def controle_dates(dossier: Dossier, du: date | None, au: date | None, aujourd_hui: date) -> list[Probleme]:
    """Les dates futures citées doivent tomber dans la disponibilité. Les dates
    passées (expériences, diplômes) ne sont pas concernées."""
    if not du or not au:
        return []
    debut, fin = du - timedelta(days=14), au + timedelta(days=14)
    problemes = []
    for cle, texte in textes(dossier).items():
        for jour, au_mois in dates_citees(texte):
            if au_mois:
                dernier = (jour.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                if dernier < aujourd_hui or (jour <= fin and dernier >= debut):
                    continue
            elif jour < aujourd_hui or debut <= jour <= fin:
                continue
            problemes.append(
                Probleme(type="incoherence", gravite="majeur", extrait=str(jour),
                         raison=f"Date citée dans {cle} hors de la disponibilité ({du} – {au}).",
                         correction=f"Indiquer une période comprise entre le {du} et le {au}.")
            )
    return problemes


def controle_longueur(dossier: Dossier, params: ParamsCritique) -> list[Probleme]:
    d = dossier.documents
    problemes = []
    for nom, texte, (mini, maxi) in (("lettre", d.texte_lettre, params.mots_lettre), ("mail", d.mail, params.mots_mail)):
        if not texte and nom == "lettre":
            continue
        n = mots(texte)
        if not mini <= n <= maxi:
            problemes.append(
                Probleme(type="forme", gravite="majeur", extrait=f"{n} mots",
                         raison=f"Longueur de {nom} hors bornes ({mini}–{maxi} mots).",
                         correction=f"Ramener {nom} entre {mini} et {maxi} mots.")
            )
    return problemes


def controle_coordonnees(dossier: Dossier, profil: ProfilStructure) -> list[Probleme]:
    t = textes(dossier)
    signe = t.get("lettre") or t.get("mail", "")
    problemes = []
    email = profil.identite.email
    if email and email.lower() not in signe.lower():
        problemes.append(Probleme(type="forme", gravite="majeur", extrait=signe[-80:],
                                  raison="E-mail du candidat absent de la signature.", correction=f"Ajouter {email}."))
    chiffres = re.sub(r"\D", "", profil.identite.telephone)[-9:]
    if chiffres and chiffres not in re.sub(r"\D", "", signe):
        problemes.append(Probleme(type="forme", gravite="majeur", extrait=signe[-80:],
                                  raison="Téléphone du candidat absent de la signature.",
                                  correction=f"Ajouter {profil.identite.telephone}."))
    return problemes


def controle_pieces_jointes(dossier: Dossier) -> tuple[list[Probleme], list[str]]:
    d = dossier.documents
    problemes, non_corrigibles = [], []
    if not d.piece_jointe_cv or not Path(d.piece_jointe_cv).exists():
        problemes.append(Probleme(type="incoherence", gravite="majeur", extrait=str(d.piece_jointe_cv),
                                  raison="CV PDF à joindre introuvable.", correction="Renseigner le CV PDF (rs profil --cv-pdf)."))
        non_corrigibles.append("CV PDF introuvable")
    if d.format == "lettre" and (not d.lettre or not Path(d.lettre).exists()):
        problemes.append(Probleme(type="incoherence", gravite="majeur", extrait=str(d.lettre),
                                  raison="Lettre attendue mais fichier absent.", correction="Produire la lettre."))
    return problemes, non_corrigibles


def controle_sources(dossier: Dossier, profil: ProfilStructure) -> list[Probleme]:
    valides = profil.identifiants() | {"offre"} | {f"fiche:{i}" for i in range(len(dossier.fiche_entreprise.faits))}
    problemes = []
    for a in dossier.documents.affirmations:
        inconnues = [s for s in a.sources if s not in valides]
        if not a.sources or inconnues:
            problemes.append(
                Probleme(type="invente", gravite="bloquant", extrait=a.texte[:120],
                         raison="Affirmation sans source valide" + (f" (références inconnues : {', '.join(inconnues)})" if inconnues else ""),
                         correction="Supprimer l'affirmation ou l'appuyer sur un élément existant du profil.")
            )
    return problemes


def controle_formules(dossier: Dossier, regles: list[dict]) -> list[Probleme]:
    tout = " ".join(textes(dossier).values())
    t = normaliser(tout)
    problemes = [
        Probleme(type="generique", gravite="mineur", extrait=f, raison="Formule creuse.", correction="Remplacer par un fait précis.")
        for f in FORMULES_CREUSES if re.search(rf"\b{f}\b", t)
    ]
    for regle in regles:
        m = re.search(regle["motif"], tout, flags=re.IGNORECASE)
        if m:
            problemes.append(Probleme(type="generique", gravite="majeur", extrait=m.group(0),
                                      raison=f"Règle apprise : {regle['raison']}", correction="Reformuler sans cette expression."))
    return problemes


def executer(
    dossier: Dossier,
    profil: ProfilStructure,
    params: ParamsCritique,
    disponible_du: date | None,
    disponible_au: date | None,
    autres_entreprises: list[str],
    regles: list[dict],
    aujourd_hui: date,
) -> ResultatControles:
    r = ResultatControles()
    r.verifier("entreprise", controle_entreprise(dossier, autres_entreprises))
    r.verifier("intitule", controle_intitule(dossier))
    r.verifier("placeholders", controle_placeholders(dossier))
    r.verifier("dates", controle_dates(dossier, disponible_du, disponible_au, aujourd_hui))
    r.verifier("longueur", controle_longueur(dossier, params))
    r.verifier("coordonnees", controle_coordonnees(dossier, profil))
    pj, non_corrigibles = controle_pieces_jointes(dossier)
    r.verifier("pieces_jointes", pj)
    r.non_corrigibles.extend(non_corrigibles)
    r.verifier("sources", controle_sources(dossier, profil))
    r.verifier("formules", controle_formules(dossier, regles))
    return r
