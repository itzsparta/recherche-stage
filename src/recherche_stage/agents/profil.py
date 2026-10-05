"""Agent 1 — Profil : représentation structurée du candidat et grille de
critères (durs / souples pondérés)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from ..modeles import (
    Competence,
    CriteresDurs,
    CritereSouple,
    Experience,
    Formation,
    GrilleCriteres,
    Identite,
    Langue,
    ProfilStructure,
    Projet,
)
from .base import Agent

SYSTEME = """Tu extrais le profil d'un candidat à un stage à partir de son CV et de ses relevés de notes.

Règles absolues :
- Recopie fidèlement. N'invente, ne déduis et n'embellis rien. Une information absente reste absente.
- Un niveau (compétence, langue) n'est renseigné que s'il est écrit dans le document. Sinon : null pour une compétence, chaîne vide pour une langue.
- Identifiants : exp1, exp2… pour les expériences ; form1… pour les formations ; proj1… pour les projets ; lang_<langue> pour les langues ; comp_<nom> pour les compétences (minuscules, sans accents, tirets bas).
- Normalise le nom des compétences (« MS Excel », « excel avancé » → nom « Excel », niveau « avancé » seulement si écrit).
- Pour chaque compétence, `preuves` liste les identifiants des expériences ou projets où elle apparaît.
- Dans `manquants`, liste ce qui est illisible, tronqué ou absent parmi : nom, email, téléphone, formation, expériences, compétences."""


class ExtractionProfil(BaseModel):
    identite: Identite
    formations: list[Formation]
    experiences: list[Experience]
    competences: list[Competence]
    langues: list[Langue]
    projets: list[Projet]
    certifications: list[str]
    centres_interet: list[str]
    manquants: list[str] = Field(description="Informations illisibles, tronquées ou absentes")


def lire_document(chemin: str | Path) -> str:
    chemin = Path(chemin)
    if chemin.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("Lecture PDF : installez l'extra `pdf` (pip install -e .[pdf]) ou fournissez un .txt") from e
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(chemin)).pages)
    if chemin.suffix.lower() == ".docx":
        return lire_docx(chemin)
    return chemin.read_text(encoding="utf-8")


def lire_docx(chemin: Path) -> str:
    """Texte d'un .docx (bibliothèque standard) : un paragraphe par ligne,
    tableaux et zones de texte compris."""
    import zipfile
    import xml.etree.ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(chemin) as archive:
        racine = ET.fromstring(archive.read("word/document.xml"))
    # Une zone de texte est stockée deux fois (version moderne et version de
    # repli pour les anciens Word) : on ne garde que la première.
    repli = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
    for parent in list(racine.iter()):
        for enfant in [e for e in parent if e.tag == repli]:
            parent.remove(enfant)

    def morceaux(noeud):
        for enfant in noeud:
            if enfant.tag == f"{w}p":
                continue  # paragraphe imbriqué (zone de texte) : lu pour lui-même
            if enfant.tag == f"{w}t" and enfant.text:
                yield enfant.text
            elif enfant.tag == f"{w}tab":
                yield " | "
            elif enfant.tag in (f"{w}br", f"{w}cr"):
                yield "\n"
            else:
                yield from morceaux(enfant)

    lignes = [texte for p in racine.iter(f"{w}p") if (texte := "".join(morceaux(p)).strip())]
    return "\n".join(lignes)


def _date(valeur) -> date | None:
    if valeur in (None, ""):
        return None
    return valeur if isinstance(valeur, date) else date.fromisoformat(str(valeur))


class AgentProfil(Agent):
    nom = "profil"

    def manquants(self, profil: ProfilStructure, preferences: dict) -> list[str]:
        """Contrôle déterministe de complétude, indépendant de l'avis du LLM."""
        manque = []
        if not (profil.identite.nom or profil.identite.prenom):
            manque.append("nom")
        if not profil.identite.email:
            manque.append("email")
        if not profil.formations:
            manque.append("formation")
        if not profil.competences:
            manque.append("compétences")
        dispo = preferences.get("disponibilite") or {}
        if not dispo.get("du") or not dispo.get("au"):
            manque.append("dates de disponibilité (préférences : disponibilite.du / disponibilite.au)")
        if not preferences.get("lieux"):
            manque.append("zone géographique (préférences : lieux)")
        return manque

    def construire_grille(self, preferences: dict, profil: ProfilStructure) -> GrilleCriteres:
        dispo = preferences.get("disponibilite") or {}
        duree = preferences.get("duree_mois") or {}
        durs = CriteresDurs(
            disponible_du=_date(dispo.get("du")),
            disponible_au=_date(dispo.get("au")),
            duree_min_mois=duree.get("min"),
            duree_max_mois=duree.get("max"),
            lieux=[str(lieu) for lieu in preferences.get("lieux", [])],
            accepte_teletravail_complet=preferences.get("teletravail_complet_accepte", True),
            gratification_min=preferences.get("gratification_min"),
            entreprises_exclues=preferences.get("entreprises_exclues", []),
            mots_exclus=preferences.get("mots_exclus", []),
        )
        souples = [CritereSouple.model_validate(c) for c in preferences.get("criteres_souples", [])]
        if not souples:
            # Par défaut : les requêtes et les compétences du profil, à poids égal.
            souples = [
                CritereSouple(nom="domaine", type="mots_cles", valeurs=preferences.get("requetes", []), poids=1),
                CritereSouple(nom="competences", type="mots_cles", valeurs=[c.nom for c in profil.competences], poids=1),
            ]
        # Retours de l'Apprentissage : facteurs multiplicatifs par critère.
        ajustements: dict = self.ctx.store.parametre("ajustements_poids", {}) or {}
        for critere in souples:
            critere.poids *= float(ajustements.get(critere.nom, 1.0))
        total = sum(c.poids for c in souples) or 1.0
        for critere in souples:
            critere.poids = round(critere.poids / total, 4)
        return GrilleCriteres(
            durs=durs,
            souples=souples,
            requetes=preferences.get("requetes", []),
            entreprises_prioritaires=preferences.get("entreprises_prioritaires", []),
            type_stage=preferences.get("type_stage", ""),
            deadline=_date(preferences.get("deadline")),
        )

    def executer(
        self, cv: str | Path, preferences: dict, releves: list[str | Path] = (), cv_pdf: str | None = None
    ) -> tuple[ProfilStructure, GrilleCriteres] | None:
        textes = [f"=== CV ===\n{lire_document(cv)}"]
        textes += [f"=== Relevé de notes ({Path(r).name}) ===\n{lire_document(r)}" for r in releves]
        source = "\n\n".join(textes)
        if len(source.strip()) < 200:
            self.escalader(None, "complement_profil", "Le CV semble illisible ou vide : fournissez une version texte (.txt).")
            return None

        extraction = self.ctx.llm.structurer(self.nom, SYSTEME, source, ExtractionProfil)
        profil = ProfilStructure.model_validate(extraction.model_dump(exclude={"manquants"}))
        if cv_pdf is None and Path(cv).suffix.lower() == ".pdf":
            cv_pdf = str(Path(cv).resolve())
        profil.cv_pdf = cv_pdf

        # Seul le contrôle déterministe bloque ; l'avis du LLM complète la question.
        bloquants = self.manquants(profil, preferences)
        signales = [m for m in extraction.manquants if m not in bloquants]
        h = self.ctx.store.memoriser_entrees({"source": source, "preferences": preferences})
        self.ctx.store.journaliser(
            self.nom, "extraction", entrees_hash=h, details={"bloquants": bloquants, "signales": signales}
        )
        if bloquants:
            question = "Profil incomplet, merci de compléter : " + ", ".join(bloquants)
            if signales:
                question += " (également signalé : " + ", ".join(signales) + ")"
            self.escalader(None, "complement_profil", question)
            return None

        grille = self.construire_grille(preferences, profil)
        self.ctx.store.definir_parametre("profil", profil)
        self.ctx.store.definir_parametre("grille", grille)
        self.ctx.store.definir_parametre("preferences", preferences)
        self.ctx.store.journaliser(self.nom, "profil_enregistre", entrees_hash=h)
        return profil, grille

    def reconstruire_grille(self) -> GrilleCriteres:
        """Après un ajustement de poids par l'Apprentissage, sans nouvel appel LLM."""
        grille = self.construire_grille(self.ctx.store.parametre("preferences", {}), self.ctx.profil())
        self.ctx.store.definir_parametre("grille", grille)
        return grille
