from datetime import timedelta

from conftest import offre

from recherche_stage.hachage import empreinte, id_dossier
from recherche_stage.modeles import Dossier, Statut
from recherche_stage.store import Store


def test_id_dossier_insensible_aux_variantes_de_saisie():
    a = id_dossier("Exemple Énergie SAS", "Stage maintenance industrielle (H/F)")
    b = id_dossier("exemple energie", "Stage - Maintenance industrielle F/H")
    c = id_dossier("EXEMPLE ÉNERGIE", "stagiaire maintenance industrielle h/f/x")
    assert a == b == c


def test_id_dossier_distingue_postes_et_entreprises():
    assert id_dossier("Exemple Énergie", "Stage maintenance") != id_dossier("Exemple Énergie", "Stage méthodes")
    assert id_dossier("Exemple Énergie", "Stage maintenance") != id_dossier("Autre Énergie", "Stage maintenance")


def test_empreinte_stable_quel_que_soit_l_ordre_des_cles():
    assert empreinte({"a": 1, "b": [1, 2]}) == empreinte({"b": [1, 2], "a": 1})
    assert empreinte({"a": 1}) != empreinte({"a": 2})


def test_envoi_reserve_une_seule_fois(horloge):
    store = Store(":memory:", horloge=horloge)
    assert store.reserver_envoi("d1", "candidature", "email")
    assert not store.reserver_envoi("d1", "candidature", "email")
    store.confirmer_envoi("d1", "candidature", "<id@x>")
    assert store.etat_envoi("d1") == "envoye"
    assert not store.reserver_envoi("d1", "candidature", "email")
    assert store.reserver_envoi("d1", "relance1", "email")  # autre nature
    assert store.envois_semaine() == 1


def test_liberer_envoi_ne_touche_pas_un_envoi_confirme(horloge):
    store = Store(":memory:", horloge=horloge)
    store.reserver_envoi("d1", "candidature", "email")
    store.confirmer_envoi("d1", "candidature", "p")
    store.liberer_envoi("d1", "candidature")
    assert store.etat_envoi("d1") == "envoye"


def test_entrees_memorisees_pour_rejouer(horloge):
    store = Store(":memory:", horloge=horloge)
    h = store.memoriser_entrees({"offre": offre()})
    assert store.entrees(h)["offre"]["entreprise"] == "Exemple Énergie"


def test_purge_rgpd_conserve_la_trace_anti_doublon(horloge):
    store = Store(":memory:", horloge=horloge)
    d = Dossier(id="d1", offre=offre(), statut=Statut.refus)
    store.save(d)
    store.reserver_envoi("d1", "candidature", "email")
    store.journaliser("test", "x", "d1")
    horloge.instant += timedelta(days=400)
    assert store.purger(horloge() - timedelta(days=365)) == 1
    assert store.get("d1") is None
    assert store.journal("d1") == []
    assert store.etat_envoi("d1") is not None


def test_escalade_non_dupliquee(horloge):
    store = Store(":memory:", horloge=horloge)
    a = store.escalader("d1", "critique", "q1")
    b = store.escalader("d1", "critique", "q2")
    assert a == b and len(store.escalades_ouvertes()) == 1
    store.resoudre_escalades("d1")
    assert store.escalades_ouvertes() == []
