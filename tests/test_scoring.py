from datetime import date

from conftest import evaluation_offre, grille, offre, profil

from recherche_stage.agents.scoring import AgentScoring, raisons_suspicion, violations_criteres_durs
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Dossier, Periode, Statut


def dossier(**champs) -> Dossier:
    o = offre(**champs)
    return Dossier(id=id_dossier(o.entreprise, o.intitule), offre=o)


def test_offre_conforme_sans_violation():
    violations, manquantes = violations_criteres_durs(dossier(), grille())
    assert violations == [] and manquantes == []


def test_criteres_durs_eliminatoires():
    g = grille()
    assert violations_criteres_durs(dossier(lieu="Marseille (13)"), g)[0]
    assert violations_criteres_durs(dossier(dates=Periode(debut=date(2027, 1, 4), fin=date(2027, 6, 25))), g)[0]
    assert violations_criteres_durs(dossier(intitule="Stage commercial maintenance"), g)[0]
    assert violations_criteres_durs(dossier(duree_mois=6, dates=Periode()), g)[0]
    g.durs.entreprises_exclues = ["ArcelorMittal"]  # ne prend pas de stagiaires : toutes ses sociétés
    assert violations_criteres_durs(dossier(entreprise="ARCELORMITTAL FRANCE SAS"), g)[0] == ["entreprise exclue"]
    assert violations_criteres_durs(dossier(entreprise="Mittal Logistique"), g)[0] == []


def test_departement_reconnu_par_code_postal():
    assert violations_criteres_durs(dossier(lieu="59140 Dunkerque"), grille())[0] == []
    assert violations_criteres_durs(dossier(lieu="Lille"), grille())[0] == []


def test_lieu_absent_signale_comme_manquant():
    violations, manquantes = violations_criteres_durs(dossier(lieu=""), grille())
    assert violations == [] and "lieu" in manquantes


def test_detection_offres_suspectes():
    fortes, _ = raisons_suspicion(dossier(description="Contactez-nous sur WhatsApp, frais de dossier de 50 euros."))
    assert fortes
    fortes, faibles = raisons_suspicion(dossier(contact_email="rh.exemple@gmail.com"))
    assert not fortes and faibles == ["adresse de contact personnelle"]
    # « signal » est un mot courant en ingénierie : il ne doit pas déclencher l'alerte
    assert raisons_suspicion(dossier(description="Traitement du signal et maintenance " * 10)) == ([], [])


def test_score_complet_et_tri(ctx):
    ctx.llm.reponses["scoring"] = evaluation_offre()
    scoring = AgentScoring(ctx)
    retenus = scoring.executer([dossier()])
    assert len(retenus) == 1
    d = retenus[0]
    assert d.statut == Statut.scoree
    assert d.score.valeur >= 60 and d.score.confiance >= 0.5
    assert {"regles", "lexical", "llm"} <= set(d.score.details)
    actions = [h.action for h in d.historique]
    assert actions == ["filtre_criteres_durs", "score"]
    assert all(h.entrees_hash for h in d.historique)


def test_preuve_inexistante_ne_compte_pas(ctx):
    ctx.llm.reponses["scoring"] = evaluation_offre(preuve="exp99")
    d = dossier()
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.score.details["exigences_couvertes"] == 1  # seule la GMAO (comp_gmao) reste valable


def test_elimination_journalisee(ctx):
    d = dossier(lieu="Marseille (13)")
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.statut == Statut.abandonnee and d.motif.startswith("critere_dur")
    assert ctx.llm.nb("scoring") == 0


def test_offre_expiree(ctx):
    d = dossier(date_publication=date(2026, 6, 1))
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.statut == Statut.abandonnee and d.motif.startswith("expiree")


def test_suspecte_selon_llm(ctx):
    ctx.llm.reponses["scoring"] = evaluation_offre(suspecte=True)
    d = dossier()
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.statut == Statut.abandonnee and d.motif.startswith("suspecte")


def test_confiance_faible_escalade(ctx):
    ctx.llm.reponses["scoring"] = evaluation_offre(certitude="basse")
    d = dossier(lieu="", description="Maintenance GMAO énergie, maintenance préventive.")
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.statut == Statut.en_attente_humain and d.etape_attente == "scoring"
    assert ctx.store.escalades_ouvertes()[0]["type"] == "confiance_faible"


def test_pre_score_trop_bas_evite_l_appel_llm(ctx):
    d = dossier(intitule="Stage logistique", description="Gestion des stocks et des expéditions, inventaires. " * 8)
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert ctx.llm.nb("scoring") == 0
    assert d.statut == Statut.scoree and d.score.valeur < 60


def test_candidature_spontanee_scoree_sur_criteres_applicables(ctx):
    d = dossier(type="spontanee", description="", secteur="35.11Z énergie", dates=Periode(), date_publication=None,
                contact_email=None, intitule="Stage maintenance (candidature spontanée)")
    AgentScoring(ctx).evaluer(d, profil(), grille())
    assert d.statut == Statut.scoree and d.score.valeur == 100 and ctx.llm.nb("scoring") == 0
