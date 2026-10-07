"""Remplissage d'une page de candidature avec Playwright.

Une « passe » parcourt tous les cadres de la page (les ATS s'intègrent souvent
dans une iframe), remplit les champs reconnus encore vides, joint les fichiers
et dit où l'on en est. Le déroulé (`deposer`) répète les passes tant que la
fenêtre est ouverte : après une connexion ou à l'étape suivante d'un formulaire
en plusieurs pages, les nouveaux champs sont remplis à leur tour.

Le bot ne clique jamais sur un bouton d'envoi : seuls les liens qui mènent au
formulaire (« Candidature spontanée », « Postuler ») peuvent être suivis.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import champs as reco

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
() => [...document.querySelectorAll("a[href], button, [role=button]")]
  .filter(e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && !e.closest("form"); })
  .map((e, i) => { e.dataset.rsLien = String(i);
    return { i: String(i), texte: (e.innerText || e.getAttribute("aria-label") || e.title || "").trim().slice(0, 80),
             href: e.tagName === "A" ? e.href : "" }; })
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
    def __init__(self, valeurs: dict[str, str], fichiers: dict[str, str], questions: dict[str, str] | None = None):
        self.valeurs = {k: v for k, v in valeurs.items() if v}
        self.fichiers = {k: v for k, v in fichiers.items() if v and Path(v).exists()}
        self.questions = questions or {}
        self.remplis: dict[str, str] = {}  # libellé -> clé, pour le compte rendu
        self.clics = 0
        self.suivis: set[str] = set()  # liens déjà suivis (pas de boucle)
        self.cookies: set[str] = set()  # pages où le bandeau de cookies a été refusé
        self.navigation: list[str] = []  # liens suivis, pour le compte rendu

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
        for _, liste in collectes:
            tous += liste
        if self.est_formulaire(tous):
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
            etat.etat, etat.message = "connexion", ("Le site demande de te connecter ou de créer un compte : fais-le dans la "
                                                    "fenêtre (tu choisis le mot de passe), je remplis la suite.")
        elif not self.est_formulaire(tous):
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
        cles = [reco.reconnaitre(c, self.questions) for c in champs if c["visible"] or c["type"] == "file"]
        return "cv" in cles or "lettre_fichier" in cles or len({c for c in cles if c}) >= 2

    def _remplir_cadre(self, cadre, liste: list[dict], manquants: list[str]) -> None:
        fichiers_mis = {c["cle"] for c in liste if c["fait"] and c["type"] == "file"}
        fichiers_restants = [c for c in liste if c["type"] == "file"]
        for c in liste:
            if c["fait"]:
                continue
            cle = reco.reconnaitre(c, self.questions)
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
        if self.clics >= 6:
            return False
        liens = self._liens(page)
        refus = next((lien for lien in liens if reco.REFUS_COOKIES.search(reco.normaliser(lien["texte"]))), None)
        if refus and page.url not in self.cookies:
            self.cookies.add(page.url)
            self._cliquer(page, refus, attendre=False)
            liens = self._liens(page)
        lien = reco.choisir_lien(liens, self.suivis)
        if lien is None:  # liens du bas de page parfois chargés au défilement
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
            except Exception:
                return False
            lien = reco.choisir_lien(self._liens(page), self.suivis)
        if lien is None:
            return False
        self.suivis.add((lien.get("href") or reco.normaliser(lien["texte"])).lower())
        self.clics += 1
        self.navigation.append(lien["texte"] or lien.get("href", ""))
        return self._cliquer(page, lien)

    @staticmethod
    def _liens(page) -> list[dict]:
        try:
            return page.evaluate(LIENS)
        except Exception:
            return []

    @staticmethod
    def _cliquer(page, lien: dict, attendre: bool = True) -> bool:
        try:
            page.locator(f'[data-rs-lien="{lien["i"]}"]').first.click(timeout=5000)
            if attendre:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(800)
        except Exception:
            return False
        return True


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
    etat = Etat(url=url)
    confirme: Etat | None = None  # une confirmation vue reste acquise, même si la page change ensuite
    precedent = None
    fin = time.monotonic() + duree_max
    while time.monotonic() < fin:
        pages = [p for p in contexte.pages if not p.is_closed()]
        if not pages:
            break
        page = pages[-1]  # un lien « Postuler » ouvre parfois un nouvel onglet
        try:
            etat = remplisseur.passe(page)
            if etat.etat == "ouverture" and remplisseur.vers_formulaire(page):
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
            etat.ecrire(chemin_etat)
            precedent = signature
        _attendre(page, 1500)
    if confirme:
        etat = confirme
    else:
        etat.etat, etat.message = "ferme", "Fenêtre fermée. Si tu as envoyé la candidature, clique « C'est déposé »."
    etat.ecrire(chemin_etat)
    return etat
