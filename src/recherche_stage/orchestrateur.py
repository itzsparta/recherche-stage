"""Orchestrateur : supervise le pipeline, route les dossiers d'un agent à
l'autre selon leur statut, tient le budget, isole les erreurs et escalade à
l'humain. Expose aussi les décisions humaines (valider, rejeter…) à la CLI."""

from __future__ import annotations

import difflib
import random
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import controles
from .agents.apprentissage import PREFIXE_MOTIF, AgentApprentissage
from .agents.base import Contexte
from .agents.critique import AgentCritique
from .agents.enquete import AgentEnquete
from .agents.envoi import AgentEnvoi
from .agents.profil import AgentProfil
from .agents.redaction import AgentRedaction
from .agents.scoring import AgentScoring
from .agents.suivi import AgentSuivi
from .agents.veille import AgentVeille
from .canaux.messagerie import Boite, Expediteur
from .depot import lancement
from .llm import BudgetEpuise, ErreurLLM, NonConnecte, QuotaEpuise
from .modeles import Critique, Dossier, Envoi, Statut
from .reseau import ErreurReseau
from .sites import site_tiers
from .sources import Source

ERREURS_AVANT_FILE = 3  # au-delà, le dossier part dans la file des irrécupérables
EN_VOL = (Statut.enquetee, Statut.redigee, Statut.en_critique, Statut.validee)
AUTO_APPLIQUEES = {"allocation_sources"}


class Orchestrateur:
    nom = "orchestrateur"

    def __init__(
        self,
        ctx: Contexte,
        sources: list[Source],
        expediteur: Expediteur | None = None,
        boite: Boite | None = None,
        rng: random.Random | None = None,
        dormir: Callable[[float], None] = time.sleep,
    ):
        self.ctx = ctx
        self.veille = AgentVeille(ctx, sources, dormir)
        self.scoring = AgentScoring(ctx)
        self.enquete = AgentEnquete(ctx)
        self.redaction = AgentRedaction(ctx)
        self.critique = AgentCritique(ctx)
        self.envoi = AgentEnvoi(ctx, expediteur)
        self.suivi = AgentSuivi(ctx, boite, expediteur)
        self.apprentissage = AgentApprentissage(ctx, rng)
        self.progres: Callable[[str], None] = lambda message: None  # avancement, affiché par la CLI
        # Agent au travail (« debut »), puis issue (« ok » / « erreur ») : animation de l'interface.
        self.activite: Callable[[str | None, str, str], None] = lambda agent, issue="debut", sujet="": None

    @property
    def store(self):
        return self.ctx.store

    # ------------------------------------------------------- outils internes

    def _escalader(self, dossier_id: str | None, type_: str, question: str) -> None:
        self.store.escalader(dossier_id, type_, question)
        self.store.journaliser(self.nom, f"escalade:{type_}", dossier_id, details={"question": question})

    def _executer(self, agent_nom: str, dossier: Dossier, action: Callable[[], None]) -> bool:
        """Lance une étape en isolant ses erreurs. Après plusieurs échecs, le
        dossier rejoint la file des irrécupérables (statut erreur) et l'humain est prévenu."""
        self.activite(agent_nom, "debut", dossier.offre.entreprise)
        try:
            action()
            self.store.save(dossier)
            self.activite(agent_nom, "ok")
            return True
        except BudgetEpuise:
            self.activite(agent_nom, "erreur")
            raise
        except (ErreurLLM, ErreurReseau, OSError, ValueError, RuntimeError) as e:
            self.activite(agent_nom, "erreur")
            echecs = sum(1 for h in dossier.historique if h.action.startswith("erreur")) + 1
            self.progres(f"  erreur ({echecs}/{ERREURS_AVANT_FILE}) : {e}")
            dossier.tracer(agent_nom, f"erreur:{e.__class__.__name__}")
            self.store.journaliser(agent_nom, "erreur", dossier.id, details={"erreur": str(e), "echecs": echecs})
            if echecs >= ERREURS_AVANT_FILE:
                dossier.statut = Statut.erreur
                dossier.motif = f"{agent_nom} : {e}"
                self._escalader(dossier.id, "erreur", f"{dossier.offre.entreprise} bloqué à l'étape {agent_nom} après {echecs} erreurs : {e}")
            self.store.save(dossier)
            return False

    def capacite(self) -> int:
        """Dossiers qu'on peut encore lancer cette semaine sans dépasser le budget d'envois."""
        en_vol = len(self.store.lister(EN_VOL))
        en_vol += sum(1 for d in self.store.lister([Statut.en_attente_humain]) if d.etape_attente in ("critique", "envoi", "depot"))
        maximum = self.ctx.config.orchestrateur.candidatures_max_par_semaine
        return max(0, maximum - self.store.envois_semaine() - en_vol)

    def _deadline_depassee(self) -> bool:
        deadline = self.ctx.grille().deadline
        if deadline and self.ctx.horloge().date() > deadline:
            self._escalader(None, "deadline", f"Deadline du {deadline} dépassée : plus de nouvelle candidature, le suivi continue.")
            return True
        return False

    # ------------------------------------------------------------------ cycle

    def cycle(self, collecter: bool = True, envoyer: bool = True, suivre: bool = True) -> Counter:
        rapport: Counter = Counter()
        # Nouvel essai : les alertes du cycle précédent seront relevées si elles persistent.
        self.store.fermer_escalades_globales(("quota", "budget", "suivi"), "nouveau cycle")
        try:
            self._cycle(rapport, collecter, envoyer, suivre)
        except NonConnecte as e:
            rapport["non_connecte"] += 1
            self.progres(f"Arrêt du cycle : {e}")
            self._escalader(
                None, "quota",
                f"{e}. Le travail fait est conservé ; dans un terminal, lancer `claude`, taper /login, "
                "puis relancer un cycle.",
            )
        except QuotaEpuise as e:
            rapport["quota_epuise"] += 1
            self.progres(f"Arrêt du cycle : {e}")
            self._escalader(
                None, "quota",
                f"{e}. Le travail fait est conservé ; relancer rs cycle après la remise à zéro "
                + ("indiquée ci-dessus." if self.ctx.config.llm.fournisseur == "claude_code" else
                   "du quota (quotidienne, vers 9 h à Paris) ou activer la facturation de la clé sur aistudio.google.com."),
            )
        except BudgetEpuise as e:
            rapport["budget_epuise"] += 1
            self._escalader(None, "budget", f"{e}. Le pipeline reprendra lundi (ou relever appels_api_max_par_semaine).")
        self.activite(None)
        self.store.journaliser(self.nom, "cycle", details=dict(rapport))
        self.mettre_a_jour_excel()  # même après un arrêt sur quota : le travail fait y figure
        return rapport

    def _cycle(self, rapport: Counter, collecter: bool, envoyer: bool, suivre: bool) -> None:
        seuil = self.ctx.config.orchestrateur.seuil_score_minimum
        seuil_confiance = self.ctx.config.orchestrateur.seuil_confiance
        par_cycle = self.ctx.config.orchestrateur.dossiers_max_par_cycle
        nouvelles_candidatures = not self._deadline_depassee()

        if collecter and nouvelles_candidatures:
            self.progres("Collecte des offres et cibles…")
            self.activite("veille")
            rapport["collectees"] += len(self.veille.executer())
            self.activite("veille", "ok")

        profil, grille = self.ctx.profil(), self.ctx.grille()
        # `filtree` : scoring interrompu par une erreur au cycle précédent, on le reprend.
        a_scorer = self.store.lister([Statut.collectee, Statut.filtree])
        for i, dossier in enumerate(a_scorer, 1):
            self.progres(f"Scoring {i}/{len(a_scorer)} : {dossier.offre.entreprise}")
            if self._executer("scoring", dossier, lambda d=dossier: self.scoring.evaluer(d, profil, grille)):
                rapport[f"scoring:{dossier.statut.value}"] += 1

        if nouvelles_candidatures:
            candidats = [
                d for d in self.store.lister([Statut.scoree])
                if d.score.valeur >= seuil and (d.score.confiance >= seuil_confiance or "scoring" in d.valide_humain)
            ]
            candidats.sort(key=lambda d: (d.prioritaire, d.score.valeur), reverse=True)
            retenus = candidats[: min(self.capacite(), par_cycle)]
            for i, dossier in enumerate(retenus, 1):
                self.progres(f"Enquête {i}/{len(retenus)} : {dossier.offre.entreprise}")
                if self._executer("enquete", dossier, lambda d=dossier: self.enquete.executer(d)):
                    rapport["enquetes"] += 1

        if not profil.cv_pdf or not Path(profil.cv_pdf).exists():
            self._escalader(None, "cv_pdf", "CV PDF introuvable : rs profil --cv-pdf <fichier>. Rédaction suspendue.")
        else:
            # `redigee` / `en_critique` : boucle interrompue par une erreur, reprise au Critique
            # en premier ; puis les meilleurs scores, dans la limite du cycle.
            a_rediger = sorted(
                self.store.lister([Statut.enquetee, Statut.redigee, Statut.en_critique]),
                key=lambda d: (d.statut != Statut.enquetee, bool(d.consigne_humain), d.prioritaire, d.score.valeur),
                reverse=True,
            )[:par_cycle]
            for i, dossier in enumerate(a_rediger, 1):
                self.progres(f"Rédaction et critique {i}/{len(a_rediger)} : {dossier.offre.entreprise}")
                self._rediger_et_critiquer(dossier, rapport)

        for dossier in self.store.lister([Statut.validee]):
            if dossier.envoi.canal is None:
                if self._executer("envoi", dossier, lambda d=dossier: self.envoi.preparer(d)):
                    rapport["brouillons"] += 1
            elif envoyer and dossier.envoi.prevu_le and dossier.envoi.prevu_le <= self.ctx.horloge():
                parti = False

                def envoyer_dossier(d=dossier):
                    nonlocal parti
                    parti = self.envoi.envoyer(d)

                self._executer("envoi", dossier, envoyer_dossier)
                rapport["envoyees"] += parti

        if suivre:
            self.progres("Suivi de la boîte mail…")
            self.activite("suivi")
            try:
                resultat = self.suivi.executer()
                rapport["reponses"] += resultat["reponses"]
                rapport["relances"] += resultat["relances"]
                self.activite("suivi", "ok")
            except (ErreurLLM, ErreurReseau) as e:
                self.activite("suivi", "erreur")
                # Boîte injoignable : le reste du cycle est acquis, le suivi reprendra au prochain.
                rapport["suivi_en_echec"] += 1
                self.store.journaliser("suivi", "erreur", details={"erreur": str(e)})
                self._escalader(None, "suivi", f"Suivi des réponses impossible : {e}. Vérifier suivi.imap dans config.yaml.")

        self._bilan_si_du(rapport)

    def evaluer_presentiel(self) -> int:
        """Juge, pour les candidatures prêtes à partir, si un dépôt en main propre
        vaut la peine. Renvoie le nombre de dépôts conseillés parmi les nouvelles évaluations."""
        params = self.ctx.config.presentiel
        if not params.actif:
            return 0
        from .excel import a_exporter
        from .presentiel import evaluer, geocoder

        a_juger = [d for d in self.store.lister() if d.presentiel is None and a_exporter(d)]
        if not a_juger:
            return 0
        depart = params.domicile or self.ctx.profil().identite.ville
        domicile = geocoder(self.ctx.http, self.store, depart) if depart else None
        if domicile is None:
            self.progres("Dépôt en présentiel non évalué : indiquer presentiel.domicile dans config.yaml")
            return 0
        conseilles = 0
        for d in a_juger:
            o = d.offre
            point = (o.latitude, o.longitude) if o.latitude is not None and o.longitude is not None else None
            if point is None and o.type == "spontanee":
                point = geocoder(self.ctx.http, self.store, o.adresse or o.lieu)
            d.presentiel = evaluer(d, domicile, point, params)
            if d.presentiel.raison == "distance inconnue":
                d.presentiel = None  # réessayé au prochain passage (réseau)
                continue
            conseilles += d.presentiel.conseille
            self.store.save(d)
        return conseilles

    def mettre_a_jour_excel(self) -> str:
        """Reporte les candidatures dans le tableau Excel de l'utilisateur, s'il en a un."""
        try:
            self.evaluer_presentiel()
        except Exception as e:  # noqa: BLE001 — un bonus : ne doit jamais empêcher la mise à jour
            self.store.journaliser(self.nom, "presentiel_erreur", details={"erreur": str(e)})
        chemin = self.ctx.config.suivi.excel
        if not chemin:
            return ""
        from .excel import ErreurExcel, mettre_a_jour

        try:
            ajoutees, maj = mettre_a_jour(chemin, self.store.lister(), self.ctx.config.suivi.excel_feuille)
        except (ErreurExcel, OSError, KeyError) as e:
            message = f"Tableau Excel non mis à jour : {e}"
            self.store.journaliser(self.nom, "excel_erreur", details={"erreur": str(e)})
        else:
            message = f"Tableau Excel : {ajoutees} ligne(s) ajoutée(s), {maj} mise(s) à jour"
            self.store.journaliser(self.nom, "excel", details={"ajoutees": ajoutees, "mises_a_jour": maj})
        self.progres(message)
        return message

    def _rediger_et_critiquer(self, dossier: Dossier, rapport: Counter) -> None:
        """Boucle Rédaction → Critique, bornée par le nombre d'itérations du Critique."""
        if not dossier.variante:
            dossier.variante = self.apprentissage.choisir_variante()
        for _ in range(self.ctx.config.orchestrateur.iterations_critique_max + 1):
            if dossier.statut == Statut.enquetee and not self._executer("redaction", dossier, lambda d=dossier: self.redaction.executer(d)):
                return
            if not self._executer("critique", dossier, lambda d=dossier: self.critique.executer(d)):
                return
            rapport[f"critique:{dossier.critique.decision}"] += 1
            if dossier.critique.decision != "renvoyer":
                return

    def _bilan_si_du(self, rapport: Counter) -> None:
        dernier = self.store.parametre("dernier_bilan")
        if dernier and self.ctx.horloge() - datetime.fromisoformat(dernier) < timedelta(days=7):
            return
        self.progres("Bilan hebdomadaire de l'Apprentissage…")
        self.activite("apprentissage")
        self.apprentissage.executer()
        self.appliquer_propositions(auto_seulement=True)
        self.activite("apprentissage", "ok")
        self.store.definir_parametre("dernier_bilan", self.ctx.horloge().isoformat(timespec="seconds"))
        rapport["bilan"] += 1

    # --------------------------------------------- propositions d'apprentissage

    def appliquer_propositions(self, auto_seulement: bool = False, ids: set[int] | None = None) -> list[str]:
        appliquees = []
        derniere_allocation = None
        for p in self.store.propositions():
            if auto_seulement and p["mecanisme"] not in AUTO_APPLIQUEES:
                continue
            if ids is not None and p["id"] not in ids:
                continue
            c = p["contenu"]
            if p["mecanisme"] == "allocation_sources":
                derniere_allocation = p  # seule la plus récente compte
                continue
            if p["mecanisme"] == "recalibrage_scoring":
                ajustements = self.store.parametre("ajustements_poids", {}) or {}
                for nom, facteur in c.get("ajustements_poids", {}).items():
                    ajustements[nom] = round(ajustements.get(nom, 1.0) * facteur, 3)
                self.store.definir_parametre("ajustements_poids", ajustements)
                s = self.ctx.config.scoring
                poids = self.store.parametre("poids_scoring", {"regles": s.poids_regles, "lexical": s.poids_lexical, "llm": s.poids_llm})
                for cle, facteur in c.get("facteurs_composantes", {}).items():
                    poids[cle] = round(poids[cle] * facteur, 3)
                self.store.definir_parametre("poids_scoring", poids)
                AgentProfil(self.ctx).reconstruire_grille()
            elif p["mecanisme"] == "calibration_critique":
                self.store.definir_parametre("seuil_validation_critique", c["seuil_validation_critique"])
            elif p["mecanisme"] == "nouvelles_regles":
                self.store.ajouter_regle(c["motif"], c["raison"])
            self.store.marquer_proposition(p["id"], "appliquee")
            appliquees.append(f"{p['mecanisme']} → {p['cible']}")
        if derniere_allocation:
            for p in self.store.propositions():
                if p["mecanisme"] == "allocation_sources" and p["id"] != derniere_allocation["id"]:
                    self.store.marquer_proposition(p["id"], "remplacee")
            self.store.definir_parametre("allocation_sources", derniere_allocation["contenu"]["allocation_sources"])
            self.store.marquer_proposition(derniere_allocation["id"], "appliquee")
            appliquees.append("allocation_sources → Veille")
        self.store.journaliser(self.nom, "propositions_appliquees", details={"liste": appliquees})
        return appliquees

    # -------------------------------------------------- décisions humaines

    def _dossier(self, dossier_id: str) -> Dossier:
        dossier = self.store.get(dossier_id)
        if dossier is None:
            raise KeyError(f"Dossier inconnu : {dossier_id}")
        return dossier

    def _relire_modifications(self, dossier: Dossier) -> str:
        """Reprend les fichiers édités par l'humain et renvoie le diff, phrase à
        phrase, entre la version générée et la version corrigée."""
        doc = dossier.documents
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        avant = f"{doc.objet_mail}\n{doc.mail}\n{doc.texte_lettre}"
        mail = (fichiers / f"mail_v{doc.version}.txt").read_text(encoding="utf-8")
        objet, _, corps = mail.partition("\n\n")
        doc.objet_mail = objet.removeprefix("Objet :").strip()
        doc.mail = corps.strip()
        if doc.lettre and Path(doc.lettre).exists():
            doc.texte_lettre = Path(doc.lettre).read_text(encoding="utf-8").strip()
        bloquants = [p for p in controles.controle_placeholders(dossier) + controles.controle_entreprise(dossier, []) if p.gravite == "bloquant"]
        if bloquants:
            raise ValueError("Correction refusée : " + "; ".join(f"{p.raison} ({p.extrait})" for p in bloquants))
        apres = f"{doc.objet_mail}\n{doc.mail}\n{doc.texte_lettre}"
        phrases = lambda t: [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", t) if s.strip()]  # noqa: E731
        diff = difflib.ndiff(phrases(avant), phrases(apres))
        return "\n".join(ligne for ligne in diff if ligne.startswith(("- ", "+ ")))

    def valider(self, dossier_id: str, modifie: bool = False) -> str:
        dossier = self._dossier(dossier_id)
        etape = dossier.etape_attente
        if not etape or etape == "depot":
            raise ValueError(f"Le dossier {dossier_id} n'attend aucune validation (statut {dossier.statut.value}, étape « {etape} »)")
        diff = self._relire_modifications(dossier) if modifie else ""
        # Résolues avant d'agir : les escalades créées par la suite (dépôt manuel…) restent ouvertes.
        self.store.resoudre_escalades(dossier.id, "validé")
        dossier.etape_attente = ""
        if etape == "scoring":
            self.store.enregistrer_decision(dossier.id, "scoring:poursuivre")
            dossier.statut = Statut.scoree
            dossier.valide_humain = sorted(set(dossier.valide_humain) | {"scoring"})
        elif etape in ("critique", "envoi"):
            # Seule la validation à l'étape d'envoi compte pour la levée de la porte humaine.
            decision = ("modifie" if modifie else "valide_tel_quel") if etape == "envoi" else f"critique:{'modifie' if modifie else 'valide_tel_quel'}"
            self.store.enregistrer_decision(dossier.id, decision, diff)
            dossier.modifie_par_humain = dossier.modifie_par_humain or modifie
            dossier.statut = Statut.validee
            dossier.valide_humain = sorted(set(dossier.valide_humain) | {etape, "envoi"})
            self.envoi.preparer(dossier)  # brouillon reconstruit avec la version validée
            if dossier.envoi.canal == "email":
                dossier.envoi.prevu_le = self.ctx.horloge()  # part au prochain cycle d'envoi
                dossier.envoi.automatique = False
        elif etape.startswith("relance:"):
            self.store.enregistrer_decision(dossier.id, "relance:validee")
            self.suivi.relancer(dossier, int(etape.split(":")[1]), valide_par_humain=True)
        else:
            raise ValueError(f"Étape inconnue : {etape}")
        dossier.tracer("humain", f"validation:{etape}", self.store.memoriser_entrees({"modifie": modifie, "diff": diff}))
        self.store.save(dossier)
        self.mettre_a_jour_excel()
        return etape

    def reecrire(self, dossier_id: str, consigne: str) -> None:
        """Renvoie les documents à la Rédaction avec la demande de l'humain
        (« moins sur le TIPE, plus sur le parcours… »). Rien n'est perdu :
        la version précédente sert de base, l'entreprise reste candidate."""
        consigne = consigne.strip()
        if not consigne:
            raise ValueError("Dire ce qu'il faut changer")
        dossier = self._dossier(dossier_id)
        etape = dossier.etape_attente
        if etape not in ("critique", "envoi"):
            raise ValueError(f"Le dossier {dossier_id} n'a pas de documents en attente de relecture")
        self.store.enregistrer_decision(dossier.id, "reecrire" if etape == "envoi" else "critique:reecrire", consigne)
        self.store.resoudre_escalades(dossier.id, "réécriture demandée")
        dossier.consigne_humain = consigne
        dossier.statut = Statut.enquetee
        dossier.etape_attente = ""
        dossier.motif = ""
        dossier.critique = Critique()
        dossier.envoi = Envoi()
        dossier.tracer("humain", "reecriture", self.store.memoriser_entrees({"consigne": consigne}))
        self.store.save(dossier)

    def rediger(self, dossier_id: str) -> Counter:
        """Rédaction → Critique pour un seul dossier, puis brouillon s'il est validé."""
        rapport: Counter = Counter()
        dossier = self._dossier(dossier_id)
        self.progres(f"Rédaction et critique : {dossier.offre.entreprise}")
        try:
            self._rediger_et_critiquer(dossier, rapport)
            if dossier.statut == Statut.validee and dossier.envoi.canal is None:
                if self._executer("envoi", dossier, lambda d=dossier: self.envoi.preparer(d)):
                    rapport["brouillons"] += 1
        except QuotaEpuise as e:
            rapport["quota_epuise"] += 1
            self.progres(f"Arrêt : {e}")
        return rapport

    def reprendre_enquetes(self) -> int:
        """Renvoie à l'Enquête les dossiers pas encore rédigés dont la fiche est
        restée minimale (recherche web en échec, quota atteint…)."""
        repris = 0
        for dossier in self.store.lister([Statut.enquetee]):
            if dossier.fiche_entreprise and dossier.fiche_entreprise.minimale and not dossier.documents.version:
                dossier.statut = Statut.scoree
                dossier.tracer(self.nom, "enquete_reprise")
                self.store.save(dossier)
                repris += 1
        self.store.journaliser(self.nom, "enquetes_reprises", details={"dossiers": repris})
        return repris

    def rejeter(self, dossier_id: str, motif: str = "") -> None:
        dossier = self._dossier(dossier_id)
        etape = dossier.etape_attente or "hors_etape"
        motif = motif.strip()
        self.store.enregistrer_decision(dossier.id, "rejete" if etape == "envoi" else f"{etape}:rejete",
                                        f"{PREFIXE_MOTIF}{motif}" if motif else "")
        if etape.startswith("relance:"):
            # Refuser une relance ne retire pas la candidature : on passe à l'échéance suivante.
            dossier.suivi.relances.append(self.ctx.horloge())
            dossier.etape_attente = ""
            dossier.tracer("humain", f"rejet:{etape}")
            self.store.resoudre_escalades(dossier.id, "relance ignorée")
            self.store.save(dossier)
            return
        dossier.statut = Statut.abandonnee
        dossier.motif = f"rejet humain ({etape}) : {motif}".strip()
        dossier.etape_attente = ""
        dossier.tracer("humain", f"rejet:{etape}", self.store.memoriser_entrees({"motif": motif}))
        self.store.resoudre_escalades(dossier.id, "rejeté")
        self.store.save(dossier)

    def indiquer_reponse(self, dossier_id: str, categorie: str, date_entretien: datetime | None = None) -> None:
        dossier = self._dossier(dossier_id)
        self.suivi.appliquer(dossier, categorie, self.ctx.horloge(), date_entretien)
        dossier.tracer("humain", f"reponse:{categorie}")
        self.store.enregistrer_decision(dossier.id, f"suivi:{categorie}")
        self.store.resoudre_escalades(dossier.id, categorie)
        self.store.save(dossier)
        self.mettre_a_jour_excel()

    def marquer_envoye(self, dossier_id: str, preuve: str) -> None:
        dossier = self._dossier(dossier_id)
        self.envoi.marquer_envoye(dossier, preuve)
        dossier.etape_attente = ""
        self.store.resoudre_escalades(dossier.id, "envoyé manuellement")
        self.store.save(dossier)
        self.mettre_a_jour_excel()

    def preparer_depot(self, dossier_id: str) -> Path:
        """Dépôt assisté sur le site carrière : trouve la page si l'Enquête ne
        l'a pas déjà, réserve l'envoi (anti-doublon) et prépare la tâche du
        navigateur. Le clic « Envoyer » reste à l'utilisateur."""
        dossier = self._dossier(dossier_id)
        if dossier.etape_attente != "depot":
            raise ValueError(f"{dossier.offre.entreprise} n'est pas à déposer.")
        if self.store.etat_envoi(dossier.id) == "envoye":
            raise ValueError(f"Candidature chez {dossier.offre.entreprise} déjà enregistrée comme envoyée.")
        fiche = dossier.fiche_entreprise
        donnee_par_toi = any(h.agent == "humain" and h.action == "page_carriere" for h in dossier.historique)
        if fiche.page_carriere and site_tiers(fiche.page_carriere) and not donnee_par_toi:
            self.progres(f"Adresse écartée (site d'offres, pas celui de l'entreprise) : {fiche.page_carriere}")
            fiche.page_carriere = ""
        if not fiche.page_carriere:
            self.progres(f"Recherche de la page carrières de {dossier.offre.entreprise}…")
            self.activite("enquete", "debut", dossier.offre.entreprise)
            try:
                self.enquete.trouver_page_carriere(dossier, self.progres)
            except (ErreurLLM, ErreurReseau, BudgetEpuise) as e:
                self.activite("enquete", "erreur")
                raise ValueError(f"Recherche de la page carrières impossible : {e}") from e
            self.activite("enquete", "ok")
            self.store.save(dossier)
            if not dossier.fiche_entreprise.page_carriere:
                raise ValueError(f"Page carrières de {dossier.offre.entreprise} introuvable : ouvre « Chercher sa page carrières », "
                                 "puis colle son adresse dans la carte.")
            self.progres(f"  page retenue : {dossier.fiche_entreprise.page_carriere}")
        else:
            self.progres(f"Page carrières de {dossier.offre.entreprise} : {dossier.fiche_entreprise.page_carriere}")
        self.store.reserver_envoi(dossier.id, "candidature", "formulaire")
        dossier.tracer("humain", "depot_assiste")
        self.store.save(dossier)
        pieces = self.envoi.pieces_jointes(dossier)
        self.progres("  pièces jointes : " + (", ".join(p.name for p in pieces) or "aucune (CV introuvable ?)"))
        return lancement.preparer(self.ctx, dossier, pieces)

    def definir_page_carriere(self, dossier_id: str, url: str) -> None:
        """Adresse donnée par l'utilisateur (quand la recherche n'a rien trouvé de sûr)."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("Adresse invalide : elle doit commencer par https://")
        dossier = self._dossier(dossier_id)
        dossier.fiche_entreprise.page_carriere = url
        dossier.tracer("humain", "page_carriere")
        self.store.save(dossier)

    def deposer_sur_place(self, dossier_id: str) -> None:
        """La candidature a été apportée en main propre (tournée présentiel).
        Si elle n'était pas encore partie, ce dépôt vaut envoi : le mail validé
        n'est alors plus envoyé (jamais deux candidatures à la même entreprise)."""
        dossier = self._dossier(dossier_id)
        if not (dossier.presentiel and dossier.presentiel.conseille):
            raise ValueError(f"{dossier.offre.entreprise} n'est pas dans la tournée")
        if dossier.presentiel.depose_le:
            raise ValueError(f"{dossier.offre.entreprise} est déjà marquée déposée")
        maintenant = self.ctx.horloge()
        dossier.presentiel.depose_le = maintenant
        dossier.tracer("humain", "depot_presentiel")
        if dossier.statut == Statut.validee or dossier.etape_attente == "depot":
            dossier.envoi.canal = None  # ni mail ni site : en main propre
            lieu = f" ({dossier.presentiel.adresse})" if dossier.presentiel.adresse else ""
            self.envoi.marquer_envoye(dossier, f"déposée en main propre le {maintenant:%d/%m/%Y}{lieu}")
            dossier.etape_attente = ""
            self.store.resoudre_escalades(dossier.id, "déposée en main propre")
        self.store.save(dossier)
        self.mettre_a_jour_excel()

    def liberer_envoi(self, dossier_id: str, nature: str = "candidature") -> None:
        """Après vérification humaine qu'aucun message n'est parti."""
        self.store.cx.execute("DELETE FROM envois WHERE dossier_id = ? AND nature = ? AND etat != 'envoye'", (dossier_id, nature))
        dossier = self._dossier(dossier_id)
        if nature == "candidature" and dossier.statut == Statut.erreur:
            dossier.statut = Statut.validee
            dossier.etape_attente = ""
        self.store.resoudre_escalades(dossier_id, "envoi libéré")
        self.store.journaliser("humain", "liberer_envoi", dossier_id, details={"nature": nature})
        self.store.save(dossier)
