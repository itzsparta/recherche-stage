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
from recherche_stage.depot import lancement
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


@pytest.fixture
def playwright_present(monkeypatch):
    monkeypatch.setattr(lancement, "disponible", lambda: True)  # le navigateur lui-même est remplacé


def test_sans_playwright_le_bouton_explique_quoi_installer(app, monkeypatch):  # noqa: F811
    d = carte(app, "Sans Adresse")["dossier"]
    app.action({"action": "valider", "dossier": d["id"]})
    monkeypatch.setattr(lancement, "disponible", lambda: False)
    with pytest.raises(Refus, match="pip install playwright"):
        app.action({"action": "remplir", "dossier": d["id"]})


def test_remplir_trouve_la_page_reserve_et_lance_le_navigateur(app, ctx, orchestrateur, playwright_present):  # noqa: F811
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


def test_page_introuvable_rien_n_est_invente(app, ctx, orchestrateur, playwright_present):  # noqa: F811
    d = carte(app, "Sans Adresse")["dossier"]
    app.action({"action": "valider", "dossier": d["id"]})
    ctx.llm.recherche = ("Rien de fiable.", ["https://autre.example/"])
    ctx.llm.reponses["enquete"] = {"url": "https://inventee.example/carrieres"}  # jamais vue par la recherche
    app.lanceur_depot = lambda tache: pytest.fail("navigateur lancé sans page")
    app.action({"action": "remplir", "dossier": d["id"]})
    assert "introuvable" in app.cycle["erreur"]
    assert par_entreprise(ctx, "Sans Adresse").fiche_entreprise.page_carriere == ""
    assert any("écartée" in ligne and "inventee.example" in ligne for ligne in app.cycle["lignes"])  # journal parlant
    vue = carte(app, "Sans Adresse")["dossier"]["depot"]
    assert vue["etat"] == "erreur" and "introuvable" in vue["message"]  # visible sur la carte

    # L'utilisateur colle l'adresse lui-même ; le navigateur qui meurt au démarrage est signalé.
    with pytest.raises(Refus, match="invalide"):
        app.action({"action": "page_carriere", "dossier": d["id"], "url": "suez.com"})
    app.action({"action": "page_carriere", "dossier": d["id"], "url": PAGE})

    class Mort:
        returncode = 1

        def poll(self):
            return 1

    def lanceur(tache):
        (tache.parent / lancement.FICHIER_JOURNAL).write_text("Traceback…\nRuntimeError: Aucun navigateur utilisable\n", encoding="utf-8")
        return Mort()

    app.lanceur_depot = lanceur
    app.action({"action": "remplir", "dossier": d["id"]})
    vue = carte(app, "Sans Adresse")["dossier"]["depot"]
    assert vue["etat"] == "erreur" and "Aucun navigateur utilisable" in vue["message"]


def test_adresse_consultee_a_la_barre_pres_ou_repli_sur_une_page_carrieres(ctx, orchestrateur):  # noqa: F811
    d = par_entreprise(ctx, "Sans Adresse")
    ctx.llm.recherche = ("Voir le site.", ["https://www.suez.com/fr/rejoignez-nous/", "https://www.suez.com/fr"])
    ctx.llm.reponses["enquete"] = {"url": "https://suez.com/fr/rejoignez-nous"}
    assert orchestrateur.enquete.trouver_page_carriere(d) == "https://www.suez.com/fr/rejoignez-nous/"
    ctx.llm.reponses["enquete"] = {"url": ""}
    d.fiche_entreprise.page_carriere = ""
    assert orchestrateur.enquete.trouver_page_carriere(d) == "https://www.suez.com/fr/rejoignez-nous/"


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
ACCUEIL = """<!doctype html><meta charset="utf-8"><title>Groupe</title>
<div id="cookies" style="position:fixed;inset:0;background:#fff"><p>Nous utilisons des cookies.</p>
  <button onclick="document.getElementById('cookies').remove()">Tout accepter</button>
  <button onclick="document.getElementById('cookies').remove()">Continuer sans accepter</button></div>
<label>Votre e-mail <input type="email" name="newsletter_mail"></label>
<div style="height:3000px">Nos activités</div>
<footer><a href="https://fr.linkedin.com/company/x">Jobs</a><a href="/carrieres2.html">Carrières</a></footer>"""
CARRIERES2 = """<!doctype html><meta charset="utf-8"><a href="/accueil.html">Accueil</a><a href="/rejoindre.html">Nous rejoindre</a>"""
REJOINDRE = """<!doctype html><meta charset="utf-8"><p>Pas d'offre ?</p><a href="/formulaire.html">Candidature spontanée</a>"""
CONNEXION = """<!doctype html><meta charset="utf-8"><label>E-mail <input name="login"></label>
<label>Mot de passe <input type="password" name="pw"></label><button>Se connecter</button>"""

# Site d'ATS : bandeau de cookies dans un shadow DOM, page entière dans un <form> (ASP.NET),
# « Je postule » qui mène à une connexion, puis la création de compte.
OFFRE = """<!doctype html><meta charset="utf-8"><div id="cmp"></div>
<script>const r = document.getElementById("cmp").attachShadow({mode: "open"});
r.innerHTML = '<div style="position:fixed;inset:0;background:#fff"><p>Ce site utilise des cookies.</p>' +
  '<button onclick="this.parentNode.remove()">Tout accepter</button><button id="non" onclick="this.parentNode.remove()">Tout refuser</button></div>';</script>
<form id="aspnet"><input type="search" placeholder="Mot-clé"><h1>Stage maintenance</h1>
<button type="button" onclick="location='/connexion2.html'">Je postule</button></form>"""
CONNEXION2 = """<!doctype html><meta charset="utf-8"><form action="/merci.html">
<label>E-mail <input name="login"></label><label>Mot de passe <input type="password" name="pw"></label>
<button type="submit">Se connecter</button><a href="/inscription.html">Créer un compte</a></form>"""
INSCRIPTION = """<!doctype html><meta charset="utf-8"><form action="/merci.html">
<label for="p">Prénom</label><input id="p" name="prenom"><label for="n">Nom</label><input id="n" name="nom">
<label for="m">E-mail</label><input id="m" type="email" name="email">
<label for="pw">Mot de passe</label><input id="pw" type="password" name="pw">
<label><input type="checkbox" name="cgu"> J'accepte les conditions</label><button type="submit">Créer mon compte</button></form>"""

# Menu replié (« Nous rejoindre » caché sous « Carrières »), page de choix sans mot de passe,
# puis inscription « e-mail + mot de passe + confirmation » dont le bouton est un faux bouton (div).
MENU = """<!doctype html><meta charset="utf-8"><nav><span>Carrières</span>
<ul style="display:none"><li><a href="/choix.html">Nous rejoindre</a></li></ul></nav><p>Énergie</p>"""
CHOIX = """<!doctype html><meta charset="utf-8"><h1>Postuler</h1>
<button onclick="location='/connexion2.html'">Se connecter</button>
<button onclick="location='/inscription2.html'">Créer un compte</button>"""
INSCRIPTION2 = """<!doctype html><meta charset="utf-8"><label for="m">Adresse e-mail</label><input id="m" type="email">
<label for="a">Mot de passe</label><input id="a" type="password"><label for="b">Vérifier le mot de passe</label><input id="b" type="password">
<div role="button" aria-label="Créer un compte" onclick="location='/merci.html'">Créer un compte</div>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    racine = tmp_path_factory.mktemp("site")
    for nom, html in {"formulaire.html": FORMULAIRE, "carrieres.html": CARRIERES, "connexion.html": CONNEXION,
                      "merci.html": "<p>Merci pour votre candidature !</p>",
                      "accueil.html": ACCUEIL, "carrieres2.html": CARRIERES2, "rejoindre.html": REJOINDRE,
                      "offre.html": OFFRE, "connexion2.html": CONNEXION2, "inscription.html": INSCRIPTION,
                      "menu.html": MENU, "choix.html": CHOIX, "inscription2.html": INSCRIPTION2}.items():
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


def test_sites_d_offres_et_alternance_ecartes(ctx, orchestrateur):  # noqa: F811
    lba = "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/recruteurs_lba/44897984900172/suez-rv-fm"
    d = par_entreprise(ctx, "Sans Adresse")
    ctx.llm.recherche = ("Voir La bonne alternance.", [lba, "https://fr.indeed.com/cmp/Suez/jobs"])
    ctx.llm.reponses["enquete"] = {"url": lba}
    assert orchestrateur.enquete.trouver_page_carriere(d) == ""  # ni l'adresse proposée, ni le repli

    # Adresse déjà enregistrée par l'ancienne version : écartée, puis nouvelle recherche.
    orchestrateur.valider(d.id)
    d = par_entreprise(ctx, "Sans Adresse")
    d.fiche_entreprise.page_carriere = lba
    ctx.store.save(d)
    ctx.llm.recherche = ("Page du groupe.", [PAGE])
    ctx.llm.reponses["enquete"] = {"url": PAGE}
    orchestrateur.preparer_depot(d.id)
    assert par_entreprise(ctx, "Sans Adresse").fiche_entreprise.page_carriere == PAGE

    # Donnée par l'utilisateur lui-même : respectée.
    orchestrateur.definir_page_carriere(d.id, lba)
    orchestrateur.preparer_depot(d.id)
    assert par_entreprise(ctx, "Sans Adresse").fiche_entreprise.page_carriere == lba


def test_descend_vers_carrieres_puis_nous_rejoindre_jusqu_au_formulaire(navigateur, site, remplisseur):
    page = navigateur.new_page()
    page.goto(f"{site}/accueil.html")
    for _ in range(4):
        etat = remplisseur.passe(page)
        if etat.etat != "ouverture":
            break
        assert remplisseur.vers_formulaire(page)
    assert page.url.endswith("/formulaire.html"), page.url
    assert remplisseur.navigation == ["Carrières", "Nous rejoindre", "Candidature spontanée"]  # cookies refusés en passant
    assert etat.etat == "manque" and page.input_value("#p") == "Rémi"
    page.goto(f"{site}/accueil.html")  # un seul champ (newsletter) : pas un formulaire de candidature, rien rempli
    nouveau = Remplisseur({"email": "remi@example.org"}, {})
    assert nouveau.passe(page).etat == "ouverture" and page.input_value("[name=newsletter_mail]") == ""


def test_cookies_postuler_puis_creation_de_compte(navigateur, site, remplisseur, tmp_path):
    comptes = tmp_path / "comptes.json"
    remplisseur.fichier_comptes = comptes
    page = navigateur.new_page()
    page.goto(f"{site}/offre.html")
    assert remplisseur.refuser_cookies(page)  # dans le shadow DOM, « Tout refuser » et non « Tout accepter »
    assert page.evaluate("document.getElementById('cmp').shadowRoot.querySelector('#non')") is None
    assert remplisseur.passe(page).etat == "ouverture"
    assert remplisseur.vers_formulaire(page) and page.url.endswith("/connexion2.html")  # « Je postule » dans le <form> de page
    assert remplisseur.passe(page).etat == "connexion"
    assert remplisseur.vers_inscription(page) and page.url.endswith("/inscription.html")  # jamais « Se connecter »
    etat = remplisseur.passe(page)
    assert etat.etat == "connexion" and "mot de passe" in etat.message
    assert page.input_value("#p") == "Rémi" and page.input_value("#m") == "remi@example.org"
    assert page.input_value("#pw") == "" and not page.is_checked("[name=cgu]")  # mot de passe et conditions : à toi
    assert not remplisseur.vers_inscription(page) and page.url.endswith("/inscription.html")  # « Créer mon compte » jamais cliqué

    # La fois suivante sur ce site : le compte existe, c'est à toi de te connecter.
    suivant = Remplisseur({"email": "remi@example.org"}, {}, fichier_comptes=comptes)
    page.goto(f"{site}/connexion2.html")
    etat = suivant.passe(page)
    assert "Compte commencé" in etat.message and not suivant.vers_inscription(page)


def test_menu_replie_page_de_choix_et_inscription_a_double_mot_de_passe(navigateur, site, tmp_path):
    r = Remplisseur({"email": "remi@example.org"}, {}, fichier_comptes=tmp_path / "comptes.json")
    page = navigateur.new_page()
    page.goto(f"{site}/menu.html")
    assert r.passe(page).etat == "ouverture" and r.vers_formulaire(page)
    assert page.url.endswith("/choix.html")  # lien caché du menu : suivi par son adresse
    assert r.passe(page).etat == "ouverture" and r.vers_formulaire(page)
    assert page.url.endswith("/inscription2.html")  # « Créer un compte », pas « Se connecter »
    etat = r.passe(page)
    assert etat.etat == "connexion" and page.input_value("#m") == "remi@example.org" and page.input_value("#a") == ""
    assert not r.vers_formulaire(page) and not r.vers_inscription(page)
    assert page.url.endswith("/inscription2.html")  # le faux bouton « Créer un compte » n'est jamais cliqué
    assert "127.0.0.1" in json.loads((tmp_path / "comptes.json").read_text(encoding="utf-8"))


def test_choix_des_liens():
    assert reco.choisir_lien([{"texte": "Postuler avec LinkedIn"}, {"texte": "Postuler sans compte"}, {"texte": "Postuler"}], set())[
        "texte"] == "Postuler sans compte"
    assert reco.choisir_lien([{"texte": "Postuler", "formulaire": True}], set()) is None  # bouton d'un formulaire rempli
    assert reco.choisir_lien([{"texte": "Je postule"}], set())["texte"] == "Je postule"
    assert reco.choisir_inscription([{"texte": "Créer mon compte", "envoi": True}, {"texte": "S'inscrire à la newsletter"}]) is None
    assert reco.choisir_inscription([{"texte": "Pas encore de compte ? Inscrivez-vous"}])
    assert reco.REFUS_COOKIES.search("tout refuser") and not reco.REFUS_COOKIES.search("tout accepter")


def test_choix_du_navigateur(tmp_path, monkeypatch):
    from recherche_stage.depot import __main__ as principal

    assert principal.candidats("")[0] == {"channel": "msedge"}
    assert principal.candidats("Chrome") == [{"channel": "chrome"}]
    brave = tmp_path / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    brave.parent.mkdir(parents=True)
    brave.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert principal.candidats("brave") == [{"executable_path": str(brave)}]
    assert principal.candidats(str(brave)) == [{"executable_path": str(brave)}]
    with pytest.raises(RuntimeError, match="inconnu"):
        principal.candidats("firefox")
    assert lancement.nom_profil("brave") == "brave" and lancement.nom_profil(None) == "edge"
    # Rien dans les Réglages : Brave s'il est installé, sinon Edge.
    assert lancement.choisi("") == "brave" and lancement.choisi(" chrome ") == "chrome"
    monkeypatch.setattr(lancement, "chemins_brave", lambda: [])
    assert lancement.choisi(None) == "" and lancement.libelle_navigateur("") == "Edge, sinon Chrome"
    assert lancement.nom_profil(r"C:\Program Files\BraveSoftware\brave.exe") == "brave"
