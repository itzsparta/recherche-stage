"""Persistance SQLite : dossiers, journal centralisé, idempotence des envois,
escalades, décisions humaines, état des sources, budget d'appels LLM,
paramètres ajustables, propositions de l'Apprentissage et mémoire entreprises.

Tout reste en local (RGPD) : aucune donnée ne quitte la machine hors des appels
explicites (API LLM, sources d'offres, SMTP/IMAP).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .hachage import empreinte, serialiser
from .modeles import Dossier, Statut

SCHEMA = """
CREATE TABLE IF NOT EXISTS dossiers (
    id TEXT PRIMARY KEY,
    statut TEXT NOT NULL,
    donnees TEXT NOT NULL,
    maj TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dossiers_statut ON dossiers(statut);

CREATE TABLE IF NOT EXISTS entrees (
    hash TEXT PRIMARY KEY,
    contenu TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    dossier_id TEXT,
    entrees_hash TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS journal_dossier ON journal(dossier_id);

-- Une ligne par (dossier, nature) : la clé primaire rend tout envoi idempotent.
CREATE TABLE IF NOT EXISTS envois (
    dossier_id TEXT NOT NULL,
    nature TEXT NOT NULL,
    etat TEXT NOT NULL,
    canal TEXT,
    horodatage TEXT NOT NULL,
    preuve TEXT,
    PRIMARY KEY (dossier_id, nature)
);

CREATE TABLE IF NOT EXISTS escalades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id TEXT,
    type TEXT NOT NULL,
    question TEXT NOT NULL,
    cree TEXT NOT NULL,
    resolue TEXT,
    reponse TEXT
);

CREATE TABLE IF NOT EXISTS decisions_humaines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    horodatage TEXT NOT NULL,
    diff TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    nom TEXT PRIMARY KEY,
    echecs_consecutifs INTEGER NOT NULL DEFAULT 0,
    coupee INTEGER NOT NULL DEFAULT 0,
    coupee_le TEXT,
    derniere_collecte TEXT,
    collectes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS appels_llm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    agent TEXT NOT NULL,
    modele TEXT,
    tokens_entree INTEGER,
    tokens_sortie INTEGER
);

CREATE TABLE IF NOT EXISTS parametres (
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL,
    maj TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS propositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cree TEXT NOT NULL,
    mecanisme TEXT NOT NULL,
    cible TEXT NOT NULL,
    contenu TEXT NOT NULL,
    etat TEXT NOT NULL DEFAULT 'proposee'
);

CREATE TABLE IF NOT EXISTS regles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    motif TEXT NOT NULL UNIQUE,
    raison TEXT NOT NULL,
    cree TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memoire_entreprises (
    entreprise TEXT PRIMARY KEY,
    donnees TEXT NOT NULL
);
"""


def debut_semaine(maintenant: datetime) -> datetime:
    lundi = maintenant - timedelta(days=maintenant.weekday())
    return lundi.replace(hour=0, minute=0, second=0, microsecond=0)


class Store:
    def __init__(self, chemin: str | Path = ":memory:", horloge: Callable[[], datetime] = datetime.now):
        self.chemin = str(chemin)
        self.horloge = horloge
        # Un seul fil à la fois par instance (le serveur de `rs interface` traite
        # les requêtes l'une après l'autre), mais pas forcément celui qui l'a créée.
        self.cx = sqlite3.connect(self.chemin, isolation_level=None, check_same_thread=False)
        self.cx.row_factory = sqlite3.Row
        if self.chemin != ":memory:":
            self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.executescript(SCHEMA)

    def _maintenant(self) -> str:
        return self.horloge().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ dossiers

    def get(self, dossier_id: str) -> Dossier | None:
        ligne = self.cx.execute("SELECT donnees FROM dossiers WHERE id = ?", (dossier_id,)).fetchone()
        return Dossier.model_validate_json(ligne["donnees"]) if ligne else None

    def existe(self, dossier_id: str) -> bool:
        return self.cx.execute("SELECT 1 FROM dossiers WHERE id = ?", (dossier_id,)).fetchone() is not None

    def save(self, dossier: Dossier) -> None:
        self.cx.execute(
            "INSERT INTO dossiers(id, statut, donnees, maj) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET statut = excluded.statut, donnees = excluded.donnees, maj = excluded.maj",
            (dossier.id, dossier.statut.value, dossier.model_dump_json(), self._maintenant()),
        )

    def lister(self, statuts: Iterable[Statut] | None = None) -> list[Dossier]:
        if statuts is None:
            lignes = self.cx.execute("SELECT donnees FROM dossiers ORDER BY maj").fetchall()
        else:
            valeurs = [s.value for s in statuts]
            marques = ",".join("?" * len(valeurs))
            lignes = self.cx.execute(
                f"SELECT donnees FROM dossiers WHERE statut IN ({marques}) ORDER BY maj", valeurs
            ).fetchall()
        return [Dossier.model_validate_json(ligne["donnees"]) for ligne in lignes]

    # ------------------------------------------------------- entrées et journal

    def memoriser_entrees(self, entrees: Any) -> str:
        """Stocke les entrées d'une décision, adressées par leur hash, pour la rejouer."""
        h = empreinte(entrees)
        self.cx.execute("INSERT OR IGNORE INTO entrees(hash, contenu) VALUES (?, ?)", (h, serialiser(entrees)))
        return h

    def entrees(self, h: str) -> Any:
        ligne = self.cx.execute("SELECT contenu FROM entrees WHERE hash = ?", (h,)).fetchone()
        return json.loads(ligne["contenu"]) if ligne else None

    def journaliser(
        self,
        agent: str,
        action: str,
        dossier_id: str | None = None,
        entrees_hash: str = "",
        details: dict | None = None,
    ) -> None:
        self.cx.execute(
            "INSERT INTO journal(horodatage, agent, action, dossier_id, entrees_hash, details) VALUES (?, ?, ?, ?, ?, ?)",
            (self._maintenant(), agent, action, dossier_id, entrees_hash, json.dumps(details or {}, ensure_ascii=False, default=str)),
        )

    def journal(self, dossier_id: str | None = None, limite: int = 500) -> list[dict]:
        if dossier_id:
            lignes = self.cx.execute(
                "SELECT * FROM journal WHERE dossier_id = ? ORDER BY id DESC LIMIT ?", (dossier_id, limite)
            ).fetchall()
        else:
            lignes = self.cx.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
        return [dict(ligne) | {"details": json.loads(ligne["details"] or "{}")} for ligne in reversed(lignes)]

    def activite_agents(self, depuis: datetime) -> dict[str, dict]:
        """Par agent : actions et erreurs depuis `depuis`, et dernier passage (toutes dates)."""
        activite: dict[str, dict] = {}
        for ligne in self.cx.execute("SELECT agent, MAX(horodatage) AS dernier FROM journal GROUP BY agent"):
            activite[ligne["agent"]] = {"dernier": ligne["dernier"], "actions": 0, "erreurs": 0}
        for ligne in self.cx.execute(
            "SELECT agent, COUNT(*) AS actions, SUM(action = 'erreur') AS erreurs FROM journal "
            "WHERE horodatage >= ? GROUP BY agent", (depuis.isoformat(timespec="seconds"),),
        ):
            activite[ligne["agent"]].update(actions=ligne["actions"], erreurs=ligne["erreurs"] or 0)
        return activite

    def dernieres_actions(self, action: str, limite: int = 200) -> list[dict]:
        """Dernières entrées du journal pour une action donnée, de la plus récente à la plus ancienne."""
        lignes = self.cx.execute("SELECT * FROM journal WHERE action = ? ORDER BY id DESC LIMIT ?", (action, limite))
        return [dict(ligne) | {"details": json.loads(ligne["details"] or "{}")} for ligne in lignes]

    def proposition(self, proposition_id: int) -> dict | None:
        ligne = self.cx.execute("SELECT * FROM propositions WHERE id = ?", (proposition_id,)).fetchone()
        return dict(ligne) | {"contenu": json.loads(ligne["contenu"])} if ligne else None

    # ------------------------------------------------------------------- envois

    def reserver_envoi(self, dossier_id: str, nature: str, canal: str) -> bool:
        """Réserve un envoi. Renvoie False si un envoi de même nature existe déjà
        pour ce dossier, quel que soit son état : c'est la garantie anti-doublon."""
        try:
            self.cx.execute(
                "INSERT INTO envois(dossier_id, nature, etat, canal, horodatage) VALUES (?, ?, 'en_cours', ?, ?)",
                (dossier_id, nature, canal, self._maintenant()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def confirmer_envoi(self, dossier_id: str, nature: str, preuve: str) -> None:
        self.cx.execute(
            "UPDATE envois SET etat = 'envoye', preuve = ?, horodatage = ? WHERE dossier_id = ? AND nature = ?",
            (preuve, self._maintenant(), dossier_id, nature),
        )

    def liberer_envoi(self, dossier_id: str, nature: str) -> None:
        """À n'appeler que si l'on sait que rien n'est parti (échec avant transmission)."""
        self.cx.execute(
            "DELETE FROM envois WHERE dossier_id = ? AND nature = ? AND etat = 'en_cours'", (dossier_id, nature)
        )

    def etat_envoi(self, dossier_id: str, nature: str = "candidature") -> str | None:
        ligne = self.cx.execute(
            "SELECT etat FROM envois WHERE dossier_id = ? AND nature = ?", (dossier_id, nature)
        ).fetchone()
        return ligne["etat"] if ligne else None

    def envois_semaine(self, nature: str = "candidature") -> int:
        depuis = debut_semaine(self.horloge()).isoformat(timespec="seconds")
        ligne = self.cx.execute(
            "SELECT COUNT(*) AS n FROM envois WHERE nature = ? AND horodatage >= ?", (nature, depuis)
        ).fetchone()
        return ligne["n"]

    # ---------------------------------------------------------------- escalades

    def escalader(self, dossier_id: str | None, type_: str, question: str) -> int:
        existante = self.cx.execute(
            "SELECT id FROM escalades WHERE dossier_id IS ? AND type = ? AND resolue IS NULL", (dossier_id, type_)
        ).fetchone()
        if existante:
            return existante["id"]
        curseur = self.cx.execute(
            "INSERT INTO escalades(dossier_id, type, question, cree) VALUES (?, ?, ?, ?)",
            (dossier_id, type_, question, self._maintenant()),
        )
        return curseur.lastrowid

    def escalades_ouvertes(self) -> list[dict]:
        return [dict(r) for r in self.cx.execute("SELECT * FROM escalades WHERE resolue IS NULL ORDER BY id")]

    def resoudre_escalades(self, dossier_id: str, reponse: str = "") -> None:
        self.cx.execute(
            "UPDATE escalades SET resolue = ?, reponse = ? WHERE dossier_id = ? AND resolue IS NULL",
            (self._maintenant(), reponse, dossier_id),
        )

    def fermer_escalade(self, id_: int, reponse: str = "") -> bool:
        curseur = self.cx.execute(
            "UPDATE escalades SET resolue = ?, reponse = ? WHERE id = ? AND resolue IS NULL",
            (self._maintenant(), reponse, id_),
        )
        return curseur.rowcount > 0

    def fermer_escalades_globales(self, types: tuple[str, ...], reponse: str = "") -> None:
        """Escalades sans dossier (quota, budget…) : à refermer quand on réessaie."""
        self.cx.execute(
            f"UPDATE escalades SET resolue = ?, reponse = ? WHERE dossier_id IS NULL AND resolue IS NULL "
            f"AND type IN ({', '.join('?' * len(types))})",
            (self._maintenant(), reponse, *types),
        )

    # ------------------------------------------------------ décisions humaines

    def enregistrer_decision(self, dossier_id: str, decision: str, diff: str = "") -> None:
        self.cx.execute(
            "INSERT INTO decisions_humaines(dossier_id, decision, horodatage, diff) VALUES (?, ?, ?, ?)",
            (dossier_id, decision, self._maintenant(), diff),
        )

    def decisions_humaines(self, limite: int | None = None) -> list[dict]:
        requete = "SELECT * FROM decisions_humaines ORDER BY id DESC"
        lignes = self.cx.execute(requete + (" LIMIT ?" if limite else ""), (limite,) if limite else ()).fetchall()
        return [dict(ligne) for ligne in reversed(lignes)]

    # ------------------------------------------------------------------ sources

    def etat_source(self, nom: str) -> dict:
        self.cx.execute("INSERT OR IGNORE INTO sources(nom) VALUES (?)", (nom,))
        return dict(self.cx.execute("SELECT * FROM sources WHERE nom = ?", (nom,)).fetchone())

    def maj_source(self, nom: str, **champs: Any) -> None:
        self.etat_source(nom)
        affectations = ", ".join(f"{cle} = ?" for cle in champs)
        self.cx.execute(f"UPDATE sources SET {affectations} WHERE nom = ?", (*champs.values(), nom))

    # -------------------------------------------------------------- budget LLM

    def compter_appel_llm(self, agent: str, modele: str, tokens_entree: int, tokens_sortie: int) -> None:
        self.cx.execute(
            "INSERT INTO appels_llm(horodatage, agent, modele, tokens_entree, tokens_sortie) VALUES (?, ?, ?, ?, ?)",
            (self._maintenant(), agent, modele, tokens_entree, tokens_sortie),
        )

    def appels_llm_par_agent(self, depuis: datetime) -> dict[str, dict]:
        lignes = self.cx.execute(
            "SELECT agent, COUNT(*) AS appels, COALESCE(SUM(tokens_entree + tokens_sortie), 0) AS tokens "
            "FROM appels_llm WHERE horodatage >= ? GROUP BY agent", (depuis.isoformat(timespec="seconds"),),
        )
        return {ligne["agent"]: {"appels": ligne["appels"], "tokens": ligne["tokens"]} for ligne in lignes}

    def appels_llm_semaine(self) -> int:
        depuis = debut_semaine(self.horloge()).isoformat(timespec="seconds")
        return self.cx.execute("SELECT COUNT(*) AS n FROM appels_llm WHERE horodatage >= ?", (depuis,)).fetchone()["n"]

    # --------------------------------------------------------------- paramètres

    def parametre(self, cle: str, defaut: Any = None) -> Any:
        ligne = self.cx.execute("SELECT valeur FROM parametres WHERE cle = ?", (cle,)).fetchone()
        return json.loads(ligne["valeur"]) if ligne else defaut

    def definir_parametre(self, cle: str, valeur: Any) -> None:
        self.cx.execute(
            "INSERT INTO parametres(cle, valeur, maj) VALUES (?, ?, ?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, maj = excluded.maj",
            (cle, serialiser(valeur), self._maintenant()),
        )

    # ------------------------------------------------------------- propositions

    def proposer(self, mecanisme: str, cible: str, contenu: dict) -> int:
        curseur = self.cx.execute(
            "INSERT INTO propositions(cree, mecanisme, cible, contenu) VALUES (?, ?, ?, ?)",
            (self._maintenant(), mecanisme, cible, serialiser(contenu)),
        )
        return curseur.lastrowid

    def propositions(self, etat: str = "proposee") -> list[dict]:
        lignes = self.cx.execute("SELECT * FROM propositions WHERE etat = ? ORDER BY id", (etat,)).fetchall()
        return [dict(ligne) | {"contenu": json.loads(ligne["contenu"])} for ligne in lignes]

    def marquer_proposition(self, proposition_id: int, etat: str) -> None:
        self.cx.execute("UPDATE propositions SET etat = ? WHERE id = ?", (etat, proposition_id))

    # ------------------------------------------------------------------- règles

    def ajouter_regle(self, motif: str, raison: str) -> None:
        self.cx.execute(
            "INSERT OR IGNORE INTO regles(motif, raison, cree) VALUES (?, ?, ?)", (motif, raison, self._maintenant())
        )

    def regles(self) -> list[dict]:
        return [dict(r) for r in self.cx.execute("SELECT motif, raison FROM regles ORDER BY id")]

    # ------------------------------------------------------- mémoire entreprises

    def memoire(self, entreprise: str) -> dict:
        ligne = self.cx.execute("SELECT donnees FROM memoire_entreprises WHERE entreprise = ?", (entreprise,)).fetchone()
        return json.loads(ligne["donnees"]) if ligne else {}

    def maj_memoire(self, entreprise: str, donnees: dict) -> None:
        self.cx.execute(
            "INSERT INTO memoire_entreprises(entreprise, donnees) VALUES (?, ?) "
            "ON CONFLICT(entreprise) DO UPDATE SET donnees = excluded.donnees",
            (entreprise, serialiser(donnees)),
        )

    # --------------------------------------------------------------------- RGPD

    def purger(self, avant: datetime) -> int:
        """Supprime les dossiers terminés et non modifiés depuis `avant`, et
        les traces qui s'y rattachent."""
        seuil = avant.isoformat(timespec="seconds")
        ids = [
            r["id"]
            for r in self.cx.execute(
                "SELECT id FROM dossiers WHERE maj < ? AND statut IN ('refus', 'acceptee', 'abandonnee', 'erreur')",
                (seuil,),
            )
        ]
        for dossier_id in ids:
            for table in ("journal", "escalades", "decisions_humaines"):
                self.cx.execute(f"DELETE FROM {table} WHERE dossier_id = ?", (dossier_id,))
            self.cx.execute("DELETE FROM dossiers WHERE id = ?", (dossier_id,))
        # Les envois sont conservés : ils empêchent de recandidater au même poste.
        self.cx.execute(
            "DELETE FROM entrees WHERE hash NOT IN (SELECT entrees_hash FROM journal WHERE entrees_hash IS NOT NULL)"
        )
        return len(ids)

    def exporter(self) -> dict:
        return {
            "dossiers": [d.model_dump(mode="json") for d in self.lister()],
            "decisions_humaines": self.decisions_humaines(),
            "escalades": [dict(r) for r in self.cx.execute("SELECT * FROM escalades")],
            "memoire_entreprises": {
                r["entreprise"]: json.loads(r["donnees"]) for r in self.cx.execute("SELECT * FROM memoire_entreprises")
            },
        }
