"""GeminiLLM contre le vrai SDK google-genai, servi par un faux serveur local."""

import json

import pytest
from test_llm import Serveur

from recherche_stage.agents.suivi import Classification
from recherche_stage.config import ParamsLLM
from recherche_stage.llm import ErreurLLM
from recherche_stage.llm_gemini import GeminiLLM, RouteurLLM, construire_llm
from recherche_stage.store import Store

genai = pytest.importorskip("google.genai")


def reponse(texte="", fin="STOP", ancrage=None):
    candidat = {"content": {"role": "model", "parts": [{"text": texte}]}, "finishReason": fin}
    if ancrage:
        candidat["groundingMetadata"] = {"groundingChunks": [{"web": {"uri": u, "title": "t"}} for u in ancrage]}
    return {"candidates": [candidat], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}


@pytest.fixture
def serveur(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    s = Serveur()
    yield s
    s.http.shutdown()


def gemini(serveur, horloge, budget=10):
    client = genai.Client(api_key="test", http_options={"base_url": serveur.url, "retry_options": {"attempts": 1}})
    return GeminiLLM(ParamsLLM(fournisseur="gemini"), Store(":memory:", horloge=horloge), budget, client=client)


def test_sortie_structuree_et_temperature_du_critique(serveur, horloge):
    attendu = {"concerne_candidature": True, "categorie": "entretien", "confiance": 0.8, "date_entretien": "", "resume": "rdv"}
    serveur.reponses.append(reponse(json.dumps(attendu)))
    llm = gemini(serveur, horloge)
    assert llm.structurer("suivi", "système", "contenu", Classification, critique=True).categorie == "entretien"
    entetes, corps = serveur.requetes[0]
    config = corps["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["properties"]["categorie"]["enum"] == ["refus", "entretien", "demande_info", "autre"]
    assert "temperature" not in config  # Gemini 3+ : température par défaut, recommandée par Google
    assert corps["systemInstruction"]["parts"][0]["text"] == "système"
    assert llm.store.appels_llm_semaine() == 1


def test_temperature_du_critique_sur_gemini_2(serveur, horloge):
    serveur.reponses.append(reponse('{"concerne_candidature": false, "categorie": "autre", "confiance": 0.5, "date_entretien": "", "resume": ""}'))
    client = genai.Client(api_key="test", http_options={"base_url": serveur.url, "retry_options": {"attempts": 1}})
    params = ParamsLLM(fournisseur="gemini", gemini_modele_critique="gemini-2.5-pro")
    GeminiLLM(params, Store(":memory:", horloge=horloge), 10, client=client).structurer("critique", "s", "c", Classification, critique=True)
    assert serveur.requetes[0][1]["generationConfig"]["temperature"] == 0.1


def test_503_reessaye_puis_reussit(serveur, horloge):
    erreur = {"_statut": 503, "error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
    serveur.reponses += [dict(erreur), reponse('{"concerne_candidature": false, "categorie": "autre", "confiance": 0.5, "date_entretien": "", "resume": ""}')]
    client = genai.Client(api_key="test", http_options={
        "base_url": serveur.url, "retry_options": {"attempts": 2, "initial_delay": 0.01, "max_delay": 0.01}})
    llm = GeminiLLM(ParamsLLM(fournisseur="gemini"), Store(":memory:", horloge=horloge), 10, client=client)
    assert llm.structurer("suivi", "s", "c", Classification).categorie == "autre"
    assert len(serveur.requetes) == 2


def test_503_persistant_message_explicite(serveur, horloge):
    serveur.reponses.append({"_statut": 503, "error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    with pytest.raises(ErreurLLM, match="surchargé"):
        gemini(serveur, horloge).structurer("suivi", "s", "c", Classification)


def test_reponse_bloquee(serveur, horloge):
    serveur.reponses.append(reponse(fin="SAFETY"))
    with pytest.raises(ErreurLLM, match="bloquée"):
        gemini(serveur, horloge).structurer("suivi", "s", "c", Classification)


def test_sortie_non_conforme(serveur, horloge):
    serveur.reponses.append(reponse('{"categorie": "peut-etre"}'))
    with pytest.raises(ErreurLLM, match="schéma"):
        gemini(serveur, horloge).structurer("suivi", "s", "c", Classification)


def test_recherche_ancree_google_search(serveur, horloge):
    serveur.reponses.append(reponse("Exemple a ouvert un site.", ancrage=["https://a.example/1", "https://a.example/1", "https://b.example/2"]))
    texte, urls = gemini(serveur, horloge).rechercher("enquete", "s", "Exemple")
    assert texte == "Exemple a ouvert un site." and urls == ["https://a.example/1", "https://b.example/2"]
    assert serveur.requetes[0][1]["tools"] == [{"googleSearch": {}}]


def test_routeur_critique_sur_un_autre_fournisseur(horloge, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    llm = construire_llm(ParamsLLM(fournisseur="claude", fournisseur_critique="gemini"), Store(":memory:", horloge=horloge), 10)
    assert isinstance(llm, RouteurLLM) and isinstance(llm.critique, GeminiLLM)
    with pytest.raises(ValueError, match="inconnu"):
        construire_llm(ParamsLLM(fournisseur="mistral"), Store(":memory:", horloge=horloge), 10)
