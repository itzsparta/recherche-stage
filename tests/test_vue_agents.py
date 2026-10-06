"""Vue « Agents » de l'interface : activité, sources, propositions, bilan."""

from datetime import timedelta

import pytest
from conftest import FauxSource
from test_interface import app  # noqa: F401  (fixture)
from test_revue import orchestrateur  # noqa: F401  (fixture)

from recherche_stage.interface import Refus
from recherche_stage.modeles import Statut
from recherche_stage.agents.apprentissage import PREFIXE_MOTIF


def test_activite_des_agents_et_animation_du_cycle(app, ctx):  # noqa: F811
    ctx.store.compter_appel_llm("redaction", "modele", 1000, 500)  # le faux LLM ne compte pas ses appels
    vue = app.agents()
    par_nom = {a["nom"]: a for a in vue["agents"]}
    assert [a["nom"] for a in vue["agents"]][:3] == ["profil", "veille", "scoring"]
    assert par_nom["redaction"]["actions"] > 0 and par_nom["redaction"]["dernier"]
    assert par_nom["redaction"]["appels_llm"] == 1 and par_nom["redaction"]["tokens"] == 1500 and par_nom["veille"]["appels_llm"] == 0
    assert par_nom["envoi"]["en_attente"] == 0 and par_nom["profil"]["en_attente"] is None

    app.lancer_cycle({"sans_suivi": True, "sans_envoi": True})
    cycle = app.etat()["cycle"]
    assert cycle["agent"] is None and not cycle["en_cours"]  # plus personne au travail une fois fini
    assert "veille" in cycle["bilan_agents"]


def test_source_coupee_puis_reactivee(app, ctx, orchestrateur):  # noqa: F811
    orchestrateur.veille.sources.append(FauxSource("panne", echecs=99))
    ctx.horloge.instant += timedelta(days=2)  # collecte de nouveau due
    for _ in range(ctx.config.veille.echecs_avant_coupure):
        orchestrateur.veille.executer(forcer=True)
    panne = next(s for s in app.agents()["sources"] if s["nom"] == "panne")
    assert panne["etat"] == "coupee" and "indisponible" in panne["erreur"] and panne["reprise"]
    assert app.etat()["agents"]["sources_coupees"] == 1
    assert any(e["type"] == "source_coupee:panne" for e in ctx.store.escalades_ouvertes())

    assert "réactivée" in app.action({"action": "source", "nom": "panne"})
    panne = next(s for s in app.agents()["sources"] if s["nom"] == "panne")
    assert panne["etat"] == "jamais" and panne["echecs"] == 0  # retentée au prochain cycle
    assert app.etat()["agents"]["sources_coupees"] == 0
    assert not any(e["type"] == "source_coupee:panne" for e in ctx.store.escalades_ouvertes())
    with pytest.raises(Refus, match="inconnue"):
        app.action({"action": "source", "nom": "inexistante"})


def test_propositions_appliquer_refuser_et_pas_de_doublon(app, ctx):  # noqa: F811
    store = ctx.store
    store.proposer("calibration_critique", "Critique", {"stats": {"faux_negatifs": "4/10", "faux_positifs": "0/0"},
                                                        "seuil_validation_critique": 83, "ancien": 80})
    store.proposer("nouvelles_regles", "Critique", {"motif": "je\\ suis\\ motivé", "raison": "Erreur récurrente (3 dossiers) : « je suis motivé »"})
    vue = app.agents()["propositions"]
    titres = [p["titre"] for p in vue]
    assert "Critique : seuil de validation 80 → 83 (plus exigeant)" in titres
    assert app.etat()["agents"]["propositions"] == len(vue)

    calibration = next(p for p in vue if p["mecanisme"] == "calibration_critique")
    assert "Appliqué" in app.action({"action": "proposition", "id": calibration["id"], "decision": "appliquer"})
    assert store.parametre("seuil_validation_critique") == 83
    regle = next(p for p in vue if p["mecanisme"] == "nouvelles_regles")
    assert "Refusé" in app.action({"action": "proposition", "id": regle["id"], "decision": "refuser"})
    assert store.regles() == []
    with pytest.raises(Refus, match="déjà traitée"):
        app.action({"action": "proposition", "id": regle["id"], "decision": "appliquer"})

    # Une règle refusée n'est pas reproposée ; un nouveau bilan remplace l'allocation précédente.
    assert "Bilan refait" in app.action({"action": "bilan"})
    assert "Bilan refait" in app.action({"action": "bilan"})
    en_attente = [p["mecanisme"] for p in store.propositions()]
    assert en_attente.count("allocation_sources") == 0  # appliquée d'office
    assert store.parametre("dernier_bilan")


def test_regle_refusee_non_reproposee(ctx, orchestrateur):  # noqa: F811
    for d in ctx.store.lister()[:3]:
        ctx.store.enregistrer_decision(d.id, "modifie", "- Je suis très motivé par votre entreprise.\n+ Autre")
    regles = orchestrateur.apprentissage.nouvelles_regles()
    assert len(regles) == 1
    pid = ctx.store.proposer("nouvelles_regles", "Critique", regles[0])
    ctx.store.marquer_proposition(pid, "refusee")
    assert orchestrateur.apprentissage.nouvelles_regles() == []


def test_bilan_affiche_motifs_et_resultats(app, ctx):  # noqa: F811
    d = next(d for d in ctx.store.lister() if d.statut == Statut.en_attente_humain)
    ctx.store.enregistrer_decision(d.id, "rejete", f"{PREFIXE_MOTIF}trop loin")
    bilan = app.agents()["bilan"]
    assert bilan["motifs_rejet"] == [{"texte": "trop loin", "n": 1}]
    assert bilan["abouties"] == 0 and bilan["variantes"] == []
