"""ClaudeLLM contre le vrai SDK Anthropic, servi par un faux serveur local :
vérifie la requête réellement sérialisée, sans réseau ni clé API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from recherche_stage.agents.suivi import Classification
from recherche_stage.config import ParamsLLM
from recherche_stage.llm import BudgetEpuise, ClaudeLLM, ErreurLLM
from recherche_stage.store import Store


class Serveur:
    def __init__(self):
        self.requetes: list[tuple[dict, dict]] = []
        self.reponses: list[dict] = []
        serveur = self

        class Gestionnaire(BaseHTTPRequestHandler):
            def do_POST(self):
                corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                serveur.requetes.append((dict(self.headers), corps))
                donnee = serveur.reponses.pop(0)
                code = donnee.pop("_statut", 200)
                reponse = json.dumps(donnee).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reponse)))
                self.end_headers()
                self.wfile.write(reponse)

            def log_message(self, *args):
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Gestionnaire)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_port}"


def message(contenu, stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": contenu,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 7}}


@pytest.fixture
def serveur():
    s = Serveur()
    yield s
    s.http.shutdown()


def llm(serveur, horloge, budget=10):
    client = anthropic.Anthropic(api_key="test", base_url=serveur.url, max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(trust_env=False))
    return ClaudeLLM(ParamsLLM(), Store(":memory:", horloge=horloge), budget, client=client)


def test_sortie_structuree_avec_fallbacks_et_effort(serveur, horloge):
    attendu = {"concerne_candidature": True, "categorie": "refus", "confiance": 0.9, "date_entretien": "", "resume": "non"}
    serveur.reponses.append(message([{"type": "text", "text": json.dumps(attendu)}]))
    client = llm(serveur, horloge)
    resultat = client.structurer("suivi", "système", "contenu", Classification, critique=True)
    assert resultat.categorie == "refus"
    entetes, corps = serveur.requetes[0]
    assert "server-side-fallback-2026-07-01" in entetes["anthropic-beta"]
    assert corps["fallbacks"] == "default"
    assert corps["model"] == "claude-opus-5-5"
    assert corps["output_config"]["effort"] == "high"  # effort du Critique
    assert corps["output_config"]["format"]["type"] == "json_schema"
    assert corps["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in corps and "thinking" not in corps
    assert client.store.appels_llm_semaine() == 1


def test_refus_du_modele(serveur, horloge):
    serveur.reponses.append(message([], stop_reason="refusal"))
    with pytest.raises(ErreurLLM, match="décliné"):
        llm(serveur, horloge).structurer("suivi", "s", "c", Classification)


def test_cle_refusee_message_explicite(serveur, horloge):
    serveur.reponses.append({"_statut": 401, "type": "error",
                             "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    with pytest.raises(ErreurLLM, match="ANTHROPIC_API_KEY"):
        llm(serveur, horloge).structurer("profil", "s", "c", Classification)


def test_budget_hebdomadaire(serveur, horloge):
    client = llm(serveur, horloge, budget=0)
    with pytest.raises(BudgetEpuise):
        client.structurer("suivi", "s", "c", Classification)
    assert serveur.requetes == []


def test_recherche_web_collecte_les_urls(serveur, horloge):
    serveur.reponses.append(message([
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "Exemple"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": "https://a.example/actu", "title": "A", "encrypted_content": "x", "page_age": None}]},
        {"type": "text", "text": "Exemple a ouvert un site.", "citations": [
            {"type": "web_search_result_location", "url": "https://b.example/page", "title": "B", "encrypted_index": "x", "cited_text": "site"}]},
    ]))
    texte, urls = llm(serveur, horloge).rechercher("enquete", "s", "Exemple")
    assert "ouvert un site" in texte
    assert urls == ["https://a.example/actu", "https://b.example/page"]
    corps = serveur.requetes[0][1]
    assert corps["tools"][0]["type"] == "web_search_20260209"
