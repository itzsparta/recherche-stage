"""Démarches faites hors de l'outil (forum, recommandation) : lecture du fichier,
tableau Excel, pas de candidature spontanée en double, rappels de relance."""

import random
from datetime import date, datetime

import pytest
from conftest import FauxExpediteur, evaluation_offre, offre
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

from recherche_stage.agents.scoring import AgentScoring
from recherche_stage.demarches import Demarche, ErreurDemarches, charger, correspondance
from recherche_stage.excel import mettre_a_jour
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Dossier, Statut
from recherche_stage.orchestrateur import Orchestrateur

FICHIER = """
demarches:
  - entreprise: Dillinger France
    statut: envoyee
    date: 2026-09-28
    canal: Recommandation d'un salarié
    lieu: Grande-Synthe (59)
  - entreprise: STEF
    statut: a_faire
    date: 2026-10-08
    canal: Forum AgoraJob
    notes: plusieurs sites en Hauts-de-France, se présenter directement
  - entreprise: Assystem
    statut: en_attente
    date: 2026-10-08
    canal: Forum AgoraJob
    lieu: Beauvais (60)
    notes: alternance l'an prochain
    relance: 2026-10-08
  - entreprise: Engie / Enedis
    date: 2026-10-08
    canal: Forum AgoraJob
    notes: stand visité
"""


@pytest.fixture
def demarches(ctx, tmp_path):
    chemin = tmp_path / "demarches.yaml"
    chemin.write_text(FICHIER, encoding="utf-8")
    ctx.config.suivi.demarches = str(chemin)
    return chemin


def test_lecture_et_correspondance(demarches, tmp_path):
    liste = charger(demarches)
    assert [d.statut for d in liste] == ["envoyee", "a_faire", "en_attente", "contact"]
    assert correspondance(liste, "STEF TRANSPORT LILLE", "Lesquin (59)").entreprise == "STEF"  # tous ses sites
    assert correspondance(liste, "DILLINGER FRANCE (DILLINGER FRANCE SAS)", "DUNKERQUE (59640)").entreprise == "Dillinger France"
    assert correspondance(liste, "Dillinger France", "Metz (57)") is None  # autre département
    assert correspondance(liste, "ENEDIS ENGIE") and correspondance(liste, "ENGIE") is None  # tous les mots du nom
    assert charger(tmp_path / "absent.yaml") == []
    (tmp_path / "faux.yaml").write_text("demarches:\n  - entreprise: X\n    statut: envoyé\n", encoding="utf-8")
    with pytest.raises(ErreurDemarches, match="démarche X : statut"):
        charger(tmp_path / "faux.yaml")


def test_pas_de_spontanee_en_double_mais_offres_en_tete(ctx, demarches):
    ctx.llm.reponses["scoring"] = evaluation_offre()
    spontanee = offre(entreprise="STEF TRANSPORT LILLE", intitule="Candidature spontanée", type="spontanee", lieu="Lesquin (59)")
    offre_stef = offre(entreprise="STEF", intitule="Stage logistique entrepôt", lieu="Lesquin (59)")
    rencontree = offre(entreprise="ENEDIS ENGIE", intitule="Candidature spontanée", type="spontanee")
    dossiers = [Dossier(id=id_dossier(o.entreprise, o.intitule), offre=o) for o in (spontanee, offre_stef, rencontree)]
    AgentScoring(ctx).executer(dossiers)
    bloque, offre_gardee, contact = dossiers
    assert bloque.statut == Statut.abandonnee and bloque.motif.startswith("deja_en_contact : STEF, Forum AgoraJob le 08/10/2026")
    assert offre_gardee.statut == Statut.scoree and offre_gardee.prioritaire  # une offre précise reste proposée, en tête
    assert contact.statut == Statut.scoree and contact.prioritaire  # stand visité : rien de bloqué


def test_tableau_excel_et_rappel_de_relance(ctx, demarches, tmp_path, horloge):
    wb = Workbook()
    ws = wb.active
    ws.append(["Suivi candidatures"])
    ws.append([])
    ws.append(["Entreprise", "Activité", "Lieu", "Contact", "Date d'envoi", "Statut", "Relance le", "Notes"])
    ws.append(["Dillinger France", "Tôles fortes", "Grande-Synthe (59)", "Recommandé par un employé", date(2026, 9, 28),
               "Entretien", None, "Relancer via l'employé"])
    ws.append(["Assystem", None, "Lille (59)", None, None, "À envoyer", None, None])  # même nom, autre site
    for n in range(5, 12):
        ws.cell(n, 7, f'=IF(E{n}="","",E{n}+10)')
    liste = DataValidation(type="list", formula1='"À envoyer,Envoyée,Relancée,Entretien,Acceptée,Refusée"')
    ws.add_data_validation(liste)
    liste.add("F4:F60")
    chemin = tmp_path / "suivi.xlsx"
    wb.save(chemin)

    assert mettre_a_jour(chemin, [], demarches=charger(demarches)) == (3, 1)  # la ligne Assystem du Nord : pas touchée
    assert mettre_a_jour(chemin, [], demarches=charger(demarches)) == (0, 4)  # retrouvées : pas de ligne en double
    ws = load_workbook(chemin).active
    lignes = {ws.cell(n, 1).value: [c.value for c in ws[n]] for n in range(4, ws.max_row + 1) if ws.cell(n, 1).value}
    assert lignes["Dillinger France"][5] == "Entretien" and lignes["Dillinger France"][7] == "Relancer via l'employé"  # à toi
    assert lignes["STEF"][5] == "À envoyer" and lignes["STEF"][7].startswith("Forum AgoraJob (08/10/2026) : plusieurs sites")
    assert lignes["STEF"][4] is None and lignes["STEF"][6].startswith("=IF")  # pas d'envoi : ni date, ni relance inventée
    assert [ws.cell(n, 6).value for n in range(4, ws.max_row + 1) if ws.cell(n, 1).value == "Assystem"] == ["À envoyer", None]
    assert lignes["Assystem"][5] is None and lignes["Assystem"][6].date() == date(2026, 10, 8)  # « En attente » absent de la liste
    assert lignes["Engie / Enedis"][7] == "Forum AgoraJob (08/10/2026) : stand visité"

    o = Orchestrateur(ctx, [], FauxExpediteur(), rng=random.Random(0), dormir=lambda s: None)
    assert o.rappeler_demarches() == 0  # le 5 octobre : pas encore
    horloge.instant = datetime(2026, 10, 9, 9, 0)
    assert o.rappeler_demarches() == 1 and o.rappeler_demarches() == 0  # un seul rappel par date
    rappel = next(e for e in ctx.store.escalades_ouvertes() if e["type"] == "relance:Assystem")
    assert "Relance prévue le 08/10/2026 : Assystem (Forum AgoraJob" in rappel["question"]
    assert Demarche(entreprise="X", statut="contact").bloque_spontanee is False
