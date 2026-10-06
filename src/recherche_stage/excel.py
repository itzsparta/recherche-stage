"""Tenue d'un tableau Excel de suivi des candidatures déjà existant.

Le tableau de l'utilisateur garde sa mise en page : les colonnes sont
reconnues à leur intitulé (« Entreprise », « Poste », « Date d'envoi »,
« Statut »…), les colonnes inconnues ne sont jamais touchées, et une colonne
de remarques n'est remplie que si elle est vide. Une candidature déjà présente
(même entreprise et même poste, à la normalisation près) est mise à jour ; les
autres sont ajoutées à la suite, dans le style de la dernière ligne. Quand une
colonne propose une liste déroulante, la valeur écrite est prise dans la liste.

Avant la première écriture, une copie du fichier d'origine est gardée à côté.
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from copy import copy
from datetime import date, datetime
from pathlib import Path

from .hachage import id_dossier
from .modeles import Dossier, Statut
from .presentiel import texte_excel

# Champ → intitulés de colonne reconnus (après normalisation : minuscules,
# sans accents ni ponctuation). Le premier intitulé égal gagne, puis le plus
# long contenu dans l'en-tête.
COLONNES: dict[str, tuple[str, ...]] = {
    "entreprise": ("entreprise", "societe", "structure", "organisme", "company", "nom de l entreprise", "employeur"),
    "poste": ("poste", "intitule", "intitule du poste", "offre", "sujet", "mission", "stage", "titre"),
    "lieu": ("lieu", "ville", "localisation", "adresse", "departement", "localite"),
    "secteur": ("secteur", "domaine", "activite"),
    "type": ("type", "type de candidature", "nature", "spontanee ou offre"),
    "canal": ("canal", "moyen", "mode", "mode de candidature", "via", "envoye par", "plateforme", "candidature via"),
    "contact": ("contact", "email", "e mail", "mail", "adresse mail", "destinataire", "recruteur", "interlocuteur"),
    "lien": ("lien", "url", "annonce", "lien annonce", "lien de l offre", "site", "source"),
    "date_candidature": ("date", "date candidature", "date de candidature", "date d envoi", "date envoi", "envoyee le",
                         "date de postulation", "postule le", "candidature le"),
    "statut": ("statut", "etat", "avancement", "situation", "etape"),
    "relance": ("relance", "relances", "date relance", "date de relance", "derniere relance", "relance le"),
    "reponse": ("reponse", "retour", "resultat", "reponse obtenue"),
    "date_reponse": ("date reponse", "date de reponse", "date retour", "reponse le"),
    "entretien": ("entretien", "date entretien", "date d entretien"),
    "score": ("score", "pertinence", "note", "interet"),
    "presentiel": ("presentiel", "depot en presentiel", "depot sur place", "sur place", "en personne", "main propre",
                   "deposer sur place", "a deposer sur place", "en main propre"),
    "remarques": ("remarques", "remarque", "commentaires", "commentaire", "notes", "observations", "infos"),
}

# Trop vagues pour être cherchés à l'intérieur d'un intitulé (« Dates du stage »,
# « Date du mail »…) : reconnus seulement quand ils sont l'intitulé entier.
EXACTS = {"stage", "mission", "titre", "type", "nature", "mode", "via", "site", "source", "note", "date",
          "infos", "etape", "mail", "offre", "activite", "situation", "retour"}

STATUTS_EXPORTES = {Statut.validee, Statut.envoyee, Statut.relancee, Statut.entretien, Statut.refus, Statut.acceptee}

LIBELLES_STATUT = {
    "a_envoyer": "À envoyer", "a_deposer": "À déposer", Statut.envoyee: "Envoyée", Statut.relancee: "Relancée",
    Statut.entretien: "Entretien", Statut.refus: "Refusée", Statut.acceptee: "Acceptée",
}
# Mots qui désignent chaque statut dans une liste déroulante, par ordre de préférence.
MOTS_STATUT = {
    "a_envoyer": ("a envoyer", "a postuler", "prepar", "brouillon", "a faire"),
    "a_deposer": ("a deposer", "a envoyer", "a postuler", "prepar", "a faire"),
    Statut.envoyee: ("envoy", "postul", "attente", "en cours", "candidat"),
    Statut.relancee: ("relanc", "attente", "en cours", "envoy"),
    Statut.entretien: ("entretien", "convoc", "en cours"),
    Statut.refus: ("refus", "non retenu", "negati", "rejet"),
    Statut.acceptee: ("accept", "retenu", "positi", "obtenu", "valide"),
}
LIBELLES_REPONSE = {"refus": "Refus", "entretien": "Entretien", "demande_info": "Demande d'informations"}


# Décrivent la candidature : sur une ligne existante, complétés seulement s'ils sont vides
# (l'utilisateur a pu écrire le nom à sa façon). Les autres suivent l'avancement.
DESCRIPTIFS = {"entreprise", "poste", "lieu", "secteur", "type", "contact", "lien", "score", "remarques", "presentiel"}

# Colonnes ajoutées au tableau quand elles y manquent et qu'il y a quelque chose à y mettre.
AJOUTABLES = {"presentiel": "Dépôt en présentiel"}
VERT = {"fill": "C6EFCE", "police": "006100"}  # style « Bon » d'Excel


class ErreurExcel(Exception):
    pass


def _norm(texte) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texte).split())


def reconnaitre_colonnes(entetes: dict[int, str]) -> dict[str, int]:
    """{champ: numéro de colonne} d'après les intitulés d'une ligne d'en-tête."""
    normes = {col: _norm(v) for col, v in entetes.items() if _norm(v)}
    trouvees: dict[str, int] = {}
    prises: set[int] = set()
    # 1. intitulés identiques ; 2. intitulé reconnu contenu dans l'en-tête (le plus long d'abord)
    for exact in (True, False):
        candidats = []
        for champ, noms in COLONNES.items():
            if champ in trouvees:
                continue
            for col, entete in normes.items():
                for nom in noms:
                    if entete == nom if exact else nom not in EXACTS and re.search(rf"\b{re.escape(nom)}\b", entete):
                        candidats.append((-len(nom), col, champ))
        for _, col, champ in sorted(candidats):
            if champ not in trouvees and col not in prises:
                trouvees[champ] = col
                prises.add(col)
    return trouvees


def _ligne_entete(ws) -> tuple[int, dict[str, int]]:
    meilleure = (0, {})
    for ligne in range(1, min(ws.max_row, 15) + 1):
        entetes = {c.column: c.value for c in ws[ligne] if isinstance(c.value, str)}
        colonnes = reconnaitre_colonnes(entetes)
        if len(colonnes) > len(meilleure[1]):
            meilleure = (ligne, colonnes)
    if "entreprise" not in meilleure[1]:
        raise ErreurExcel("colonne « Entreprise » introuvable dans les 15 premières lignes du tableau")
    return meilleure


def _liste_deroulante(ws, cellule) -> list[str]:
    for dv in ws.data_validations.dataValidation:
        if dv.type == "list" and cellule.coordinate in dv.sqref and dv.formula1 and dv.formula1.startswith('"'):
            return [v.strip() for v in dv.formula1.strip('"').split(",") if v.strip()]
    return []


def _choisir(valeurs: list[str], mots: tuple[str, ...], defaut: str) -> str:
    for mot in mots:
        for v in valeurs:
            n = _norm(v)
            if mot in n and not (mot in ("retenu", "valide", "positi") and ("non" in n.split() or "refus" in n)):
                return v
    return defaut


def _etat(d: Dossier) -> str | Statut:
    if d.statut == Statut.validee:
        return "a_envoyer"
    if d.statut == Statut.en_attente_humain and d.etape_attente == "depot":
        return "a_deposer"
    return d.statut


def a_exporter(d: Dossier) -> bool:
    return d.statut in STATUTS_EXPORTES or (d.statut == Statut.en_attente_humain and d.etape_attente == "depot")


def valeurs(d: Dossier) -> dict[str, object]:
    o = d.offre
    canal = {"email": "Mail", "formulaire": "Site de l'entreprise", "plateforme": o.source}.get(d.envoi.canal or "", "")
    envoi = d.envoi.horodatage
    return {
        "entreprise": o.entreprise,
        "poste": o.intitule,
        "lieu": o.lieu,
        "secteur": d.fiche_entreprise.secteur or "",
        "type": "Spontanée" if o.type == "spontanee" else "Offre",
        "canal": canal,
        "contact": d.envoi.destinataire if d.envoi.canal == "email" else (o.contact_email or d.fiche_entreprise.contact or ""),
        "lien": o.url,
        "date_candidature": envoi.date() if envoi else None,
        "relance": d.suivi.relances[-1].date() if d.suivi.relances else None,
        "reponse": LIBELLES_REPONSE.get(d.suivi.reponse, ""),
        "date_reponse": d.suivi.date_reponse.date() if d.suivi.date_reponse else None,
        "entretien": d.suivi.entretien_le if d.suivi.entretien_le else None,
        "score": round(d.score.valeur) if d.score.valeur else None,
        "remarques": d.envoi.preuve if d.envoi.canal != "email" else "",
        "presentiel": texte_excel(d.presentiel),
    }


def _ajouter_colonne(ws, ligne_entete: int, intitule: str) -> int:
    """Ajoute une colonne après la dernière colonne titrée, dans le style de l'en-tête
    voisin, et l'intègre au tableau Excel mis en forme qui s'arrêtait juste avant."""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import TableColumn

    derniere = max((c.column for c in ws[ligne_entete] if c.value not in (None, "")), default=0)
    col = derniere + 1
    cellule = ws.cell(ligne_entete, col, intitule)
    if derniere and ws.cell(ligne_entete, derniere).has_style:
        cellule._style = copy(ws.cell(ligne_entete, derniere)._style)
    lettre = get_column_letter(col)
    precedente = get_column_letter(derniere) if derniere else None
    if precedente and ws.column_dimensions[precedente].width:
        ws.column_dimensions[lettre].width = max(ws.column_dimensions[precedente].width, 28)
    else:
        ws.column_dimensions[lettre].width = 40
    for table in ws.tables.values():
        debut, fin = table.ref.split(":")
        if re.match(r"[A-Z]+", fin).group(0) == precedente and int(re.search(r"\d+", debut).group(0)) == ligne_entete:
            fin_ligne = re.search(r"\d+", fin).group(0)
            table.ref = f"{debut}:{lettre}{fin_ligne}"
            table.tableColumns.append(TableColumn(id=len(table.tableColumns) + 1, name=intitule))
            if table.autoFilter is not None:
                table.autoFilter.ref = table.ref
    return col


def _surligner(cellule) -> None:
    from openpyxl.styles import Font, PatternFill

    cellule.fill = PatternFill("solid", start_color=VERT["fill"], end_color=VERT["fill"])
    cellule.font = Font(color=VERT["police"], bold=True)


def mettre_a_jour(chemin: str | Path, dossiers: list[Dossier], feuille: str | None = None) -> tuple[int, int]:
    """Met le tableau à jour ; renvoie (lignes ajoutées, lignes mises à jour)."""
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise ErreurExcel("module openpyxl absent : pip install openpyxl") from e

    chemin = Path(chemin)
    if not chemin.exists():
        raise ErreurExcel(f"fichier introuvable : {chemin}")
    sauvegarde = chemin.with_name(f"{chemin.stem}.avant_rs{chemin.suffix}")
    if not sauvegarde.exists():
        shutil.copy2(chemin, sauvegarde)

    classeur = load_workbook(chemin)
    ws = classeur[feuille] if feuille else classeur.worksheets[0]
    ligne_entete, colonnes = _ligne_entete(ws)
    a_ecrire = [d for d in dossiers if a_exporter(d)]
    for champ, intitule in AJOUTABLES.items():
        if champ not in colonnes and any(valeurs(d)[champ] for d in a_ecrire):
            colonnes[champ] = _ajouter_colonne(ws, ligne_entete, intitule)

    def cle(entreprise, poste) -> str:
        return id_dossier(str(entreprise or ""), str(poste or "")) if "poste" in colonnes else _norm(entreprise)

    existantes: dict[str, int] = {}
    derniere = ligne_entete
    for ligne in range(ligne_entete + 1, ws.max_row + 1):
        entreprise = ws.cell(ligne, colonnes["entreprise"]).value
        if entreprise in (None, ""):
            continue
        derniere = ligne
        poste = ws.cell(ligne, colonnes["poste"]).value if "poste" in colonnes else ""
        existantes.setdefault(cle(entreprise, poste), ligne)

    ajoutees = maj = 0
    for d in sorted(dossiers, key=lambda d: d.envoi.horodatage or datetime.max):
        if not a_exporter(d):
            continue
        champs = valeurs(d)
        ligne = existantes.get(cle(d.offre.entreprise, d.offre.intitule))
        nouvelle = ligne is None
        if nouvelle:
            derniere += 1
            ligne = derniere
            existantes[cle(d.offre.entreprise, d.offre.intitule)] = ligne
            if ligne - 1 > ligne_entete:  # style de la ligne précédente
                for col in range(1, ws.max_column + 1):
                    source, cible = ws.cell(ligne - 1, col), ws.cell(ligne, col)
                    if source.has_style:
                        cible._style = copy(source._style)
            ajoutees += 1
        else:
            maj += 1
        for champ, col in colonnes.items():
            cellule = ws.cell(ligne, col)
            if champ == "statut":
                etat = _etat(d)
                valeur = _choisir(_liste_deroulante(ws, cellule), MOTS_STATUT[etat], LIBELLES_STATUT[etat])
            elif (champ in DESCRIPTIFS and not nouvelle and cellule.value not in (None, "")
                  and not (champ == "presentiel" and str(cellule.value).startswith("Oui : "))):
                continue  # saisi à la main (nom, notes…) : intouchable ; « Oui : » vient de l'outil
            else:
                valeur = champs[champ]
            if valeur in (None, ""):
                continue  # une case déjà remplie à la main n'est pas vidée
            cellule.value = valeur
            if champ == "presentiel":
                _surligner(cellule)
            if isinstance(valeur, (date, datetime)) and cellule.number_format == "General":
                cellule.number_format = "DD/MM/YYYY HH:MM" if isinstance(valeur, datetime) else "DD/MM/YYYY"

    for table in ws.tables.values():  # tableau Excel mis en forme : on l'étend aux nouvelles lignes
        debut, fin = table.ref.split(":")
        fin_col = re.match(r"[A-Z]+", fin).group(0)
        if int(re.search(r"\d+", fin).group(0)) < derniere:
            table.ref = f"{debut}:{fin_col}{derniere}"

    temporaire = chemin.with_name(f"~rs_{chemin.name}")
    try:
        classeur.save(temporaire)
        os.replace(temporaire, chemin)
    except PermissionError as e:
        temporaire.unlink(missing_ok=True)
        raise ErreurExcel(f"{chemin.name} est ouvert dans Excel : le fermer, il sera mis à jour au prochain passage") from e
    return ajoutees, maj
