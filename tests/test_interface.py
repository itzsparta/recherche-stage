"""`rs interface` : actions de la page et protections du serveur local."""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest
from test_revue import orchestrateur, par_entreprise  # noqa: F401  (fixture)

from recherche_stage.interface import Application, Refus, gestionnaire
from recherche_stage.modeles import Statut


@pytest.fixture
def app(ctx, orchestrateur):  # noqa: F811
    ouverts = []
    application = Application(lambda: (ctx, orchestrateur), ouvrir=ouverts.append, arriere_plan=False)
    application.ouverts = ouverts
    return application


def carte(app, entreprise):
    return next(e for e in app.etat()["file"] if e["dossier"] and e["dossier"]["entreprise"] == entreprise)


def test_etat_montre_la_file_et_les_documents(app):
    etat = app.etat()
    assert etat["statuts"]["en_attente_humain"] == 3 and etat["envois"] == [0, 15]
    e = carte(app, "Exemple Énergie")
    assert e["actions"] == ["valider", "reecrire", "rejeter", "ouvrir"]
    assert e["dossier"]["modifiable"] and e["dossier"]["mail"].startswith("Bonjour") and e["dossier"]["lettre"]


def test_valider_tel_quel_corrige_rejeter_et_deposer(app, ctx):
    d = carte(app, "Exemple Énergie")["dossier"]
    assert "part à recrutement@" in app.action({"action": "valider", "dossier": d["id"], "objet": d["objet"],
                                                "mail": d["mail"], "lettre": d["lettre"]})
    assert ctx.store.decisions_humaines()[-1]["decision"] == "valide_tel_quel"

    d = carte(app, "Autre Énergie")["dossier"]
    app.action({"action": "valider", "dossier": d["id"], "objet": d["objet"], "lettre": d["lettre"],
                "mail": d["mail"].replace("Bonjour", "Madame, Monsieur")})
    assert ctx.store.decisions_humaines()[-1]["decision"] == "modifie"
    assert par_entreprise(ctx, "Autre Énergie").documents.mail.startswith("Madame, Monsieur")

    d = carte(app, "Sans Adresse")["dossier"]
    assert "déposer" in app.action({"action": "valider", "dossier": d["id"]})
    assert carte(app, "Sans Adresse")["actions"] == ["deposer", "rejeter", "ouvrir"]
    app.action({"action": "ouvrir", "dossier": d["id"]})
    assert app.ouverts[-1].endswith(d["id"])
    app.action({"action": "deposer", "dossier": d["id"], "preuve": "site carrières"})
    assert par_entreprise(ctx, "Sans Adresse").statut == Statut.envoyee
    assert app.etat()["file"] == []


def test_corrections_refusees_et_actions_perimees(app, ctx):
    d = carte(app, "Exemple Énergie")["dossier"]
    with pytest.raises(Refus, match="Correction refusée"):
        app.action({"action": "valider", "dossier": d["id"], "objet": d["objet"], "lettre": d["lettre"],
                    "mail": d["mail"] + "\n[NOM DU RECRUTEUR]"})
    app.action({"action": "rejeter", "dossier": d["id"], "motif": "pas pour moi"})
    with pytest.raises(Refus, match="plus possible"):
        app.action({"action": "valider", "dossier": d["id"]})


def test_reecrire_avec_consigne_et_preferences(app, ctx):
    assert "enregistrées" in app.action({"action": "consignes", "texte": "Parcours académique d'abord, TIPE en une phrase."})
    assert app.etat()["consignes"].startswith("Parcours")
    d = carte(app, "Exemple Énergie")["dossier"]
    assert "Réécriture" in app.action({"action": "reecrire", "dossier": d["id"], "consigne": "moins sur le TIPE"})

    redaction = [c for a, c, _ in ctx.llm.appels if a == "redaction"][-1]
    assert "PRÉFÉRENCES DU CANDIDAT" in redaction and "Parcours académique d'abord" in redaction
    assert "DEMANDE DU CANDIDAT POUR CETTE VERSION" in redaction and "moins sur le TIPE" in redaction
    assert "VERSION PRÉCÉDENTE" in redaction
    nouveau = par_entreprise(ctx, "Exemple Énergie")
    assert nouveau.documents.version == d["version"] + 1 and nouveau.etape_attente == "envoi"  # de retour dans la file
    assert ctx.store.decisions_humaines()[-1]["decision"] == "reecrire"
    assert app.cycle["rapport"] and not app.cycle["en_cours"]
    with pytest.raises(Refus, match="changer"):
        app.action({"action": "reecrire", "dossier": d["id"], "consigne": "  "})


def test_cycle_depuis_la_page(app):
    app.lancer_cycle({"sans_suivi": True})
    assert not app.cycle["en_cours"] and app.cycle["rapport"] is not None and app.cycle["lignes"][0].startswith("Collecte")


@pytest.fixture
def serveur(app):
    s = HTTPServer(("127.0.0.1", 0), None)
    port = s.server_address[1]
    s.RequestHandlerClass = gestionnaire(app, {f"127.0.0.1:{port}"})
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    s.shutdown()
    s.server_close()


def appel(url, donnees=None, **entetes):
    corps = json.dumps(donnees).encode() if donnees is not None else None
    requete = urllib.request.Request(url, data=corps, headers=entetes)
    try:
        with urllib.request.urlopen(requete) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_serveur_page_jeton_et_hote(serveur, app):
    statut, page = appel(serveur + "/")
    assert statut == 200 and app.jeton in page and "__JETON__" not in page
    assert appel(serveur + "/api/etat")[0] == 200
    assert appel(serveur + "/api/etat", Host="attaquant.example")[0] == 403  # rebinding DNS
    action = {"action": "fermer", "escalade": 1}
    assert appel(serveur + "/api/action", action, **{"Content-Type": "application/json"})[0] == 403  # sans jeton
    assert appel(serveur + "/api/action", action, **{"X-Jeton": app.jeton, "Content-Type": "text/plain"})[0] == 403
    statut, corps = appel(serveur + "/api/action", {"action": "valider", "dossier": "inconnu"},
                          **{"X-Jeton": app.jeton, "Content-Type": "application/json"})
    assert statut == 409 and "inconnu" in corps
