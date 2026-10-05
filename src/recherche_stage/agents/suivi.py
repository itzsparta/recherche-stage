"""Agent 8 — Suivi : réponses, relances J+7 / J+14, entretiens."""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from ..autonomie import envoi_automatique_debloque
from ..canaux.agenda import ecrire_evenement
from ..canaux.messagerie import Boite, Expediteur, MessageRecu, construire_message
from ..hachage import normaliser, normaliser_entreprise
from ..modeles import Dossier, Statut
from .base import Agent, Contexte
from .redaction import date_francaise

SUIVIS = (Statut.envoyee, Statut.relancee, Statut.entretien)
DOMAINES_PERSONNELS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "live.", "laposte.net", "orange.fr", "free.fr")

SYSTEME_CLASSIFICATION = """Tu classes la réponse reçue à une candidature de stage.
- refus : la candidature n'est pas retenue.
- entretien : proposition d'entretien ou de rendez-vous (donne la date au format AAAA-MM-JJTHH:MM si elle est fixée, sinon chaîne vide).
- demande_info : l'entreprise demande un document ou une information.
- autre : accusé de réception automatique, message sans rapport, etc.
`concerne_candidature` : faux si le message ne parle pas de cette candidature.
`confiance` entre 0 et 1 : sois honnête, un message ambigu mérite une confiance basse."""

SYSTEME_PREPARATION = """Tu prépares un étudiant à un entretien de stage.
N'utilise que le profil, l'offre et la fiche entreprise fournis ; n'invente aucun fait sur l'entreprise.
Pour chaque question probable, indique les éléments du profil (identifiants) qui permettent d'y répondre.
`a_verifier` : ce que le candidat devra vérifier lui-même avant l'entretien."""


class Classification(BaseModel):
    concerne_candidature: bool
    categorie: Literal["refus", "entretien", "demande_info", "autre"]
    confiance: float
    date_entretien: str
    resume: str


class QuestionPreparee(BaseModel):
    question: str
    elements_du_profil: list[str]
    piste_de_reponse: str


class FichePreparation(BaseModel):
    points_cles_entreprise: list[str]
    questions_probables: list[QuestionPreparee]
    questions_a_poser: list[str]
    a_verifier: list[str]


class AgentSuivi(Agent):
    nom = "suivi"

    def __init__(self, ctx: Contexte, boite: Boite | None = None, expediteur: Expediteur | None = None):
        super().__init__(ctx)
        self.boite = boite
        self.expediteur = expediteur

    # ---------------------------------------------------------- rattachement

    @staticmethod
    def _message_id(dossier: Dossier) -> str:
        return dossier.envoi.preuve.split(" ;")[0].strip()

    def rattacher(self, message: MessageRecu, dossiers: list[Dossier]) -> Dossier | None:
        for d in dossiers:
            if self._message_id(d) and self._message_id(d) in message.references:
                return d
        domaine = message.expediteur.rsplit("@", 1)[-1]
        if not domaine.startswith(DOMAINES_PERSONNELS):
            par_domaine = [d for d in dossiers if d.envoi.destinataire.lower().endswith("@" + domaine)]
            if len(par_domaine) == 1:
                return par_domaine[0]
        texte = normaliser(f"{message.objet} {message.corps[:2000]} {domaine.replace('.', ' ')}")
        par_nom = [d for d in dossiers if normaliser_entreprise(d.offre.entreprise) and f" {normaliser_entreprise(d.offre.entreprise)} " in f" {texte} "]
        return par_nom[0] if len(par_nom) == 1 else None

    # --------------------------------------------------------------- réponses

    def traiter_reponses(self) -> int:
        if self.boite is None:
            return 0
        store = self.ctx.store
        derniere = store.parametre("suivi_derniere_lecture")
        depuis = datetime.fromisoformat(derniere) - timedelta(days=1) if derniere else self.ctx.horloge() - timedelta(days=30)
        traites: list[str] = store.parametre("messages_traites", []) or []
        # Les alertes d'offres déjà lues par la Veille citent des entreprises
        # sans être des réponses : ne pas les rattacher par le nom.
        alertes = set(store.parametre("alertes_traitees", []) or [])
        dossiers = store.lister(SUIVIS)
        n = 0
        for message in self.boite.messages_depuis(depuis):
            if message.message_id in traites or message.message_id in alertes:
                continue
            traites.append(message.message_id)
            dossier = self.rattacher(message, dossiers)
            if dossier is None:
                continue
            self.classer(dossier, message)
            store.save(dossier)
            n += 1
        store.definir_parametre("messages_traites", traites[-2000:])
        store.definir_parametre("suivi_derniere_lecture", self.ctx.horloge().isoformat(timespec="seconds"))
        return n

    def classer(self, dossier: Dossier, message: MessageRecu) -> None:
        o = dossier.offre
        c = self.ctx.llm.structurer(
            self.nom,
            SYSTEME_CLASSIFICATION,
            f"Candidature : « {o.intitule} » chez {o.entreprise}\nDe : {message.expediteur}\n"
            f"Objet : {message.objet}\n\n{message.corps[:6000]}",
            Classification,
        )
        entrees = {"message_id": message.message_id, "objet": message.objet, "corps": message.corps[:6000]}
        if not c.concerne_candidature or c.categorie == "autre":
            self.tracer(dossier, "message_ignore", entrees, {"resume": c.resume})
            return
        if c.confiance < self.ctx.config.suivi.seuil_confiance_classification:
            self.tracer(dossier, "classification_incertaine", entrees, {"categorie": c.categorie, "confiance": c.confiance})
            self.escalader(
                dossier,
                "classification",
                f"Réponse de {o.entreprise} difficile à classer (« {c.resume} »). "
                f"Indiquer : rs reponse {dossier.id} refus|entretien|demande_info",
            )
            return
        date_entretien = None
        if c.date_entretien:
            try:
                date_entretien = datetime.fromisoformat(c.date_entretien)
            except ValueError:
                date_entretien = None
        self.appliquer(dossier, c.categorie, message.date, date_entretien, message.expediteur, c.resume)
        self.tracer(dossier, f"reponse:{c.categorie}", entrees, {"confiance": c.confiance})

    def appliquer(self, dossier: Dossier, categorie: str, recue_le: datetime, date_entretien: datetime | None, expediteur: str = "", resume: str = "") -> None:
        dossier.suivi.reponse = categorie
        dossier.suivi.date_reponse = recue_le
        self._memoriser(dossier, recue_le, expediteur)
        if categorie == "refus":
            dossier.statut = Statut.refus
        elif categorie == "entretien":
            dossier.statut = Statut.entretien
            dossier.suivi.entretien_le = date_entretien
            self.preparer_entretien(dossier)
            quand = f"le {date_entretien:%d/%m/%Y à %H:%M}" if date_entretien else "date à convenir"
            self.escalader(dossier, "information", f"Entretien proposé par {dossier.offre.entreprise} ({quand}). Fiche : {dossier.suivi.fiche_preparation}")
        elif categorie == "demande_info":
            self.escalader(dossier, "demande_info", f"{dossier.offre.entreprise} demande une information : {resume}")

    def _memoriser(self, dossier: Dossier, recue_le: datetime, expediteur: str) -> None:
        cle = normaliser_entreprise(dossier.offre.entreprise)
        memoire = self.ctx.store.memoire(cle)
        if dossier.envoi.horodatage:
            memoire.setdefault("delais_jours", []).append(round((recue_le - dossier.envoi.horodatage).total_seconds() / 86400, 1))
        if expediteur and not expediteur.rsplit("@", 1)[-1].startswith(DOMAINES_PERSONNELS):
            memoire["contact"] = expediteur  # interlocuteur qui répond : à privilégier ensuite
        self.ctx.store.maj_memoire(cle, memoire)

    def preparer_entretien(self, dossier: Dossier) -> None:
        profil = self.ctx.profil()
        fiche = self.ctx.llm.structurer(
            self.nom,
            SYSTEME_PREPARATION,
            f"PROFIL :\n{profil.model_dump_json(exclude={'cv_pdf'})}\n\nOFFRE :\n{dossier.offre.model_dump_json()}\n\n"
            f"FICHE ENTREPRISE :\n{dossier.fiche_entreprise.model_dump_json()}",
            FichePreparation,
        )
        lignes = [f"# Préparation d'entretien — {dossier.offre.intitule} chez {dossier.offre.entreprise}", "", "## Points clés de l'entreprise"]
        lignes += [f"- {p}" for p in fiche.points_cles_entreprise] or ["- (fiche minimale : se renseigner)"]
        lignes += ["", "## Questions probables"]
        for q in fiche.questions_probables:
            lignes += [f"### {q.question}", f"Appuis : {', '.join(q.elements_du_profil)}", q.piste_de_reponse, ""]
        lignes += ["## Questions à poser"] + [f"- {q}" for q in fiche.questions_a_poser]
        lignes += ["", "## À vérifier avant l'entretien"] + [f"- {q}" for q in fiche.a_verifier]
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        chemin = fichiers / "preparation_entretien.md"
        chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        dossier.suivi.fiche_preparation = str(chemin)
        if dossier.suivi.entretien_le:
            ecrire_evenement(
                fichiers / "entretien.ics",
                dossier.id,
                dossier.suivi.entretien_le,
                f"Entretien de stage — {dossier.offre.entreprise}",
                f"{dossier.offre.intitule}\nPréparation : {chemin}",
            )

    # --------------------------------------------------------------- relances

    def texte_relance(self, dossier: Dossier) -> tuple[str, str]:
        profil, grille = self.ctx.profil(), self.ctx.grille()
        ident = profil.identite
        envoye = date_francaise(dossier.envoi.horodatage.date()) if dossier.envoi.horodatage else "récemment"
        periode = ""
        if grille.durs.disponible_du and grille.durs.disponible_au:
            periode = f" Je suis disponible du {date_francaise(grille.durs.disponible_du)} au {date_francaise(grille.durs.disponible_au)}."
        corps = (
            "Bonjour,\n\n"
            f"Le {envoye}, je vous ai adressé ma candidature au poste « {dossier.offre.intitule} ». "
            f"Elle reste d'actualité.{periode} Je reste à votre disposition pour en échanger.\n\n"
            f"Bien cordialement,\n{ident.prenom} {ident.nom}\n{ident.email} · {ident.telephone}\n"
        )
        return f"Re: {dossier.documents.objet_mail}", corps

    def relances_dues(self) -> list[tuple[Dossier, int]]:
        dues = []
        for d in self.ctx.store.lister((Statut.envoyee, Statut.relancee)):
            if d.suivi.reponse != "aucune" or not d.envoi.horodatage or d.etape_attente.startswith("relance"):
                continue
            ecoules = (self.ctx.horloge() - d.envoi.horodatage).days
            numero = len(d.suivi.relances) + 1
            jours = self.ctx.config.suivi.relances_jours
            if numero <= len(jours) and ecoules >= jours[numero - 1]:
                dues.append((d, numero))
        return dues

    def relancer(self, dossier: Dossier, numero: int, valide_par_humain: bool = False) -> bool:
        if dossier.envoi.canal != "email":
            self.escalader(dossier, "relance_manuelle", f"Relance n°{numero} conseillée pour {dossier.offre.entreprise} (candidature déposée hors e-mail).")
            dossier.suivi.relances.append(self.ctx.horloge())
            dossier.statut = Statut.relancee
            return False
        objet, corps = self.texte_relance(dossier)
        auto = envoi_automatique_debloque(self.ctx.store, self.ctx.config) and not dossier.prioritaire
        if not (auto or valide_par_humain):
            chemin = self.ctx.dossier_fichiers(dossier.id) / f"relance{numero}.txt"
            chemin.write_text(f"Objet : {objet}\n\n{corps}", encoding="utf-8")
            dossier.etape_attente = f"relance:{numero}"
            self.escalader(dossier, "validation_relance", f"Relance n°{numero} prête pour {dossier.offre.entreprise} ({chemin}). rs valider {dossier.id}")
            return False
        if not self.ctx.store.reserver_envoi(dossier.id, f"relance{numero}", "email"):
            return False  # déjà envoyée : idempotence
        if self.expediteur is None:
            self.ctx.store.liberer_envoi(dossier.id, f"relance{numero}")
            raise RuntimeError("Aucun expéditeur SMTP configuré")
        expediteur = self.ctx.config.envoi.smtp.expediteur or self.ctx.profil().identite.email
        message = construire_message(expediteur, dossier.envoi.destinataire, objet, corps, en_reponse_a=self._message_id(dossier) or None)
        try:
            preuve = self.expediteur.envoyer(message)
        except (smtplib.SMTPException, OSError) as e:
            self.escalader(
                dossier, "echec_envoi",
                f"Relance n°{numero} à {dossier.offre.entreprise} en échec ({e}). Vérifier les Envoyés, "
                f"puis rs liberer-envoi {dossier.id} --nature relance{numero}",
            )
            return False
        self.ctx.store.confirmer_envoi(dossier.id, f"relance{numero}", preuve)
        dossier.suivi.relances.append(self.ctx.horloge())
        dossier.statut = Statut.relancee
        dossier.etape_attente = ""
        self.tracer(dossier, f"relance{numero}", {"objet": objet, "corps": corps}, {"message_id": preuve})
        return True

    def executer(self) -> dict:
        reponses = self.traiter_reponses()
        relances = 0
        for dossier, numero in self.relances_dues():
            relances += self.relancer(dossier, numero)
            self.ctx.store.save(dossier)
        return {"reponses": reponses, "relances": relances}
