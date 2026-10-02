"""Clés et empreintes.

- `id_dossier` : clé unique entreprise + intitulé, insensible à la casse, aux
  accents, à la ponctuation et aux mentions « H/F ». C'est elle qui garantit
  qu'on ne candidate jamais deux fois au même poste.
- `empreinte` : hash stable des entrées d'une décision, pour la rejouer.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel

_MENTIONS = re.compile(r"\b(h\s*/\s*f|f\s*/\s*h|h\s*-\s*f|f\s*-\s*h|m\s*/\s*f|f\s*/\s*m|h\s*f)(\s*/\s*x)?\b")
_FORMES_JURIDIQUES = re.compile(r"\b(sas|sasu|sa|sarl|eurl|sca|snc|gie)\b")


def normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    bas = sans_accents.lower()
    bas = re.sub(r"[()\[\]]", " ", bas)
    bas = _MENTIONS.sub(" ", bas)
    bas = re.sub(r"[^a-z0-9]+", " ", bas)
    return " ".join(bas.split())


def normaliser_entreprise(nom: str) -> str:
    return " ".join(_FORMES_JURIDIQUES.sub(" ", normaliser(nom)).split()) or normaliser(nom)


def normaliser_intitule(intitule: str) -> str:
    texte = normaliser(intitule)
    texte = re.sub(r"\b(stage|stagiaire|internship|intern|offre de)\b", " ", texte)
    return " ".join(texte.split()) or normaliser(intitule)


def id_dossier(entreprise: str, intitule: str) -> str:
    cle = f"{normaliser_entreprise(entreprise)}|{normaliser_intitule(intitule)}"
    return hashlib.sha256(cle.encode()).hexdigest()[:16]


def _canonique(valeur: Any) -> Any:
    if isinstance(valeur, BaseModel):
        return valeur.model_dump(mode="json")
    if isinstance(valeur, dict):
        return {str(k): _canonique(v) for k, v in valeur.items()}
    if isinstance(valeur, (list, tuple)):
        return [_canonique(v) for v in valeur]
    return valeur


def serialiser(entrees: Any) -> str:
    return json.dumps(_canonique(entrees), sort_keys=True, ensure_ascii=False, default=str)


def empreinte(entrees: Any) -> str:
    return hashlib.sha256(serialiser(entrees).encode()).hexdigest()[:20]
