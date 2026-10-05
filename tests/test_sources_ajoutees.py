"""Sites carrière (ATS supplémentaires, détection), flux RSS, Adzuna, alertes mail."""

from datetime import datetime

import pytest
from conftest import FausseBoite, FauxHttp, FauxLLM, grille
from pydantic import ValidationError

from recherche_stage.canaux.messagerie import MessageRecu
from recherche_stage.config import EntrepriseATS, FluxRSS
from recherche_stage.sources.adzuna import SourceAdzuna
from recherche_stage.sources.alertes_mail import SourceAlertesMail, texte_avec_liens, url_canonique
from recherche_stage.sources.ats import SourceATS, ats_sans_api, detecter_ats, detecter_dans_page
from recherche_stage.sources.flux import SourceFlux

# ------------------------------------------------------------------ ATS


@pytest.mark.parametrize(
    ("url", "attendu"),
    [
        ("https://boards.greenhouse.io/exemple", ("greenhouse", "exemple")),
        ("https://job-boards.eu.greenhouse.io/exemple/jobs/123", ("greenhouse", "exemple")),
        ("https://jobs.lever.co/exemple/abc-123", ("lever", "exemple")),
        ("https://jobs.eu.lever.co/exemple", ("lever_eu", "exemple")),
        ("https://jobs.smartrecruiters.com/Exemple1/7444", ("smartrecruiters", "Exemple1")),
        ("https://apply.workable.com/exemple/j/ABC123/", ("workable", "exemple")),
        ("https://exemple.workable.com", ("workable", "exemple")),
        ("https://jobs.ashbyhq.com/exemple", ("ashby", "exemple")),
        ("https://exemple.recruitee.com/o/stage-maintenance", ("recruitee", "exemple")),
        ("https://Exemple.jobs.personio.de/job/42", ("personio", "exemple.jobs.personio.de")),
        ("https://www.exemple.fr/carrieres", None),
    ],
)
def test_detection_ats_par_url(url, attendu):
    assert detecter_ats(url) == attendu


def test_config_ats_par_url_ou_erreur_claire():
    cible = EntrepriseATS(nom="Exemple", url="https://jobs.ashbyhq.com/exemple")
    assert (cible.ats, cible.identifiant) == ("ashby", "exemple")
    with pytest.raises(ValidationError, match="detecter-ats"):
        EntrepriseATS(nom="Exemple", url="https://www.exemple.fr/carrieres")


def test_detection_dans_la_page_carriere_et_ats_sans_api():
    http = FauxHttp(texte_par_url={"exemple.fr": '<script src="https://boards.greenhouse.io/embed/job_board/js?for=exemple"></script>'})
    assert detecter_dans_page(http, "https://www.exemple.fr/carrieres") == ("greenhouse", "exemple")
    assert ats_sans_api("https://exemple.wd3.myworkdayjobs.com/fr-FR/Careers") == "Workday"


def test_workable_ashby_recruitee_gardent_les_stages_y_compris_par_type_de_contrat():
    http = FauxHttp({
        "workable.com": {"jobs": [
            {"title": "Technicien maintenance", "employment_type": "Internship", "city": "Lille", "country": "France",
             "url": "https://apply.workable.com/w/j/1", "description": "<p>GMAO</p>", "published_on": "2026-10-01"},
            {"title": "Technicien maintenance", "employment_type": "Full-time", "url": "https://apply.workable.com/w/j/2"},
        ]},
        "ashbyhq.com": {"jobs": [
            {"title": "Stage fiabilité", "location": "Calais", "jobUrl": "https://jobs.ashbyhq.com/a/1", "isListed": True, "isRemote": False},
            {"title": "Stage masqué", "jobUrl": "https://jobs.ashbyhq.com/a/2", "isListed": False},
        ]},
        "recruitee.com": {"offers": [
            {"title": "Assistant méthodes", "employment_type_code": "internship", "location": "Lille",
             "careers_url": "https://r.recruitee.com/o/1", "description": "<b>x</b>", "requirements": "GMAO"},
        ]},
    })
    workable = SourceATS(http, EntrepriseATS(nom="W", ats="workable", identifiant="w")).collecter(grille(), 10)
    assert [(o.url, o.lieu) for o in workable] == [("https://apply.workable.com/w/j/1", "Lille, France")]
    ashby = SourceATS(http, EntrepriseATS(nom="A", ats="ashby", identifiant="a")).collecter(grille(), 10)
    assert [o.intitule for o in ashby] == ["Stage fiabilité"]
    recruitee = SourceATS(http, EntrepriseATS(nom="R", ats="recruitee", identifiant="r")).collecter(grille(), 10)
    assert recruitee[0].description == "x GMAO"
    assert any("r.recruitee.com/api/offers" in url for url, _ in http.requetes)


def test_personio_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs><position><id>42</id><office>Lille</office>
    <name>Technicien maintenance</name><employmentType>intern</employmentType><createdAt>2026-09-30T10:00:00+00:00</createdAt>
    <jobDescriptions><jobDescription><name>Missions</name><value><![CDATA[<p>GMAO</p>]]></value></jobDescription></jobDescriptions>
    </position></workzag-jobs>"""
    http = FauxHttp(texte_par_url={"personio": xml})
    offres = SourceATS(http, EntrepriseATS(nom="P", url="https://p.jobs.personio.de")).collecter(grille(), 10)
    assert offres[0].url == "https://p.jobs.personio.de/job/42" and offres[0].description == "GMAO"


# ------------------------------------------------------------------ flux


def test_flux_rss_entreprise_fixe_et_agregateur():
    rss = """<rss version="2.0"><channel>
      <item><title>Stage maintenance - Ateliers du Nord</title><link>https://x/1</link><description>GMAO</description>
      <pubDate>Thu, 01 Oct 2026 08:00:00 +0200</pubDate></item>
      <item><title>Stage sans entreprise</title><link>https://x/2</link></item>
      <item><title>CDI technicien - Autre</title><link>https://x/3</link></item>
    </channel></rss>"""
    http = FauxHttp(texte_par_url={"x.fr": rss})
    agregateur = SourceFlux(http, FluxRSS(nom="Site", url="https://x.fr/rss", entreprise_fixe=False)).collecter(grille(), 10)
    assert [(o.intitule, o.entreprise) for o in agregateur] == [("Stage maintenance", "Ateliers du Nord")]
    assert agregateur[0].date_publication.isoformat() == "2026-10-01"
    entreprise = SourceFlux(http, FluxRSS(nom="Exemple", url="https://x.fr/rss")).collecter(grille(), 10)
    assert {o.entreprise for o in entreprise} == {"Exemple"} and len(entreprise) == 2


def test_flux_atom():
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Stage méthodes</title>
      <link rel="alternate" href="https://y/1"/><summary>GMAO</summary><published>2026-09-29T10:00:00Z</published></entry></feed>"""
    offres = SourceFlux(FauxHttp(texte_par_url={"y.fr": atom}), FluxRSS(nom="Y", url="https://y.fr/atom")).collecter(grille(), 10)
    assert offres[0].url == "https://y/1" and offres[0].entreprise == "Y"


# ------------------------------------------------------------------ Adzuna


def test_adzuna():
    http = FauxHttp({"adzuna": {"results": [
        {"title": "<strong>Stage</strong> maintenance", "company": {"display_name": "Exemple"}, "location": {"display_name": "Lille, Nord"},
         "description": "GMAO…", "redirect_url": "https://adzuna/1", "created": "2026-10-01T08:00:00Z", "salary_min": 7200},
        {"title": "Technicien CDI", "company": {"display_name": "Autre"}, "redirect_url": "https://adzuna/2"},
    ]}})
    offres = SourceAdzuna(http, "id", "cle").collecter(grille(), 10)
    assert [(o.intitule, o.gratification) for o in offres] == [("Stage maintenance", 600)]
    url, params = http.requetes[0]
    assert "/jobs/fr/search/1" in url and params["what"] == "stage maintenance" and params["where"] == "59"


# ------------------------------------------------------------------ alertes mail

HTML_INDEED = """<html><head><style>.x{}</style></head><body>
<a href="https://fr.indeed.com/rc/clk/dl?jk=abc123&amp;from=ja&amp;utm_source=mail"><b>Stage maintenance industrielle H/F</b></a>
<div>Ateliers du Nord - Lille (59)</div><div>Suivi des interventions GMAO.</div>
<a href="https://fr.indeed.com/rc/clk/dl?jk=def456">Stagiaire méthodes</a><div>Exemple Énergie - Calais</div>
<a href="https://fr.indeed.com/unsubscribe">Se désabonner</a>
</body></html>"""


def message(expediteur="alert@indeed.com", html=HTML_INDEED, message_id="<a1@indeed>"):
    return MessageRecu(message_id=message_id, expediteur=expediteur, objet="2 nouveaux emplois : stage maintenance",
                       corps="", date=datetime(2026, 10, 4, 7, 0), references=[], html=html)


def test_texte_avec_liens_et_url_canonique():
    texte, liens = texte_avec_liens(message())
    assert "Stage maintenance industrielle H/F [1]" in texte and "Se désabonner [3]" in texte and "style" not in texte
    assert url_canonique(liens[0]) == "https://fr.indeed.com/viewjob?jk=abc123"
    assert url_canonique("https://www.jobteaser.com/fr/job-offers/1-stage?utm_source=a&ref=b") == "https://www.jobteaser.com/fr/job-offers/1-stage?ref=b"


def test_alertes_mail_garde_seulement_ce_que_le_mail_prouve(ctx):
    ctx.llm = FauxLLM({"veille": {"offres": [
        {"intitule": "Stage maintenance industrielle H/F", "entreprise": "Ateliers du Nord", "lieu": "Lille (59)", "lien": 1,
         "extrait": "Suivi des interventions GMAO."},
        {"intitule": "Stagiaire méthodes", "entreprise": "Exemple Énergie", "lieu": "Calais", "lien": 2},
        {"intitule": "Stage inventé", "entreprise": "Ateliers du Nord", "lien": 1},           # intitulé absent du mail
        {"intitule": "Stagiaire méthodes", "entreprise": "Entreprise fantôme", "lien": 2},    # entreprise absente
        {"intitule": "Stagiaire méthodes", "entreprise": "Exemple Énergie", "lien": 9},       # lien inexistant
        {"intitule": "Stagiaire méthodes", "entreprise": "", "lien": 2},                      # entreprise non écrite
    ]}})
    boite = FausseBoite([message(), message(expediteur="ami@gmail.com", message_id="<perso>")])
    source = SourceAlertesMail(ctx, boite)
    offres = source.collecter(grille(), 10)
    assert [(o.entreprise, o.url, o.source) for o in offres] == [
        ("Ateliers du Nord", "https://fr.indeed.com/viewjob?jk=abc123", "alerte_indeed"),
        ("Exemple Énergie", "https://fr.indeed.com/viewjob?jk=def456", "alerte_indeed"),
    ]
    assert ctx.llm.nb("veille") == 1  # le mail personnel n'est pas envoyé au LLM
    assert source.collecter(grille(), 10) == [] and ctx.llm.nb("veille") == 1  # mail déjà traité


def test_alertes_mail_plateformes_retenues_et_limite(ctx):
    ctx.llm = FauxLLM({"veille": {"offres": [
        {"intitule": "Stage maintenance industrielle H/F", "entreprise": "Ateliers du Nord", "lien": 1},
        {"intitule": "Stagiaire méthodes", "entreprise": "Exemple Énergie", "lien": 2},
    ]}})
    ctx.config.sources.alertes_plateformes = ["jobteaser"]
    boite = FausseBoite([message(), message(expediteur="notifications@mail.jobteaser.com", message_id="<j1>"),
                         message(expediteur="notifications@mail.jobteaser.com", message_id="<j2>")])
    offres = SourceAlertesMail(ctx, boite).collecter(grille(), 3)
    assert {o.source for o in offres} == {"alerte_jobteaser"} and len(offres) == 2
    # le second mail JobTeaser dépasserait la limite : il reste pour le prochain cycle
    assert ctx.store.parametre("alertes_traitees") == ["<j1>"]


def test_le_suivi_ignore_les_alertes_deja_lues(ctx):
    from recherche_stage.agents.suivi import AgentSuivi

    ctx.store.definir_parametre("alertes_traitees", ["<a1@indeed>"])
    ctx.llm = FauxLLM()
    AgentSuivi(ctx, FausseBoite([message()])).traiter_reponses()
    assert ctx.llm.appels == []


# ------------------------------------------------------- annuaire (spontané)


def _entreprise(siren, commune="Paris", cp="75008", agence=None):
    r = {"siren": siren, "nom_complet": f"SOCIETE {siren}", "activite_principale": "71.12B",
         "siege": {"libelle_commune": commune, "code_postal": cp}}
    if agence:
        r["matching_etablissements"] = [{"libelle_commune": agence[0], "code_postal": agence[1]}]
    return r


def test_annuaire_agence_locale_et_filtre_effectif():
    from recherche_stage.sources.annuaire import SourceAnnuaire

    http = FauxHttp({"recherche-entreprises": {"results": [_entreprise("1", agence=("Lille", "59000"))], "total_pages": 1}})
    offres = SourceAnnuaire(http, ["71.12B"], ["59"], effectif_min=10).collecter(grille(), 5)
    assert offres[0].lieu == "Lille (59000)"
    assert offres[0].intitule.startswith("Stage ") and not offres[0].intitule.lower().startswith("stage stage")
    assert "03" not in http.requetes[0][1]["tranche_effectif_salarie"].split(",")
    assert "11" in http.requetes[0][1]["tranche_effectif_salarie"].split(",")


def test_annuaire_reprend_ou_il_s_est_arrete(ctx):
    from recherche_stage.sources.annuaire import SourceAnnuaire

    class Pages(FauxHttp):
        def get_json(self, url, params=None, entetes=None):
            self.requetes.append((url, params))
            p = params["page"]
            return {"results": [_entreprise(f"{p}-{i}") for i in range(4)] if p <= 2 else [], "total_pages": 2}

    http = Pages()
    source = SourceAnnuaire(http, ["71.12B"], ["59"], store=ctx.store, max_par_cycle=3)
    lots = [source.collecter(grille(), 50) for _ in range(4)]
    assert [len(lot) for lot in lots] == [3, 1, 3, 1]  # au plus 3 par cycle, reste de page repris
    noms = [o.entreprise for lot in lots for o in lot]
    assert len(set(noms)) == 8
    assert source.collecter(grille(), 50) == []  # tout vu, couple épuisé


def test_spontanee_annuaire_passe_le_seuil(ctx):
    from recherche_stage.agents.scoring import score_regles
    from recherche_stage.modeles import CritereSouple, Dossier, Offre

    g = grille()
    g.souples = [CritereSouple(nom="secteur", type="secteur", valeurs=["éolien", "énergie"], poids=3)]
    offre = Offre(intitule="Stage ingénieur (candidature spontanée)", entreprise="X", lieu="Lille (59000)",
                  source="annuaire", type="spontanee", secteur="71.12B")
    assert score_regles(Dossier(id="x", offre=offre), g) == 1.0
