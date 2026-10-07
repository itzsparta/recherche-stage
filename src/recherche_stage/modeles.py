"""Schéma partagé : le dossier de candidature que chaque agent enrichit, le
profil du candidat et la grille de critères.

Les noms suivent la spécification (`schema_dossier_candidature`). Les champs
ajoutés à la spécification sont signalés en commentaire.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Statut(StrEnum):
    collectee = "collectee"
    filtree = "filtree"
    scoree = "scoree"
    enquetee = "enquetee"
    redigee = "redigee"
    en_critique = "en_critique"
    validee = "validee"
    en_attente_humain = "en_attente_humain"
    envoyee = "envoyee"
    relancee = "relancee"
    entretien = "entretien"
    refus = "refus"
    acceptee = "acceptee"
    abandonnee = "abandonnee"
    erreur = "erreur"


STATUTS_TERMINAUX = {Statut.refus, Statut.acceptee, Statut.abandonnee}


# --------------------------------------------------------------------------
# Offre
# --------------------------------------------------------------------------


class Periode(BaseModel):
    debut: date | None = None
    fin: date | None = None


class Offre(BaseModel):
    intitule: str
    entreprise: str
    lieu: str = ""
    dates: Periode = Field(default_factory=Periode)
    description: str = ""
    url: str = ""
    source: str = ""
    date_collecte: datetime = Field(default_factory=datetime.now)
    # Ajouts : utiles au filtrage et à l'envoi.
    type: Literal["offre", "spontanee"] = "offre"
    contact_email: str | None = None
    gratification: float | None = None  # euros par mois
    duree_mois: float | None = None
    teletravail: bool | None = None
    secteur: str | None = None
    date_publication: date | None = None
    sources_fusionnees: list[str] = Field(default_factory=list)
    collecteur: str = ""  # source de l'agent Veille qui l'a collectée (bandit)
    # Établissement visé (annuaire) : sert à juger un dépôt en présentiel.
    adresse: str = ""
    latitude: float | None = None
    longitude: float | None = None
    tranche_effectif: str | None = None  # code INSEE


# --------------------------------------------------------------------------
# Blocs du dossier
# --------------------------------------------------------------------------


class Score(BaseModel):
    valeur: float = 0.0  # 0-100
    confiance: float = 0.0  # 0-1
    justification: str = ""
    details: dict[str, float] = Field(default_factory=dict)  # ajout : sous-scores


class FaitSource(BaseModel):
    texte: str
    source: str  # URL


class FicheEntreprise(BaseModel):
    activite: str = ""
    actualites: list[str] = Field(default_factory=list)
    contact: str = ""
    sources: list[str] = Field(default_factory=list)
    # Ajouts
    faits: list[FaitSource] = Field(default_factory=list)
    minimale: bool = False
    siren: str | None = None
    effectif: str | None = None
    secteur: str | None = None
    page_carriere: str = ""  # page carrières ou formulaire de candidature (pour le dépôt assisté)


class Affirmation(BaseModel):
    """Phrase factuelle d'un document et les éléments du profil qui la fondent."""

    texte: str
    sources: list[str]  # identifiants d'éléments du profil ou "fiche:N"


class Documents(BaseModel):
    cv: str | None = None  # chemin du CV adapté
    lettre: str | None = None  # chemin de la lettre
    mail: str = ""
    version: int = 0
    # Ajouts
    objet_mail: str = ""
    format: Literal["lettre", "mail", "formulaire"] = "lettre"
    angle: str = ""
    texte_lettre: str = ""
    affirmations: list[Affirmation] = Field(default_factory=list)
    experiences_retenues: list[str] = Field(default_factory=list)
    piece_jointe_cv: str | None = None  # PDF joint à l'envoi
    reponses_formulaire: dict[str, str] = Field(default_factory=dict)


TypeProbleme = Literal["invente", "exagere", "generique", "placeholder", "forme", "incoherence"]
Gravite = Literal["bloquant", "majeur", "mineur"]


class Probleme(BaseModel):
    type: TypeProbleme
    gravite: Gravite
    extrait: str = ""
    raison: str = ""
    correction: str = ""


DecisionCritique = Literal["valider", "renvoyer", "escalader", "abandonner"]


class Critique(BaseModel):
    note: float = 0.0
    decision: str = ""
    iteration: int = 0
    problemes: list[Probleme] = Field(default_factory=list)
    details: dict[str, float] = Field(default_factory=dict)  # ajout : note par critère
    question_humain: str = ""  # ajout : question précise en cas d'escalade


class Envoi(BaseModel):
    canal: Literal["email", "formulaire", "plateforme"] | None = None
    horodatage: datetime | None = None
    preuve: str = ""
    # Ajouts
    destinataire: str = ""
    prevu_le: datetime | None = None
    automatique: bool = False


class Suivi(BaseModel):
    relances: list[datetime] = Field(default_factory=list)
    reponse: Literal["aucune", "refus", "entretien", "demande_info"] = "aucune"
    date_reponse: datetime | None = None
    # Ajouts
    entretien_le: datetime | None = None
    fiche_preparation: str | None = None


class EntreeHistorique(BaseModel):
    agent: str
    action: str
    horodatage: datetime = Field(default_factory=datetime.now)
    entrees_hash: str = ""


class Presentiel(BaseModel):
    """Vaut-il la peine d'apporter la candidature en main propre ?"""
    conseille: bool = False
    distance_km: float | None = None
    raison: str = ""
    adresse: str = ""
    depose_le: datetime | None = None  # passé la déposer (tournée)


class Dossier(BaseModel):
    id: str
    statut: Statut = Statut.collectee
    offre: Offre
    score: Score = Field(default_factory=Score)
    fiche_entreprise: FicheEntreprise = Field(default_factory=FicheEntreprise)
    documents: Documents = Field(default_factory=Documents)
    critique: Critique = Field(default_factory=Critique)
    envoi: Envoi = Field(default_factory=Envoi)
    suivi: Suivi = Field(default_factory=Suivi)
    historique: list[EntreeHistorique] = Field(default_factory=list)
    # Ajouts
    motif: str = ""  # raison du dernier abandon ou de la dernière escalade
    prioritaire: bool = False
    variante: str = ""  # bras A/B attribué par l'Apprentissage
    modifie_par_humain: bool = False
    etape_attente: str = ""  # étape bloquée sur l'humain : scoring, enquete, critique, envoi, suivi
    valide_humain: list[str] = Field(default_factory=list)  # étapes débloquées par l'humain
    consigne_humain: str = ""  # demande de l'humain pour la dernière réécriture
    presentiel: Presentiel | None = None  # None : pas encore évalué

    def tracer(self, agent: str, action: str, entrees_hash: str = "") -> None:
        self.historique.append(EntreeHistorique(agent=agent, action=action, entrees_hash=entrees_hash))


# --------------------------------------------------------------------------
# Profil du candidat
# --------------------------------------------------------------------------


class Identite(BaseModel):
    prenom: str = ""
    nom: str = ""
    email: str = ""
    telephone: str = ""
    ville: str = ""


class Formation(BaseModel):
    id: str
    diplome: str
    etablissement: str = ""
    debut: str = ""
    fin: str = ""
    details: str = ""


class Experience(BaseModel):
    id: str
    intitule: str
    structure: str = ""
    debut: str = ""
    fin: str = ""
    missions: list[str] = Field(default_factory=list)
    competences: list[str] = Field(default_factory=list)


class Competence(BaseModel):
    id: str
    nom: str  # forme normalisée
    categorie: str = ""
    niveau: str | None = None  # uniquement si le document source l'indique
    preuves: list[str] = Field(default_factory=list)  # identifiants d'expériences ou de projets


class Langue(BaseModel):
    id: str
    langue: str
    niveau: str = ""


class Projet(BaseModel):
    id: str
    titre: str
    description: str = ""
    competences: list[str] = Field(default_factory=list)


class ProfilStructure(BaseModel):
    identite: Identite = Field(default_factory=Identite)
    formations: list[Formation] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    competences: list[Competence] = Field(default_factory=list)
    langues: list[Langue] = Field(default_factory=list)
    projets: list[Projet] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    centres_interet: list[str] = Field(default_factory=list)
    cv_pdf: str | None = None  # CV d'origine, joint par défaut

    def identifiants(self) -> set[str]:
        ids: set[str] = set()
        for groupe in (self.formations, self.experiences, self.competences, self.langues, self.projets):
            ids.update(e.id for e in groupe)
        return ids | {"identite"}

    def element(self, identifiant: str) -> BaseModel | None:
        for groupe in (self.formations, self.experiences, self.competences, self.langues, self.projets):
            for e in groupe:
                if e.id == identifiant:
                    return e
        return self.identite if identifiant == "identite" else None


class CriteresDurs(BaseModel):
    disponible_du: date | None = None
    disponible_au: date | None = None
    duree_min_mois: float | None = None
    duree_max_mois: float | None = None
    lieux: list[str] = Field(default_factory=list)  # villes, départements, régions
    accepte_teletravail_complet: bool = True
    gratification_min: float | None = None
    entreprises_exclues: list[str] = Field(default_factory=list)
    mots_exclus: list[str] = Field(default_factory=list)


class CritereSouple(BaseModel):
    nom: str
    type: Literal["mots_cles", "secteur", "lieu_prefere", "teletravail", "gratification"]
    valeurs: list[str] = Field(default_factory=list)
    poids: float = 1.0


class GrilleCriteres(BaseModel):
    durs: CriteresDurs = Field(default_factory=CriteresDurs)
    souples: list[CritereSouple] = Field(default_factory=list)
    requetes: list[str] = Field(default_factory=list)  # mots-clés de recherche
    entreprises_prioritaires: list[str] = Field(default_factory=list)
    type_stage: str = ""
    deadline: date | None = None
