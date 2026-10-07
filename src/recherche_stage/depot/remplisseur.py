"""Remplissage d'une page de candidature avec Playwright.

Une « passe » parcourt tous les cadres de la page (les ATS s'intègrent souvent
dans une iframe), remplit les champs reconnus encore vides, joint les fichiers
et dit où l'on en est. Le déroulé (`deposer`) répète les passes tant que la
fenêtre est ouverte : après une connexion ou à l'étape suivante d'un formulaire
en plusieurs pages, les nouveaux champs sont remplis à leur tour.

Le bot ne clique jamais sur un bouton d'envoi : seuls les liens qui mènent au
formulaire (« Candidature spontanée », « Postuler », « Créer un compte ») et le
refus des cookies peuvent être cliqués. Le mot de passe n'est jamais rempli.

Quand les mots-clés ne trouvent ni lien ni sens à un champ, le guide IA
(`guide.py`) lit la page et propose un lien ou des correspondances de champs,
choisis dans des listes déjà filtrées par le code et revérifiées ensuite.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import champs as reco
from .guide import DESCRIPTIONS, champs_proposables, liens_proposables

# Collecte des champs d'un cadre ; chacun reçoit un repère data-rs pour être retrouvé.
COLLECTE = r"""
() => {
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const texte = e => (e ? (e.innerText || e.textContent || "") : "").trim().slice(0, 200);
  let n = Number(document.documentElement.dataset.rsN || 0);
  const liste = [];
  for (const e of document.querySelectorAll("input, textarea, select")) {
    const type = (e.getAttribute("type") || "").toLowerCase();
    if (["hidden", "submit", "button", "image", "reset"].includes(type)) continue;
    if (type !== "file" && !visible(e)) continue;
    if (!e.dataset.rs) e.dataset.rs = String(++n);
    let libelle = e.labels && e.labels.length ? texte(e.labels[0]) : "";
    const par = e.getAttribute("aria-labelledby");
    if (!libelle && par) libelle = par.split(/\s+/).map(i => texte(document.getElementById(i))).join(" ");
    if (!libelle) {  // texte du bloc qui entoure le champ (formulaires sans <label>)
      let p = e.parentElement;
      for (let i = 0; p && i < 3 && !libelle; i++, p = p.parentElement) {
        const t = texte(p);
        if (t && t.length < 120) libelle = t;
      }
    }
    liste.push({ rs: e.dataset.rs, balise: e.tagName.toLowerCase(), type, name: e.name || "", id: e.id || "",
      autocomplete: e.getAttribute("autocomplete") || "", placeholder: e.getAttribute("placeholder") || "",
      aria: e.getAttribute("aria-label") || "", libelle, obligatoire: e.required || e.getAttribute("aria-required") === "true",
      visible: visible(e), vide: type === "file" ? e.files.length === 0 : !e.value, fait: e.dataset.rsFait === "1", cle: e.dataset.rsCle || "" });
  }
  document.documentElement.dataset.rsN = String(n);
  return liste;
}
"""

LIENS = r"""
() => {
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  // Formulaire « à remplir » : un bouton qui s'y trouve pourrait l'envoyer.
  const aRemplir = f => !!f && [...f.querySelectorAll("input, textarea")].some(c =>
    !["hidden", "submit", "button", "image", "reset", "checkbox", "radio", "search"].includes((c.getAttribute("type") || "").toLowerCase()) && visible(c));
  // Les liens cachés dans un menu replié (« Nous rejoindre » sous « Carrières ») comptent aussi : on ira à leur adresse.
  const lien = e => e.tagName === "A" && (e.getAttribute("href") || "").trim() && !/^(#|javascript:)/i.test(e.getAttribute("href").trim());
  return [...document.querySelectorAll("a, button, [role=button], [role=link]")].filter(e => visible(e) || lien(e)).map((e, i) => {
    e.dataset.rsLien = String(i);
    const f = e.closest("form");
    return { i: String(i), texte: (e.innerText || e.textContent || e.getAttribute("aria-label") || e.title || "").trim().replace(/\s+/g, " ").slice(0, 80),
             href: lien(e) ? e.href : "", visible: visible(e), formulaire: aRemplir(f), envoi: !!f && e.type === "submit" };
  });
}
"""

# Refus du bandeau de cookies, y compris dans un shadow DOM : sélecteurs connus, puis texte du bouton.
COOKIES = r"""
([motif, selecteurs]) => {
  const re = new RegExp(motif);
  const norm = t => (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const racines = [document];
  for (let i = 0; i < racines.length; i++)
    for (const e of racines[i].querySelectorAll("*")) if (e.shadowRoot) racines.push(e.shadowRoot);
  for (const r of racines) for (const sel of selecteurs) {
    const e = r.querySelector(sel);
    if (e && visible(e)) { e.click(); return sel; }
  }
  if (!/cookie|traceur|consent/.test(norm(racines.map(r => (r === document ? document.documentElement : r).textContent || "").join(" ")))) return "";
  for (const r of racines) for (const e of r.querySelectorAll("button, a, [role=button]")) {
    const t = norm(e.innerText || e.getAttribute("aria-label") || "");
    if (t && t.length < 60 && re.test(t) && visible(e)) { e.click(); return t; }
  }
  return "";
}
"""

BANDEAU = r"""
(message) => {
  let b = document.getElementById("rs-bandeau");
  if (!b) {
    b = document.createElement("div");
    b.id = "rs-bandeau";
    b.style.cssText = "position:fixed;z-index:2147483647;left:12px;right:12px;bottom:12px;padding:10px 14px;border-radius:8px;" +
      "background:#1f4f7a;color:#fff;font:14px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.3);pointer-events:none";
    document.body.appendChild(b);
  }
  b.textContent = "recherche-stage — " + message;
}
"""

MARQUER = r"""
([rs, couleur, cle]) => { const e = document.querySelector(`[data-rs="${rs}"]`);
  if (e) { e.style.outline = `2px solid ${couleur}`; e.style.outlineOffset = "1px"; if (cle) { e.dataset.rsFait = "1"; e.dataset.rsCle = cle; } } }
"""


@dataclass
class Etat:
    etat: str = "ouverture"  # ouverture | rempli | connexion | captcha | manque | confirme | ferme | erreur
    message: str = ""
    url: str = ""
    remplis: list[str] = field(default_factory=list)
    manquants: list[str] = field(default_factory=list)
    horodatage: str = ""

    def ecrire(self, chemin: Path) -> None:
        self.horodatage = datetime.now().isoformat(timespec="seconds")
        temporaire = chemin.with_suffix(".tmp")
        temporaire.write_text(json.dumps(self.__dict__, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(chemin)


class Remplisseur:
    def __init__(self, valeurs: dict[str, str], fichiers: dict[str, str], questions: dict[str, str] | None = None,
                 fichier_comptes: Path | None = None, guide=None):
        self.valeurs = {k: v for k, v in valeurs.items() if v}
        self.fichiers = {k: v for k, v in fichiers.items() if v and Path(v).exists()}
        self.questions = questions or {}
        self.remplis: dict[str, str] = {}  # libellé -> clé, pour le compte rendu
        self.clics = 0
        self.suivis: set[str] = set()  # liens déjà suivis (pas de boucle)
        self.cookies: dict[str, int] = {}  # page -> essais de refus du bandeau (3 au plus)
        self.navigation: list[str] = []  # liens suivis, pour le compte rendu
        self.formulaire = False  # la dernière passe a vu un formulaire à remplir
        self.inscriptions: set[str] = set()  # sites où « Créer un compte » a été ouvert pendant ce dépôt
        self.derniere_url = ""
        self.guide = guide  # guide IA, quand les mots-clés ne suffisent pas
        self.attributions: dict[tuple[str, str], str] = {}  # (cadre, repère du champ) -> clé proposée par le guide IA
        self.demandes: set[tuple[str, str]] = set()  # champs déjà montrés au guide IA (pas deux fois)
        # Sites où « Créer un compte » a déjà été ouvert (nom du site et date, rien d'autre) :
        # la fois suivante, c'est à toi de te connecter.
        self.fichier_comptes = fichier_comptes
        self.comptes: dict[str, str] = {}
        if fichier_comptes:
            try:
                self.comptes = json.loads(Path(fichier_comptes).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def _cle(self, champ: dict) -> str | None:
        """Clé du champ : par les mots-clés, sinon celle proposée par le guide IA."""
        return reco.reconnaitre(champ, self.questions) or self.attributions.get((champ.get("cadre", ""), champ.get("rs", "")))

    def informations(self) -> dict[str, str]:
        """Noms et sens des informations disponibles pour le guide IA (jamais leurs valeurs)."""
        infos = {cle: sens for cle, sens in DESCRIPTIONS.items() if cle in self.valeurs}
        infos.update({f"question:{q}": f"réponse rédigée à la question « {q} »" for q in self.questions})
        return infos

    def _demander(self, page, liens: list[dict], champs: list[dict]) -> dict | None:
        """Demande au guide IA ; retient les champs reconnus et renvoie le lien proposé."""
        if not self.guide:
            return None
        try:
            texte, titre = page.inner_text("body", timeout=2000), page.title()
        except Exception:
            texte, titre = "", ""
        self.demandes.update((c["cadre"], c["rs"]) for c in champs)
        bandeau(page, "Le guide IA lit la page…")
        lien, attributions = self.guide.analyser(page.url, titre, texte, liens, champs, self.informations(), journal=journal)
        for i, cle in attributions.items():
            self.attributions[(champs[i]["cadre"], champs[i]["rs"])] = cle
        return lien

    def _valeur(self, cle: str) -> str | None:
        if cle.startswith("question:"):
            return self.questions.get(cle.split(":", 1)[1])
        return self.valeurs.get(cle)

    def passe(self, page) -> Etat:
        """Remplit ce qui peut l'être dans tous les cadres et renvoie l'état."""
        manquants: list[str] = []
        tous: list[dict] = []
        cadres = [f.url for f in page.frames]
        collectes = []
        for cadre in page.frames:
            if reco.cadre_captcha(cadre.url):  # on ne touche jamais au CAPTCHA : c'est à toi
                continue
            try:
                collectes.append((cadre, cadre.evaluate(COLLECTE)))
            except Exception:  # cadre détaché ou d'une autre origine bloquée : on passe
                continue
        for cadre, liste in collectes:
            for c in liste:
                c["cadre"] = cadre.url
            tous += liste
        if page.url != self.derniere_url:
            self.derniere_url = page.url
            journal(f"page : {page.url}")
        # Création de compte (mot de passe + confirmation) : on remplit ce qu'on connaît, jamais le mot de passe.
        mots_de_passe = sum(1 for c in tous if c["type"] == "password" and c["visible"])
        self.formulaire = mots_de_passe >= 2 or self.est_formulaire(tous)
        # Champs que les mots-clés ne comprennent pas, sur un formulaire (ou une page qui en a beaucoup) : le guide IA.
        inconnus = champs_proposables([c for c in tous if not self._cle(c) and (c["cadre"], c["rs"]) not in self.demandes])
        if inconnus and (self.formulaire or (not mots_de_passe and len(inconnus) >= 3)):
            self._demander(page, [], inconnus)
            self.formulaire = mots_de_passe >= 2 or self.est_formulaire(tous)
        if self.formulaire and mots_de_passe:
            self._noter_compte(page.url)
        if self.formulaire:
            for cadre, liste in collectes:
                self._remplir_cadre(cadre, liste, manquants)
        etat = Etat(url=page.url, remplis=list(self.remplis), manquants=list(dict.fromkeys(manquants)))
        texte = ""
        try:
            texte = page.inner_text("body", timeout=2000)
        except Exception:
            pass
        bloque = reco.blocage(tous, cadres)
        if reco.est_confirmation(texte):
            etat.etat, etat.message = "confirme", "Confirmation de candidature détectée sur la page."
        elif bloque == "captcha":
            etat.etat, etat.message = "captcha", "Un CAPTCHA est affiché : coche-le dans la fenêtre, je reprends ensuite."
        elif bloque == "connexion":
            etat.etat = "connexion"
            cree = self.comptes.get(urlsplit(page.url).hostname or "")
            if self.formulaire:
                etat.message = ("Création de compte : j'ai rempli ce que je connais. Choisis ton mot de passe (je n'en mets "
                                "jamais), accepte les conditions et valide toi-même ; je remplis la suite.")
            elif cree:
                etat.message = (f"Compte commencé sur ce site le {cree} : connecte-toi, je remplis la suite. "
                                "Pas fini de le créer ? Clique « Créer un compte », je remplirai.")
            else:
                etat.message = ("Le site demande de te connecter ou de créer un compte : fais-le dans la "
                                "fenêtre (tu choisis le mot de passe), je remplis la suite.")
        elif not self.formulaire:
            chemin = " → ".join(self.navigation[-4:])
            etat.etat, etat.message = "ouverture", ("Pas encore de formulaire sur cette page" + (f" (suivi : {chemin})" if chemin else "")
                                                    + " : ouvre le formulaire de candidature, je le remplirai.")
        elif etat.manquants:
            etat.etat, etat.message = "manque", "À compléter toi-même (rien n'est deviné) : " + ", ".join(etat.manquants[:6])
        else:
            etat.etat, etat.message = "rempli", "Formulaire rempli : relis, coche les consentements, puis clique toi-même sur Envoyer."
        return etat

    def est_formulaire(self, champs: list[dict]) -> bool:
        """Formulaire de candidature : un fichier à joindre, ou au moins deux champs
        reconnus (une simple case « newsletter » ou une recherche ne compte pas)."""
        if self.remplis:
            return True
        cles = [self._cle(c) for c in champs if c["visible"] or c["type"] == "file"]
        return "cv" in cles or "lettre_fichier" in cles or len({c for c in cles if c}) >= 2

    def _remplir_cadre(self, cadre, liste: list[dict], manquants: list[str]) -> None:
        fichiers_mis = {c["cle"] for c in liste if c["fait"] and c["type"] == "file"}
        fichiers_restants = [c for c in liste if c["type"] == "file"]
        for c in liste:
            if c["fait"]:
                continue
            cle = self._cle(c)
            if c["type"] == "file":
                # Deux champs « fichier » sans libellé clair : le CV puis la lettre.
                if cle == "cv" and "cv" in fichiers_mis and len(fichiers_restants) > 1:
                    cle = "lettre_fichier"
                chemin = self.fichiers.get(cle or "")
                if chemin and c["vide"] and cle not in fichiers_mis:
                    cadre.locator(f'[data-rs="{c["rs"]}"]').set_input_files(chemin)
                    fichiers_mis.add(cle)
                    self._marquer(cadre, c, cle, "#2a8")
                elif c["obligatoire"] and c["vide"]:
                    manquants.append(c["libelle"] or c["name"] or "fichier")
                continue
            valeur = self._valeur(cle) if cle else None
            if valeur and c["vide"]:
                try:
                    cadre.locator(f'[data-rs="{c["rs"]}"]').fill(valeur, timeout=3000)
                    self._marquer(cadre, c, cle, "#2a8")
                except Exception:
                    pass
            elif c["obligatoire"] and c["vide"] and c["visible"] and c["balise"] != "select":
                manquants.append(c["libelle"] or c["placeholder"] or c["name"] or "champ")
                cadre.evaluate(MARQUER, [c["rs"], "#d33", ""])

    def _marquer(self, cadre, c: dict, cle: str, couleur: str) -> None:
        self.remplis[c["libelle"] or c["name"] or cle] = cle
        try:
            cadre.evaluate(MARQUER, [c["rs"], couleur, cle])
        except Exception:
            pass

    def vers_formulaire(self, page) -> bool:
        """Sur une page sans formulaire : refuse les cookies si un bandeau gêne,
        descend dans la page, puis suit « Candidature spontanée » / « Postuler »,
        ou à défaut « Carrières » / « Nous rejoindre » (six liens au plus en tout).
        Jamais un bouton d'envoi."""
        if self.clics >= 8:
            return False
        self.refuser_cookies(page)
        self.suivis.add(page.url.lower())  # « Carrières » qui renvoie sur la page où l'on est déjà
        liens = self._liens(page)
        lien = reco.choisir_lien(liens, self.suivis)
        if lien is None:  # liens du bas de page parfois chargés au défilement
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
            except Exception:
                return False
            liens = self._liens(page)
            lien = reco.choisir_lien(liens, self.suivis)
        if lien is None:  # page « Se connecter / Créer un compte » d'un espace candidat (pas l'espace client du site)
            try:
                texte = reco.normaliser(page.inner_text("body", timeout=2000))
            except Exception:
                texte = ""
            if reco.ESPACE_CANDIDAT.search(texte) and self.vers_inscription(page, liens):
                return True
            lien = self._demander(page, liens_proposables(liens, self.suivis), [])
            if lien is None:
                journal(f"aucun lien utile sur cette page ; liens vus : {[x['texte'] for x in liens if x['texte']][:25]}")
                return False
            journal("lien choisi par le guide IA")
        self.suivis.add((lien.get("href") or reco.normaliser(lien["texte"])).lower())
        self.clics += 1
        self.navigation.append(lien["texte"] or lien.get("href", ""))
        journal(f"lien suivi : « {lien['texte']} » {lien.get('href', '')}")
        return self._cliquer(page, lien)

    def refuser_cookies(self, page) -> bool:
        """Refuse le bandeau de cookies de la page (trois essais par adresse : il
        arrive souvent après le chargement). Jamais « Tout accepter »."""
        essais = self.cookies.get(page.url, 0)
        if essais >= 3:
            return False
        self.cookies[page.url] = essais + 1
        for cadre in page.frames:
            if reco.cadre_captcha(cadre.url):
                continue
            try:
                clic = cadre.evaluate(COOKIES, [reco.REFUS_COOKIES.pattern, list(reco.SELECTEURS_REFUS)])
            except Exception:
                continue
            if clic:
                journal(f"cookies refusés ({clic})")
                self.cookies[page.url] = 3
                _attendre(page, 600)
                return True
        return False

    def vers_inscription(self, page, liens: list[dict] | None = None) -> bool:
        """Page de connexion : ouvre « Créer un compte » pour que le formulaire
        d'inscription soit rempli (sauf le mot de passe, toujours choisi par toi).
        Pas sur un site où un compte a déjà été créé : là, tu te connectes."""
        hote = urlsplit(page.url).hostname or ""
        if self.formulaire or not hote or hote in self.comptes or hote in self.inscriptions or self.clics >= 10:
            return False
        liens = liens if liens is not None else self._liens(page)
        lien = reco.choisir_inscription(liens)
        if lien is None:
            lien = self._demander(page, liens_proposables(liens, self.suivis), [])
            if lien is None:
                textes = [x["texte"] for x in liens if x["texte"]][:25]
                journal(f"aucun lien utile sur cette page ; liens vus : {textes}")
                return False
            self.suivis.add((lien.get("href") or reco.normaliser(lien["texte"])).lower())
        self.inscriptions.add(hote)
        self.clics += 1
        self.navigation.append(lien["texte"])
        journal(f"création de compte : « {lien['texte']} »")
        return self._cliquer(page, lien)

    def _noter_compte(self, url: str) -> None:
        """Formulaire d'inscription vu : la fois suivante sur ce site, on se connecte."""
        hote = urlsplit(url).hostname or ""
        if not hote or hote in self.comptes:
            return
        self.comptes[hote] = datetime.now().strftime("%d/%m/%Y")
        journal(f"formulaire de création de compte sur {hote}")
        if self.fichier_comptes:
            try:
                Path(self.fichier_comptes).write_text(json.dumps(self.comptes, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _liens(page) -> list[dict]:
        try:
            return page.evaluate(LIENS)
        except Exception:
            return []

    @staticmethod
    def _cliquer(page, lien: dict, attendre: bool = True) -> bool:
        element = page.locator(f'[data-rs-lien="{lien["i"]}"]').first
        try:
            if not lien.get("visible", True) and lien.get("href"):  # dans un menu replié : on va à son adresse
                page.goto(lien["href"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(800)
                return True
            try:
                element.click(timeout=5000)
            except Exception:  # recouvert (bandeau, fenêtre surgissante) : clic direct sur le lien
                element.evaluate("e => e.click()")
            if attendre:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(800)
        except Exception:
            return False
        return True


def journal(message: str) -> None:
    """Ligne du journal du dépôt (depot_journal.txt) : adresses et textes des liens,
    jamais les valeurs saisies."""
    try:
        print(f"{datetime.now():%H:%M:%S} {message}", flush=True)
    except (OSError, ValueError):  # sortie fermée ou encodage : le journal ne doit jamais arrêter le dépôt
        pass


def bandeau(page, message: str) -> None:
    try:
        page.evaluate(BANDEAU, message)
    except Exception:
        pass


def _attendre(page, ms: int) -> None:
    """Pause qui laisse Playwright traiter ses événements (onglets fermés, navigation)."""
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


def deposer(contexte, url: str, remplisseur: Remplisseur, chemin_etat: Path, duree_max: float = 7200) -> Etat:
    """Ouvre `url` et accompagne le dépôt jusqu'à la fermeture de la fenêtre."""
    page = contexte.pages[0] if contexte.pages else contexte.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    for autre in contexte.pages:  # onglet vide ouvert au démarrage par le navigateur (Brave) : fermé
        if autre is not page and not autre.url.startswith("http"):
            try:
                autre.close()
            except Exception:
                pass
    etat = Etat(url=url)
    confirme: Etat | None = None  # une confirmation vue reste acquise, même si la page change ensuite
    precedent = None
    fin = time.monotonic() + duree_max
    while time.monotonic() < fin:
        pages = [p for p in contexte.pages if not p.is_closed()]
        if not pages:
            break
        # Un lien « Postuler » ouvre parfois un nouvel onglet : le plus récent qui montre un site
        # (pas un onglet vide ou une page du navigateur ouverte en même temps).
        sites = [p for p in pages if p.url.startswith("http")]
        page = sites[-1] if sites else pages[-1]
        try:
            remplisseur.refuser_cookies(page)
            etat = remplisseur.passe(page)
            if (etat.etat == "ouverture" and remplisseur.vers_formulaire(page)) or (
                    etat.etat == "connexion" and remplisseur.vers_inscription(page)):
                page.wait_for_timeout(1500)
                continue
            bandeau(page, etat.message)
        except Exception:  # navigation en cours ou fenêtre fermée : on réessaie au tour suivant
            _attendre(page, 500)
            continue
        if etat.etat == "confirme":
            confirme = etat
        signature = (etat.etat, etat.url, tuple(etat.remplis), tuple(etat.manquants))
        if signature != precedent:
            if not precedent or etat.etat != precedent[0]:
                journal(f"état : {etat.etat} — {etat.message}")
            etat.ecrire(chemin_etat)
            precedent = signature
        _attendre(page, 1500)
    if confirme:
        etat = confirme
    else:
        etat.etat, etat.message = "ferme", "Fenêtre fermée. Si tu as envoyé la candidature, clique « C'est déposé »."
    etat.ecrire(chemin_etat)
    return etat
