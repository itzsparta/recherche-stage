"""Accès au modèle de langage.

Les agents ne voient que l'interface `LLM` : une sortie structurée validée par
un schéma Pydantic, et une recherche web sourcée. `ClaudeLLM` l'implémente
avec l'API Claude ; les tests utilisent un faux.

Chaque appel est compté dans la base, et refusé au-delà du budget
hebdomadaire fixé par l'orchestrateur.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from .config import ParamsLLM
from .store import Store

T = TypeVar("T", bound=BaseModel)

BETA_FALLBACK = "server-side-fallback-2026-07-01"


class ErreurLLM(RuntimeError):
    pass


class BudgetEpuise(RuntimeError):
    pass


class LLM(Protocol):
    def structurer(self, agent: str, systeme: str, contenu: str, schema: type[T], critique: bool = False) -> T: ...

    def rechercher(self, agent: str, systeme: str, requete: str) -> tuple[str, list[str]]:
        """Renvoie le texte de synthèse et la liste des URL réellement consultées."""
        ...


class ClaudeLLM:
    def __init__(
        self,
        params: ParamsLLM,
        store: Store,
        budget_hebdo: int,
        client: anthropic.Anthropic | None = None,
    ):
        self.params = params
        self.store = store
        self.budget_hebdo = budget_hebdo
        self.client = client or anthropic.Anthropic()

    def _verifier_budget(self) -> None:
        if self.store.appels_llm_semaine() >= self.budget_hebdo:
            raise BudgetEpuise(f"Budget de {self.budget_hebdo} appels LLM atteint cette semaine")

    def _compter(self, agent: str, reponse) -> None:
        usage = reponse.usage
        self.store.compter_appel_llm(agent, reponse.model, usage.input_tokens, usage.output_tokens)

    @staticmethod
    def _controler_arret(reponse) -> None:
        if reponse.stop_reason == "refusal":
            raise ErreurLLM("Le modèle a décliné la requête")
        if reponse.stop_reason == "max_tokens":
            raise ErreurLLM("Réponse tronquée (max_tokens atteint)")

    def structurer(self, agent: str, systeme: str, contenu: str, schema: type[T], critique: bool = False) -> T:
        self._verifier_budget()
        modele = self.params.modele_critique if critique else self.params.modele
        effort = self.params.effort_critique if critique else self.params.effort
        try:
            reponse = self.client.beta.messages.parse(
                model=modele,
                max_tokens=16000,
                # Le prompt système est stable par agent : on le met en cache.
                system=[{"type": "text", "text": systeme, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": contenu}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[BETA_FALLBACK],
                fallbacks="default",
            )
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            raise ErreurLLM(f"{agent} : appel LLM en échec ({e.__class__.__name__})") from e
        self._compter(agent, reponse)
        self._controler_arret(reponse)
        if reponse.parsed_output is None:
            raise ErreurLLM(f"{agent} : sortie structurée absente")
        return reponse.parsed_output

    def rechercher(self, agent: str, systeme: str, requete: str) -> tuple[str, list[str]]:
        self._verifier_budget()
        messages: list = [{"role": "user", "content": requete}]
        textes: list[str] = []
        urls: list[str] = []
        for _ in range(3):  # pause_turn : la recherche peut demander une reprise
            try:
                reponse = self.client.beta.messages.create(
                    model=self.params.modele,
                    max_tokens=16000,
                    system=systeme,
                    messages=messages,
                    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                    output_config={"effort": "low"},
                    betas=[BETA_FALLBACK],
                    fallbacks="default",
                )
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                raise ErreurLLM(f"{agent} : recherche web en échec ({e.__class__.__name__})") from e
            self._compter(agent, reponse)
            self._controler_arret(reponse)
            for bloc in reponse.content:
                if bloc.type == "web_search_tool_result" and isinstance(bloc.content, list):
                    urls.extend(r.url for r in bloc.content if getattr(r, "url", None))
                elif bloc.type == "text":
                    textes.append(bloc.text)
                    for citation in getattr(bloc, "citations", None) or []:
                        if getattr(citation, "url", None):
                            urls.append(citation.url)
            if reponse.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": reponse.content}]
        return "\n".join(textes), list(dict.fromkeys(urls))
