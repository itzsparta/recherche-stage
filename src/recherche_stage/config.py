"""Configuration : valeurs par défaut de la spécification, surchargées par
`config.yaml`. Les secrets ne sont jamais écrits dans le fichier : il ne
contient que le nom des variables d'environnement qui les portent."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class ParamsOrchestrateur(BaseModel):
    candidatures_max_par_semaine: int = 15
    seuil_score_minimum: float = 60
    iterations_critique_max: int = 2
    appels_api_max_par_semaine: int = 600
    seuil_confiance: float = 0.5


class ParamsAutonomie(BaseModel):
    fenetre: int = 30
    taux_requis: float = 0.95


class ParamsLLM(BaseModel):
    # Fournisseur des agents, et du Critique à part : « claude » ou « gemini ».
    # Un Critique sur un autre fournisseur que le rédacteur est vraiment indépendant.
    fournisseur: str = "claude"
    fournisseur_critique: str | None = None  # par défaut : le même que `fournisseur`
    modele: str = "claude-opus-5-5"
    # Spécification : modèle ou prompt distinct du rédacteur. Le prompt l'est
    # toujours ; le modèle peut l'être ici.
    modele_critique: str = "claude-opus-5-5"
    effort: str = "medium"
    effort_critique: str = "high"
    recherche_web: bool = True
    # Gemini (clé dans la variable d'environnement nommée ci-dessous)
    gemini_modele: str = "gemini-3.8-flash"
    gemini_modele_critique: str = "gemini-3.8-flash"
    gemini_modele_secours: str | None = "gemini-2.5-flash"  # si le modèle principal est saturé (503/429)
    gemini_cle_env: str = "GEMINI_API_KEY"
    temperature_critique: float = 0.1  # appliquée par Gemini 1.x et 2.x seulement ; Claude et Gemini 3+ la déconseillent


class ParamsVeille(BaseModel):
    echecs_avant_coupure: int = 3
    delais_reessai: list[float] = Field(default_factory=lambda: [2, 8, 30])
    intervalle_collecte_heures: float = 12
    offres_max_par_cycle: int = 60


class ParamsScoring(BaseModel):
    poids_regles: float = 0.4
    poids_lexical: float = 0.2
    poids_llm: float = 0.4
    jours_avant_expiration: int = 45


class ParamsCritique(BaseModel):
    seuil_validation: float = 80
    seuil_renvoi: float = 60
    mots_lettre: tuple[int, int] = (200, 450)
    mots_mail: tuple[int, int] = (50, 220)
    fautes_max: int = 2
    languagetool_url: str | None = None  # serveur LanguageTool, idéalement local


class ParamsSMTP(BaseModel):
    hote: str = ""
    port: int = 465
    utilisateur: str = ""
    mot_de_passe_env: str = "SMTP_MOT_DE_PASSE"
    expediteur: str = ""


class FenetreEnvoi(BaseModel):
    jours: list[int] = Field(default_factory=lambda: [0, 1, 2, 3])  # lundi = 0
    heure_debut: int = 8
    heure_fin: int = 11


class ParamsEnvoi(BaseModel):
    smtp: ParamsSMTP = Field(default_factory=ParamsSMTP)
    fenetre: FenetreEnvoi = Field(default_factory=FenetreEnvoi)


class ParamsIMAP(BaseModel):
    hote: str = ""
    port: int = 993
    utilisateur: str = ""
    mot_de_passe_env: str = "IMAP_MOT_DE_PASSE"
    dossier: str = "INBOX"


class ParamsSuivi(BaseModel):
    imap: ParamsIMAP = Field(default_factory=ParamsIMAP)
    relances_jours: list[int] = Field(default_factory=lambda: [7, 14])
    seuil_confiance_classification: float = 0.7


class EntrepriseATS(BaseModel):
    """Un site carrière. Donner soit `url` (page carrière hébergée par l'ATS),
    soit `ats` + `identifiant`."""

    nom: str
    ats: str = ""  # greenhouse | lever | lever_eu | smartrecruiters | workable | ashby | recruitee | personio
    identifiant: str = ""
    url: str = ""

    @model_validator(mode="after")
    def _depuis_url(self):
        if not (self.ats and self.identifiant):
            from .sources.ats import detecter_ats

            trouve = detecter_ats(self.url) if self.url else None
            if trouve is None:
                raise ValueError(
                    f"{self.nom} : ATS non reconnu{f' pour {self.url}' if self.url else ''}. "
                    "Préciser ats et identifiant, ou essayer : rs detecter-ats <url de la page carrière>"
                )
            self.ats, self.identifiant = trouve
        return self


class FluxRSS(BaseModel):
    nom: str  # entreprise ou site qui publie le flux
    url: str
    entreprise_fixe: bool = True  # False pour un agrégateur : l'entreprise est lue dans chaque article


class ParamsSources(BaseModel):
    france_travail: bool = False
    france_travail_client_id_env: str = "FT_CLIENT_ID"
    france_travail_secret_env: str = "FT_CLIENT_SECRET"
    import_fichier: bool = True
    dossier_imports: str = "imports"
    ats: list[EntrepriseATS] = Field(default_factory=list)
    annuaire: bool = False
    annuaire_codes_naf: list[str] = Field(default_factory=list)
    annuaire_departements: list[str] = Field(default_factory=list)
    annuaire_max: int = 20           # cibles nouvelles par cycle
    annuaire_effectif_min: int = 10  # salariés ; 0 : aucune limite
    # Agrégateur Adzuna (API gratuite, clés sur developer.adzuna.com)
    adzuna: bool = False
    adzuna_pays: str = "fr"
    adzuna_app_id_env: str = "ADZUNA_APP_ID"
    adzuna_app_key_env: str = "ADZUNA_APP_KEY"
    # Flux RSS/Atom publiés par des sites carrière ou des sites d'offres
    flux: list[FluxRSS] = Field(default_factory=list)
    # Alertes mail des plateformes (Indeed, JobTeaser…) lues dans la boîte IMAP du Suivi
    alertes_mail: bool = False
    alertes_plateformes: list[str] = Field(default_factory=lambda: ["indeed", "jobteaser", "wttj", "hellowork", "apec"])
    alertes_dossier: str = ""  # dossier ou libellé IMAP des alertes ; par défaut celui du Suivi
    alertes_jours: int = 7


class Config(BaseModel):
    donnees: str = "donnees"
    orchestrateur: ParamsOrchestrateur = Field(default_factory=ParamsOrchestrateur)
    autonomie: ParamsAutonomie = Field(default_factory=ParamsAutonomie)
    llm: ParamsLLM = Field(default_factory=ParamsLLM)
    veille: ParamsVeille = Field(default_factory=ParamsVeille)
    scoring: ParamsScoring = Field(default_factory=ParamsScoring)
    critique: ParamsCritique = Field(default_factory=ParamsCritique)
    envoi: ParamsEnvoi = Field(default_factory=ParamsEnvoi)
    suivi: ParamsSuivi = Field(default_factory=ParamsSuivi)
    sources: ParamsSources = Field(default_factory=ParamsSources)
    conservation_mois: int = 12

    @property
    def dossier_donnees(self) -> Path:
        return Path(self.donnees)

    @property
    def chemin_base(self) -> Path:
        return self.dossier_donnees / "recherche_stage.db"


def charger(chemin: str | Path = "config.yaml") -> Config:
    chemin = Path(chemin)
    if not chemin.exists():
        return Config()
    contenu = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    config = Config.model_validate(contenu)
    # Les chemins relatifs du fichier sont relatifs au fichier lui-même.
    base = chemin.parent
    if not Path(config.donnees).is_absolute():
        config.donnees = str(base / config.donnees)
    if not Path(config.sources.dossier_imports).is_absolute():
        config.sources.dossier_imports = str(base / config.sources.dossier_imports)
    return config


def secret(nom_variable: str) -> str:
    valeur = os.environ.get(nom_variable, "")
    if not valeur:
        raise RuntimeError(f"Variable d'environnement {nom_variable} absente")
    return valeur
