"""Sites qui ne sont pas ceux d'une entreprise (offres, alternance, annuaires)."""

from __future__ import annotations

# Sites d'offres, d'alternance et annuaires : pas l'endroit où l'entreprise reçoit une candidature de stage.
SITES_TIERS = (
    "labonnealternance", "alternance.emploi.gouv", "indeed.", "linkedin.", "welcometothejungle.", "hellowork.", "jobteaser.",
    "francetravail.", "pole-emploi.", "apec.fr", "monster.", "glassdoor.", "meteojob.", "regionsjob.", "cadremploi.", "jobijoba.",
    "keljob.", "optioncarriere.", "studentjob.", "letudiant.fr", "1jeune1solution", "annuaire-entreprises.", "societe.com",
    "pappers.", "pagesjaunes.", "infogreffe.", "manageo.", "verif.com", "wikipedia.", "facebook.", "google.",
    "twitter.", "x.com", "youtube.", "instagram.", "tiktok.",
)


def site_tiers(url: str) -> bool:
    hote = url.split("://", 1)[-1].split("/", 1)[0].lower()
    return any(hote == t or hote.endswith("." + t) if t == "x.com" else t in hote for t in SITES_TIERS)
