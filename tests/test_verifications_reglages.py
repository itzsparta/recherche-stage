"""Vérifications avant un cycle et réglages écrits dans config.yaml."""

import socket

import pytest

from recherche_stage.reglages import ReglageRefuse, enregistrer, remplacer
from recherche_stage.verifications import classeur_ouvert, joignable, verifier


def test_classeur_ouvert_et_serveur_injoignable(ctx, tmp_path, monkeypatch):
    classeur = tmp_path / "Suivi.xlsx"
    classeur.write_bytes(b"x")
    assert not classeur_ouvert(classeur)
    (tmp_path / "~$Suivi.xlsx").write_bytes(b"")  # fichier posé par Excel
    assert classeur_ouvert(classeur)

    libre = socket.socket()
    libre.bind(("127.0.0.1", 0))
    port = libre.getsockname()[1]
    libre.close()  # port fermé : connexion refusée
    assert not joignable("127.0.0.1", port, delai=1)

    ctx.config.suivi.excel = str(classeur)
    ctx.config.suivi.imap.hote, ctx.config.suivi.imap.port = "127.0.0.1", port
    ctx.config.llm.fournisseur = "gemini"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    points = {p["cle"]: p for p in verifier(ctx)}
    assert points["excel"]["ok"] is None and "ouvert" in points["excel"]["detail"]
    assert points["imap"]["ok"] is False and "injoignable" in points["imap"]["detail"]
    assert points["llm"]["ok"] is False and "GEMINI_API_KEY absente" in points["llm"]["detail"]
    monkeypatch.setenv("GEMINI_API_KEY", "factice")
    assert {p["cle"]: p for p in verifier(ctx)}["llm"]["ok"] is True

    ctx.config.llm.fournisseur = "claude_code"
    ctx.config.llm.claude_code_commande = "commande-qui-n-existe-pas"
    assert "introuvable" in {p["cle"]: p for p in verifier(ctx)}["llm"]["detail"]


CONFIG = """# Configuration
donnees: donnees

orchestrateur:
  dossiers_max_par_cycle: 5 # étale la consommation

sources:
  annuaire: true
  annuaire_codes_naf: []    # ex. ["33.12Z"]
  annuaire_departements:
    - "62"
  annuaire_max: 20
# --- commentaire de la section suivante
presentiel:
  domicile:
"""


def test_reglages_gardent_le_fichier(tmp_path):
    chemin = tmp_path / "config.yaml"
    chemin.write_text(CONFIG, encoding="utf-8")
    changes = enregistrer(chemin, {
        "orchestrateur.dossiers_max_par_cycle": "3",
        "sources.annuaire_codes_naf": "35.11Z, 71.12B",
        "sources.annuaire_departements": "62 59 80",
        "sources.annuaire_effectif_min": "20",
        "presentiel.domicile": "Boulogne-sur-Mer",
        "suivi.excel": r"C:\Users\moi\Suivi.xlsx",
        "orchestrateur.seuil_score_minimum": "60",  # inchangé (valeur par défaut)
    })
    assert len(changes) == 6
    texte = chemin.read_text(encoding="utf-8")
    assert "  dossiers_max_par_cycle: 3 # étale la consommation\n" in texte
    assert '  annuaire_codes_naf: ["35.11Z", "71.12B"]    # ex. ["33.12Z"]\n' in texte
    assert '  annuaire_departements: ["62", "59", "80"]\n  annuaire_max: 20\n  annuaire_effectif_min: 20\n# --- commentaire' in texte
    assert '  domicile: "Boulogne-sur-Mer"\n' in texte and "Suivi.xlsx" in texte
    assert texte.startswith("# Configuration\n") and (tmp_path / "config.yaml.precedent").read_text(encoding="utf-8") == CONFIG

    from recherche_stage.config import charger

    cfg = charger(chemin)
    assert cfg.sources.annuaire_departements == ["62", "59", "80"] and cfg.suivi.excel == r"C:\Users\moi\Suivi.xlsx"
    assert cfg.presentiel.domicile == "Boulogne-sur-Mer" and cfg.orchestrateur.dossiers_max_par_cycle == 3

    avant = chemin.read_text(encoding="utf-8")
    with pytest.raises(ReglageRefuse, match="Score minimum"):
        enregistrer(chemin, {"orchestrateur.seuil_score_minimum": "beaucoup"})
    assert chemin.read_text(encoding="utf-8") == avant  # rien d'écrit
    assert enregistrer(chemin, {"inconnu.cle": "1", "presentiel.domicile": "Boulogne-sur-Mer"}) == []


def test_section_sur_une_ligne_refusee():
    with pytest.raises(ReglageRefuse, match="à la main"):
        remplacer("presentiel: {actif: true}\n", "presentiel", "domicile", "Calais")
    assert remplacer("", "presentiel", "domicile", "Calais") == '\npresentiel:\n  domicile: "Calais"\n'
