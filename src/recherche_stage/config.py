"""Configuration : valeurs par défaut de la spécification, surchargées par
`config.yaml`. Les secrets ne sont jamais écrits dans le fichier : il ne
contient que le nom des variables d'environnement qui les portent."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


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
    gemini_modele: str = "gemini-2.5-pro"
    gemini_modele_critique: str = "gemini-2.5-pro"
    gemini_cle_env: str = "GEMINI_API_KEY"
    temperature_critique: float = 0.1  # appliquée par Gemini ; les modèles Claude actuels n'en acceptent pas


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
    nom: str
    ats: str  # greenhouse | lever | smartrecruiters
    identifiant: str


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
    annuaire_max: int = 20


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
