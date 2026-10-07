"""Dépôt assisté sur les sites carrière : reconnaissance des champs, page
carrière trouvée par l'Enquête, bouton « Remplir pour moi », et remplissage
réel d'un formulaire local par un navigateur sans écran."""

import json
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest
from test_interface import app, carte  # noqa: F401  (fixture)
from test_revue import orchestrateur, par_entreprise  # noqa: F401  (fixture)

from recherche_stage.depot import champs as reco
from recherche_stage.depot.remplisseur import Remplisseur, deposer
from recherche_stage.interface import Refus

PAGE = "https://suez.example/carrieres/candidature-spontanee"


@pytest.mark.parametrize("champ, cle", [
    ({"libelle": "Prénom *"}, "prenom"),
    ({"name": "first_name"}, "prenom"),
    ({"libelle": "Nom"}, "nom"),
    ({"name": "lastName", "libelle": "Last name"}, "nom"),
    ({"libelle": "Nom de l'entreprise actuelle"}, None),
    ({"libelle": "Nom et prénom"}, "nom_complet"),
    ({"autocomplete": "given-name", "libelle": "Champ 1"}, "prenom"),
    ({"type": "email", "libelle": "Adresse"}, "email"),
    ({"libelle": "Courriel"}, "email"),
    ({"libelle": "Téléphone portable"}, "telephone"),
    ({"libelle": "Ville de résidence"}, "ville"),
    ({"libelle": "École / Université"}, "ecole"),
    ({"libelle": "Diplôme préparé"}, "diplome"),
    ({"type": "file", "libelle": "Votre CV (PDF)"}, "cv"),
    ({"type": "file", "libelle": "Lettre de motivation"}, "lettre_fichier"),
    ({"type": "file", "libelle": "Pièce jointe"}, "cv"),
    ({"balise": "textarea", "libelle": "Votre message"}, "lettre_texte"),
    ({"balise": "textarea", "libelle": "Numéro de sécurité sociale"}, None),
    ({"balise": "select", "libelle": "Pays"}, None),
    ({"type": "password", "libelle": "Mot de passe"}, None),
    ({"libelle": "Numéro de sécurité sociale"}, None),
])
def test_reconnaissance_des_champs(champ, cle):
    assert reco.reconnaitre({"balise": "input", "type": "text", **champ}) == cle


def test_questions_ouvertes_et_confirmation():
    questions = {"Pourquoi souhaitez-vous rejoindre notre entreprise ?": "Parce que…"}
    champ = {"balise": "textarea", "libelle": "Pourquoi souhaitez-vous rejoindre notre entreprise"}
    assert reco.reconnaitre(champ, questions) == "question:Pourquoi souhaitez-vous rejoindre notre entreprise ?"
    assert reco.est_confirmation("Merci pour votre candidature ! Nous revenons vers vous.")
    assert reco.est_confirmation("Thank you for applying")
    assert not reco.est_confirmation("Envoyer votre candidature")
    assert reco.blocage([{"type": "password", "visible": True}], []) == "connexion"
    assert reco.blocage([], ["https://www.google.com/recaptcha/api2/anchor"]) == "captcha"


# ------------------------------------------------------- orchestrateur, interface


def test_remplir_trouve_la_page_reserve_et_lance_le_navigateur(app, ctx, orchestrateur):  # noqa: F811
    d = carte(app, "Sans Adresse")["dossier"]
    app.action({"action": "valider", "dossier": d["id"]})
    assert carte(app, "Sans Adresse")["actions"][0] == "remplir"
    ctx.llm.recherche = (f"Candidature spontanée : {PAGE}", [PAGE])
    ctx.llm.reponses["enquete"] = {"url": PAGE}
    lances = []
    app.lanceur_depot = lances.append
    app.action({"action": "remplir", "dossier": d["id"]})
    assert app.cycle["erreur"] is None, app.cycle["erreur"]

    dossier = par_entreprise(ctx, "Sans Adresse")
    assert dossier.fiche_entreprise.page_carriere == PAGE
    assert ctx.store.etat_envoi(dossier.id) == "en_cours"  # réservé : jamais deux dépôts
    tache = json.loads(lances[0].read_text(encoding="utf-8"))
    assert tache["url"] == PAGE and tache["valeurs"]["prenom"] and tache["valeurs"]["lettre_texte"]
    assert tache["fichiers"]["lettre_fichier"].endswith(".pdf")
    vue = carte(app, "Sans Adresse")["dossier"]
    assert vue["page_carriere"] == PAGE and vue["depot"]["etat"] == "ouverture"

    # Le dépôt confirmé à la main lance le suivi.
    app.action({"action": "deposer", "dossier": d["id"], "preuve": "site carrières"})
    with pytest.raises(Refus):
        app.action({"action": "remplir", "dossier": d["id"]})


def test_page_introuvable_rien_n_est_invente(app, ctx, orchestrateur):  # noqa: F811
    d = carte(app, "Sans Adresse")["dossier"]
    app.action({"action": "valider", "dossier": d["id"]})
    ctx.llm.recherche = ("Rien de fiable.", ["https://autre.example/"])
    ctx.llm.reponses["enquete"] = {"url": "https://inventee.example/carrieres"}  # jamais vue par la recherche
    app.lanceur_depot = lambda tache: pytest.fail("navigateur lancé sans page")
    app.action({"action": "remplir", "dossier": d["id"]})
    assert "introuvable" in app.cycle["erreur"]
    assert par_entreprise(ctx, "Sans Adresse").fiche_entreprise.page_carriere == ""


# ------------------------------------------------------------ navigateur réel

FORMULAIRE = """<!doctype html><meta charset="utf-8"><title>Candidature</title>
<form action="/merci.html" method="get">
  <label for="p">Prénom *</label><input id="p" name="firstname" required>
  <label for="n">Nom *</label><input id="n" name="lastname" required>
  <div class="champ"><span>Adresse e-mail *</span><input name="contact" type="email" required></div>
  <input name="phone" placeholder="Téléphone">
  <label>Nom de votre employeur actuel <input name="employeur"></label>
  <label for="ss">Numéro de sécurité sociale *</label><input id="ss" name="ss" required>
  <label for="cv">Votre CV</label><input id="cv" type="file" name="cv">
  <label for="lm">Lettre de motivation (fichier)</label><input id="lm" type="file" name="lm">
  <label for="msg">Votre message</label><textarea id="msg" name="msg"></textarea>
  <label><input type="checkbox" name="rgpd" required> J'accepte la politique de confidentialité</label>
  <button type="submit">Envoyer ma candidature</button>
</form>"""
CARRIERES = """<!doctype html><meta charset="utf-8"><title>Carrières</title>
<input type="search" placeholder="Rechercher une offre"><a href="/formulaire.html">Candidature spontanée</a>"""
CONNEXION = """<!doctype html><meta charset="utf-8"><label>E-mail <input name="login"></label>
<label>Mot de passe <input type="password" name="pw"></label><button>Se connecter</button>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    racine = tmp_path_factory.mktemp("site")
    for nom, html in {"formulaire.html": FORMULAIRE, "carrieres.html": CARRIERES, "connexion.html": CONNEXION,
                      "merci.html": "<p>Merci pour votre candidature !</p>"}.items():
        (racine / nom).write_text(html, encoding="utf-8")
    class Silencieux(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    serveur = HTTPServer(("127.0.0.1", 0), partial(Silencieux, directory=str(racine)))
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{serveur.server_address[1]}"
    serveur.shutdown()


@pytest.fixture(scope="module")
def navigateur():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = None
        for options in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):  # Chromium préinstallé d'une autre version
            try:
                b = p.chromium.launch(headless=True, **options)
                break
            except Exception as e:
                erreur = e
        if b is None:
            pytest.skip(f"Chromium indisponible : {erreur}")
        yield b
        b.close()


@pytest.fixture
def remplisseur(tmp_path):
    cv, lettre = tmp_path / "cv.pdf", tmp_path / "Lettre_motivation.pdf"
    cv.write_bytes(b"%PDF-1.4 cv"), lettre.write_bytes(b"%PDF-1.4 lettre")
    valeurs = {"prenom": "Rémi", "nom": "Test", "email": "remi@example.org", "telephone": "0600000000",
               "lettre_texte": "Madame, Monsieur, …"}
    return Remplisseur(valeurs, {"cv": str(cv), "lettre_fichier": str(lettre)})


def test_remplit_joint_et_ne_clique_jamais_envoyer(navigateur, site, remplisseur):
    page = navigateur.new_page()
    page.goto(f"{site}/carrieres.html")
    assert remplisseur.passe(page).etat == "ouverture"
    assert remplisseur.vers_formulaire(page)  # suit « Candidature spontanée »
    etat = remplisseur.passe(page)
    assert page.url.endswith("/formulaire.html")
    assert page.input_value("#p") == "Rémi" and page.input_value("#n") == "Test"
    assert page.input_value("[name=contact]") == "remi@example.org" and page.input_value("[name=phone]") == "0600000000"
    assert page.input_value("#msg").startswith("Madame")
    assert page.input_value("[name=employeur]") == ""  # pas le nom du candidat dans « employeur »
    assert page.eval_on_selector("#cv", "e => e.files[0].name") == "cv.pdf"
    assert page.eval_on_selector("#lm", "e => e.files[0].name") == "Lettre_motivation.pdf"
    assert not page.is_checked("[name=rgpd]")  # le consentement reste à l'utilisateur
    assert etat.etat == "manque" and etat.manquants == ["Numéro de sécurité sociale *"]

    page.fill("#ss", "1")  # l'utilisateur complète ; une nouvelle passe ne réécrit rien
    page.fill("#p", "Remi")
    assert remplisseur.passe(page).etat == "rempli" and page.input_value("#p") == "Remi"
    assert page.url.endswith("/formulaire.html")  # rien n'a été envoyé


def test_connexion_rendue_a_l_utilisateur_puis_confirmation(navigateur, site, remplisseur, tmp_path):
    page = navigateur.new_page()
    page.goto(f"{site}/connexion.html")
    etat = remplisseur.passe(page)
    assert etat.etat == "connexion" and page.input_value("[name=pw]") == ""
    page.goto(f"{site}/merci.html")
    assert remplisseur.passe(page).etat == "confirme"

    contexte = navigateur.new_context()
    chemin = tmp_path / "etat.json"
    final = deposer(contexte, f"{site}/merci.html", remplisseur, chemin, duree_max=2)
    assert final.etat == "confirme" and json.loads(chemin.read_text(encoding="utf-8"))["etat"] == "confirme"
    contexte.close()
