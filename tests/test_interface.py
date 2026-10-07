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
    assert carte(app, "Sans Adresse")["actions"] == ["remplir", "deposer", "rejeter", "ouvrir"]
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
    assert app.cycle["rapport"] and not app.cycle["en_cours"] and app.cycle["nature"] == "reecriture"
    precedente = carte(app, "Exemple Énergie")["dossier"]["precedente"]  # pour comparer les deux versions
    assert precedente["version"] == d["version"] and precedente["objet"] == d["objet"] and precedente["mail"] == d["mail"]
    assert precedente["lettre"] == d["lettre"]
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


def test_mes_candidatures_reponse_et_motif_de_rejet(app, ctx):
    from recherche_stage.agents.apprentissage import AgentApprentissage

    d = carte(app, "Exemple Énergie")["dossier"]
    assert d["justification"] and d["url"] and d["source"]
    app.action({"action": "valider", "dossier": d["id"]})
    app.action({"action": "rejeter", "dossier": carte(app, "Autre Énergie")["dossier"]["id"], "motif": "Trop loin"})
    sans = carte(app, "Sans Adresse")["dossier"]["id"]
    app.action({"action": "valider", "dossier": sans})
    app.action({"action": "deposer", "dossier": sans, "preuve": "site carrières"})

    lignes = {c["entreprise"]: c for c in app.etat()["candidatures"]}
    assert lignes["Exemple Énergie"]["etat"] == "À envoyer" and not lignes["Exemple Énergie"]["repondable"]
    assert lignes["Autre Énergie"]["etat"] == "Rejetée" and lignes["Autre Énergie"]["motif"] == "Trop loin"
    envoyee = lignes["Sans Adresse"]
    assert envoyee["etat"] == "Envoyée" and envoyee["repondable"]
    assert (envoyee["prochaine_relance"] - envoyee["envoyee_le"]).days == 7
    assert AgentApprentissage(ctx).motifs_rejet() == [("trop loin", 1)]

    assert "Refus" in app.action({"action": "reponse", "dossier": sans, "categorie": "refus"})
    assert par_entreprise(ctx, "Sans Adresse").statut == Statut.refus
    with pytest.raises(Refus, match="déjà enregistrée"):
        app.action({"action": "reponse", "dossier": sans, "categorie": "refus"})
    with pytest.raises(Refus, match="inconnue"):
        app.action({"action": "reponse", "dossier": sans, "categorie": "acceptee"})


def test_tournee_sur_place(app, ctx):
    from recherche_stage.modeles import Presentiel

    ctx.config.presentiel.domicile = "Boulogne-sur-Mer"
    ctx.store.definir_parametre("geo:boulogne-sur-mer", [50.7264, 1.6147])
    proche, loin = par_entreprise(ctx, "Exemple Énergie"), par_entreprise(ctx, "Sans Adresse")
    for d, point, km in ((proche, (50.667, 1.833), 17), (loin, (50.95, 1.86), 30)):
        d.offre.latitude, d.offre.longitude = point
        d.presentiel = Presentiel(conseille=True, distance_km=km, raison=f"à {km} km", adresse=f"1 rue {d.offre.entreprise}")
        ctx.store.save(d)
    assert app.etat()["tournee"]["arrets"] == [] and app.etat()["tournee"]["a_valider"] == 2  # rien de validé

    app.action({"action": "valider", "dossier": proche.id})  # mail validé, pas encore parti
    app.action({"action": "valider", "dossier": loin.id})  # sans adresse : à déposer
    t = app.etat()["tournee"]
    assert [a["entreprise"] for a in t["arrets"]] == ["Exemple Énergie", "Sans Adresse"]
    assert t["itineraires"][0].startswith("https://www.google.com/maps/dir/Boulogne-sur-Mer/1%20rue%20Exemple")

    # Déposée en main propre : vaut envoi, le mail validé ne partira plus.
    app.action({"action": "sur_place", "dossier": proche.id})
    d = par_entreprise(ctx, "Exemple Énergie")
    assert d.statut == Statut.envoyee and d.presentiel.depose_le and ctx.store.etat_envoi(d.id) == "envoye"
    assert "main propre" in d.envoi.preuve
    with pytest.raises(Refus, match="déjà marquée"):
        app.action({"action": "sur_place", "dossier": proche.id})

    app.action({"action": "sur_place", "dossier": loin.id})
    assert par_entreprise(ctx, "Sans Adresse").statut == Statut.envoyee
    etat = app.etat()
    assert etat["tournee"]["arrets"] == [] and [e["dossier"]["entreprise"] for e in etat["file"]] == ["Autre Énergie"]
    with pytest.raises(Refus, match="pas dans la tournée"):
        app.action({"action": "sur_place", "dossier": par_entreprise(ctx, "Autre Énergie").id})


def test_apercu_pdf_verifications_et_reglages(app, ctx, tmp_path):
    d = carte(app, "Exemple Énergie")["dossier"]
    assert d["precedente"] is None  # première version
    assert app.lettre_pdf(d["id"]).startswith(b"%PDF")
    with pytest.raises(Refus, match="Pas de lettre"):
        app.lettre_pdf("inconnu")

    cles = {p["cle"]: p for p in app.verifier()}
    assert cles["profil"]["ok"] and "llm" in cles and "excel" not in cles  # pas de tableau configuré

    assert app.etat()["reglages"] is None  # interface lancée sans fichier de configuration
    with pytest.raises(Refus, match="indisponibles"):
        app.action({"action": "reglages", "valeurs": {}})

    chemin = tmp_path / "config.yaml"
    chemin.write_text("presentiel:\n  distance_max_km: 30  # garder ce commentaire\n", encoding="utf-8")
    app.chemin_config = chemin
    proche = par_entreprise(ctx, "Exemple Énergie")
    from recherche_stage.modeles import Presentiel

    proche.presentiel = Presentiel(conseille=False, raison="à 40 km")
    ctx.store.save(proche)
    champs = {c["id"]: c for c in app.etat()["reglages"]}
    assert champs["presentiel.distance_max_km"]["valeur"] == 30
    assert "Distance maximale" in app.action({"action": "reglages", "valeurs": {"presentiel.distance_max_km": "45",
                                                                                  "sources.annuaire_departements": "62, 59"}})
    texte = chemin.read_text(encoding="utf-8")
    assert "distance_max_km: 45.0  # garder ce commentaire" in texte and 'annuaire_departements: ["62", "59"]' in texte
    assert par_entreprise(ctx, "Exemple Énergie").presentiel is None  # rejugé avec le nouveau seuil
    assert app.action({"action": "reglages", "valeurs": {"presentiel.distance_max_km": "45"}}) == "Rien n'a changé."
    with pytest.raises(Refus, match="pas un nombre"):
        app.action({"action": "reglages", "valeurs": {"orchestrateur.dossiers_max_par_cycle": "trois"}})


def test_tester_le_llm(app, ctx):
    from recherche_stage.llm import NonConnecte

    ctx.llm.reponses["verification"] = {"ok": True}
    app.tester_llm()
    assert app.cycle["nature"] == "test" and "✓ Le LLM répond" in app.cycle["lignes"][-1]

    def expire(contenu, schema):
        raise NonConnecte("verification : Claude Code non connecté")

    ctx.llm.reponses["verification"] = expire
    app.tester_llm()
    assert "✗ Session Claude expirée" in app.cycle["lignes"][-1] and "/login" in app.cycle["lignes"][-1]
