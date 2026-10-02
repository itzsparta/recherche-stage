import random
from datetime import timedelta

import pytest
from conftest import FauxExpediteur, FauxSource, evaluation_critique, evaluation_offre, offre, sortie_redaction

from recherche_stage.llm import BudgetEpuise, ErreurLLM
from recherche_stage.modeles import Statut
from recherche_stage.orchestrateur import Orchestrateur

URL_ACTU = "https://exemple-energie.fr/actus"


def fiche_extraite() -> dict:
    return {
        "activite": "Production d'énergie", "activite_source": URL_ACTU,
        "faits": [
            {"texte": "Mise en service d'une turbine en 2026", "source": URL_ACTU, "type": "projet"},
            {"texte": "Rumeur non sourcée", "source": "https://ailleurs.example", "type": "actualite"},
        ],
        "contact_email": "", "contact_source": "", "fiable": True,
    }


@pytest.fixture
def orchestrateur(ctx):
    ctx.llm.recherche = ("Exemple Énergie a mis en service une turbine en 2026.", [URL_ACTU])
    ctx.llm.reponses.update({
        "scoring": evaluation_offre(),
        "enquete": fiche_extraite(),
        "redaction": sortie_redaction(),
        # 1re critique : information hors fiche (bloquante) → renvoi ; 2e : validée
        "critique": [evaluation_critique(infos_entreprise_hors_fiche=["chiffre d'affaires de 2 Md€"]), evaluation_critique()],
    })
    o = Orchestrateur(ctx, [FauxSource("jobteaser", [offre()])], FauxExpediteur(), rng=random.Random(0), dormir=lambda s: None)
    o.apprentissage.choisir_variante = lambda: "experience_terrain"  # format lettre, déterministe
    return o


def test_pipeline_complet_avec_porte_humaine(orchestrateur, ctx, horloge):
    rapport = orchestrateur.cycle()
    assert rapport["collectees"] == 1 and rapport["enquetes"] == 1
    assert rapport["critique:renvoyer"] == 1 and rapport["critique:valider"] == 1
    assert rapport["brouillons"] == 1 and rapport["bilan"] == 1

    [d] = ctx.store.lister()
    assert d.statut == Statut.en_attente_humain and d.etape_attente == "envoi"
    assert [f.texte for f in d.fiche_entreprise.faits] == ["Mise en service d'une turbine en 2026"]  # fait non sourcé écarté
    assert d.documents.version == 2 and d.critique.iteration == 2
    assert "CORRECTIONS DU RELECTEUR" in [c for a, c, _ in ctx.llm.appels if a == "redaction"][1]
    agents = [h.agent for h in d.historique]
    assert agents[:4] == ["veille", "scoring", "scoring", "enquete"] and "critique" in agents

    orchestrateur.valider(d.id)
    d = ctx.store.get(d.id)
    assert d.statut == Statut.validee and ctx.store.decisions_humaines()[-1]["decision"] == "valide_tel_quel"

    rapport = orchestrateur.cycle(collecter=False)
    assert rapport["envoyees"] == 1
    assert ctx.store.get(d.id).statut == Statut.envoyee

    # La même offre revient d'une autre source plus tard : aucun second envoi.
    horloge.instant += timedelta(hours=13)
    rapport = orchestrateur.cycle()
    assert rapport["collectees"] == 0 and rapport["envoyees"] == 0
    assert len(orchestrateur.envoi.expediteur.envoyes) == 1


def test_capacite_hebdomadaire_limite_les_dossiers_lances(ctx):
    ctx.config.orchestrateur.candidatures_max_par_semaine = 1
    ctx.llm.reponses.update({"scoring": evaluation_offre(), "enquete": fiche_extraite(), "redaction": sortie_redaction(),
                             "critique": evaluation_critique()})
    sources = [FauxSource("jobteaser", [offre(), offre(entreprise="Autre Énergie", contact_email="rh@autre.fr")])]
    rapport = Orchestrateur(ctx, sources, rng=random.Random(0), dormir=lambda s: None).cycle()
    assert rapport["enquetes"] == 1
    assert len(ctx.store.lister([Statut.scoree])) == 1  # le second attend la semaine suivante


def test_budget_llm_epuise(ctx):
    def epuise(contenu, schema):
        raise BudgetEpuise("budget atteint")

    ctx.llm.reponses["scoring"] = epuise
    rapport = Orchestrateur(ctx, [FauxSource("x", [offre()])], dormir=lambda s: None).cycle()
    assert rapport["budget_epuise"] == 1
    assert ctx.store.escalades_ouvertes()[-1]["type"] == "budget"


def test_erreurs_repetees_file_des_irrecuperables(ctx, horloge):
    def panne(contenu, schema):
        raise ErreurLLM("surcharge")

    ctx.llm.reponses["scoring"] = panne
    orchestrateur = Orchestrateur(ctx, [FauxSource("x", [offre()])], dormir=lambda s: None)
    for _ in range(3):
        orchestrateur.cycle()
        horloge.instant += timedelta(hours=1)
    [d] = ctx.store.lister()
    assert d.statut == Statut.erreur
    assert any(e["type"] == "erreur" for e in ctx.store.escalades_ouvertes())


def test_validation_avec_modification_journalise_le_diff(orchestrateur, ctx):
    orchestrateur.cycle()
    [d] = ctx.store.lister()
    fichiers = ctx.dossier_fichiers(d.id)
    mail = fichiers / f"mail_v{d.documents.version}.txt"
    mail.write_text(mail.read_text(encoding="utf-8").replace("Bien cordialement", "Cordialement"), encoding="utf-8")
    orchestrateur.valider(d.id, modifie=True)
    decision = ctx.store.decisions_humaines()[-1]
    assert decision["decision"] == "modifie" and "+ " in decision["diff"]
    assert "Cordialement" in ctx.store.get(d.id).documents.mail


def test_modification_avec_placeholder_refusee(orchestrateur, ctx):
    orchestrateur.cycle()
    [d] = ctx.store.lister()
    lettre = ctx.dossier_fichiers(d.id) / f"lettre_v{d.documents.version}.txt"
    lettre.write_text(lettre.read_text(encoding="utf-8") + "\n[à compléter]", encoding="utf-8")
    with pytest.raises(ValueError, match="Champ à compléter"):
        orchestrateur.valider(d.id, modifie=True)


def test_depot_manuel_sur_plateforme(ctx):
    ctx.llm.reponses.update({"scoring": evaluation_offre(), "enquete": fiche_extraite(),
                             "redaction": sortie_redaction(), "critique": evaluation_critique()})
    ctx.config.llm.recherche_web = False
    orchestrateur = Orchestrateur(ctx, [FauxSource("jobteaser", [offre(contact_email=None)])], dormir=lambda s: None)
    orchestrateur.cycle()
    [d] = ctx.store.lister()
    assert d.envoi.canal == "plateforme" and d.etape_attente == "envoi"
    orchestrateur.valider(d.id)
    d = ctx.store.get(d.id)
    assert d.etape_attente == "depot"
    assert [e["type"] for e in ctx.store.escalades_ouvertes()] == ["depot_manuel"]
    orchestrateur.marquer_envoye(d.id, "capture JobTeaser du 05/10")
    assert ctx.store.get(d.id).statut == Statut.envoyee and ctx.store.escalades_ouvertes() == []


def test_propositions_appliquees(orchestrateur, ctx):
    ctx.store.proposer("calibration_critique", "Critique", {"seuil_validation_critique": 83})
    ctx.store.proposer("nouvelles_regles", "Critique", {"motif": "vif int[ée]r[êe]t", "raison": "récurrent"})
    ctx.store.proposer("recalibrage_scoring", "Scoring", {"ajustements_poids": {"secteur": 1.4}, "facteurs_composantes": {"llm": 1.2}})
    ctx.store.definir_parametre("preferences", {"disponibilite": {"du": "2027-04-06", "au": "2027-06-25"}, "lieux": ["59"],
                                               "criteres_souples": [{"nom": "domaine", "type": "mots_cles", "valeurs": ["x"], "poids": 1},
                                                                    {"nom": "secteur", "type": "secteur", "valeurs": ["y"], "poids": 1}]})
    assert orchestrateur.appliquer_propositions(auto_seulement=True) == []
    assert len(orchestrateur.appliquer_propositions()) == 3
    assert ctx.store.parametre("seuil_validation_critique") == 83
    assert ctx.store.regles()[0]["motif"] == "vif int[ée]r[êe]t"
    assert ctx.store.parametre("poids_scoring")["llm"] == pytest.approx(0.48)
    poids = {c.nom: c.poids for c in ctx.grille().souples}
    assert poids["secteur"] > poids["domaine"]


def test_refuser_une_relance_garde_la_candidature(orchestrateur, ctx, horloge):
    from conftest import offre as fabrique
    from recherche_stage.modeles import Dossier

    d = Dossier(id="d1", offre=fabrique(), statut=Statut.envoyee)
    d.envoi.canal, d.envoi.destinataire, d.envoi.horodatage = "email", "rh@exemple-energie.fr", horloge() - timedelta(days=8)
    d.documents.objet_mail = "Candidature"
    ctx.store.save(d)
    orchestrateur.suivi.executer()
    assert ctx.store.get("d1").etape_attente == "relance:1"
    orchestrateur.rejeter("d1")
    d = ctx.store.get("d1")
    assert d.statut == Statut.envoyee and len(d.suivi.relances) == 1 and d.etape_attente == ""
