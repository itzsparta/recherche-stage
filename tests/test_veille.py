from datetime import timedelta

from conftest import FauxSource, offre

from recherche_stage.agents.veille import AgentVeille
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Statut
from recherche_stage.resilience import reessayer


def veille(ctx, *sources):
    return AgentVeille(ctx, list(sources), dormir=lambda s: None)


def test_reessayer_delais_croissants():
    attentes, essais = [], []

    def fragile():
        essais.append(1)
        if len(essais) < 3:
            raise ConnectionError
        return "ok"

    assert reessayer(fragile, [2, 8, 30], dormir=attentes.append) == "ok"
    assert attentes == [2, 8]


def test_collecte_et_deduplication_inter_sources(ctx):
    a = FauxSource("jobteaser", [offre()])
    b = FauxSource("indeed", [offre(source="indeed", intitule="Stage Maintenance Industrielle (F/H)", contact_email=None, gratification=700)])
    nouveaux = veille(ctx, a, b).executer()
    assert len(nouveaux) == 1
    d = ctx.store.get(nouveaux[0].id)
    assert d.offre.sources_fusionnees == ["indeed"]
    assert d.offre.gratification == 700  # complété par la fusion
    assert d.offre.contact_email == "recrutement@exemple-energie.fr"
    assert d.offre.collecteur in ("jobteaser", "indeed")


def test_frequence_de_collecte_respectee(ctx, horloge):
    source = FauxSource("jobteaser", [offre()])
    v = veille(ctx, source)
    v.executer()
    v.executer()
    assert len(source.appels) == 1
    horloge.instant += timedelta(hours=13)
    v.executer()
    assert len(source.appels) == 2


def test_source_en_panne_quota_reporte_sur_l_alternative(ctx):
    panne = FauxSource("france_travail", echecs=99)
    secours = FauxSource("ats", [offre()])
    ctx.store.definir_parametre("allocation_sources", {"france_travail": 0.8, "ats": 0.2})
    veille(ctx, panne, secours).executer()
    assert len(panne.appels) == 1 + len(ctx.config.veille.delais_reessai)
    assert secours.appels == [12 + 48]  # son quota plus celui de la source en panne


def test_disjoncteur_coupe_puis_retente(ctx, horloge):
    panne = FauxSource("france_travail", echecs=99)
    v = veille(ctx, panne)
    for _ in range(3):
        v.executer(forcer=True)
    assert ctx.store.etat_source("france_travail")["coupee"] == 1
    assert any(e["type"].startswith("source_coupee") for e in ctx.store.escalades_ouvertes())
    appels = len(panne.appels)
    v.executer(forcer=True)
    assert len(panne.appels) == appels  # coupée : plus interrogée
    horloge.instant += timedelta(hours=25)
    panne.echecs = 0
    v.executer(forcer=True)
    assert ctx.store.etat_source("france_travail")["coupee"] == 0


def test_jamais_recreer_un_dossier_deja_candidate(ctx):
    o = offre()
    ctx.store.reserver_envoi(id_dossier(o.entreprise, o.intitule), "candidature", "email")
    assert veille(ctx, FauxSource("x", [o])).executer() == []


def test_entreprise_prioritaire_marquee(ctx):
    g = ctx.grille()
    g.entreprises_prioritaires = ["exemple énergie sas"]
    ctx.store.definir_parametre("grille", g)
    d = veille(ctx, FauxSource("x", [offre()])).executer()[0]
    assert d.prioritaire and d.statut == Statut.collectee


def test_import_lu_a_chaque_cycle(ctx, tmp_path):
    from recherche_stage.sources.import_fichier import SourceImport

    v = veille(ctx, SourceImport(tmp_path))
    v.executer()
    (tmp_path / "indeed_1.csv").write_text("intitule,entreprise,lieu\nStage maintenance,Exemple,Lille\n", encoding="utf-8")
    assert len(v.executer()) == 1
