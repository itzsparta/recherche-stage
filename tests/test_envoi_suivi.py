from datetime import datetime, timedelta

import pytest

from conftest import FausseBoite, FauxExpediteur, classification, offre, preparation, sortie_redaction

from recherche_stage.agents.envoi import AgentEnvoi
from recherche_stage.agents.redaction import AgentRedaction
from recherche_stage.agents.suivi import AgentSuivi
from recherche_stage.autonomie import envoi_automatique_debloque, raisons_validation
from recherche_stage.canaux.messagerie import MessageRecu
from recherche_stage.hachage import id_dossier
from recherche_stage.modeles import Dossier, Score, Statut


def dossier_valide(ctx, **champs) -> Dossier:
    o = offre(**champs)
    d = Dossier(id=id_dossier(o.entreprise, o.intitule), offre=o, statut=Statut.enquetee, score=Score(valeur=80, confiance=0.9))
    ctx.llm.reponses["redaction"] = sortie_redaction()
    AgentRedaction(ctx).executer(d)
    d.statut = Statut.validee
    return d


def debloquer_autonomie(ctx, n=30):
    for i in range(n):
        ctx.store.enregistrer_decision(f"x{i}", "valide_tel_quel")


# ------------------------------------------------------------ autonomie


def test_porte_humaine_initiale(ctx):
    d = dossier_valide(ctx)
    d.envoi.canal = "email"
    assert any(r.startswith("validation_humaine_avant_envoi") for r in raisons_validation(d, ctx.store, ctx.config))


def test_levee_a_95_pourcent_sur_30(ctx):
    debloquer_autonomie(ctx, 28)
    ctx.store.enregistrer_decision("y", "modifie")
    assert not envoi_automatique_debloque(ctx.store, ctx.config)  # 29 décisions seulement
    ctx.store.enregistrer_decision("z", "valide_tel_quel")
    assert envoi_automatique_debloque(ctx.store, ctx.config)  # 29/30 = 96,7 %
    ctx.store.enregistrer_decision("w", "modifie")
    assert not envoi_automatique_debloque(ctx.store, ctx.config)  # 28/30


def test_validation_toujours_requise(ctx):
    debloquer_autonomie(ctx)
    d = dossier_valide(ctx)
    d.envoi.canal = "email"
    assert raisons_validation(d, ctx.store, ctx.config) == []
    d.prioritaire = True
    assert "entreprise_prioritaire" in raisons_validation(d, ctx.store, ctx.config)
    d.prioritaire, d.score.confiance = False, 0.3
    assert "confiance_faible" in raisons_validation(d, ctx.store, ctx.config)
    d.score.confiance, d.documents.reponses_formulaire = 0.9, {"Pourquoi nous ?": "..."}
    assert "formulaire_avec_questions_ouvertes" in raisons_validation(d, ctx.store, ctx.config)


# ----------------------------------------------------------------- envoi


def test_choix_du_canal(ctx):
    envoi = AgentEnvoi(ctx)
    assert envoi.choisir_canal(dossier_valide(ctx))[0] == "email"
    assert envoi.choisir_canal(dossier_valide(ctx, contact_email=None))[0] == "plateforme"
    assert envoi.choisir_canal(dossier_valide(ctx, contact_email=None, source="greenhouse"))[0] == "formulaire"


def test_prochain_creneau(ctx):
    envoi = AgentEnvoi(ctx)
    vendredi_soir = datetime(2026, 10, 9, 18, 0)
    assert envoi.prochain_creneau(vendredi_soir) == datetime(2026, 10, 12, 8, 0)
    assert envoi.prochain_creneau(datetime(2026, 10, 5, 9, 30)) == datetime(2026, 10, 5, 9, 30)


def test_brouillon_et_porte_humaine(ctx):
    d = dossier_valide(ctx)
    AgentEnvoi(ctx).preparer(d)
    assert d.statut == Statut.en_attente_humain and d.etape_attente == "envoi"
    fichiers = ctx.dossier_fichiers(d.id)
    assert (fichiers / "brouillon.eml").exists()
    assert list(fichiers.glob("Lettre_motivation_*.pdf"))


def test_envoi_automatique_idempotent(ctx):
    debloquer_autonomie(ctx)
    expediteur = FauxExpediteur()
    envoi = AgentEnvoi(ctx, expediteur)
    d = dossier_valide(ctx)
    envoi.preparer(d)
    assert d.statut == Statut.validee and d.envoi.automatique
    assert envoi.envoyer(d) is True
    assert d.statut == Statut.envoyee and (ctx.dossier_fichiers(d.id) / "envoye.eml").exists()
    message = expediteur.envoyes[0]
    assert {p.get_filename() for p in message.iter_attachments()} >= {"cv.pdf"}
    d.statut = Statut.validee  # même si l'état local se perdait…
    assert envoi.envoyer(d) is False  # … la base refuse le second envoi
    assert len(expediteur.envoyes) == 1 and d.statut == Statut.envoyee


def test_retry_unique_puis_escalade(ctx):
    debloquer_autonomie(ctx)
    expediteur = FauxExpediteur(echecs=2)
    envoi = AgentEnvoi(ctx, expediteur)
    d = dossier_valide(ctx)
    envoi.preparer(d)
    assert envoi.envoyer(d) is False
    assert d.statut == Statut.erreur
    assert ctx.store.etat_envoi(d.id) == "echec"
    assert ctx.store.escalades_ouvertes()[-1]["type"] == "echec_envoi"


def test_un_echec_puis_succes(ctx):
    debloquer_autonomie(ctx)
    expediteur = FauxExpediteur(echecs=1)
    envoi = AgentEnvoi(ctx, expediteur)
    d = dossier_valide(ctx)
    envoi.preparer(d)
    assert envoi.envoyer(d) is True


def test_budget_hebdomadaire(ctx):
    debloquer_autonomie(ctx)
    ctx.config.orchestrateur.candidatures_max_par_semaine = 1
    ctx.store.reserver_envoi("autre", "candidature", "email")
    envoi = AgentEnvoi(ctx, FauxExpediteur())
    d = dossier_valide(ctx)
    envoi.preparer(d)
    assert envoi.envoyer(d) is False and d.statut == Statut.validee


# ----------------------------------------------------------------- suivi


def dossier_envoye(ctx, horloge, jours=0) -> Dossier:
    d = dossier_valide(ctx)
    d.statut = Statut.envoyee
    d.envoi.canal = "email"
    d.envoi.destinataire = "recrutement@exemple-energie.fr"
    d.envoi.horodatage = horloge() - timedelta(days=jours)
    d.envoi.preuve = "<abc@exemple.fr> ; copie : x"
    ctx.store.save(d)
    return d


def message(**champs):
    base = dict(message_id="<r1@exemple-energie.fr>", expediteur="rh@exemple-energie.fr", objet="Votre candidature",
                corps="Nous ne donnerons pas suite.", date=datetime(2026, 10, 12), references=["<abc@exemple.fr>"])
    base.update(champs)
    return MessageRecu(**base)


def test_reponse_refus_rattachee_et_memorisee(ctx, horloge):
    d = dossier_envoye(ctx, horloge, jours=3)
    ctx.llm.reponses["suivi"] = classification("refus")
    suivi = AgentSuivi(ctx, FausseBoite([message()]))
    assert suivi.traiter_reponses() == 1
    d = ctx.store.get(d.id)
    assert d.statut == Statut.refus and d.suivi.reponse == "refus"
    assert ctx.store.memoire("exemple energie")["contact"] == "rh@exemple-energie.fr"
    assert suivi.traiter_reponses() == 0  # message déjà traité


def test_entretien_cree_ics_et_fiche(ctx, horloge):
    d = dossier_envoye(ctx, horloge)
    ctx.llm.reponses["suivi"] = [classification("entretien", date_entretien="2026-10-20T14:00"), preparation()]
    AgentSuivi(ctx, FausseBoite([message(references=[], corps="Seriez-vous disponible mardi 20 à 14 h ?")])).traiter_reponses()
    d = ctx.store.get(d.id)
    assert d.statut == Statut.entretien and d.suivi.entretien_le == datetime(2026, 10, 20, 14, 0)
    fichiers = ctx.dossier_fichiers(d.id)
    assert "DTSTART:20261020T140000" in (fichiers / "entretien.ics").read_text()
    assert "Parlez-moi de votre stage" in (fichiers / "preparation_entretien.md").read_text()


def test_classification_incertaine_escaladee(ctx, horloge):
    d = dossier_envoye(ctx, horloge)
    ctx.llm.reponses["suivi"] = classification("entretien", confiance=0.4)
    AgentSuivi(ctx, FausseBoite([message()])).traiter_reponses()
    assert ctx.store.get(d.id).statut == Statut.envoyee
    assert ctx.store.escalades_ouvertes()[-1]["type"] == "classification"


def test_message_sans_rapport_ignore(ctx, horloge):
    dossier_envoye(ctx, horloge)
    suivi = AgentSuivi(ctx, FausseBoite([message(expediteur="promo@boutique.com", references=[], objet="Soldes", corps="Promo")]))
    assert suivi.traiter_reponses() == 0


def test_relances_j7_et_j14(ctx, horloge):
    debloquer_autonomie(ctx)
    expediteur = FauxExpediteur()
    d = dossier_envoye(ctx, horloge, jours=6)
    suivi = AgentSuivi(ctx, expediteur=expediteur)
    assert suivi.relances_dues() == []
    horloge.instant += timedelta(days=1)
    assert suivi.executer()["relances"] == 1
    d = ctx.store.get(d.id)
    assert d.statut == Statut.relancee and len(d.suivi.relances) == 1
    assert expediteur.envoyes[0]["In-Reply-To"] == "<abc@exemple.fr>"
    assert suivi.executer()["relances"] == 0  # pas de doublon
    horloge.instant += timedelta(days=7)
    assert suivi.executer()["relances"] == 1
    horloge.instant += timedelta(days=30)
    assert suivi.executer()["relances"] == 0  # deux relances au plus


def test_relance_soumise_a_validation_sans_autonomie(ctx, horloge):
    d = dossier_envoye(ctx, horloge, jours=8)
    suivi = AgentSuivi(ctx, expediteur=FauxExpediteur())
    assert suivi.executer()["relances"] == 0
    d = ctx.store.get(d.id)
    assert d.etape_attente == "relance:1"
    assert (ctx.dossier_fichiers(d.id) / "relance1.txt").exists()


def test_boite_imap_injoignable_erreur_explicite(monkeypatch):
    import socket

    from recherche_stage.canaux.messagerie import BoiteIMAP
    from recherche_stage.config import ParamsIMAP
    from recherche_stage.reseau import ErreurReseau

    with socket.socket() as s:  # port local libre, donc fermé
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("IMAP_MOT_DE_PASSE", "x")
    boite = BoiteIMAP(ParamsIMAP(hote="127.0.0.1", port=port, utilisateur="moi@exemple.fr"))
    with pytest.raises(ErreurReseau, match=f"127.0.0.1:{port}"):
        boite.messages_depuis(datetime(2026, 1, 1))


def test_spontanee_sans_adresse_ne_parle_pas_de_formulaire(ctx):
    d = dossier_valide(ctx, type="spontanee", source="annuaire", contact_email="",
                       url="https://annuaire-entreprises.data.gouv.fr/entreprise/123456789")
    AgentEnvoi(ctx).preparer(d)
    [alerte] = ctx.store.escalades_ouvertes()
    assert "aucune adresse publique" in alerte["question"] and "formulaire →" not in alerte["question"]
