"""Accès à Gemini (API Google AI, SDK `google-genai`), même interface `LLM`
que `ClaudeLLM`, et routeur qui choisit le fournisseur de chaque appel.

Sorties structurées : schéma JSON du modèle Pydantic passé en
`response_json_schema`, puis validation Pydantic de la réponse.
Recherche web : ancrage Google Search ; les URL consultées viennent des
métadonnées d'ancrage, pas du texte généré.
"""

from __future__ import annotations

import re

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import ParamsLLM, secret
from .llm import LLM, BudgetEpuise, ErreurLLM
from .store import Store

T = TypeVar("T", bound=BaseModel)

ARRETS_BLOQUES = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class GeminiLLM:
    def __init__(self, params: ParamsLLM, store: Store, budget_hebdo: int, client=None):
        self.params = params
        self.store = store
        self.budget_hebdo = budget_hebdo
        if client is None:
            from google import genai

            # Gemini répond souvent 503 (surcharge) ou 429 : le SDK ne réessaie pas par défaut.
            client = genai.Client(
                api_key=secret(params.gemini_cle_env),
                http_options={"retry_options": {"attempts": 5, "initial_delay": 2, "max_delay": 30}},
            )
        self.client = client

    def _verifier_budget(self) -> None:
        if self.store.appels_llm_semaine() >= self.budget_hebdo:
            raise BudgetEpuise(f"Budget de {self.budget_hebdo} appels LLM atteint cette semaine")

    def _generer(self, agent: str, modele: str, contenu: str, config: dict):
        from google.genai import errors

        self._verifier_budget()
        config = {**config, "automatic_function_calling": {"disable": True}}  # aucun outil Python à appeler
        try:
            reponse = self.client.models.generate_content(model=modele, contents=contenu, config=config)
        except errors.APIError as e:
            if e.code in (400, 401, 403) and "key" in str(e.message or "").lower():
                raise ErreurLLM(f"{agent} : clé Gemini refusée ({e.code}). Vérifier {self.params.gemini_cle_env}") from e
            if e.code in (429, 503):
                raise ErreurLLM(
                    f"{agent} : Gemini surchargé ou quota atteint ({e.code}) malgré 5 essais. "
                    "Relancer dans quelques minutes"
                ) from e
            raise ErreurLLM(f"{agent} : appel Gemini en échec ({e.code})") from e
        usage = reponse.usage_metadata
        self.store.compter_appel_llm(
            agent, modele, (usage.prompt_token_count or 0) if usage else 0, (usage.candidates_token_count or 0) if usage else 0
        )
        if not reponse.candidates:
            raise ErreurLLM(f"{agent} : requête bloquée par Gemini")
        fin = reponse.candidates[0].finish_reason
        nom_fin = getattr(fin, "name", str(fin or ""))
        if nom_fin in ARRETS_BLOQUES:
            raise ErreurLLM(f"{agent} : réponse bloquée par Gemini ({nom_fin})")
        if nom_fin == "MAX_TOKENS":
            raise ErreurLLM(f"{agent} : réponse tronquée (max_output_tokens atteint)")
        return reponse

    def structurer(self, agent: str, systeme: str, contenu: str, schema: type[T], critique: bool = False) -> T:
        config = {
            "system_instruction": systeme,
            "response_mime_type": "application/json",
            "response_json_schema": schema.model_json_schema(),
            "max_output_tokens": 16000,
        }
        modele = self.params.gemini_modele_critique if critique else self.params.gemini_modele
        # Google déconseille d'abaisser la température des modèles Gemini 3 et
        # suivants (boucles, raisonnement dégradé) : réservée aux modèles 1.x et 2.x.
        if critique and re.match(r"gemini-[12][.-]", modele):
            config["temperature"] = self.params.temperature_critique
        reponse = self._generer(agent, modele, contenu, config)
        try:
            return schema.model_validate_json(reponse.text or "")
        except ValidationError as e:
            raise ErreurLLM(f"{agent} : sortie Gemini non conforme au schéma") from e

    def rechercher(self, agent: str, systeme: str, requete: str) -> tuple[str, list[str]]:
        config = {"system_instruction": systeme, "tools": [{"google_search": {}}], "max_output_tokens": 16000}
        reponse = self._generer(agent, self.params.gemini_modele, requete, config)
        metadonnees = reponse.candidates[0].grounding_metadata
        urls = [
            morceau.web.uri
            for morceau in (getattr(metadonnees, "grounding_chunks", None) or [])
            if getattr(morceau, "web", None) and morceau.web.uri
        ]
        return reponse.text or "", list(dict.fromkeys(urls))


class RouteurLLM:
    """Envoie les appels du Critique à un fournisseur, les autres à un autre."""

    def __init__(self, principal: LLM, critique: LLM):
        self.principal = principal
        self.critique = critique

    def structurer(self, agent: str, systeme: str, contenu: str, schema: type[T], critique: bool = False) -> T:
        cible = self.critique if critique else self.principal
        return cible.structurer(agent, systeme, contenu, schema, critique=critique)

    def rechercher(self, agent: str, systeme: str, requete: str) -> tuple[str, list[str]]:
        return self.principal.rechercher(agent, systeme, requete)


def construire_llm(params: ParamsLLM, store: Store, budget_hebdo: int) -> LLM:
    from .llm import ClaudeLLM

    fabriques = {"claude": ClaudeLLM, "gemini": GeminiLLM}
    noms = {params.fournisseur, params.fournisseur_critique or params.fournisseur}
    inconnus = noms - fabriques.keys()
    if inconnus:
        raise ValueError(f"Fournisseur LLM inconnu : {', '.join(sorted(inconnus))} (claude ou gemini)")
    instances = {nom: fabriques[nom](params, store, budget_hebdo) for nom in noms}
    principal = instances[params.fournisseur]
    critique = instances[params.fournisseur_critique or params.fournisseur]
    return principal if principal is critique else RouteurLLM(principal, critique)
