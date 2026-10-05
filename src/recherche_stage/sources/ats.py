"""Sites carrière des entreprises via les API publiques de leur ATS.

Greenhouse, Lever, SmartRecruiters, Workable, Ashby, Recruitee et Personio
exposent les offres publiées en JSON ou XML, précisément pour qu'on les
affiche ailleurs : c'est l'accès prévu, pas du scraping. On ne garde que les
offres de stage.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..config import EntrepriseATS
from ..modeles import GrilleCriteres, Offre
from ..reseau import Http
from . import est_stage, lire_date, texte_brut

# Chaque lecteur renvoie (offre, type de contrat annoncé par l'ATS).
Lu = tuple[Offre, str]


def _greenhouse(http: Http, cible: EntrepriseATS) -> list[Lu]:
    donnees = http.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{cible.identifiant}/jobs", params={"content": "true"}
    )
    return [
        (
            Offre(
                intitule=job.get("title", ""),
                entreprise=cible.nom,
                lieu=(job.get("location") or {}).get("name", ""),
                description=texte_brut(job.get("content", "")),
                url=job.get("absolute_url", ""),
                source="greenhouse",
                date_publication=lire_date(job.get("updated_at")),
            ),
            "",
        )
        for job in donnees.get("jobs", [])
    ]


def _lever(http: Http, cible: EntrepriseATS, hote: str = "api.lever.co") -> list[Lu]:
    donnees = http.get_json(f"https://{hote}/v0/postings/{cible.identifiant}", params={"mode": "json"})
    return [
        (
            Offre(
                intitule=job.get("text", ""),
                entreprise=cible.nom,
                lieu=(job.get("categories") or {}).get("location", ""),
                description=job.get("descriptionPlain") or texte_brut(job.get("description", "")),
                url=job.get("hostedUrl", ""),
                source="lever",
                date_publication=lire_date(job.get("createdAt")),
            ),
            (job.get("categories") or {}).get("commitment", ""),
        )
        for job in donnees or []
    ]


def _lever_eu(http: Http, cible: EntrepriseATS) -> list[Lu]:
    return _lever(http, cible, hote="api.eu.lever.co")


def _smartrecruiters(http: Http, cible: EntrepriseATS) -> list[Lu]:
    donnees = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{cible.identifiant}/postings")
    return [
        (
            Offre(
                intitule=job.get("name", ""),
                entreprise=cible.nom,
                lieu=(job.get("location") or {}).get("city", ""),
                url=f"https://jobs.smartrecruiters.com/{cible.identifiant}/{job.get('id', '')}",
                source="smartrecruiters",
                date_publication=lire_date(job.get("releasedDate")),
            ),
            (job.get("typeOfEmployment") or {}).get("label", ""),
        )
        for job in donnees.get("content", [])
    ]


def _workable(http: Http, cible: EntrepriseATS) -> list[Lu]:
    donnees = http.get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{cible.identifiant}", params={"details": "true"}
    )
    offres = []
    for job in donnees.get("jobs", []):
        lieu = ", ".join(v for v in (job.get("city"), job.get("country")) if v)
        offres.append(
            (
                Offre(
                    intitule=job.get("title", ""),
                    entreprise=cible.nom,
                    lieu=lieu,
                    description=texte_brut(job.get("description", "")),
                    url=job.get("url") or job.get("shortlink", ""),
                    source="workable",
                    teletravail=job.get("telecommuting"),
                    date_publication=lire_date(job.get("published_on") or job.get("created_at")),
                ),
                job.get("employment_type", ""),
            )
        )
    return offres


def _ashby(http: Http, cible: EntrepriseATS) -> list[Lu]:
    donnees = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{cible.identifiant}")
    return [
        (
            Offre(
                intitule=job.get("title", ""),
                entreprise=cible.nom,
                lieu=job.get("location", ""),
                description=job.get("descriptionPlain") or texte_brut(job.get("descriptionHtml", "")),
                url=job.get("jobUrl", ""),
                source="ashby",
                teletravail=job.get("isRemote"),
                date_publication=lire_date(job.get("publishedAt")),
            ),
            job.get("employmentType", ""),
        )
        for job in donnees.get("jobs", [])
        if job.get("isListed", True)
    ]


def _recruitee(http: Http, cible: EntrepriseATS) -> list[Lu]:
    donnees = http.get_json(f"https://{cible.identifiant}.recruitee.com/api/offers/")
    return [
        (
            Offre(
                intitule=job.get("title", ""),
                entreprise=cible.nom,
                lieu=job.get("location") or job.get("city", ""),
                description=texte_brut(f"{job.get('description', '')} {job.get('requirements', '')}"),
                url=job.get("careers_url", ""),
                source="recruitee",
                teletravail=job.get("remote"),
                date_publication=lire_date(job.get("published_at")),
            ),
            job.get("employment_type_code", ""),
        )
        for job in donnees.get("offers", [])
    ]


def _personio(http: Http, cible: EntrepriseATS) -> list[Lu]:
    hote = cible.identifiant if "." in cible.identifiant else f"{cible.identifiant}.jobs.personio.de"
    racine = ET.fromstring(http.get_texte(f"https://{hote}/xml", params={"language": "fr"}))
    offres = []
    for poste in racine.iter("position"):
        description = " ".join(texte_brut(v.text or "") for v in poste.iter("value"))
        offres.append(
            (
                Offre(
                    intitule=poste.findtext("name", ""),
                    entreprise=cible.nom,
                    lieu=poste.findtext("office", ""),
                    description=description,
                    url=f"https://{hote}/job/{poste.findtext('id', '')}",
                    source="personio",
                    date_publication=lire_date(poste.findtext("createdAt")),
                ),
                poste.findtext("employmentType", ""),
            )
        )
    return offres


LECTEURS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "lever_eu": _lever_eu,
    "smartrecruiters": _smartrecruiters,
    "workable": _workable,
    "ashby": _ashby,
    "recruitee": _recruitee,
    "personio": _personio,
}

# Adresse de la page carrière hébergée par l'ATS -> (ats, identifiant)
MOTIFS_URL = [
    (r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", "greenhouse"),
    (r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([\w-]+)", "greenhouse"),
    (r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", "greenhouse"),
    (r"jobs\.eu\.lever\.co/([\w.-]+)", "lever_eu"),
    (r"jobs\.lever\.co/([\w.-]+)", "lever"),
    (r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", "smartrecruiters"),
    (r"api\.smartrecruiters\.com/v1/companies/([\w-]+)", "smartrecruiters"),
    (r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?([\w-]+)", "workable"),
    (r"([\w-]+)\.workable\.com", "workable"),
    (r"jobs\.ashbyhq\.com/([\w.%-]+)", "ashby"),
    (r"api\.ashbyhq\.com/posting-api/job-board/([\w.%-]+)", "ashby"),
    (r"([\w-]+)\.recruitee\.com", "recruitee"),
    (r"([\w-]+\.jobs\.personio\.(?:de|com))", "personio"),
]
IGNORES = {"www", "api", "apply", "jobs", "careers", "boards", "app", "embed"}

# ATS fréquents sans API publique documentée : message explicite plutôt qu'un échec muet.
SANS_API = {
    "myworkdayjobs.com": "Workday",
    "taleo.net": "Taleo",
    "successfactors": "SAP SuccessFactors",
    "teamtailor.com": "Teamtailor",
    "welcomekit.co": "Welcome Kit",
    "icims.com": "iCIMS",
}


def detecter_ats(url: str) -> tuple[str, str] | None:
    for motif, ats in MOTIFS_URL:
        m = re.search(motif, url, re.IGNORECASE)
        if m and m.group(1).lower() not in IGNORES:
            identifiant = m.group(1)
            return ats, identifiant.lower() if ats == "personio" else identifiant
    return None


def ats_sans_api(texte: str) -> str | None:
    texte = texte.lower()
    return next((nom for motif, nom in SANS_API.items() if motif in texte), None)


def detecter_dans_page(http: Http, url: str) -> tuple[str, str] | None:
    """La page carrière de l'entreprise intègre souvent le widget de son ATS :
    on cherche ses adresses dans le HTML de cette seule page."""
    trouve = detecter_ats(url)
    if trouve:
        return trouve
    page = http.get_texte(url)
    for lien in re.findall(r"https?://[^\s\"'<>]+", page):
        trouve = detecter_ats(lien)
        if trouve:
            return trouve
    return None


class SourceATS:
    """Une instance par entreprise : le disjoncteur coupe un site sans couper les autres."""

    def __init__(self, http: Http, cible: EntrepriseATS):
        self.http = http
        self.cible = cible
        self.nom = f"ats:{cible.ats}:{cible.identifiant}"

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        lecteur = LECTEURS.get(self.cible.ats)
        if lecteur is None:
            raise ValueError(f"ATS inconnu : {self.cible.ats} (connus : {', '.join(LECTEURS)})")
        offres = [o for o, contrat in lecteur(self.http, self.cible) if est_stage(f"{o.intitule} {contrat}", o.description)]
        return offres[:limite]
