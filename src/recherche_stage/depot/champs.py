"""Reconnaissance des champs d'un formulaire de candidature (fonctions pures).

Chaque champ est décrit par un dictionnaire collecté dans la page : balise,
type, name, id, autocomplete, libellé, placeholder, obligatoire. On en déduit
la clé du profil à y mettre, ou None si le champ n'est pas reconnu.
"""

from __future__ import annotations

import re
import unicodedata

AUTOCOMPLETE = {
    "given-name": "prenom", "family-name": "nom", "name": "nom_complet", "email": "email",
    "tel": "telephone", "tel-national": "telephone", "address-level2": "ville",
}

# (clé, motif) testés dans l'ordre sur le texte normalisé du champ.
MOTIFS = [
    ("nom_complet", r"full ?name|nom complet|nom et prenom|prenom et nom|votre nom et"),
    ("prenom", r"prenom|first ?name|given"),
    ("nom", r"(?<!pre)\bnom\b|last ?name|surname|family"),
    ("email", r"e-?mail|courriel|\bmail\b"),
    ("telephone", r"telephone|phone|mobile|portable|\btel\b|gsm"),
    ("ville", r"\bville\b|\bcity\b|commune|localite"),
    ("ecole", r"ecole|school|universite|university|etablissement"),
    ("diplome", r"diplome|degree|\bcursus\b|formation suivie|niveau d.?etude"),
]
EXCLUS = re.compile(r"entreprise|company|societe|employeur|employer|parrain|referent|manager|recherche|search|newsletter")
FICHIER_CV = re.compile(r"\bcv\b|resume|curriculum")
FICHIER_LETTRE = re.compile(r"lettre|cover|motivation")
TEXTE_LETTRE = re.compile(r"lettre|motivation|cover|message|commentaire|presentez|presentation|parlez.nous")
CONFIRMATION = re.compile(
    r"merci (pour|de) votre candidature|candidature (a bien ete|a ete) (envoyee|transmise|recue|enregistree)"
    r"|votre candidature (a bien ete|a ete) |nous avons (bien )?recu votre candidature"
    r"|thank you for (applying|your application)|application (has been|was) (submitted|received)"
)
# Liens ou boutons qui mènent au formulaire (jamais un bouton d'envoi).
VERS_FORMULAIRE = re.compile(r"candidature spontanee|candidater|postuler|deposer (un|mon|votre) cv|apply( now)?$|spontaneous")
ENVOI = re.compile(r"envoyer|soumettre|submit|send|valider|confirmer|terminer")


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[_\[\]]", " ", texte.lower())).strip()


def texte_du_champ(champ: dict) -> str:
    return normaliser(" ".join(str(champ.get(k) or "") for k in ("libelle", "placeholder", "aria", "name", "id")))


def reconnaitre(champ: dict, questions: dict[str, str] | None = None) -> str | None:
    """Clé de valeur pour ce champ : prenom, nom, …, cv, lettre_fichier,
    lettre_texte ou « question:<intitulé> » ; None si inconnu."""
    texte = texte_du_champ(champ)
    type_ = (champ.get("type") or "").lower()
    if type_ == "file":
        if FICHIER_LETTRE.search(texte) and not FICHIER_CV.search(texte):
            return "lettre_fichier"
        return "cv"  # le premier fichier demandé est presque toujours le CV
    if type_ in ("password", "hidden", "checkbox", "radio", "submit", "button", "search", "date", "number"):
        return None
    if champ.get("balise") == "select":
        return None  # listes fermées (civilité, pays, niveau) : laissées à l'utilisateur
    if champ.get("balise") == "textarea":
        question = _question(texte, questions or {})
        if question:
            return f"question:{question}"
    if EXCLUS.search(texte):
        return None
    if champ.get("balise") == "textarea":
        return "lettre_texte" if TEXTE_LETTRE.search(texte) or not texte else None
    auto = (champ.get("autocomplete") or "").lower().split()
    for jeton in auto:
        if jeton in AUTOCOMPLETE:
            return AUTOCOMPLETE[jeton]
    if type_ == "email":
        return "email"
    if type_ == "tel":
        return "telephone"
    for cle, motif in MOTIFS:
        if re.search(motif, texte):
            return cle
    return None


def _question(texte: str, questions: dict[str, str]) -> str | None:
    """Question ouverte déjà rédigée et validée dont l'intitulé ressemble au libellé."""
    mots = set(re.findall(r"[a-z]{4,}", texte))
    meilleure, score = None, 0.0
    for q in questions:
        cible = set(re.findall(r"[a-z]{4,}", normaliser(q)))
        if cible:
            s = len(mots & cible) / len(cible)
            if s > score:
                meilleure, score = q, s
    return meilleure if score >= 0.6 else None


def blocage(champs: list[dict], cadres: list[str]) -> str | None:
    """« captcha » si un CAPTCHA est affiché, « connexion » si la page demande un
    mot de passe : la main revient à l'utilisateur."""
    if any(re.search(r"recaptcha|hcaptcha|turnstile|captcha", c) for c in cadres):
        return "captcha"
    if any((c.get("type") or "").lower() == "password" and c.get("visible") for c in champs):
        return "connexion"
    return None


def est_confirmation(texte_page: str) -> bool:
    return bool(CONFIRMATION.search(normaliser(texte_page)))
