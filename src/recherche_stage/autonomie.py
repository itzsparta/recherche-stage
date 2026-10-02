"""Mode d'autonomie : quand l'envoi peut se passer de validation humaine.

Niveau initial : validation humaine avant chaque envoi. La porte se lève pour
les candidatures standard lorsque, sur les 30 dernières validations, au
moins 95 % ont été acceptées sans modification. Certaines candidatures
exigent toujours une validation, quel que soit le niveau atteint.
"""

from __future__ import annotations

from .config import Config
from .modeles import Dossier
from .store import Store

DECISIONS_ENVOI = ("valide_tel_quel", "modifie", "rejete")


def taux_validation(store: Store, fenetre: int) -> tuple[float, int]:
    """Taux de validations sans modification sur les `fenetre` dernières décisions d'envoi."""
    decisions = [d for d in store.decisions_humaines() if d["decision"] in DECISIONS_ENVOI][-fenetre:]
    if not decisions:
        return 0.0, 0
    return sum(d["decision"] == "valide_tel_quel" for d in decisions) / len(decisions), len(decisions)


def envoi_automatique_debloque(store: Store, config: Config) -> bool:
    taux, n = taux_validation(store, config.autonomie.fenetre)
    return n >= config.autonomie.fenetre and taux >= config.autonomie.taux_requis


def raisons_validation(dossier: Dossier, store: Store, config: Config) -> list[str]:
    """Liste vide : envoi automatique autorisé. Sinon, les raisons d'attendre l'humain."""
    raisons = []
    if dossier.score.confiance < config.orchestrateur.seuil_confiance:
        raisons.append("confiance_faible")
    if dossier.prioritaire:
        raisons.append("entreprise_prioritaire")
    if dossier.documents.format == "formulaire" or dossier.documents.reponses_formulaire:
        raisons.append("formulaire_avec_questions_ouvertes")
    if dossier.envoi.canal != "email":
        raisons.append(f"canal {dossier.envoi.canal} : dépôt manuel")
    if not envoi_automatique_debloque(store, config):
        taux, n = taux_validation(store, config.autonomie.fenetre)
        raisons.append(f"validation_humaine_avant_envoi ({taux:.0%} validés tels quels sur {n}/{config.autonomie.fenetre})")
    return raisons
