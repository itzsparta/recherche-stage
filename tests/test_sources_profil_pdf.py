import re

from conftest import FauxHttp, grille, profil

from recherche_stage.agents.profil import AgentProfil
from recherche_stage.canaux.pdf_simple import texte_vers_pdf
from recherche_stage.config import EntrepriseATS
from recherche_stage.sources.annuaire import SourceAnnuaire
from recherche_stage.sources.ats import SourceATS
from recherche_stage.sources.france_travail import SourceFranceTravail
from recherche_stage.sources.import_fichier import SourceImport


def test_import_csv_point_virgule_et_archivage(tmp_path):
    (tmp_path / "jobteaser_octobre.csv").write_text(
        "intitule;entreprise;lieu;debut;contact_email\nStage maintenance;Exemple;Lille;06/04/2027;rh@exemple.fr\n;Sans titre;;;\n",
        encoding="utf-8",
    )
    offres = SourceImport(tmp_path).collecter(grille(), 10)
    assert len(offres) == 1
    assert offres[0].source == "jobteaser" and offres[0].dates.debut.isoformat() == "2027-04-06"
    assert (tmp_path / "traites" / "jobteaser_octobre.csv").exists()
    assert SourceImport(tmp_path).collecter(grille(), 10) == []


def test_ats_ne_garde_que_les_stages():
    http = FauxHttp({
        "greenhouse": {"jobs": [
            {"title": "Stage maintenance", "location": {"name": "Lille"}, "absolute_url": "https://g/1", "content": "&lt;p&gt;GMAO&lt;/p&gt;"},
            {"title": "Ingénieur CDI", "location": {"name": "Lille"}, "absolute_url": "https://g/2", "content": ""},
        ]},
        "lever": [{"text": "Internship reliability", "categories": {"location": "Calais"}, "hostedUrl": "https://l/1", "descriptionPlain": "x", "createdAt": 1790000000000}],
    })
    gh = SourceATS(http, EntrepriseATS(nom="G", ats="greenhouse", identifiant="g")).collecter(grille(), 10)
    assert [o.intitule for o in gh] == ["Stage maintenance"]
    lv = SourceATS(http, EntrepriseATS(nom="L", ats="lever", identifiant="l")).collecter(grille(), 10)
    assert lv[0].lieu == "Calais" and lv[0].date_publication is not None


def test_france_travail():
    http = FauxHttp({"offres/search": {"resultats": [
        {"id": "1", "intitule": "Stage technicien maintenance", "description": "stage", "entreprise": {"nom": "E"},
         "lieuTravail": {"libelle": "59 - Lille"}, "contact": {"courriel": "rh@e.fr"}, "dateCreation": "2026-10-01T10:00:00Z"},
    ]}})
    offres = SourceFranceTravail(http, "id", "secret").collecter(grille(), 10)
    assert offres[0].contact_email == "rh@e.fr" and offres[0].lieu == "59 - Lille"
    assert http.requetes[0][1]["grant_type"] == "client_credentials"


def test_annuaire_cibles_spontanees_sans_donnees_personnelles():
    http = FauxHttp({"recherche-entreprises": {"results": [
        {"siren": "123", "nom_complet": "ATELIERS DU LITTORAL", "siege": {"libelle_commune": "Calais"},
         "activite_principale": "33.12Z", "dirigeants": [{"nom": "Dupont"}]},
    ]}})
    offres = SourceAnnuaire(http, ["33.12Z"], ["62"]).collecter(grille(), 5)
    assert offres[0].type == "spontanee" and "Dupont" not in offres[0].model_dump_json()


def test_pdf_lettre_valide(tmp_path):
    chemin = texte_vers_pdf("Madame, Monsieur,\n\n" + "Une ligne accentuée : élève, « guillemets ». " * 200, tmp_path / "l.pdf")
    brut = chemin.read_bytes()
    assert brut.startswith(b"%PDF-1.4") and brut.rstrip().endswith(b"%%EOF")
    debut_xref = int(re.search(rb"startxref\n(\d+)", brut).group(1))
    assert brut[debut_xref:].startswith(b"xref")
    assert int(re.search(rb"/Count (\d+)", brut).group(1)) >= 2  # paginé
    for numero, position in enumerate(re.findall(rb"(\d{10}) 00000 n", brut), start=1):
        assert brut[int(position):].startswith(f"{numero} 0 obj".encode())


def test_grille_normalisee_et_ajustee(ctx):
    preferences = {
        "disponibilite": {"du": "2027-04-06", "au": "2027-06-25"}, "lieux": ["59"],
        "criteres_souples": [{"nom": "domaine", "type": "mots_cles", "valeurs": ["maintenance"], "poids": 3},
                             {"nom": "secteur", "type": "secteur", "valeurs": ["énergie"], "poids": 1}],
    }
    ctx.store.definir_parametre("ajustements_poids", {"secteur": 3})
    g = AgentProfil(ctx).construire_grille(preferences, profil())
    poids = {c.nom: c.poids for c in g.souples}
    assert poids == {"domaine": 0.5, "secteur": 0.5}


def test_profil_incomplet_escalade(ctx, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Camille Martin, étudiante en maintenance industrielle. " * 10, encoding="utf-8")
    sans_email = profil().model_dump(exclude={"cv_pdf"})
    sans_email["identite"]["email"] = ""
    ctx.llm.reponses["profil"] = sans_email | {"manquants": ["téléphone"]}
    resultat = AgentProfil(ctx).executer(cv, {"disponibilite": {"du": "2027-04-06", "au": "2027-06-25"}, "lieux": ["59"]})
    assert resultat is None
    question = ctx.store.escalades_ouvertes()[0]["question"]
    assert "email" in question and "téléphone" in question


def test_profil_complet_enregistre(ctx, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Camille Martin, étudiante en maintenance industrielle. " * 10, encoding="utf-8")
    ctx.llm.reponses["profil"] = profil().model_dump(exclude={"cv_pdf"}) | {"manquants": []}
    p, g = AgentProfil(ctx).executer(cv, {"disponibilite": {"du": "2027-04-06", "au": "2027-06-25"}, "lieux": ["59"], "requetes": ["maintenance"]}, cv_pdf="cv.pdf")
    assert p.cv_pdf == "cv.pdf" and g.souples and ctx.store.parametre("grille") is not None


def test_lecture_docx_zones_de_texte_sans_doublon(tmp_path):
    import zipfile

    from recherche_stage.agents.profil import lire_document

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    document = f"""<w:document {w} {mc}><w:body>
      <w:p><w:r><w:t>Rémi</w:t></w:r><w:r><w:tab/><w:t>Lille</w:t></w:r></w:p>
      <w:p><w:r><mc:AlternateContent>
        <mc:Choice><w:txbxContent><w:p><w:r><w:t>Compétences : GMAO</w:t></w:r></w:p></w:txbxContent></mc:Choice>
        <mc:Fallback><w:txbxContent><w:p><w:r><w:t>Compétences : GMAO</w:t></w:r></w:p></w:txbxContent></mc:Fallback>
      </mc:AlternateContent></w:r><w:r><w:t>Expériences</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>2025</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Stage maintenance</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>"""
    chemin = tmp_path / "cv.docx"
    with zipfile.ZipFile(chemin, "w") as archive:
        archive.writestr("word/document.xml", document)
    assert lire_document(chemin).split("\n") == ["Rémi | Lille", "Expériences", "Compétences : GMAO", "2025", "Stage maintenance"]
