"""Libellés des codes NAF (rév. 2) : « 71.12B » devient « Ingénierie, études
techniques ». Les sous-classes les plus courantes pour un stage technique sont
nommées ; les autres prennent le libellé de leur division (deux premiers
chiffres). Un texte qui n'est pas un code NAF est rendu tel quel."""

from __future__ import annotations

import re

SOUS_CLASSES = {
    "25.11Z": "Fabrication de structures métalliques",
    "25.62B": "Mécanique industrielle",
    "27.11Z": "Fabrication de moteurs, génératrices et transformateurs électriques",
    "27.12Z": "Fabrication de matériel de distribution et de commande électrique",
    "27.90Z": "Fabrication d'autres matériels électriques",
    "28.11Z": "Fabrication de moteurs et turbines",
    "28.29B": "Fabrication d'autres machines d'usage général",
    "33.11Z": "Réparation d'ouvrages en métaux",
    "33.12Z": "Réparation de machines et équipements mécaniques",
    "33.13Z": "Réparation de matériels électroniques et optiques",
    "33.14Z": "Réparation d'équipements électriques",
    "33.20A": "Installation de structures métalliques, chaudronnées et de tuyauterie",
    "33.20B": "Installation de machines et équipements mécaniques",
    "33.20C": "Conception d'ensemble et assemblage sur site industriel d'équipements de contrôle des processus",
    "33.20D": "Installation d'équipements électriques, électroniques et optiques",
    "35.11Z": "Production d'électricité",
    "35.12Z": "Transport d'électricité",
    "35.13Z": "Distribution d'électricité",
    "35.14Z": "Commerce d'électricité",
    "35.21Z": "Production de combustibles gazeux",
    "35.22Z": "Distribution de combustibles gazeux par conduites",
    "35.30Z": "Production et distribution de vapeur et d'air conditionné",
    "36.00Z": "Captage, traitement et distribution d'eau",
    "37.00Z": "Collecte et traitement des eaux usées",
    "38.11Z": "Collecte des déchets non dangereux",
    "38.21Z": "Traitement et élimination des déchets non dangereux",
    "38.32Z": "Récupération de déchets triés",
    "42.22Z": "Construction de réseaux électriques et de télécommunications",
    "43.21A": "Travaux d'installation électrique dans tous locaux",
    "43.21B": "Travaux d'installation électrique sur la voie publique",
    "43.22B": "Travaux d'installation d'équipements thermiques et de climatisation",
    "43.29A": "Travaux d'isolation",
    "70.22Z": "Conseil pour les affaires et autres conseils de gestion",
    "71.11Z": "Activités d'architecture",
    "71.12A": "Activité des géomètres",
    "71.12B": "Ingénierie, études techniques",
    "71.20A": "Contrôle technique automobile",
    "71.20B": "Analyses, essais et inspections techniques",
    "72.19Z": "Recherche-développement en autres sciences physiques et naturelles",
    "74.90B": "Activités spécialisées, scientifiques et techniques diverses",
}

DIVISIONS = {
    "01": "Agriculture", "02": "Sylviculture", "03": "Pêche et aquaculture", "05": "Extraction de houille et de lignite",
    "06": "Extraction d'hydrocarbures", "07": "Extraction de minerais métalliques", "08": "Autres industries extractives",
    "09": "Services de soutien aux industries extractives", "10": "Industries alimentaires", "11": "Fabrication de boissons",
    "12": "Fabrication de produits à base de tabac", "13": "Fabrication de textiles", "14": "Industrie de l'habillement",
    "15": "Industrie du cuir et de la chaussure", "16": "Travail du bois", "17": "Industrie du papier et du carton",
    "18": "Imprimerie et reproduction d'enregistrements", "19": "Cokéfaction et raffinage", "20": "Industrie chimique",
    "21": "Industrie pharmaceutique", "22": "Fabrication de produits en caoutchouc et en plastique",
    "23": "Fabrication d'autres produits minéraux non métalliques", "24": "Métallurgie",
    "25": "Fabrication de produits métalliques", "26": "Fabrication de produits informatiques, électroniques et optiques",
    "27": "Fabrication d'équipements électriques", "28": "Fabrication de machines et équipements",
    "29": "Industrie automobile", "30": "Fabrication d'autres matériels de transport", "31": "Fabrication de meubles",
    "32": "Autres industries manufacturières", "33": "Réparation et installation de machines et d'équipements",
    "35": "Production et distribution d'électricité, de gaz et de vapeur", "36": "Captage, traitement et distribution d'eau",
    "37": "Collecte et traitement des eaux usées", "38": "Collecte, traitement et élimination des déchets",
    "39": "Dépollution et gestion des déchets", "41": "Construction de bâtiments", "42": "Génie civil",
    "43": "Travaux de construction spécialisés", "45": "Commerce et réparation d'automobiles", "46": "Commerce de gros",
    "47": "Commerce de détail", "49": "Transports terrestres", "50": "Transports par eau", "51": "Transports aériens",
    "52": "Entreposage et services auxiliaires des transports", "53": "Activités de poste et de courrier",
    "55": "Hébergement", "56": "Restauration", "58": "Édition", "59": "Production audiovisuelle",
    "60": "Programmation et diffusion", "61": "Télécommunications", "62": "Programmation, conseil et activités informatiques",
    "63": "Services d'information", "64": "Activités des services financiers", "65": "Assurance",
    "66": "Activités auxiliaires de services financiers et d'assurance", "68": "Activités immobilières",
    "69": "Activités juridiques et comptables", "70": "Activités des sièges sociaux ; conseil de gestion",
    "71": "Architecture et ingénierie ; contrôle et analyses techniques", "72": "Recherche-développement scientifique",
    "73": "Publicité et études de marché", "74": "Autres activités spécialisées, scientifiques et techniques",
    "75": "Activités vétérinaires", "77": "Activités de location et location-bail", "78": "Activités liées à l'emploi",
    "79": "Agences de voyage et voyagistes", "80": "Enquêtes et sécurité", "81": "Services relatifs aux bâtiments",
    "82": "Activités administratives et de soutien aux entreprises", "84": "Administration publique",
    "85": "Enseignement", "86": "Activités pour la santé humaine", "87": "Hébergement médico-social et social",
    "88": "Action sociale sans hébergement", "90": "Activités créatives, artistiques et de spectacle",
    "91": "Bibliothèques, archives, musées", "92": "Jeux de hasard", "93": "Activités sportives et de loisirs",
    "94": "Activités des organisations associatives", "95": "Réparation d'ordinateurs et de biens personnels",
    "96": "Autres services personnels",
}

CODE = re.compile(r"^\s*(\d{2})\.?(\d{2})([A-Z])\s*$", re.IGNORECASE)


def libelle(code: str | None) -> str:
    """Libellé lisible d'un code NAF ; tout autre texte est rendu tel quel."""
    m = CODE.match(code or "")
    if not m:
        return code or ""
    division, classe, lettre = m.group(1), m.group(2), m.group(3).upper()
    return SOUS_CLASSES.get(f"{division}.{classe}{lettre}") or DIVISIONS.get(division) or f"{division}.{classe}{lettre}"
