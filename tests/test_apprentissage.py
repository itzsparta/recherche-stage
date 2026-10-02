import random
from datetime import timedelta

from conftest import offre

from recherche_stage.agents.apprentissage import AgentApprentissage, parts_thompson
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Dossier, Probleme, Score, Statut


def ajouter_resultats(ctx, horloge, collecteur, entretiens, refus, variante="experience_terrain"):
    for i in range(entretiens + refus):
        o = offre(entreprise=f"{collecteur} {i}", collecteur=collecteur)
        d = Dossier(id=id_dossier(o.entreprise, o.intitule), offre=o, variante=variante,
                    statut=Statut.entretien if i < entretiens else Statut.refus,
                    score=Score(valeur=70, confiance=0.9, details={"regles": 0.8 if i < entretiens else 0.2, "lexical": 0.5, "llm": 0.6}))
        d.envoi.horodatage = horloge() - timedelta(days=10)
        ctx.store.save(d)


def test_thompson_privilegie_le_meilleur_bras_sans_couper_l_exploration():
    parts = parts_thompson({"bonne": (8, 2), "mauvaise": (1, 9), "neuve": (0, 0)}, random.Random(1))
    assert parts["bonne"] > parts["neuve"] > 0 and parts["mauvaise"] > 0
    assert abs(sum(parts.values()) - 1) < 0.01


def test_allocation_des_sources(ctx, horloge):
    ajouter_resultats(ctx, horloge, "ats:greenhouse:x", 4, 1)
    ajouter_resultats(ctx, horloge, "annuaire", 0, 6)
    app = AgentApprentissage(ctx, random.Random(2))
    allocation = app.allocation_sources(app.donnees_etiquetees())
    assert allocation["ats:greenhouse:x"] > allocation["annuaire"]


def test_silence_compte_apres_30_jours(ctx, horloge):
    o = offre()
    d = Dossier(id="d", offre=o, statut=Statut.envoyee)
    d.envoi.horodatage = horloge() - timedelta(days=10)
    ctx.store.save(d)
    app = AgentApprentissage(ctx, random.Random(0))
    assert app.donnees_etiquetees() == []
    horloge.instant += timedelta(days=25)
    assert app.donnees_etiquetees()[0]["resultat"] == "silence"


def test_ab_testing_choisit_un_bras_connu(ctx, horloge):
    ajouter_resultats(ctx, horloge, "x", 5, 0, variante="competence_cle")
    app = AgentApprentissage(ctx, random.Random(3))
    choix = [app.choisir_variante() for _ in range(30)]
    assert choix.count("competence_cle") > 10


def test_recalibrage_du_scoring(ctx, horloge):
    ajouter_resultats(ctx, horloge, "x", 4, 8)
    app = AgentApprentissage(ctx, random.Random(0))
    proposition = app.recalibrage_scoring(app.donnees_etiquetees())
    assert proposition["facteurs_composantes"]["regles"] > 1


def test_calibration_du_critique_sur_faux_negatifs(ctx):
    for i in range(10):
        ctx.store.journaliser("critique", "critique:valider", f"d{i}")
        ctx.store.enregistrer_decision(f"d{i}", "modifie" if i < 4 else "valide_tel_quel")
    calibration = AgentApprentissage(ctx).calibration_critique()
    assert calibration["seuil_validation_critique"] == 83


def test_nouvelle_regle_sur_erreur_recurrente(ctx):
    for i in range(3):
        d = Dossier(id=f"d{i}", offre=offre(entreprise=f"E{i}"))
        d.critique.problemes = [Probleme(type="generique", gravite="mineur", extrait="passionné depuis toujours", raison="creux")]
        ctx.store.save(d)
    regles = AgentApprentissage(ctx).nouvelles_regles()
    assert regles and "passionn" in regles[0]["motif"]


def test_bilan_ecrit_et_propositions(ctx, horloge):
    ajouter_resultats(ctx, horloge, "x", 1, 2)
    bilan = AgentApprentissage(ctx, random.Random(0)).executer()
    assert "Bilan hebdomadaire" in bilan
    assert list((ctx.config.dossier_donnees / "bilans").glob("bilan_*.md"))
    assert any(p["mecanisme"] == "allocation_sources" for p in ctx.store.propositions())
