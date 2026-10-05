"""Alertes mail des plateformes d'offres (Indeed, JobTeaser, Welcome to the
Jungle, HelloWork, APEC), lues dans ta propre boîte.

On ne visite pas la plateforme : on lit les mails d'alerte qu'elle t'envoie
(créer d'abord, sur la plateforme, une alerte « stage » avec tes critères).
Les mails changent souvent de mise en page : le LLM repère les offres, puis
le code vérifie que l'intitulé et l'entreprise figurent bien dans le mail et
que le lien existe. Rien n'est gardé sans ces trois preuves.
"""

from __future__ import annotations

import html as html_lib
import re
import urllib.parse
from datetime import timedelta

from pydantic import BaseModel, Field

from ..agents.base import Contexte
from ..canaux.messagerie import Boite, MessageRecu
from ..hachage import normaliser
from ..modeles import GrilleCriteres, Offre
from . import texte_brut

# plateforme -> domaines d'expédition des alertes
PLATEFORMES = {
    "indeed": ("indeed.com", "indeed.fr", "indeedemail.com"),
    "jobteaser": ("jobteaser.com",),
    "wttj": ("welcometothejungle.com", "wttj.co"),
    "hellowork": ("hellowork.com", "hellowork.fr"),
    "apec": ("apec.fr",),
}
DOMAINES_ALERTES = tuple(d for domaines in PLATEFORMES.values() for d in domaines)

SYSTEME = """Tu lis un mail d'alerte envoyé par un site d'offres d'emploi.
Liste chaque offre présente dans le mail. Recopie l'intitulé, l'entreprise et
le lieu exactement comme ils sont écrits ; le numéro [N] du lien qui ouvre
l'offre ; et la phrase de description si le mail en donne une.
N'invente rien et ne complète rien : si l'entreprise n'est pas écrite, laisse
le champ vide. Ignore les publicités, les liens de désinscription et les
suggestions sans rapport avec une offre.
est_un_stage : vrai si l'offre est un stage (stage, stagiaire, intern,
internship) ou si le mail est une alerte de stages ; faux pour une alternance,
un CDI, un CDD."""


class OffreAlerte(BaseModel):
    intitule: str
    entreprise: str = ""
    lieu: str = ""
    lien: int = Field(description="numéro N du lien [N] qui mène à l'offre")
    extrait: str = ""
    est_un_stage: bool = True


class ExtractionAlerte(BaseModel):
    offres: list[OffreAlerte] = Field(default_factory=list)


def plateforme_de(expediteur: str, retenues: list[str]) -> str | None:
    domaine = expediteur.rsplit("@", 1)[-1].lower()
    for nom in retenues:
        if any(domaine == d or domaine.endswith(f".{d}") for d in PLATEFORMES.get(nom, ())):
            return nom
    return None


def est_alerte(expediteur: str) -> bool:
    return plateforme_de(expediteur, list(PLATEFORMES)) is not None


def texte_avec_liens(message: MessageRecu) -> tuple[str, list[str]]:
    """Texte du mail où chaque lien devient « texte [N] » ; renvoie aussi la liste des URL."""
    liens: list[str] = []

    def numero(url: str) -> int:
        url = html_lib.unescape(url.strip())
        if url not in liens:
            liens.append(url)
        return liens.index(url) + 1

    if message.html:
        source = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", " ", message.html)

        def ancre(m: re.Match) -> str:
            texte = texte_brut(m.group(2))
            return f" {texte} [{numero(m.group(1))}] " if texte else f" [{numero(m.group(1))}] "

        source = re.sub(r'(?is)<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', ancre, source)
        return texte_brut(source), liens
    texte = re.sub(r"https?://\S+", lambda m: f"[{numero(m.group(0))}]", message.corps)
    return " ".join(texte.split()), liens


def url_canonique(url: str) -> str:
    """Retire le pistage quand l'identifiant de l'offre est lisible dans l'adresse."""
    morceaux = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(morceaux.query)
    if "indeed." in morceaux.netloc and params.get("jk"):
        return f"https://fr.indeed.com/viewjob?jk={params['jk'][0]}"
    gardes = [(k, v) for k, valeurs in params.items() for v in valeurs if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(morceaux._replace(query=urllib.parse.urlencode(gardes)))


class SourceAlertesMail:
    nom = "alertes_mail"

    def __init__(self, ctx: Contexte, boite: Boite):
        self.ctx = ctx
        self.boite = boite

    def _extraire(self, message: MessageRecu, plateforme: str) -> list[Offre]:
        texte, liens = texte_avec_liens(message)
        if not liens:
            return []
        extraction = self.ctx.llm.structurer(
            "veille", SYSTEME, f"Objet : {message.objet}\n\n{texte[:15000]}", ExtractionAlerte
        )
        texte_normalise = f" {normaliser(f'{message.objet} {texte}')} "
        offres = []
        for o in extraction.offres:
            intitule, entreprise = normaliser(o.intitule), normaliser(o.entreprise)
            preuves = (
                o.est_un_stage
                and intitule
                and entreprise
                and f" {intitule} " in texte_normalise
                and f" {entreprise} " in texte_normalise
                and 1 <= o.lien <= len(liens)
            )
            if not preuves:
                continue
            offres.append(
                Offre(
                    intitule=o.intitule.strip(),
                    entreprise=o.entreprise.strip(),
                    lieu=o.lieu.strip(),
                    description=o.extrait.strip(),
                    url=url_canonique(liens[o.lien - 1]),
                    source=f"alerte_{plateforme}",
                    date_publication=message.date.date(),
                )
            )
        return offres

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        store = self.ctx.store
        params = self.ctx.config.sources
        traites: list[str] = store.parametre("alertes_traitees", []) or []
        depuis = self.ctx.horloge() - timedelta(days=params.alertes_jours)
        offres: list[Offre] = []
        for message in sorted(self.boite.messages_depuis(depuis), key=lambda m: m.date):
            plateforme = plateforme_de(message.expediteur, params.alertes_plateformes)
            if plateforme is None or message.message_id in traites:
                continue
            extraites = self._extraire(message, plateforme)
            if offres and len(offres) + len(extraites) > limite:
                break  # mail laissé pour le prochain cycle plutôt que tronqué
            offres += extraites
            traites.append(message.message_id)
        store.definir_parametre("alertes_traitees", traites[-2000:])
        return offres[:limite]
