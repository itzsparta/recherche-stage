"""Réglages modifiables depuis l'interface, écrits dans `config.yaml`.

Seule une liste fermée de champs est proposée : zone, secteurs, rythme,
dépôt en présentiel, navigateur du dépôt assisté, tableau Excel. Jamais de secret ni de nom de variable
d'environnement. Le fichier est modifié ligne à ligne pour garder ses
commentaires ; l'ancienne version est copiée dans `config.yaml.precedent`.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import yaml

from .config import Config

# (section, clé) -> (libellé, type, aide)
CHAMPS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("sources", "annuaire_departements"): ("Départements visés", "liste", "Numéros séparés par des virgules, ex. 59, 62, 80"),
    ("sources", "annuaire_codes_naf"): ("Secteurs (codes NAF)", "liste", "Codes NAF séparés par des virgules, ex. 35.11Z, 71.12B"),
    ("sources", "annuaire_effectif_min"): ("Effectif minimum des entreprises", "entier", "0 : aucune limite"),
    ("sources", "annuaire_max"): ("Nouvelles entreprises par cycle", "entier", ""),
    ("orchestrateur", "dossiers_max_par_cycle"): ("Enquêtes et rédactions par cycle", "entier", "Plus bas : consomme moins par cycle"),
    ("orchestrateur", "candidatures_max_par_semaine"): ("Candidatures envoyées par semaine, au plus", "entier", ""),
    ("orchestrateur", "seuil_score_minimum"): ("Score minimum pour préparer une candidature", "nombre", "Sur 100"),
    ("presentiel", "domicile"): ("Point de départ des dépôts sur place", "texte", "Adresse ou ville ; vide : la ville du CV"),
    ("presentiel", "distance_max_km"): ("Distance maximale pour un dépôt sur place (km)", "nombre", ""),
    ("presentiel", "effectif_max"): ("Effectif maximal pour un dépôt sur place", "entier", ""),
    ("depot", "navigateur"): ("Navigateur pour « Remplir pour moi »", "texte",
                              "brave, edge ou chrome, ou chemin complet du .exe ; vide : Edge"),
    ("suivi", "excel"): ("Tableau Excel de suivi", "texte", "Chemin complet du fichier .xlsx ; vide : aucun"),
}


class ReglageRefuse(ValueError):
    pass


def lire(config: Config) -> list[dict]:
    champs = []
    for (section, cle), (libelle, type_, aide) in CHAMPS.items():
        valeur = getattr(getattr(config, section), cle)
        champs.append({"id": f"{section}.{cle}", "libelle": libelle, "type": type_, "aide": aide, "valeur": valeur})
    return champs


def convertir(type_: str, brut) -> object:
    texte = str(brut if brut is not None else "").strip()
    if type_ == "liste":
        return [x.strip() for x in re.split(r"[,;\s]+", texte) if x.strip()]
    if type_ == "texte":
        return texte or None
    if not texte:
        raise ReglageRefuse("valeur vide")
    try:
        return int(texte) if type_ == "entier" else float(texte.replace(",", "."))
    except ValueError:
        raise ReglageRefuse(f"« {texte} » n'est pas un nombre") from None


def _yaml(valeur) -> str:
    """Valeur en YAML sur une ligne (JSON est du YAML valide)."""
    if valeur is None:
        return "null"
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    if isinstance(valeur, (int, float)):
        return repr(valeur)
    return json.dumps(valeur, ensure_ascii=False)


def _indentation(ligne: str) -> int:
    return len(ligne) - len(ligne.lstrip(" "))


def remplacer(texte: str, section: str, cle: str, valeur) -> str:
    """Remplace `section.cle` dans le YAML en gardant le reste du fichier et
    le commentaire en fin de ligne ; ajoute la clé (ou la section) si besoin."""
    lignes = texte.splitlines(keepends=True)
    nouvelle = _yaml(valeur)
    debut = next((i for i, ligne in enumerate(lignes) if re.match(rf"{re.escape(section)}:\s*(#.*)?$", ligne.rstrip("\n"))), None)
    if debut is None:
        if any(re.match(rf"{re.escape(section)}:\s*[^#\s]", ligne) for ligne in lignes):
            raise ReglageRefuse(f"section « {section} » écrite sur une ligne dans config.yaml : la modifier à la main")
        fin_fichier = "" if not lignes or lignes[-1].endswith("\n") else "\n"
        return texte + f"{fin_fichier}\n{section}:\n  {cle}: {nouvelle}\n"
    fin = len(lignes)
    for i in range(debut + 1, len(lignes)):
        brute = lignes[i].rstrip("\n")
        if brute.strip() and not brute.lstrip().startswith("#") and _indentation(brute) == 0:
            fin = i
            break
    for i in range(debut + 1, fin):
        m = re.match(rf"^( +){re.escape(cle)}:(?:\s*(.*?))?(\s*#.*)?$", lignes[i].rstrip("\n"))
        if not m or (m.group(2) or "").startswith("#"):
            continue
        retrait = len(m.group(1))
        commentaire = m.group(3) or ""
        if commentaire and not commentaire.startswith((" ", "\t")):
            commentaire = " " + commentaire
        # Valeur en bloc sur les lignes suivantes (liste « - x », ou plus indentée) : retirée.
        j = i + 1
        while j < fin:
            brute = lignes[j].rstrip("\n")
            if not brute.strip():
                break
            if _indentation(brute) > retrait or (_indentation(brute) == retrait and brute.lstrip().startswith("- ")):
                j += 1
                continue
            break
        lignes[i:j] = [f"{m.group(1)}{cle}: {nouvelle}{commentaire}\n"]
        return "".join(lignes)
    # Clé absente : ajoutée à la fin de la section, avant les lignes vides finales.
    insertion = fin
    while insertion > debut + 1 and (not lignes[insertion - 1].strip()
                                     or (lignes[insertion - 1].startswith("#"))):  # commentaire de la section suivante
        insertion -= 1
    if insertion > 0 and not lignes[insertion - 1].endswith("\n"):
        lignes[insertion - 1] += "\n"
    lignes.insert(insertion, f"  {cle}: {nouvelle}\n")
    return "".join(lignes)


def enregistrer(chemin: str | Path, donnees: dict) -> list[str]:
    """Écrit les réglages changés dans config.yaml. Renvoie les identifiants
    (« section.cle ») des réglages modifiés.
    Rien n'est écrit si une valeur est invalide pour la configuration."""
    chemin = Path(chemin)
    texte = chemin.read_text(encoding="utf-8") if chemin.exists() else ""
    contenu = yaml.safe_load(texte) or {}
    actuelle = Config.model_validate(contenu)
    changes, nouveau = [], texte
    for (section, cle), (libelle, type_, _) in CHAMPS.items():
        identifiant = f"{section}.{cle}"
        if identifiant not in donnees:
            continue
        try:
            valeur = convertir(type_, donnees[identifiant])
        except ReglageRefuse as e:
            raise ReglageRefuse(f"{libelle} : {e}") from None
        if valeur == getattr(getattr(actuelle, section), cle):
            continue
        contenu.setdefault(section, {})[cle] = valeur
        nouveau = remplacer(nouveau, section, cle, valeur)
        changes.append(identifiant)
    if not changes:
        return []
    try:
        Config.model_validate(contenu)
    except ValueError as e:
        raise ReglageRefuse(f"Réglage refusé : {e}") from None
    relu = yaml.safe_load(nouveau) or {}
    if any((relu.get(s) or {}).get(c) != (contenu.get(s) or {}).get(c) for s, c in CHAMPS):
        raise ReglageRefuse("config.yaml n'a pas pu être modifié sans risque : le modifier à la main")
    if chemin.exists():
        shutil.copyfile(chemin, chemin.with_name(chemin.name + ".precedent"))
    chemin.write_text(nouveau, encoding="utf-8")
    return changes
