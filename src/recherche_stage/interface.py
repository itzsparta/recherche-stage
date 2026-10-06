"""`rs interface` : interface web locale, à la place des commandes.

Un petit serveur de la bibliothèque standard, joignable seulement depuis cet
ordinateur (127.0.0.1), sert une page qui montre l'état du pipeline, lance un
cycle et règle la file d'attente : relire et corriger le mail et la lettre,
valider, rejeter, marquer un dépôt manuel. Rien ne quitte la machine.

Protection : chaque action exige un jeton tiré au lancement et inscrit dans la
page, envoyé en JSON (une autre page web ouverte dans le navigateur ne peut ni
le lire ni le deviner) ; l'en-tête Host est vérifié (rebinding DNS).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import traceback
from collections import Counter
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from .agents.base import Contexte
from .autonomie import envoi_automatique_debloque, taux_validation
from .excel import LIBELLES_STATUT, _etat, a_exporter
from .modeles import Dossier, Statut
from .orchestrateur import Orchestrateur
from .presentiel import itineraires, ordonner, point_connu, point_dossier
from .revue import VALIDABLES, ouvrir

PAGE = Path(__file__).resolve().parent / "interface.html"

Fabrique = Callable[[], tuple[Contexte, Orchestrateur]]


class Refus(Exception):
    """Action impossible, expliquée à l'utilisateur."""


class Application:
    def __init__(self, fabrique: Fabrique, ouvrir: Callable[[str], None] = ouvrir, arriere_plan: bool = True):
        self.fabrique = fabrique
        self.ctx, self.orch = fabrique()
        self.ouvrir = ouvrir
        self.arriere_plan = arriere_plan
        self.jeton = secrets.token_urlsafe(24)
        self.cycle = {"en_cours": False, "lignes": [], "rapport": None, "erreur": None}

    # ------------------------------------------------------------------ état

    def etat(self) -> dict:
        store, cfg = self.ctx.store, self.ctx.config
        dossiers = store.lister()
        compte = Counter(d.statut.value for d in dossiers)
        taux, n = taux_validation(store, cfg.autonomie.fenetre)
        return {
            "statuts": dict(sorted(compte.items())),
            "envois": [store.envois_semaine(), cfg.orchestrateur.candidatures_max_par_semaine],
            "appels_llm": [store.appels_llm_semaine(), cfg.orchestrateur.appels_api_max_par_semaine],
            "fournisseur": cfg.llm.fournisseur,
            "autonomie": {"debloquee": envoi_automatique_debloque(store, cfg), "taux": taux, "n": n, "fenetre": cfg.autonomie.fenetre},
            "cycle": self.cycle,
            "consignes": store.parametre("consignes_redaction", "") or "",
            "file": self._file(),
            "candidatures": self._candidatures(dossiers),
            "tournee": self._tournee(dossiers),
        }

    # ------------------------------------------------------- mes candidatures

    def _candidatures(self, dossiers: list[Dossier]) -> list[dict]:
        """Tout ce qui a été validé ou envoyé, et ce que l'humain a rejeté."""
        jours = self.ctx.config.suivi.relances_jours
        lignes = []
        for d in dossiers:
            rejete = d.statut == Statut.abandonnee and d.motif.startswith("rejet humain")
            if not (a_exporter(d) or rejete):
                continue
            etat = _etat(d)
            relance = None
            numero = len(d.suivi.relances) + 1
            if d.statut in (Statut.envoyee, Statut.relancee) and d.suivi.reponse == "aucune" and d.envoi.horodatage and numero <= len(jours):
                relance = d.envoi.horodatage + timedelta(days=jours[numero - 1])
            p = d.presentiel
            lignes.append({
                "id": d.id, "entreprise": d.offre.entreprise, "intitule": d.offre.intitule, "lieu": d.offre.lieu, "url": d.offre.url,
                "statut": d.statut.value, "etat": "Rejetée" if rejete else LIBELLES_STATUT.get(etat, str(etat)),
                "envoyee_le": d.envoi.horodatage, "canal": d.envoi.canal, "destinataire": d.envoi.destinataire,
                "relances": len(d.suivi.relances), "prochaine_relance": relance,
                "reponse": d.suivi.reponse, "entretien_le": d.suivi.entretien_le,
                "motif": re.sub(r"^rejet humain \([^)]*\)\s*:?\s*", "", d.motif) if rejete else "",
                "sur_place": {"depose_le": p.depose_le} if p and p.conseille else None,
                "repondable": d.statut in (Statut.envoyee, Statut.relancee) and d.suivi.reponse == "aucune",
                "date": d.envoi.horodatage or d.offre.date_collecte,
            })
        lignes.sort(key=lambda x: x["date"], reverse=True)
        return lignes

    # ------------------------------------------------------------- tournée

    def _domicile(self) -> str:
        cfg = self.ctx.config.presentiel
        if cfg.domicile:
            return cfg.domicile
        try:
            return self.ctx.profil().identite.ville
        except RuntimeError:
            return ""

    def _tournee(self, dossiers: list[Dossier]) -> dict:
        """Candidatures à apporter en main propre, dans l'ordre d'une tournée."""
        prets, a_valider = [], 0
        for d in dossiers:
            p = d.presentiel
            if not (p and p.conseille) or p.depose_le:
                continue
            if a_exporter(d):
                prets.append(d)
            elif d.statut == Statut.en_attente_humain:
                a_valider += 1  # pas encore validée : rien à imprimer
        domicile = self._domicile()
        depart = point_connu(self.ctx.store, domicile) if domicile else None
        arrets = ordonner(depart, [(d, point_dossier(self.ctx.store, d)) for d in prets])
        return {
            "domicile": domicile,
            "a_valider": a_valider,
            "itineraires": itineraires(domicile, [d.presentiel.adresse or d.offre.lieu for d in arrets]),
            "arrets": [{
                "id": d.id, "entreprise": d.offre.entreprise, "intitule": d.offre.intitule, "lieu": d.offre.lieu,
                "adresse": d.presentiel.adresse or d.offre.lieu, "distance_km": d.presentiel.distance_km,
                "raison": d.presentiel.raison, "lettre": d.documents.texte_lettre,
                "deja_envoyee": d.envoi.canal == "email" and d.statut != Statut.validee,
            } for d in arrets],
        }

    def _file(self) -> list[dict]:
        elements, vus = [], set()
        for e in self.ctx.store.escalades_ouvertes():
            dossier = self.ctx.store.get(e["dossier_id"]) if e["dossier_id"] else None
            if dossier is not None and dossier.id in vus:
                continue  # une carte par dossier
            if dossier is not None:
                vus.add(dossier.id)
            elements.append({
                "escalade": e["id"], "type": e["type"], "question": e["question"], "cree": e["cree"],
                "dossier": self._dossier(dossier) if dossier else None,
                "actions": self._actions(dossier),
            })
        elements.sort(key=lambda e: e["dossier"] is not None)  # alertes générales d'abord
        return elements

    def _dossier(self, d: Dossier) -> dict:
        o, doc = d.offre, d.documents
        return {
            "id": d.id, "entreprise": o.entreprise, "intitule": o.intitule, "lieu": o.lieu, "url": o.url,
            "source": o.source, "type": o.type, "etape": d.etape_attente, "motif": d.motif,
            "modifiable": bool(doc.version) and d.etape_attente in ("critique", "envoi"),
            "score": d.score.valeur, "confiance": d.score.confiance, "justification": d.score.justification,
            "critique": {"note": d.critique.note, "iteration": d.critique.iteration,
                         "problemes": [{"gravite": p.gravite, "raison": p.raison} for p in d.critique.problemes]},
            "objet": doc.objet_mail, "mail": doc.mail, "lettre": doc.texte_lettre, "version": doc.version,
            "canal": d.envoi.canal, "destinataire": d.envoi.destinataire,
            "presentiel": d.presentiel.model_dump() if d.presentiel and d.presentiel.conseille else None,
            "faits": [{"texte": f.texte, "source": f.source} for f in d.fiche_entreprise.faits],
            "recherche": f"https://duckduckgo.com/?q={quote_plus(o.entreprise + ' recrutement stage candidature')}",
        }

    @staticmethod
    def _actions(d: Dossier | None) -> list[str]:
        if d is None:
            return ["fermer"]
        if d.etape_attente == "depot":
            return ["deposer", "rejeter", "ouvrir"]
        if d.etape_attente in VALIDABLES or d.etape_attente.startswith("relance:"):
            if d.documents.version and d.etape_attente in ("critique", "envoi"):
                return ["valider", "reecrire", "rejeter", "ouvrir"]
            return ["valider", "rejeter", "ouvrir"]
        return ["fermer", "rejeter"]

    # --------------------------------------------------------------- actions

    def action(self, donnees: dict) -> str:
        if self.cycle["en_cours"]:
            raise Refus("Un cycle est en cours : attendre sa fin.")
        nom = donnees.get("action")
        if nom == "consignes":
            self.ctx.store.definir_parametre("consignes_redaction", str(donnees.get("texte", "")).strip())
            return "Préférences enregistrées : elles s'appliquent aux prochaines rédactions et réécritures."
        if nom == "fermer":
            if not self.ctx.store.fermer_escalade(int(donnees["escalade"]), "interface"):
                raise Refus("Élément déjà traité.")
            return "Retiré de la file."
        dossier = self.ctx.store.get(str(donnees.get("dossier", "")))
        if dossier is None:
            raise Refus("Dossier inconnu.")
        if nom in ("sur_place", "reponse"):
            return self._suivre(dossier, nom, donnees)
        if nom not in self._actions(dossier):
            raise Refus("Action plus possible pour ce dossier : la page va se rafraîchir.")
        try:
            if nom == "valider":
                modifie = self._ecrire_corrections(dossier, donnees)
                self.orch.valider(dossier.id, modifie=modifie)
                d = self.ctx.store.get(dossier.id)
                if d.etape_attente == "depot":
                    return "Validé. Pas d'adresse mail : à déposer sur le site de l'entreprise."
                if d.envoi.canal == "email":
                    return f"Validé{' avec tes corrections' if modifie else ''} : part à {d.envoi.destinataire} au prochain cycle (lun.–jeu., 8 h–11 h)."
                return "Validé."
            if nom == "reecrire":
                self.orch.reecrire(dossier.id, str(donnees.get("consigne", "")))
                self._lancer(lambda orch: orch.rediger(dossier.id))
                return f"Réécriture de {dossier.offre.entreprise} lancée : la nouvelle version revient dans la file."
            if nom == "rejeter":
                self.orch.rejeter(dossier.id, str(donnees.get("motif", "")).strip())
                return "Rejeté."
            if nom == "deposer":
                preuve = str(donnees.get("preuve", "")).strip() or "déposé (interface)"
                self.orch.marquer_envoye(dossier.id, preuve)
                return "Dépôt enregistré : le suivi et les relances démarrent."
            if nom == "ouvrir":
                self.ouvrir(str(self.ctx.dossier_fichiers(dossier.id)))
                return "Dossier ouvert dans l'Explorateur."
        except ValueError as e:  # correction refusée (placeholder, information hors fiche…)
            raise Refus(str(e)) from e
        raise Refus(f"Action inconnue : {nom}")

    def _suivre(self, dossier: Dossier, nom: str, donnees: dict) -> str:
        """Actions des onglets « Mes candidatures » et « Tournée »."""
        try:
            if nom == "sur_place":
                self.orch.deposer_sur_place(dossier.id)
                return f"{dossier.offre.entreprise} : dépôt en main propre enregistré."
            categorie = donnees.get("categorie")
            if categorie not in ("entretien", "refus"):
                raise Refus("Réponse inconnue.")
            if dossier.statut not in (Statut.envoyee, Statut.relancee) or dossier.suivi.reponse != "aucune":
                raise Refus("Réponse déjà enregistrée : la page va se rafraîchir.")
            if categorie == "refus":
                self.orch.indiquer_reponse(dossier.id, "refus")
                return f"Refus de {dossier.offre.entreprise} enregistré : plus de relance."
            # Un entretien prépare une fiche (appel au LLM) : en arrière-plan, comme un cycle.
            self._lancer(lambda orch: orch.indiquer_reponse(dossier.id, "entretien") or Counter(entretien=1))
            return f"Entretien chez {dossier.offre.entreprise} : la fiche de préparation se rédige, elle arrivera dans « À traiter »."
        except (ValueError, RuntimeError) as e:
            raise Refus(str(e)) from e

    def _ecrire_corrections(self, dossier: Dossier, donnees: dict) -> bool:
        """Écrit les textes corrigés là où `valider --modifie` les relit."""
        doc = dossier.documents
        objet = str(donnees.get("objet", doc.objet_mail)).strip()
        mail = str(donnees.get("mail", doc.mail)).strip()
        lettre = str(donnees.get("lettre", doc.texte_lettre or "")).strip()
        if dossier.etape_attente not in ("critique", "envoi") or not doc.version or (objet, mail, lettre) == (doc.objet_mail.strip(), doc.mail.strip(), (doc.texte_lettre or "").strip()):
            return False
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        (fichiers / f"mail_v{doc.version}.txt").write_text(f"Objet : {objet}\n\n{mail}", encoding="utf-8")
        if doc.lettre:
            Path(doc.lettre).write_text(lettre, encoding="utf-8")
        return True

    # ----------------------------------------------------------------- cycle

    def lancer_cycle(self, options: dict) -> str:
        if self.cycle["en_cours"]:
            raise Refus("Un cycle est déjà en cours.")
        self._lancer(lambda orch: orch.cycle(
            collecter=True, envoyer=not options.get("sans_envoi"), suivre=not options.get("sans_suivi")))
        return "Cycle lancé."

    def _lancer(self, travail: Callable[[Orchestrateur], Counter]) -> None:
        """Travail long (cycle, réécriture) hors du fil du serveur, suivi par la page."""
        self.cycle = {"en_cours": True, "lignes": [], "rapport": None, "erreur": None}
        execution = threading.Thread(target=self._travailler, args=(travail,), daemon=True)
        if self.arriere_plan:
            execution.start()
        else:
            execution.run()

    def _travailler(self, travail: Callable[[Orchestrateur], Counter]) -> None:
        etat = self.cycle
        try:
            # Connexion SQLite propre au fil d'exécution du travail.
            _, orch = self.fabrique() if self.arriere_plan else (self.ctx, self.orch)
            orch.progres = etat["lignes"].append
            etat["rapport"] = dict(sorted(travail(orch).items()))
        except Exception as e:  # affiché dans la page plutôt que perdu dans la console
            etat["erreur"] = f"{type(e).__name__} : {e}"
            traceback.print_exc()
        finally:
            etat["en_cours"] = False


# ------------------------------------------------------------------ serveur


def gestionnaire(app: Application, hotes: set[str]):
    class Gestionnaire(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # console silencieuse
            pass

        def _repondre(self, code: int, corps: bytes, type_: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", type_)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(corps)

        def _json(self, code: int, donnees) -> None:
            self._repondre(code, json.dumps(donnees, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _hote_ok(self) -> bool:
            if self.headers.get("Host", "") in hotes:
                return True
            self._json(403, {"erreur": "hôte refusé"})
            return False

        def do_GET(self) -> None:
            if not self._hote_ok():
                return
            if self.path == "/":
                page = PAGE.read_text(encoding="utf-8").replace("__JETON__", app.jeton)
                self._repondre(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/etat":
                self._json(200, app.etat())
            else:
                self._json(404, {"erreur": "introuvable"})

        def do_POST(self) -> None:
            if not self._hote_ok():
                return
            if self.headers.get("X-Jeton") != app.jeton or not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(403, {"erreur": "jeton invalide : recharger la page"})
                return
            try:
                longueur = int(self.headers.get("Content-Length", 0))
                donnees = json.loads(self.rfile.read(longueur) or b"{}")
                if self.path == "/api/action":
                    self._json(200, {"message": app.action(donnees)})
                elif self.path == "/api/cycle":
                    self._json(200, {"message": app.lancer_cycle(donnees)})
                else:
                    self._json(404, {"erreur": "introuvable"})
            except Refus as e:
                self._json(409, {"erreur": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"erreur": f"{type(e).__name__} : {e}"})

    return Gestionnaire


def servir(app: Application, port: int = 8765, ouvrir_navigateur: bool = True) -> None:
    try:
        serveur = HTTPServer(("127.0.0.1", port), None)
    except OSError:
        serveur = HTTPServer(("127.0.0.1", 0), None)  # port pris : un autre au hasard
    port = serveur.server_address[1]
    serveur.RequestHandlerClass = gestionnaire(app, {f"127.0.0.1:{port}", f"localhost:{port}"})
    adresse = f"http://127.0.0.1:{port}/"
    print(f"Interface : {adresse}  (Ctrl+C pour arrêter)", flush=True)
    if ouvrir_navigateur:
        import webbrowser

        webbrowser.open(adresse)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("Interface arrêtée.")
    finally:
        serveur.server_close()
