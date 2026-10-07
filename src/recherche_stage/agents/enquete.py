"""Agent 4 — Enquête entreprise : contexte sourcé pour personnaliser la
candidature, et meilleur contact.

Toute information retenue porte l'URL d'où elle vient, et cette URL doit
figurer parmi les pages réellement consultées par la recherche web : un fait
sans source vérifiable est écarté.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..hachage import normaliser_entreprise
from ..llm import ErreurLLM
from ..modeles import Dossier, FaitSource, FicheEntreprise, Statut
from ..reseau import ErreurReseau
from .base import Agent

SYSTEME_RECHERCHE = """Tu prépares une candidature de stage. Renseigne-toi sur l'entreprise indiquée :
activité, projets ou réalisations récentes, actualités des 18 derniers mois, culture affichée,
une adresse e-mail publique de recrutement ou de contact professionnel (jamais une adresse personnelle),
et l'adresse de sa page carrières ou de son formulaire de candidature spontanée (celle du groupe à défaut).
Appuie chaque information sur une page consultée et cite-la. Si tu ne trouves rien de fiable, dis-le."""

SYSTEME_EXTRACTION = """Tu structures des notes de recherche sur une entreprise.
N'utilise que ce qui figure dans les notes. Chaque fait reprend une information précise et vérifiable,
avec pour source l'une des URL de la liste autorisée, à l'identique. Aucune extrapolation, aucun superlatif.
`contact_email` : seulement une adresse écrite telle quelle dans les notes, sinon chaîne vide.
`page_carriere` : l'URL de la page carrières ou du formulaire de candidature de l'entreprise, telle quelle, sinon chaîne vide.
`fiable` : faux si les notes ne permettent pas d'identifier l'entreprise avec certitude (homonymes, rien trouvé)."""

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
DOMAINES_PERSONNELS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "live.", "laposte.net", "orange.fr", "free.fr")


class FaitExtrait(BaseModel):
    texte: str
    source: str
    type: Literal["activite", "projet", "actualite", "culture"]


class FicheExtraite(BaseModel):
    activite: str
    activite_source: str
    faits: list[FaitExtrait]
    contact_email: str
    contact_source: str
    fiable: bool
    page_carriere: str = ""


SYSTEME_CARRIERE = """Trouve où déposer une candidature spontanée de stage dans l'entreprise indiquée :
sa page carrières, son espace candidat ou son formulaire de candidature (ceux du groupe à défaut).
Donne l'URL exacte de la page la plus directe, consultée réellement. Si tu ne trouves rien de fiable, dis-le."""

SYSTEME_CARRIERE_EXTRACTION = """Extrais des notes l'URL de la page carrières ou du formulaire de candidature
de l'entreprise, à l'identique, parmi les URL de la liste autorisée ou écrite telle quelle dans les notes.
Chaîne vide si les notes n'en donnent pas avec certitude."""


class PageCarriere(BaseModel):
    url: str


class AgentEnquete(Agent):
    nom = "enquete"

    def _annuaire(self, dossier: Dossier, fiche: FicheEntreprise) -> None:
        try:
            donnees = self.ctx.http.get_json(
                "https://recherche-entreprises.api.gouv.fr/search", params={"q": dossier.offre.entreprise, "per_page": 1}
            )
        except ErreurReseau:
            return
        resultats = (donnees or {}).get("results", [])
        if not resultats:
            return
        r = resultats[0]
        nom = r.get("nom_complet") or ""
        if normaliser_entreprise(nom).split()[:1] != normaliser_entreprise(dossier.offre.entreprise).split()[:1]:
            return  # homonyme probable : on ne mélange pas deux entreprises
        fiche.siren = r.get("siren")
        fiche.effectif = r.get("tranche_effectif_salarie")
        fiche.secteur = r.get("activite_principale")
        url = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{fiche.siren}"
        fiche.sources.append(url)

    def _recherche_web(self, dossier: Dossier, fiche: FicheEntreprise) -> bool:
        o = dossier.offre
        requete = f"Entreprise : {o.entreprise}\nLieu : {o.lieu}\nPoste visé : {o.intitule}\nSite de l'offre : {o.url}"
        notes, urls = self.ctx.llm.rechercher(self.nom, SYSTEME_RECHERCHE, requete)
        if not urls:
            return False
        extrait = self.ctx.llm.structurer(
            self.nom,
            SYSTEME_EXTRACTION,
            "URL autorisées :\n" + "\n".join(urls) + f"\n\nNOTES :\n{notes}",
            FicheExtraite,
        )
        if not extrait.fiable:
            return False
        autorisees = set(urls)
        if extrait.activite and extrait.activite_source in autorisees:
            fiche.activite = extrait.activite
            fiche.sources.append(extrait.activite_source)
        for fait in extrait.faits:
            if fait.source not in autorisees:
                continue  # fait non sourcé : écarté
            fiche.faits.append(FaitSource(texte=fait.texte, source=fait.source))
            fiche.sources.append(fait.source)
            if fait.type in ("actualite", "projet"):
                fiche.actualites.append(fait.texte)
        email = extrait.contact_email.strip()
        if (
            email
            and EMAIL.fullmatch(email)
            and email in notes
            and extrait.contact_source in autorisees
            and not email.split("@")[-1].startswith(DOMAINES_PERSONNELS)
        ):
            fiche.contact = email
            fiche.sources.append(extrait.contact_source)
        fiche.page_carriere = self._url_verifiee(extrait.page_carriere, notes, urls)
        return bool(fiche.faits or fiche.activite)

    @staticmethod
    def _url_verifiee(url: str, notes: str, urls: list[str]) -> str:
        """Une URL n'est gardée que si la recherche l'a réellement vue."""
        url = url.strip()
        return url if url.startswith(("http://", "https://")) and (url in urls or url in notes) else ""

    def trouver_page_carriere(self, dossier: Dossier) -> str:
        """Recherche ciblée de la page où déposer la candidature (dossiers enquêtés sans elle)."""
        o = dossier.offre
        notes, urls = self.ctx.llm.rechercher(self.nom, SYSTEME_CARRIERE, f"Entreprise : {o.entreprise}\nLieu : {o.lieu}\nFiche : {o.url}")
        if not urls:
            return ""
        extrait = self.ctx.llm.structurer(self.nom, SYSTEME_CARRIERE_EXTRACTION,
                                          "URL autorisées :\n" + "\n".join(urls) + f"\n\nNOTES :\n{notes}", PageCarriere)
        url = self._url_verifiee(extrait.url, notes, urls)
        if url:
            dossier.fiche_entreprise.page_carriere = url
            self.tracer(dossier, "page_carriere", {"entreprise": o.entreprise}, {"url": url})
        return url

    def executer(self, dossier: Dossier) -> None:
        o = dossier.offre
        entrees = {"offre": o}
        if o.url and o.type == "offre" and not self.ctx.http.accessible(o.url):
            dossier.statut = Statut.abandonnee
            dossier.motif = "expiree : offre retirée (URL introuvable)"
            self.tracer(dossier, "url_morte", entrees, {"url": o.url})
            return

        fiche = FicheEntreprise()
        self._annuaire(dossier, fiche)
        trouve = False
        if self.ctx.config.llm.recherche_web:
            try:
                trouve = self._recherche_web(dossier, fiche)
            except ErreurLLM as e:
                self.ctx.store.journaliser(self.nom, "recherche_echouee", dossier.id, details={"erreur": str(e)})

        # Meilleur contact : celui de l'offre, puis celui qui a déjà répondu, puis celui trouvé en ligne.
        memoire = self.ctx.store.memoire(normaliser_entreprise(o.entreprise))
        fiche.contact = o.contact_email or memoire.get("contact", "") or fiche.contact
        fiche.sources = list(dict.fromkeys(fiche.sources))
        fiche.minimale = not trouve
        dossier.fiche_entreprise = fiche
        dossier.statut = Statut.enquetee
        self.tracer(
            dossier,
            "fiche_minimale" if fiche.minimale else "fiche_complete",
            entrees,
            {"faits": len(fiche.faits), "contact": bool(fiche.contact)},
        )
