"""Accès à Claude par la commande `claude` de Claude Code, en mode non
interactif (`claude -p`) : les appels passent par le compte Claude connecté
(abonnement Pro ou Max), sans clé d'API. Même interface `LLM` que `ClaudeLLM`.

Chaque appel lance `claude -p` dans un dossier temporaire vide (aucun
CLAUDE.md ni réglage de projet), sans outil pour les sorties structurées et
avec WebSearch / WebFetch seulement pour la recherche. Le contenu passe par
l'entrée standard et le prompt système par fichier : aucun texte libre en
argument de commande.

Recherche web : les URL retenues sont celles que les outils ont réellement
renvoyées (résultats de recherche, pages lues sans erreur), pas celles du
texte généré.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .config import ParamsLLM
from .llm import BudgetEpuise, ErreurLLM, NonConnecte, QuotaEpuise
from .store import Store

T = TypeVar("T", bound=BaseModel)

# Une clé d'API ou un jeton dans l'environnement passerait avant le compte
# connecté (abonnement) : on ne les transmet pas à `claude`.
VARIABLES_ECARTEES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

CONSIGNE_JSON = """

FORMAT DE RÉPONSE : uniquement un objet JSON conforme à ce schéma, sans texte autour ni bloc de code.
{schema}"""


# Chaque recherche et chaque page lue sont facturées sur l'abonnement.
BUDGET_RECHERCHE = """

BUDGET : 3 recherches web et 2 pages lues au plus. Arrête-toi dès que tu as l'activité,
deux ou trois faits récents sourcés et, si elle existe, une adresse de contact."""


def _json_dans(texte: str) -> str:
    """Retire un éventuel bloc ```json … ``` autour de la réponse."""
    bloc = re.search(r"```(?:json)?\s*(.*?)```", texte, re.S)
    return (bloc.group(1) if bloc else texte).strip()


class ClaudeCodeLLM:
    def __init__(self, params: ParamsLLM, store: Store, budget_hebdo: int, executer=subprocess.run):
        self.params = params
        self.store = store
        self.budget_hebdo = budget_hebdo
        self.executer = executer
        self.commande = shutil.which(params.claude_code_commande) or params.claude_code_commande

    def _verifier_budget(self) -> None:
        if self.store.appels_llm_semaine() >= self.budget_hebdo:
            raise BudgetEpuise(f"Budget de {self.budget_hebdo} appels LLM atteint cette semaine")

    def _modele(self, agent: str, critique: bool = False) -> str:
        defaut = self.params.claude_code_modele_critique if critique else self.params.claude_code_modele
        return self.params.claude_code_modele_par_agent.get(agent, defaut)

    def _lancer(self, agent: str, modele: str, systeme: str, contenu: str, options: list[str], flux: bool = False):
        """Lance `claude -p` ; renvoie les événements (mode flux) et l'événement final."""
        self._verifier_budget()
        with tempfile.TemporaryDirectory(prefix="rs-claude-") as dossier:
            fichier = Path(dossier) / "systeme.txt"
            fichier.write_text(systeme, encoding="utf-8")
            commande = [
                self.commande, "-p",
                "--output-format", "stream-json" if flux else "json",
                "--system-prompt-file", str(fichier),
                "--model", modele,
                "--no-session-persistence",
                "--strict-mcp-config",
                # Ni CLAUDE.md, ni skills, ni hooks ou plugins de l'utilisateur : autant
                # de jetons en moins à chaque appel.
                "--setting-sources", "project",
                "--disable-slash-commands",
                *options,
            ]
            if flux:
                commande.append("--verbose")
            try:
                fini = self.executer(
                    commande, input=contenu, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.params.claude_code_delai_secondes, cwd=dossier,
                    env={k: v for k, v in os.environ.items() if k not in VARIABLES_ECARTEES},
                )
            except FileNotFoundError as e:
                raise ErreurLLM(
                    f"{agent} : commande « {self.params.claude_code_commande} » introuvable. Installer Claude Code "
                    "(https://claude.com/claude-code), lancer `claude` une fois pour se connecter, puis réessayer"
                ) from e
            except subprocess.TimeoutExpired as e:
                raise ErreurLLM(f"{agent} : Claude Code trop lent (> {self.params.claude_code_delai_secondes:.0f} s)") from e

        evenements = []
        for ligne in (fini.stdout or "").splitlines():
            if ligne.strip().startswith("{"):
                try:
                    evenements.append(json.loads(ligne))
                except json.JSONDecodeError:
                    continue
        final = next((e for e in reversed(evenements) if e.get("type") == "result"), None)
        if final is None:
            sortie = (fini.stderr or fini.stdout or "").strip()[:300]
            raise ErreurLLM(f"{agent} : Claude Code n'a pas répondu (code {fini.returncode}) : {sortie}")

        usage = final.get("usage") or {}
        self.store.compter_appel_llm(agent, modele, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if final.get("is_error") or final.get("subtype") != "success":
            message = str(final.get("result") or final.get("subtype") or "")
            bas = message.lower()
            if final.get("api_error_status") == 429 or "limit" in bas:
                raise QuotaEpuise(f"Limite d'usage de l'abonnement Claude atteinte [{message[:200]}]")
            if final.get("api_error_status") in (401, 403) or "login" in bas or "authenticat" in bas:
                raise NonConnecte(f"{agent} : Claude Code non connecté [{message[:200]}]. Lancer `claude` puis /login")
            raise ErreurLLM(f"{agent} : Claude Code en échec [{message[:300]}]")
        return evenements, final

    def structurer(self, agent: str, systeme: str, contenu: str, schema: type[T], critique: bool = False) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        modele = self._modele(agent, critique)
        options = ["--tools", "", "--effort", self.params.claude_code_effort_critique if critique else self.params.claude_code_effort]
        # Un .exe reçoit le schéma intact ; un script .cmd (installation npm sous
        # Windows) le déformerait : il reste alors dans le prompt système seul.
        if not self.commande.lower().endswith((".cmd", ".bat")):
            options += ["--json-schema", json.dumps(schema.model_json_schema())]
        _, final = self._lancer(agent, modele, systeme + CONSIGNE_JSON.format(schema=schema_json), contenu, options)
        try:
            if final.get("structured_output") is not None:
                return schema.model_validate(final["structured_output"])
            return schema.model_validate_json(_json_dans(str(final.get("result") or "")))
        except ValidationError as e:
            raise ErreurLLM(f"{agent} : sortie Claude Code non conforme au schéma") from e

    def rechercher(self, agent: str, systeme: str, requete: str) -> tuple[str, list[str]]:
        options = ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch", "--effort", "low"]
        evenements, final = self._lancer(agent, self._modele(agent), systeme + BUDGET_RECHERCHE, requete, options, flux=True)
        urls: list[str] = []
        lectures: dict[str, str] = {}  # id de l'appel WebFetch -> URL demandée
        for e in evenements:
            contenu = (e.get("message") or {}).get("content")
            if not isinstance(contenu, list):
                continue
            for bloc in contenu:
                if bloc.get("type") == "tool_use" and bloc.get("name") == "WebFetch":
                    lectures[bloc.get("id", "")] = (bloc.get("input") or {}).get("url", "")
                elif bloc.get("type") == "tool_result" and not bloc.get("is_error"):
                    if bloc.get("tool_use_id") in lectures:
                        urls.append(lectures[bloc["tool_use_id"]])
            resultat: Any = e.get("tool_use_result")
            if isinstance(resultat, dict):
                for groupe in resultat.get("results") or []:
                    for lien in (groupe.get("content") if isinstance(groupe, dict) else None) or []:
                        if isinstance(lien, dict) and lien.get("url"):
                            urls.append(lien["url"])
        return str(final.get("result") or ""), list(dict.fromkeys(u for u in urls if u))
