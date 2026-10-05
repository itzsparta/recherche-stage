"""ClaudeCodeLLM contre une fausse commande `claude -p`."""

import json
import subprocess

import pytest

from recherche_stage.agents.suivi import Classification
from recherche_stage.config import ParamsLLM
from recherche_stage.llm import ErreurLLM, QuotaEpuise
from recherche_stage.llm_claude_code import ClaudeCodeLLM
from recherche_stage.llm_gemini import construire_llm
from recherche_stage.store import Store

CLASSE = {"concerne_candidature": True, "categorie": "entretien", "confiance": 0.9, "date_entretien": "", "resume": "rdv"}


def resultat(**champs):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "",
            "usage": {"input_tokens": 100, "output_tokens": 20}} | champs


class FausseCommande:
    def __init__(self, *sorties):
        self.sorties = list(sorties)
        self.appels = []

    def __call__(self, commande, **kwargs):
        systeme = commande[commande.index("--system-prompt-file") + 1]
        with open(systeme, encoding="utf-8") as f:
            self.appels.append({"commande": commande, "systeme": f.read(), **kwargs})
        sortie = self.sorties.pop(0)
        if isinstance(sortie, BaseException):
            raise sortie
        lignes = sortie if isinstance(sortie, list) else [sortie]
        return subprocess.CompletedProcess(commande, 0, "\n".join(json.dumps(x) for x in lignes), "")


def llm(horloge, commande, **params):
    instance = ClaudeCodeLLM(ParamsLLM(fournisseur="claude_code", **params), Store(":memory:", horloge=horloge), 10, executer=commande)
    instance.commande = "claude"
    return instance


def test_sortie_structuree_sans_outil_ni_texte_libre_en_argument(horloge, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat-pas-une-cle")
    commande = FausseCommande(resultat(structured_output=CLASSE))
    instance = llm(horloge, commande)
    assert instance.structurer("suivi", "Classe ce mail.", "Bonjour, entretien mardi", Classification).categorie == "entretien"
    appel = commande.appels[0]
    args = appel["commande"]
    assert args[:2] == ["claude", "-p"] and args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5" and args[args.index("--effort") + 1] == "low"
    assert appel["input"] == "Bonjour, entretien mardi"  # contenu par l'entrée standard
    assert appel["systeme"].startswith("Classe ce mail.") and '"categorie"' in appel["systeme"]
    assert "--json-schema" in args
    assert args[args.index("--setting-sources") + 1] == "project" and "--disable-slash-commands" in args
    assert "ANTHROPIC_API_KEY" not in appel["env"]  # le compte connecté (abonnement) fait foi
    assert instance.store.appels_llm_semaine() == 1


def test_script_cmd_sans_json_schema_et_reponse_en_bloc_de_code(horloge):
    commande = FausseCommande(resultat(result="Voici :\n```json\n" + json.dumps(CLASSE) + "\n```"))
    instance = llm(horloge, commande)
    instance.commande = r"C:\Users\x\AppData\Roaming\npm\claude.CMD"
    assert instance.structurer("critique", "s", "c", Classification, critique=True).categorie == "entretien"
    args = commande.appels[0]["commande"]
    assert "--json-schema" not in args and args[args.index("--effort") + 1] == "medium"


def test_recherche_garde_les_url_renvoyees_par_les_outils(horloge):
    flux = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "…"}]},
         "tool_use_result": {"query": "x", "results": [{"content": [{"title": "a", "url": "https://a.fr/actu"}]}, "texte libre"]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": "https://b.fr/page"}},
            {"type": "tool_use", "id": "t3", "name": "WebFetch", "input": {"url": "https://c.fr/refus"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "contenu"},
            {"type": "tool_result", "tool_use_id": "t3", "content": "proxy refused", "is_error": True}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Voir https://invente.fr"}]}},
        resultat(result="Notes : activité… (https://invente.fr)"),
    ]
    commande = FausseCommande(flux)
    notes, urls = llm(horloge, commande, claude_code_modele_par_agent={"enquete": "claude-haiku-4-5"}).rechercher("enquete", "s", "Entreprise : X")
    assert notes.startswith("Notes")
    assert urls == ["https://a.fr/actu", "https://b.fr/page"]  # ni page en erreur, ni URL du seul texte
    args = commande.appels[0]["commande"]
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch" and "stream-json" in args
    assert args[args.index("--model") + 1] == "claude-haiku-4-5"  # modèle propre à l'agent
    assert "BUDGET : 3 recherches" in commande.appels[0]["systeme"]


def test_limite_abonnement_et_connexion(horloge):
    commande = FausseCommande(
        resultat(is_error=True, subtype="success", result="Claude AI usage limit reached|1760000000"),
        resultat(is_error=True, result="Invalid API key · Please run /login", api_error_status=401),
        FileNotFoundError("claude"),
    )
    instance = llm(horloge, commande)
    with pytest.raises(QuotaEpuise, match="abonnement Claude"):
        instance.structurer("redaction", "s", "c", Classification)
    with pytest.raises(ErreurLLM, match="/login"):
        instance.structurer("redaction", "s", "c", Classification)
    with pytest.raises(ErreurLLM, match="introuvable"):
        instance.structurer("redaction", "s", "c", Classification)


def test_routeur_accepte_claude_code(horloge):
    instance = construire_llm(ParamsLLM(fournisseur="claude_code"), Store(":memory:", horloge=horloge), 10)
    assert isinstance(instance, ClaudeCodeLLM)
