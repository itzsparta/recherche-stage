"""Agent 7 — Envoi : choisit le canal et le moment, prépare le brouillon,
attend la validation humaine si nécessaire, puis envoie de façon idempotente.

Idempotence : un envoi est réservé dans la base (clé dossier + nature) avant
toute transmission. Une réservation existante bloque tout nouvel envoi ; en
cas d'état incertain, on escalade au lieu de réessayer.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from pathlib import Path

from ..autonomie import raisons_validation
from ..canaux.messagerie import Expediteur, construire_message
from ..canaux.pdf_simple import texte_vers_pdf
from ..modeles import Dossier, Statut
from .base import Agent, Contexte

PLATEFORMES = {"jobteaser", "indeed", "welcometothejungle", "wttj", "linkedin", "hellowork", "france_travail"}


class AgentEnvoi(Agent):
    nom = "envoi"

    def __init__(self, ctx: Contexte, expediteur: Expediteur | None = None):
        super().__init__(ctx)
        self.expediteur = expediteur

    # ------------------------------------------------------------- décisions

    def choisir_canal(self, dossier: Dossier) -> tuple[str, str]:
        contact = dossier.offre.contact_email or dossier.fiche_entreprise.contact
        if contact:
            return "email", contact
        if dossier.offre.source.lower() in PLATEFORMES:
            return "plateforme", dossier.offre.url
        return "formulaire", dossier.offre.url

    def prochain_creneau(self, maintenant: datetime) -> datetime:
        """Prochain créneau d'envoi : jours ouvrés choisis, en matinée."""
        f = self.ctx.config.envoi.fenetre
        moment = maintenant
        for _ in range(8):
            if moment.weekday() in f.jours:
                debut = moment.replace(hour=f.heure_debut, minute=0, second=0, microsecond=0)
                fin = moment.replace(hour=f.heure_fin, minute=0, second=0, microsecond=0)
                if moment < debut:
                    return debut
                if moment < fin:
                    return moment
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return maintenant

    # ------------------------------------------------------------- brouillon

    def pieces_jointes(self, dossier: Dossier) -> list[Path]:
        doc = dossier.documents
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        pieces: list[Path] = []
        cv_adapte_pdf = fichiers / f"cv_v{doc.version}.pdf"  # export PDF manuel du CV HTML, s'il existe
        cv = cv_adapte_pdf if cv_adapte_pdf.exists() else (Path(doc.piece_jointe_cv) if doc.piece_jointe_cv else None)
        if cv and cv.exists():
            pieces.append(cv)
        if doc.texte_lettre:
            identite = self.ctx.profil().identite
            nom = f"Lettre_motivation_{identite.prenom}_{identite.nom}".replace(" ", "_")
            pieces.append(texte_vers_pdf(doc.texte_lettre, fichiers / f"{nom}.pdf"))
        return pieces

    def preparer(self, dossier: Dossier) -> None:
        """Dossier validé par le Critique → brouillon, puis porte humaine ou créneau d'envoi."""
        canal, destinataire = self.choisir_canal(dossier)
        dossier.envoi.canal = canal
        dossier.envoi.destinataire = destinataire
        doc = dossier.documents
        pieces = self.pieces_jointes(dossier)
        fichiers = self.ctx.dossier_fichiers(dossier.id)
        if canal == "email":
            message = construire_message(self.ctx.config.envoi.smtp.expediteur or self.ctx.profil().identite.email,
                                         destinataire, doc.objet_mail, doc.mail, pieces)
            (fichiers / "brouillon.eml").write_bytes(bytes(message))
        else:
            reponses = "\n\n".join(f"## {q}\n{r}" for q, r in doc.reponses_formulaire.items())
            (fichiers / "a_deposer.md").write_text(
                f"# Candidature à déposer\n\nAdresse : {destinataire}\n\nPièces : {', '.join(str(p) for p in pieces)}\n\n{reponses}\n",
                encoding="utf-8",
            )

        # Un humain qui vient de relire et valider ces documents vaut validation d'envoi.
        raisons = [] if "envoi" in dossier.valide_humain else raisons_validation(dossier, self.ctx.store, self.ctx.config)
        if not raisons and canal != "email":
            dossier.statut = Statut.en_attente_humain
            dossier.etape_attente = "depot"
            dossier.motif = "depot_manuel"
            ou = f"({canal}) : {destinataire}"
            if dossier.offre.type == "spontanee":
                ou = (f"chez {dossier.offre.entreprise} : aucune adresse publique trouvée, passer par le "
                      f"formulaire de contact ou l'adresse RH de son site (fiche : {destinataire})")
            self.escalader(
                dossier,
                "depot_manuel",
                f"Déposer la candidature « {dossier.offre.intitule} » {ou}. "
                f"Pièces et réponses : {fichiers / 'a_deposer.md'}. Puis : rs marquer-envoye {dossier.id} \"<preuve>\"",
            )
        elif raisons:
            dossier.statut = Statut.en_attente_humain
            dossier.etape_attente = "envoi"
            dossier.motif = "validation_envoi"
            ou = f"{canal} → {destinataire}"
            if canal != "email" and dossier.offre.type == "spontanee":
                ou = f"aucune adresse publique trouvée : passer par le site de l'entreprise ; fiche : {destinataire}"
            self.escalader(
                dossier,
                "validation_envoi",
                f"« {dossier.offre.intitule} » chez {dossier.offre.entreprise} ({ou}) prêt. "
                f"Relire {fichiers} puis : rs valider {dossier.id} [--modifie] ou rs rejeter {dossier.id}. "
                f"Raisons : {', '.join(raisons)}",
            )
        else:
            dossier.envoi.prevu_le = self.prochain_creneau(self.ctx.horloge())
            dossier.envoi.automatique = True
        self.tracer(dossier, "brouillon", {"documents": doc, "canal": canal, "destinataire": destinataire}, {"raisons": raisons})

    # ----------------------------------------------------------------- envoi

    def envoyer(self, dossier: Dossier) -> bool:
        """Envoie un dossier validé dont le créneau est arrivé. Renvoie True si parti."""
        if dossier.envoi.canal != "email":
            return False  # dépôt manuel, enregistré par `rs marquer-envoye`
        if self.expediteur is None:
            raise RuntimeError("Aucun expéditeur SMTP configuré")
        maximum = self.ctx.config.orchestrateur.candidatures_max_par_semaine
        if self.ctx.store.envois_semaine() >= maximum:
            self.ctx.store.journaliser(self.nom, "budget_hebdo_atteint", dossier.id)
            return False

        if not self.ctx.store.reserver_envoi(dossier.id, "candidature", "email"):
            etat = self.ctx.store.etat_envoi(dossier.id)
            if etat == "envoye":
                dossier.statut = Statut.envoyee
                return False
            self._echec(dossier, f"Envoi déjà tenté (état « {etat} ») : vérifier le dossier Envoyés avant toute nouvelle tentative.")
            return False

        doc = dossier.documents
        expediteur_adresse = self.ctx.config.envoi.smtp.expediteur or self.ctx.profil().identite.email
        message = construire_message(expediteur_adresse, dossier.envoi.destinataire, doc.objet_mail, doc.mail, self.pieces_jointes(dossier))
        erreur = None
        for _ in range(2):  # un essai + un retry unique
            try:
                preuve = self.expediteur.envoyer(message)
                break
            except RuntimeError:  # configuration (mot de passe absent) : rien n'est parti
                self.ctx.store.liberer_envoi(dossier.id, "candidature")
                raise
            except (smtplib.SMTPException, OSError) as e:
                erreur = e
        else:
            self.ctx.store.cx.execute(
                "UPDATE envois SET etat = 'echec' WHERE dossier_id = ? AND nature = 'candidature'", (dossier.id,)
            )
            self._echec(dossier, f"Échec d'envoi après retry ({erreur}). Vérifier le dossier Envoyés, puis rs liberer-envoi {dossier.id} pour réessayer.")
            return False

        self.ctx.store.confirmer_envoi(dossier.id, "candidature", preuve)
        copie = self.ctx.dossier_fichiers(dossier.id) / "envoye.eml"
        copie.write_bytes(bytes(message))
        dossier.envoi.horodatage = self.ctx.horloge()
        dossier.envoi.preuve = f"{preuve} ; copie : {copie}"
        dossier.statut = Statut.envoyee
        self.tracer(dossier, "envoi", {"message_id": preuve, "destinataire": dossier.envoi.destinataire}, {"automatique": dossier.envoi.automatique})
        return True

    def _echec(self, dossier: Dossier, question: str) -> None:
        dossier.statut = Statut.erreur
        dossier.etape_attente = "envoi"
        dossier.motif = "echec_envoi"
        self.escalader(dossier, "echec_envoi", f"{dossier.offre.entreprise} : {question}")

    def marquer_envoye(self, dossier: Dossier, preuve: str) -> None:
        """Dépôt manuel (formulaire, plateforme) ou envoi fait à la main."""
        reservee = self.ctx.store.reserver_envoi(dossier.id, "candidature", dossier.envoi.canal or "manuel")
        if not reservee and self.ctx.store.etat_envoi(dossier.id) == "envoye":
            raise RuntimeError("Candidature déjà enregistrée comme envoyée")
        self.ctx.store.confirmer_envoi(dossier.id, "candidature", preuve)
        dossier.envoi.horodatage = self.ctx.horloge()
        dossier.envoi.preuve = preuve
        dossier.statut = Statut.envoyee
        self.tracer(dossier, "envoi_manuel", {"preuve": preuve})
