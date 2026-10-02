from datetime import date

import pytest
from conftest import evaluation_critique, grille, lettre, offre, profil, sortie_redaction

from recherche_stage import controles
from recherche_stage.agents.critique import AgentCritique
from recherche_stage.agents.redaction import AgentRedaction
from recherche_stage.config import ParamsCritique
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Dossier, Statut


def dossier_redige(ctx, **redaction) -> Dossier:
    o = offre()
    d = Dossier(id=id_dossier(o.entreprise, o.intitule), offre=o, statut=Statut.enquetee, variante="experience_terrain")
    ctx.llm.reponses["redaction"] = sortie_redaction(**redaction)
    AgentRedaction(ctx).executer(d)
    return d


def ctrl(ctx, d, autres=()):
    g = grille()
    return controles.executer(d, profil(ctx.profil().cv_pdf), ParamsCritique(), g.durs.disponible_du, g.durs.disponible_au,
                              list(autres), ctx.store.regles(), date(2026, 10, 5))


# ------------------------------------------------------------ couche 1


def test_documents_propres_passent_tous_les_controles(ctx):
    r = ctrl(ctx, dossier_redige(ctx))
    assert r.problemes == [] and r.score == 1.0


@pytest.mark.parametrize("trou", ["[Nom du recruteur]", "{{entreprise}}", "XXXX", "(nom de l'entreprise)", "<poste>"])
def test_placeholder_bloquant(ctx, trou):
    d = dossier_redige(ctx, lettre_texte=lettre(extra=f"Je m'adresse à {trou}."))
    r = ctrl(ctx, d)
    assert any(p.type == "placeholder" and p.gravite == "bloquant" for p in r.problemes)


def test_nom_d_une_autre_entreprise_bloquant(ctx):
    d = dossier_redige(ctx, lettre_texte=lettre(extra="Comme chez Autre Industrie, je saurai m'adapter."))
    r = ctrl(ctx, d, autres=["Autre Industrie"])
    assert any("autre entreprise" in p.raison for p in r.problemes)


def test_nom_entreprise_absent_bloquant(ctx):
    d = dossier_redige(ctx, lettre_texte=lettre(entreprise="votre société"))
    assert any(p.gravite == "bloquant" and "n'apparaît pas" in p.raison for p in ctrl(ctx, d).problemes)


def test_date_future_hors_disponibilite(ctx):
    d = dossier_redige(ctx, lettre_texte=lettre(extra="Je peux commencer dès le 1er janvier 2027."))
    assert any(p.extrait == "2027-01-01" for p in ctrl(ctx, d).problemes)


def test_dates_passees_d_experience_ignorees(ctx):
    d = dossier_redige(ctx, lettre_texte=lettre(extra="En juillet 2025, j'ai rejoint l'usine."))
    assert ctrl(ctx, d).problemes == []


def test_coordonnees_manquantes(ctx):
    texte = lettre().replace("06 12 34 56 78", "")
    d = dossier_redige(ctx, lettre_texte=texte)
    assert any("Téléphone" in p.raison for p in ctrl(ctx, d).problemes)


def test_affirmation_sans_source_valide(ctx):
    d = dossier_redige(ctx, sources=("exp7",))
    assert any(p.type == "invente" and p.gravite == "bloquant" for p in ctrl(ctx, d).problemes)


def test_regle_apprise_appliquee(ctx):
    ctx.store.ajouter_regle("suivi rigoureux", "formule récurrente")
    d = dossier_redige(ctx)
    assert any("Règle apprise" in p.raison for p in ctrl(ctx, d).problemes)


# ----------------------------------------------------- note et décisions


def critiquer(ctx, d, **evaluation):
    ctx.llm.reponses["critique"] = evaluation_critique(**evaluation)
    AgentCritique(ctx).executer(d)
    return d


def test_valider(ctx):
    d = critiquer(ctx, dossier_redige(ctx))
    assert d.critique.decision == "valider" and d.statut == Statut.validee
    assert d.critique.note >= 80
    assert ctx.llm.appels[-1][2] is True  # appel en mode critique (modèle/prompt distincts)


def test_affirmation_inventee_renvoyee_a_la_redaction(ctx):
    d = critiquer(ctx, dossier_redige(ctx), affirmations=[
        {"affirmation": "maîtrise de SAP", "ancree": False, "exageree": False, "commentaire": "absent du profil"}])
    assert d.critique.decision == "renvoyer" and d.statut == Statut.enquetee
    assert any(p.type == "invente" and p.gravite == "bloquant" for p in d.critique.problemes)


def test_exigences_non_adressees_bloquantes(ctx):
    d = critiquer(ctx, dossier_redige(ctx), exigences_cles=[
        {"exigence": "a", "adressee": True, "extrait": ""}, {"exigence": "b", "adressee": False, "extrait": ""},
        {"exigence": "c", "adressee": False, "extrait": ""}])
    assert d.critique.decision != "valider"
    assert any("exigences clés" in p.raison for p in d.critique.problemes)


def test_trop_de_fautes_bloquant(ctx):
    fautes = [{"extrait": f"faute{i}", "correction": "x"} for i in range(3)]
    d = critiquer(ctx, dossier_redige(ctx), fautes=fautes)
    assert d.critique.decision != "valider"
    assert sum(p.gravite == "bloquant" and p.type == "forme" for p in d.critique.problemes) == 3


def test_troisieme_echec_escalade(ctx):
    d = dossier_redige(ctx)
    d.critique.iteration = 2  # deux réécritures déjà faites
    critiquer(ctx, d, substituable=True, accroche_concrete=False, formules_creuses=["vif intérêt"],
              affirmations=[{"affirmation": "x", "ancree": False, "exageree": False, "commentaire": ""}])
    assert d.critique.iteration == 3
    assert d.critique.decision == "escalader" and d.statut == Statut.en_attente_humain
    assert "réécritures" in d.critique.question_humain


def test_note_tres_basse_escalade(ctx):
    d = critiquer(ctx, dossier_redige(ctx, sources=("exp9",)), substituable=True, accroche_concrete=False,
                  registre_adapte=False, coherent_cv_lettre=False,
                  exigences_cles=[{"exigence": "a", "adressee": False, "extrait": ""}, {"exigence": "b", "adressee": False, "extrait": ""}],
                  affirmations=[{"affirmation": "x", "ancree": False, "exageree": True, "commentaire": ""}])
    assert d.critique.note < 60 and d.critique.decision == "escalader"


def test_cas_ambigu_escalade_avec_question(ctx):
    d = critiquer(ctx, dossier_redige(ctx), cas_ambigu=True, question_ambigu="L'offre exige le permis : l'avez-vous ?")
    assert d.critique.decision == "escalader"
    assert "permis" in ctx.store.escalades_ouvertes()[0]["question"]


def test_profil_trop_eloigne_abandon(ctx):
    d = critiquer(ctx, dossier_redige(ctx), profil_trop_eloigne=True)
    assert d.critique.decision == "abandonner" and d.statut == Statut.abandonnee
    assert any(j["action"] == "retour_scoring" for j in ctx.store.journal())


def test_seuil_calibre_par_l_apprentissage(ctx):
    ctx.store.definir_parametre("seuil_validation_critique", 99)
    d = critiquer(ctx, dossier_redige(ctx), accroche_concrete=False)
    assert d.critique.decision == "renvoyer"


def test_nom_d_un_mot_courant_pas_confondu(ctx):
    d = dossier_redige(ctx, lettre_texte=lettre(extra="Au total, mon stage a duré dix semaines."))
    assert ctrl(ctx, d, autres=["Total"]).problemes == []
    d = dossier_redige(ctx, lettre_texte=lettre(extra="Chez Total, j'ai observé une ligne."))
    assert any("autre entreprise" in p.raison for p in ctrl(ctx, d, autres=["Total"]).problemes)
