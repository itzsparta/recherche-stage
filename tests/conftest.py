"""Faux composants et jeux de données communs. Aucun appel réseau ni LLM réel."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from recherche_stage.agents.base import Contexte
from recherche_stage.canaux.messagerie import MessageRecu
from recherche_stage.config import Config
from recherche_stage.modeles import (
    Competence,
    CriteresDurs,
    CritereSouple,
    Experience,
    Formation,
    GrilleCriteres,
    Identite,
    Offre,
    Periode,
    ProfilStructure,
)
from recherche_stage.store import Store

MAINTENANT = datetime(2026, 10, 5, 9, 0)  # un lundi, dans la fenêtre d'envoi


class Horloge:
    def __init__(self, instant: datetime = MAINTENANT):
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


class FauxLLM:
    """Réponses programmées par agent : un dict, une liste consommée dans
    l'ordre, ou une fonction (contenu, schema) -> dict."""

    def __init__(self, reponses: dict | None = None, recherche: tuple[str, list[str]] = ("", [])):
        self.reponses = reponses or {}
        self.recherche = recherche
        self.appels: list[tuple[str, str, bool]] = []

    def structurer(self, agent, systeme, contenu, schema, critique=False):
        self.appels.append((agent, contenu, critique))
        reponse = self.reponses[agent]
        if isinstance(reponse, list):
            reponse = reponse.pop(0) if len(reponse) > 1 else reponse[0]
        if callable(reponse):
            reponse = reponse(contenu, schema)
        return schema.model_validate(reponse)

    def rechercher(self, agent, systeme, requete):
        self.appels.append((agent, requete, False))
        return self.recherche

    def nb(self, agent: str) -> int:
        return sum(1 for a in self.appels if a[0] == agent)


class FauxHttp:
    def __init__(self, json_par_url: dict | None = None, mortes: set[str] = frozenset(), texte_par_url: dict | None = None):
        self.json_par_url = json_par_url or {}
        self.texte_par_url = texte_par_url or {}
        self.mortes = mortes
        self.requetes: list[tuple[str, dict | None]] = []

    def get_json(self, url, params=None, entetes=None):
        self.requetes.append((url, params))
        for motif, reponse in self.json_par_url.items():
            if motif in url:
                return reponse
        return {}

    def get_texte(self, url, params=None, entetes=None):
        self.requetes.append((url, params))
        for motif, reponse in self.texte_par_url.items():
            if motif in url:
                return reponse
        return ""

    def post_form(self, url, donnees, entetes=None):
        self.requetes.append((url, donnees))
        return {"access_token": "jeton"}

    def accessible(self, url):
        return url not in self.mortes


class FauxSource:
    def __init__(self, nom: str, offres: list[Offre] | None = None, echecs: int = 0):
        self.nom = nom
        self.offres = offres or []
        self.echecs = echecs
        self.appels: list[int] = []

    def collecter(self, grille, limite):
        self.appels.append(limite)
        if self.echecs:
            self.echecs -= 1
            raise ConnectionError(f"{self.nom} indisponible")
        return [o.model_copy(deep=True) for o in self.offres][:limite]


class FauxExpediteur:
    def __init__(self, echecs: int = 0):
        self.echecs = echecs
        self.envoyes = []

    def envoyer(self, message):
        if self.echecs:
            self.echecs -= 1
            import smtplib

            raise smtplib.SMTPServerDisconnected("coupure")
        self.envoyes.append(message)
        return message["Message-ID"]


class FausseBoite:
    def __init__(self, messages: list[MessageRecu] | None = None):
        self.messages = messages or []

    def messages_depuis(self, depuis):
        return list(self.messages)


# ------------------------------------------------------------------ données


def offre(**champs) -> Offre:
    base = dict(
        intitule="Stage maintenance industrielle H/F",
        entreprise="Exemple Énergie",
        lieu="Dunkerque (59)",
        dates=Periode(debut=date(2027, 4, 6), fin=date(2027, 6, 25)),
        description=(
            "Au sein du service maintenance de notre site de production d'énergie, vous participerez à l'analyse "
            "des pannes, à la mise à jour de la GMAO et au suivi des plans de maintenance préventive. Vous serez "
            "accompagné par un technicien méthodes et travaillerez avec les équipes d'exploitation sur la fiabilité "
            "des équipements tournants."
        ),
        url="https://exemple-energie.fr/offres/1",
        source="jobteaser",
        contact_email="recrutement@exemple-energie.fr",
        date_publication=date(2026, 10, 1),
        date_collecte=MAINTENANT,
    )
    base.update(champs)
    return Offre(**base)


def profil(cv_pdf: str | None = None) -> ProfilStructure:
    return ProfilStructure(
        identite=Identite(prenom="Camille", nom="Martin", email="camille.martin@exemple.fr", telephone="06 12 34 56 78", ville="Boulogne-sur-Mer"),
        formations=[Formation(id="form1", diplome="BUT Génie Industriel et Maintenance", etablissement="IUT du Littoral")],
        experiences=[
            Experience(id="exp1", intitule="Stage technicien maintenance", structure="Usine Exemple",
                       missions=["maintenance préventive des convoyeurs", "mise à jour de la GMAO"], competences=["GMAO"]),
        ],
        competences=[
            Competence(id="comp_gmao", nom="GMAO", preuves=["exp1"]),
            Competence(id="comp_electrotechnique", nom="électrotechnique"),
        ],
        cv_pdf=cv_pdf,
    )


def grille() -> GrilleCriteres:
    return GrilleCriteres(
        durs=CriteresDurs(
            disponible_du=date(2027, 4, 6), disponible_au=date(2027, 6, 25), duree_min_mois=2, duree_max_mois=3,
            lieux=["59", "62", "Lille"], mots_exclus=["commercial"],
        ),
        souples=[
            CritereSouple(nom="domaine", type="mots_cles", valeurs=["maintenance", "GMAO", "énergie"], poids=0.6),
            CritereSouple(nom="secteur", type="secteur", valeurs=["énergie"], poids=0.4),
        ],
        requetes=["maintenance"],
        type_stage="stage maintenance",
    )


# --------------------------------------------------------- réponses du LLM


def evaluation_offre(couverte="oui", preuve="exp1", certitude="haute", suspecte=False) -> dict:
    return {
        "exigences": [
            {"exigence": "maintenance préventive", "obligatoire": True, "couverte": couverte, "preuve": preuve},
            {"exigence": "GMAO", "obligatoire": False, "couverte": couverte, "preuve": "comp_gmao" if couverte != "non" else ""},
        ],
        "suspecte": suspecte,
        "raison_suspecte": "arnaque" if suspecte else "",
        "informations_manquantes": [],
        "certitude": certitude,
        "justification": "Profil aligné sur la maintenance préventive et la GMAO.",
    }


def lettre(entreprise="Exemple Énergie", intitule="Stage maintenance industrielle", extra="") -> str:
    paragraphe = (
        "Mon stage de technicien maintenance m'a conduit à assurer la maintenance préventive des convoyeurs "
        "et à mettre à jour la GMAO de l'usine, deux missions au cœur du poste proposé. "
    )
    corps = paragraphe * 7
    return (
        f"Madame, Monsieur,\n\nVotre offre « {intitule} » chez {entreprise} demande un suivi rigoureux des plans "
        f"de maintenance préventive. {extra}\n\n{corps}\n\nÉtudiant en BUT Génie Industriel et Maintenance, je suis "
        f"disponible du 6 avril 2027 au 25 juin 2027.\n\nCamille Martin\ncamille.martin@exemple.fr\n06 12 34 56 78"
    )


def sortie_redaction(lettre_texte: str | None = None, mail: str | None = None, sources=("exp1",)) -> dict:
    texte_mail = mail or (
        "Bonjour,\n\nVeuillez trouver ci-joint ma candidature au poste « Stage maintenance industrielle » chez "
        "Exemple Énergie : mon CV et ma lettre de motivation. Mon stage en maintenance préventive et ma pratique de "
        "la GMAO correspondent aux missions décrites dans votre offre.\n\nBien cordialement,\nCamille Martin\n"
        "camille.martin@exemple.fr\n06 12 34 56 78"
    )
    return {
        "angle": "experience_terrain",
        "experiences_retenues": ["exp1"],
        "objet_mail": "Candidature : Stage maintenance industrielle",
        "mail": texte_mail,
        "lettre": lettre_texte if lettre_texte is not None else lettre(),
        "affirmations": [{"texte": "maintenance préventive des convoyeurs", "sources": list(sources)}],
        "titre_cv": "Étudiant en BUT GIM",
        "accroche_cv": "Maintenance préventive et GMAO en usine.",
        "competences_cv": ["comp_gmao"],
        "reponses_formulaire": [],
    }


def evaluation_critique(**champs) -> dict:
    base = {
        "affirmations": [{"affirmation": "maintenance préventive des convoyeurs", "ancree": True, "exageree": False, "commentaire": ""}],
        "phrases_factuelles_non_declarees": [],
        "infos_entreprise_hors_fiche": [],
        "exigences_cles": [
            {"exigence": "maintenance préventive", "adressee": True, "extrait": "maintenance préventive"},
            {"exigence": "GMAO", "adressee": True, "extrait": "GMAO"},
            {"exigence": "fiabilité", "adressee": True, "extrait": "suivi rigoureux"},
        ],
        "substituable": False,
        "accroche_concrete": True,
        "profil_trop_eloigne": False,
        "fautes": [],
        "registre_adapte": True,
        "coherent_cv_lettre": True,
        "formules_creuses": [],
        "cas_ambigu": False,
        "question_ambigu": "",
    }
    base.update(champs)
    return base


def classification(categorie="refus", confiance=0.95, date_entretien="") -> dict:
    return {"concerne_candidature": True, "categorie": categorie, "confiance": confiance, "date_entretien": date_entretien, "resume": "réponse"}


def preparation() -> dict:
    return {
        "points_cles_entreprise": ["Producteur d'énergie"],
        "questions_probables": [{"question": "Parlez-moi de votre stage", "elements_du_profil": ["exp1"], "piste_de_reponse": "Convoyeurs, GMAO"}],
        "questions_a_poser": ["Quels équipements ?"],
        "a_verifier": ["Adresse du site"],
    }


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def horloge():
    return Horloge()


@pytest.fixture
def cv_pdf(tmp_path) -> Path:
    chemin = tmp_path / "cv.pdf"
    chemin.write_bytes(b"%PDF-1.4 faux cv")
    return chemin


@pytest.fixture
def ctx(tmp_path, horloge, cv_pdf):
    store = Store(":memory:", horloge=horloge)
    config = Config(donnees=str(tmp_path / "donnees"))
    contexte = Contexte(store=store, config=config, llm=FauxLLM(), http=FauxHttp())
    store.definir_parametre("profil", profil(str(cv_pdf)))
    store.definir_parametre("grille", grille())
    return contexte
