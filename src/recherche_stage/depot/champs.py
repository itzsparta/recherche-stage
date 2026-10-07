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
VERS_FORMULAIRE = re.compile(r"candidature spontanee|candidater|candidatez|postul(er|ez|e)\b|deposer (un|mon|votre) cv|apply( now)?$"
                             r"|spontaneous")
SPONTANEE = re.compile(r"spontane|deposer (un|mon|votre) cv|sans offre|spontaneous|unsolicited")
# Sur une fenêtre « Postuler avec LinkedIn / en invité / créer un compte » : sans compte d'abord.
SANS_COMPTE = re.compile(r"sans (creer de |creer un |)compte|en tant qu.invite|\binvite\b|guest|candidature (rapide|express)")
# Options qui passeraient par un autre site : jamais.
VIA_TIERS = re.compile(r"linkedin|indeed|facebook|google|apple|welcome to the jungle|wttj|hellowork|jobteaser")
# Page de connexion sans compte : le lien qui mène à la création de compte.
CREER_COMPTE = re.compile(r"cree[rz]? (un |mon |votre |ton )?(compte|profil)|inscri(re|vez|ption)|nouveau candidat|nouvel utilisateur"
                          r"|premiere (visite|candidature)|pas (encore )?de compte|create (an |your )?account|sign ?up|register|new user")
# Puis, à défaut, ceux qui mènent vers l'espace recrutement du site.
VERS_CARRIERES = re.compile(r"nous rejoindre|rejoignez|rejoindre|carrieres?\b|careers?\b|recrutement|recrute|travailler (chez|avec)"
                            r"|join us|talents?\b|offres? d.emploi|nos offres|emplois?\b|\bjobs?\b|espace candidat")
ADRESSE_CARRIERES = re.compile(r"carriere|career|recrut|rejoindre|rejoignez|join|jobs?\b|emploi|candidat|talent")
ESPACE_CANDIDAT = re.compile(r"candidat|candidature|postul|recrutement|emploi|carriere|career|\bapply\b|job")
# Refus des cookies (texte normalisé ; motif compatible JavaScript, il sert aussi dans la page).
REFUS_COOKIES = re.compile(r"^(tout refuser|refuser( tout| tous les cookies| les cookies| et fermer)?|je refuse|tout rejeter"
                           r"|rejeter( tout| tous les cookies)?|non merci|reject( all)?|decline( all)?|deny( all)?|refuse all"
                           r"|(cookies )?necessaires? uniquement|uniquement les (cookies )?necessaires|only necessary)$"
                           r"|continuer sans accepter|continue without accepting")
# Boutons « refuser » des bandeaux les plus courants (OneTrust, Didomi, Axeptio, Cookiebot, tarteaucitron,
# Usercentrics, Complianz, CookieYes, Klaro).
SELECTEURS_REFUS = ("#onetrust-reject-all-handler", "#didomi-notice-disagree-button", ".didomi-continue-without-agreeing",
                    "#axeptio_btn_dismiss", "#CybotCookiebotDialogBodyButtonDecline", "#tarteaucitronAllDenied2",
                    "[data-testid=uc-deny-all-button]", ".cmplz-deny", ".cky-btn-reject", ".cm-btn-decline")
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


def cadre_captcha(url: str) -> bool:
    return bool(re.search(r"recaptcha|hcaptcha|turnstile|captcha|challenges\.cloudflare", url or ""))


def blocage(champs: list[dict], cadres: list[str]) -> str | None:
    """« captcha » si un CAPTCHA est affiché, « connexion » si la page demande un
    mot de passe : la main revient à l'utilisateur."""
    if any(cadre_captcha(c) for c in cadres):
        return "captcha"
    if any((c.get("type") or "").lower() == "password" and c.get("visible") for c in champs):
        return "connexion"
    return None


def est_confirmation(texte_page: str) -> bool:
    return bool(CONFIRMATION.search(normaliser(texte_page)))


def choisir_lien(liens: list[dict], deja: set[str]) -> dict | None:
    """Lien à suivre vers le formulaire : d'abord « Candidature spontanée » ou
    « Postuler », sinon « Carrières » / « Nous rejoindre ». Jamais un bouton
    d'envoi, un site d'offres ou un lien déjà suivi."""
    from ..sites import site_tiers

    meilleur, rang = None, 9
    for lien in liens:
        texte, adresse = normaliser(lien.get("texte", "")), (lien.get("href") or "").lower()
        cle = adresse or texte
        if not cle or cle in deja or ENVOI.search(texte) or len(texte) > 60 or adresse.startswith(("mailto:", "tel:")):
            continue
        if (adresse.startswith("http") and site_tiers(adresse)) or VIA_TIERS.search(texte) or lien.get("formulaire"):
            continue  # un bouton pris dans un formulaire à remplir pourrait l'envoyer
        if SANS_COMPTE.search(texte):
            r = -1
        elif SPONTANEE.search(texte):
            r = -0.5  # la candidature spontanée avant le « Postuler » d'une offre précise
        elif VERS_FORMULAIRE.search(texte):
            r = 0
        elif VERS_CARRIERES.search(texte):
            r = 1
        elif ADRESSE_CARRIERES.search(adresse.split("?")[0]):
            r = 2
        else:
            continue
        if not lien.get("visible", True):
            r += 0.5  # caché dans un menu : après un lien visible de même sorte
        if r < rang:
            meilleur, rang = lien, r
    return meilleur


def vers_offre(lien: dict) -> bool:
    """« Postuler » / « Je postule » qui n'est pas la candidature spontanée : il mène à une
    offre précise, à vérifier au regard des critères avant de la suivre."""
    texte = normaliser(lien.get("texte", ""))
    return bool((VERS_FORMULAIRE.search(texte) or SANS_COMPTE.search(texte)) and not SPONTANEE.search(texte))


def choisir_inscription(liens: list[dict]) -> dict | None:
    """Sur une page de connexion : le lien « Créer un compte » (jamais un bouton
    qui envoie un formulaire, jamais une inscription à la newsletter)."""
    for lien in liens:
        texte = normaliser(lien.get("texte", ""))
        if (CREER_COMPTE.search(texte) and not lien.get("envoi") and len(texte) <= 60 and "newsletter" not in texte
                and not VIA_TIERS.search(texte) and not ENVOI.search(texte)):
            return lien
    return None
