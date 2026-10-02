"""Agent 5 — Rédaction : CV adapté, lettre et mail.

Règle absolue : aucune affirmation absente du profil source. Le rédacteur
doit déclarer, pour chaque phrase factuelle, les éléments du profil (ou de la
fiche entreprise) qui la fondent ; le Critique vérifie ces références.
"""

from __future__ import annotations

import html
from typing import Literal

from pydantic import BaseModel

from ..modeles import Affirmation, Dossier, ProfilStructure, Statut
from .base import Agent

ANGLES = {
    "actualite_entreprise": "Ouvrir sur un projet ou une actualité précise de l'entreprise tirée de la fiche, puis relier au profil.",
    "competence_cle": "Ouvrir sur l'exigence principale de l'offre et la compétence du profil qui y répond, preuve à l'appui.",
    "experience_terrain": "Ouvrir sur une expérience concrète du profil, transposée aux missions du poste.",
    "projet_formation": "Ouvrir sur un projet ou un enseignement de la formation en lien direct avec le poste.",
}

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

SYSTEME = """Tu rédiges en français la candidature d'un étudiant à un stage.

RÈGLE ABSOLUE : aucune affirmation absente des sources. N'invente ni compétence, ni niveau, ni chiffre, ni expérience, ni résultat.
Ne surévalue rien : « notions » ne devient pas « maîtrise », un projet scolaire ne devient pas une mission en entreprise.
Les informations sur l'entreprise viennent uniquement des faits de la fiche (identifiants fiche:N). Si la fiche est minimale, n'évoque l'entreprise que par ce que dit l'offre.

Exigences :
- Nom de l'entreprise et intitulé du poste recopiés exactement.
- Aucun champ à compléter, aucun crochet, aucune variable : le document part tel quel.
- Signature avec prénom, nom, e-mail et téléphone du candidat.
- Accroche concrète et propre à cette entreprise : elle ne doit plus tenir si l'on remplace le nom de l'entreprise.
- Répondre aux exigences clés de l'offre, chacune appuyée sur un élément du profil.
- Pas de formules creuses (« dynamique et motivé », « je me permets », « vif intérêt », « n'hésitez pas »).
- Lettre : 250 à 400 mots, registre soutenu, en paragraphes. Mail : 80 à 150 mots, il accompagne les pièces jointes.
- `affirmations` : chaque phrase factuelle de la lettre, du mail et de l'accroche du CV, avec les identifiants qui la fondent (ex. exp1, comp_python, form1, fiche:0, offre).
- `competences_cv` : identifiants de compétences du profil à mettre en avant, les plus pertinentes d'abord.
- Si des corrections du relecteur sont fournies, applique-les toutes."""


class QuestionReponse(BaseModel):
    question: str
    reponse: str


class SortieRedaction(BaseModel):
    angle: str
    experiences_retenues: list[str]
    objet_mail: str
    mail: str
    lettre: str
    affirmations: list[Affirmation]
    titre_cv: str
    accroche_cv: str
    competences_cv: list[str]
    reponses_formulaire: list[QuestionReponse]


def date_francaise(jour) -> str:
    return f"{jour.day}{'er' if jour.day == 1 else ''} {MOIS[jour.month - 1]} {jour.year}"


def rendre_cv(profil: ProfilStructure, sortie: SortieRedaction) -> str:
    """CV HTML imprimable : mêmes éléments que le profil, réordonnés. Rien n'y
    est ajouté hors du titre et de l'accroche, contrôlés par le Critique."""
    e = html.escape
    ident = profil.identite
    ordre = {i: n for n, i in enumerate(sortie.experiences_retenues)}
    experiences = sorted(profil.experiences, key=lambda x: ordre.get(x.id, len(ordre)))
    competences_mises = [c for i in sortie.competences_cv for c in profil.competences if c.id == i]
    autres = [c for c in profil.competences if c not in competences_mises]
    lignes = [
        "<!doctype html><html lang='fr'><meta charset='utf-8'>",
        f"<title>CV {e(ident.prenom)} {e(ident.nom)}</title>",
        "<style>body{font-family:Georgia,serif;max-width:46em;margin:2em auto;line-height:1.4}"
        "h1{margin:0}h2{border-bottom:1px solid #999;font-size:1.1em;margin-top:1.4em}"
        ".meta{color:#444}li{margin:.2em 0}</style>",
        f"<h1>{e(ident.prenom)} {e(ident.nom)}</h1>",
        f"<p class='meta'>{e(ident.email)} · {e(ident.telephone)} · {e(ident.ville)}</p>",
        f"<p><strong>{e(sortie.titre_cv)}</strong></p>",
        f"<p>{e(sortie.accroche_cv)}</p>",
        "<h2>Formation</h2><ul>",
        *[f"<li><strong>{e(f.diplome)}</strong> — {e(f.etablissement)} ({e(f.debut)} – {e(f.fin)}) {e(f.details)}</li>" for f in profil.formations],
        "</ul><h2>Expériences</h2>",
    ]
    for x in experiences:
        lignes.append(f"<p><strong>{e(x.intitule)}</strong> — {e(x.structure)} ({e(x.debut)} – {e(x.fin)})</p><ul>")
        lignes += [f"<li>{e(m)}</li>" for m in x.missions]
        lignes.append("</ul>")
    if profil.projets:
        lignes.append("<h2>Projets</h2><ul>")
        lignes += [f"<li><strong>{e(p.titre)}</strong> : {e(p.description)}</li>" for p in profil.projets]
        lignes.append("</ul>")
    lignes.append("<h2>Compétences</h2><p>")
    lignes.append(", ".join(f"<strong>{e(c.nom)}</strong>" for c in competences_mises))
    if autres:
        lignes.append((", " if competences_mises else "") + ", ".join(e(c.nom) + (f" ({e(c.niveau)})" if c.niveau else "") for c in autres))
    lignes.append("</p>")
    if profil.langues:
        lignes.append("<h2>Langues</h2><p>" + ", ".join(f"{e(lg.langue)} {e(lg.niveau)}".strip() for lg in profil.langues) + "</p>")
    if profil.centres_interet:
        lignes.append("<h2>Centres d'intérêt</h2><p>" + ", ".join(e(c) for c in profil.centres_interet) + "</p>")
    return "\n".join(lignes) + "\n</html>\n"


class AgentRedaction(Agent):
    nom = "redaction"

    def choisir_format(self, dossier: Dossier) -> Literal["lettre", "mail", "formulaire"]:
        if not (dossier.offre.contact_email or dossier.fiche_entreprise.contact):
            return "formulaire"  # pas d'adresse : candidature via le site ou la plateforme
        return "mail" if dossier.variante.endswith("+mail_seul") else "lettre"

    def choisir_angle(self, dossier: Dossier) -> str:
        angle = dossier.variante.split("+")[0] if dossier.variante else ""
        if angle == "actualite_entreprise" and dossier.fiche_entreprise.minimale:
            return ""  # angle impossible sans fiche sourcée : le rédacteur choisit
        return angle if angle in ANGLES else ""

    def executer(self, dossier: Dossier) -> None:
        profil = self.ctx.profil()
        format_ = self.choisir_format(dossier)
        angle = self.choisir_angle(dossier)
        fiche = dossier.fiche_entreprise
        faits = "\n".join(f"fiche:{i} — {f.texte} (source : {f.source})" for i, f in enumerate(fiche.faits))
        consignes = [
            f"Date du jour : {date_francaise(self.ctx.horloge().date())}",
            f"Format : {format_}"
            + {
                "lettre": " (mail court d'accompagnement + lettre de motivation jointe)",
                "mail": " (mail de candidature autonome, pas de lettre : laisse `lettre` vide)",
                "formulaire": " (lettre à déposer sur le site + réponses aux questions habituelles d'un formulaire : motivation, disponibilités, apport au poste)",
            }[format_],
            f"Angle d'accroche imposé : {ANGLES[angle]}" if angle else "Angle d'accroche : à choisir parmi " + ", ".join(ANGLES),
            "Fiche entreprise minimale : aucune information vérifiée sur l'entreprise en dehors de l'offre." if fiche.minimale else "",
        ]
        if dossier.critique.decision == "renvoyer":
            corrections = "\n".join(
                f"- [{p.gravite}/{p.type}] « {p.extrait} » : {p.raison} → {p.correction}" for p in dossier.critique.problemes
            )
            consignes.append(
                f"CORRECTIONS DU RELECTEUR (itération {dossier.critique.iteration}) :\n{corrections}\n\n"
                f"VERSION PRÉCÉDENTE :\nObjet : {dossier.documents.objet_mail}\nMail : {dossier.documents.mail}\n"
                f"Lettre : {dossier.documents.texte_lettre}"
            )
        contenu = "\n\n".join(
            [
                "\n".join(c for c in consignes if c),
                f"PROFIL (seule source sur le candidat) :\n{profil.model_dump_json(exclude={'cv_pdf'})}",
                f"OFFRE :\n{dossier.offre.model_dump_json()}",
                f"FICHE ENTREPRISE :\nActivité : {fiche.activite}\nContact : {fiche.contact}\n{faits}",
            ]
        )
        sortie = self.ctx.llm.structurer(self.nom, SYSTEME, contenu, SortieRedaction)

        doc = dossier.documents
        doc.version += 1
        doc.format = format_
        doc.angle = sortie.angle or angle
        doc.objet_mail = sortie.objet_mail
        doc.mail = sortie.mail
        doc.texte_lettre = sortie.lettre if format_ != "mail" else ""
        doc.affirmations = sortie.affirmations
        doc.experiences_retenues = sortie.experiences_retenues
        doc.reponses_formulaire = {q.question: q.reponse for q in sortie.reponses_formulaire} if format_ == "formulaire" else {}
        doc.piece_jointe_cv = profil.cv_pdf

        dossier_fichiers = self.ctx.dossier_fichiers(dossier.id)
        cv = dossier_fichiers / f"cv_v{doc.version}.html"
        cv.write_text(rendre_cv(profil, sortie), encoding="utf-8")
        doc.cv = str(cv)
        if doc.texte_lettre:
            lettre = dossier_fichiers / f"lettre_v{doc.version}.txt"
            lettre.write_text(doc.texte_lettre, encoding="utf-8")
            doc.lettre = str(lettre)
        else:
            doc.lettre = None
        (dossier_fichiers / f"mail_v{doc.version}.txt").write_text(f"Objet : {doc.objet_mail}\n\n{doc.mail}", encoding="utf-8")

        dossier.statut = Statut.redigee
        self.tracer(
            dossier,
            f"redaction_v{doc.version}",
            {"profil": profil, "offre": dossier.offre, "fiche": fiche, "consignes": consignes},
            {"format": format_, "angle": doc.angle},
        )
