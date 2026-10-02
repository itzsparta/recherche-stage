"""Import de fichiers CSV ou JSON déposés par l'utilisateur.

C'est la porte d'entrée des plateformes sans API publique ou dont les CGU
interdisent l'automatisation (JobTeaser de l'école, Indeed, Welcome to the
Jungle) : l'utilisateur copie ou exporte les offres qui l'intéressent.

Colonnes reconnues : intitule, entreprise, lieu, url, description, debut, fin,
source, contact_email, gratification, duree_mois, teletravail, secteur.
Les fichiers traités sont déplacés dans `traites/`.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from ..modeles import GrilleCriteres, Offre, Periode
from . import lire_date


def _flottant(valeur) -> float | None:
    try:
        return float(str(valeur).replace(",", ".")) if valeur not in (None, "") else None
    except ValueError:
        return None


def _booleen(valeur) -> bool | None:
    if valeur in (None, ""):
        return None
    return str(valeur).strip().lower() in ("1", "oui", "true", "vrai", "yes")


def ligne_vers_offre(ligne: dict, source_defaut: str) -> Offre | None:
    intitule = (ligne.get("intitule") or "").strip()
    entreprise = (ligne.get("entreprise") or "").strip()
    if not intitule or not entreprise:
        return None
    return Offre(
        intitule=intitule,
        entreprise=entreprise,
        lieu=(ligne.get("lieu") or "").strip(),
        dates=Periode(debut=lire_date(ligne.get("debut")), fin=lire_date(ligne.get("fin"))),
        description=(ligne.get("description") or "").strip(),
        url=(ligne.get("url") or "").strip(),
        source=(ligne.get("source") or source_defaut).strip(),
        contact_email=(ligne.get("contact_email") or "").strip() or None,
        gratification=_flottant(ligne.get("gratification")),
        duree_mois=_flottant(ligne.get("duree_mois")),
        teletravail=_booleen(ligne.get("teletravail")),
        secteur=(ligne.get("secteur") or "").strip() or None,
        type="spontanee" if str(ligne.get("type", "")).strip() == "spontanee" else "offre",
        date_collecte=datetime.now(),
    )


class SourceImport:
    nom = "import"
    sans_frequence = True  # un fichier déposé est lu au cycle suivant, sans attendre

    def __init__(self, dossier: str | Path):
        self.dossier = Path(dossier)

    def collecter(self, grille: GrilleCriteres, limite: int) -> list[Offre]:
        if not self.dossier.exists():
            return []
        traites = self.dossier / "traites"
        offres: list[Offre] = []
        for fichier in sorted(self.dossier.iterdir()):
            if fichier.suffix.lower() not in (".csv", ".json") or not fichier.is_file():
                continue
            source = fichier.stem.split("_")[0] or "import"
            if fichier.suffix.lower() == ".csv":
                with fichier.open(encoding="utf-8-sig", newline="") as f:
                    premiere = f.readline()
                    f.seek(0)
                    lignes = list(csv.DictReader(f, delimiter=";" if ";" in premiere else ","))
            else:
                contenu = json.loads(fichier.read_text(encoding="utf-8"))
                lignes = contenu if isinstance(contenu, list) else contenu.get("offres", [])
            offres.extend(o for o in (ligne_vers_offre(ligne, source) for ligne in lignes) if o)
            traites.mkdir(exist_ok=True)
            fichier.replace(traites / fichier.name)
        # Pas de limite : l'utilisateur a choisi ces offres une à une.
        return offres
