"""`rs revue` : la file réglée d'une touche par élément."""

import json
import random

import pytest
from conftest import FauxExpediteur, FauxSource, evaluation_critique, evaluation_offre, offre, sortie_redaction
from test_orchestrateur import fiche_extraite

from recherche_stage.modeles import Statut
from recherche_stage.orchestrateur import Orchestrateur
from recherche_stage.revue import Revue


def redaction(contenu, schema):
    """Documents au nom de l'entreprise du dossier (le Critique vérifie le nom)."""
    nom = next((n for n in ("Sans Adresse", "Autre Énergie") if n in contenu), "Exemple Énergie")
    return json.loads(json.dumps(sortie_redaction(), ensure_ascii=False).replace("Exemple Énergie", nom))


@pytest.fixture
def orchestrateur(ctx):
    ctx.llm.recherche = ("Exemple Énergie a mis en service une turbine en 2026.", ["https://exemple-energie.fr/actus"])
    ctx.llm.reponses.update({"scoring": evaluation_offre(), "enquete": fiche_extraite(), "redaction": redaction,
                             "critique": evaluation_critique()})
    sources = [FauxSource("jobteaser", [
        offre(),
        offre(entreprise="Sans Adresse", intitule="Stage ingénieur (candidature spontanée)", type="spontanee", contact_email="",
              url="https://annuaire-entreprises.data.gouv.fr/entreprise/123456789"),
        offre(entreprise="Autre Énergie", contact_email="rh@autre.fr"),
    ])]
    o = Orchestrateur(ctx, sources, FauxExpediteur(), rng=random.Random(0), dormir=lambda s: None)
    o.apprentissage.choisir_variante = lambda: "experience_terrain"
    o.cycle(envoyer=False, suivre=False)
    return o


def revue(ctx, orchestrateur, *reponses, **outils):
    entrees = list(reponses)
    sortie = []
    r = Revue(ctx, orchestrateur, demander=lambda invite: entrees.pop(0), afficher=sortie.append, **outils)
    return r.executer(), "\n".join(sortie)


def par_entreprise(ctx, nom):
    return next(d for d in ctx.store.lister() if d.offre.entreprise == nom)


def test_valider_rejeter_et_deposer_en_une_commande(ctx, orchestrateur):
    ouverts = []
    attendus = [e for e in ctx.store.escalades_ouvertes() if e["type"] == "validation_envoi"]
    assert len(attendus) == 3
    ordre = [ctx.store.get(e["dossier_id"]).offre.entreprise for e in attendus]
    touches = {"Exemple Énergie": ["v"], "Autre Énergie": ["r", "pas mon secteur"],
               "Sans Adresse": ["x", "v", "o", "d", "site carrières, 06/10"]}  # « x » : touche inconnue, redemandée
    bilan, texte = revue(ctx, orchestrateur, *[t for nom in ordre for t in touches[nom]], ouvrir=ouverts.append)

    assert bilan == {"validees": 2, "rejetees": 1, "deposees": 1}
    assert "Objet :" in texte and "Taper une de ces lettres" in texte
    assert par_entreprise(ctx, "Exemple Énergie").statut == Statut.validee  # part au prochain cycle, pas avant
    assert par_entreprise(ctx, "Autre Énergie").statut == Statut.abandonnee
    sans = par_entreprise(ctx, "Sans Adresse")
    assert sans.statut == Statut.envoyee and ouverts[1].startswith("https://duckduckgo.com/?q=Sans+Adresse")
    assert not ctx.store.escalades_ouvertes()


def test_quitter_et_corriger(ctx, orchestrateur):
    premier = ctx.store.get(ctx.store.escalades_ouvertes()[0]["dossier_id"])

    def corriger(fichiers):
        mail = fichiers[0]
        mail.write_text(mail.read_text(encoding="utf-8").replace("Bonjour", "Madame, Monsieur"), encoding="utf-8")

    bilan, _ = revue(ctx, orchestrateur, "m", "q", editer=corriger)
    assert bilan == {"validees": 1}
    d = ctx.store.get(premier.id)
    assert d.modifie_par_humain and ctx.store.decisions_humaines()[-1]["decision"] == "modifie"
    assert len(ctx.store.escalades_ouvertes()) == 2  # le reste attend la prochaine revue


def test_alerte_globale_fermee(ctx, orchestrateur):
    for e in ctx.store.escalades_ouvertes():
        ctx.store.fermer_escalade(e["id"])
    ctx.store.escalader(None, "deadline", "Deadline dépassée")
    assert revue(ctx, orchestrateur, "f")[0] == {"fermees": 1}
    assert revue(ctx, orchestrateur)[1].startswith("Rien en attente.")
