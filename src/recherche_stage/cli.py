"""Interface en ligne de commande : `rs <commande>`.

    rs init                          crée config.yaml, preferences.yaml et le dossier imports/
    rs profil --cv cv.pdf            agent Profil (CV en PDF, DOCX ou texte ; relevés ; préférences)
    rs cycle                         un passage complet du pipeline
    rs file                          ce qui attend une décision humaine
    rs voir <id>                     détail d'un dossier et de ses documents
    rs valider <id> [--modifie]      valide (après édition éventuelle des fichiers)
    rs rejeter <id> [motif]
    rs reponse <id> refus|entretien|demande_info [--date AAAA-MM-JJTHH:MM]
    rs marquer-envoye <id> <preuve>  dépôt manuel effectué
    rs liberer-envoi <id> [--nature relance1]
    rs statut                        tableau de bord
    rs fermer <n°> [note]            retire un élément de rs file (alerte traitée)
    rs reprendre-enquetes            relance l'Enquête des dossiers non rédigés à fiche minimale
    rs journal [<id>]                journal centralisé
    rs rejouer <id>                  entrées de chaque décision d'un dossier
    rs bilan [--appliquer]           bilan d'apprentissage et application des propositions
    rs rgpd export|purger
    rs detecter-ats <url> [--nom X]  trouve l'ATS d'une page carrière (ligne à copier dans config.yaml)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import config as configuration
from .agents.base import Contexte
from .agents.profil import AgentProfil
from .canaux.messagerie import BoiteIMAP, ExpediteurSMTP
from .llm_gemini import construire_llm
from .orchestrateur import Orchestrateur
from .reseau import HttpStandard
from .sources.adzuna import SourceAdzuna
from .sources.alertes_mail import SourceAlertesMail
from .sources.annuaire import SourceAnnuaire
from .sources.ats import SourceATS, ats_sans_api, detecter_ats, detecter_dans_page
from .sources.flux import SourceFlux
from .sources.france_travail import SourceFranceTravail
from .sources.import_fichier import SourceImport
from .store import Store

EXEMPLES = Path(__file__).resolve().parent / "exemples"


def construire(chemin_config: str) -> tuple[Contexte, Orchestrateur]:
    cfg = configuration.charger(chemin_config)
    cfg.dossier_donnees.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.chemin_base)
    llm = construire_llm(cfg.llm, store, cfg.orchestrateur.appels_api_max_par_semaine)
    http = HttpStandard()
    ctx = Contexte(store=store, config=cfg, llm=llm, http=http)

    s = cfg.sources
    sources = []
    if s.import_fichier:
        sources.append(SourceImport(s.dossier_imports))
    sources += [SourceATS(http, cible) for cible in s.ats]
    if s.france_travail:
        sources.append(SourceFranceTravail(http, configuration.secret(s.france_travail_client_id_env), configuration.secret(s.france_travail_secret_env)))
    if s.annuaire:
        sources.append(SourceAnnuaire(
            http, s.annuaire_codes_naf, s.annuaire_departements, store, s.annuaire_effectif_min, s.annuaire_max
        ))
    if s.adzuna:
        sources.append(SourceAdzuna(http, configuration.secret(s.adzuna_app_id_env), configuration.secret(s.adzuna_app_key_env), s.adzuna_pays))
    sources += [SourceFlux(http, flux) for flux in s.flux]
    if s.alertes_mail:
        if not cfg.suivi.imap.hote:
            raise SystemExit("alertes_mail : renseigner d'abord suivi.imap dans config.yaml")
        sources.append(SourceAlertesMail(ctx, BoiteIMAP(cfg.suivi.imap, s.alertes_dossier)))

    expediteur = ExpediteurSMTP(cfg.envoi.smtp) if cfg.envoi.smtp.hote else None
    boite = BoiteIMAP(cfg.suivi.imap) if cfg.suivi.imap.hote else None
    return ctx, Orchestrateur(ctx, sources, expediteur, boite)


def cmd_init(args) -> None:
    for nom in ("config.yaml", "preferences.yaml"):
        cible = Path(nom)
        if cible.exists():
            print(f"{nom} existe déjà, conservé")
        else:
            shutil.copy(EXEMPLES / nom.replace(".yaml", ".exemple.yaml"), cible)
            print(f"{nom} créé : à compléter")
    Path("imports").mkdir(exist_ok=True)
    print("Dossier imports/ prêt : déposez-y les offres JobTeaser, Indeed, Welcome to the Jungle (CSV ou JSON).")


def cmd_profil(args) -> None:
    ctx, _ = construire(args.config)
    preferences = yaml.safe_load(Path(args.preferences).read_text(encoding="utf-8")) or {}
    resultat = AgentProfil(ctx).executer(args.cv, preferences, args.releves or [], args.cv_pdf)
    if resultat is None:
        print("Profil incomplet : voir `rs file`.")
        sys.exit(1)
    profil, grille = resultat
    print(f"Profil de {profil.identite.prenom} {profil.identite.nom} : {len(profil.experiences)} expériences, "
          f"{len(profil.competences)} compétences.")
    print("Critères souples : " + ", ".join(f"{c.nom} ({c.poids:.0%})" for c in grille.souples))


def cmd_cycle(args) -> None:
    _, orchestrateur = construire(args.config)
    orchestrateur.progres = lambda message: print(message, flush=True)
    rapport = orchestrateur.cycle(collecter=not args.sans_collecte, envoyer=not args.sans_envoi, suivre=not args.sans_suivi)
    print("Cycle terminé : " + (", ".join(f"{k} {v}" for k, v in sorted(rapport.items())) or "rien à faire"))
    ouvertes = orchestrateur.store.escalades_ouvertes()
    if ouvertes:
        print(f"{len(ouvertes)} élément(s) attendent une décision : rs file")


def cmd_file(args) -> None:
    ctx, _ = construire(args.config)
    ouvertes = ctx.store.escalades_ouvertes()
    if not ouvertes:
        print("Rien en attente.")
    for e in ouvertes:
        print(f"[{e['id']}] {e['cree']} {e['type']} {e['dossier_id'] or ''}\n    {e['question']}")


def cmd_voir(args) -> None:
    ctx, _ = construire(args.config)
    dossier = ctx.store.get(args.id)
    if dossier is None:
        sys.exit(f"Dossier inconnu : {args.id}")
    o, d = dossier.offre, dossier.documents
    print(f"{o.intitule} — {o.entreprise} ({o.lieu}) [{dossier.statut.value}{' / ' + dossier.etape_attente if dossier.etape_attente else ''}]")
    print(f"Source : {o.source}  URL : {o.url}")
    print(f"Score {dossier.score.valeur} (confiance {dossier.score.confiance}) : {dossier.score.justification}")
    if dossier.critique.iteration:
        print(f"Critique : {dossier.critique.decision} — note {dossier.critique.note} (itération {dossier.critique.iteration})")
        for p in dossier.critique.problemes:
            print(f"  - [{p.gravite}/{p.type}] {p.raison} « {p.extrait[:80]} »")
    if d.version:
        print(f"\nObjet : {d.objet_mail}\n\n{d.mail}\n")
        if d.texte_lettre:
            print(f"--- Lettre ({d.lettre}) ---\n{d.texte_lettre}\n")
        print(f"Fichiers : {ctx.dossier_fichiers(dossier.id)}")
    if dossier.motif:
        print(f"Motif : {dossier.motif}")


def cmd_valider(args) -> None:
    _, orchestrateur = construire(args.config)
    etape = orchestrateur.valider(args.id, modifie=args.modifie)
    print(f"Validé (étape {etape}).")


def cmd_rejeter(args) -> None:
    _, orchestrateur = construire(args.config)
    orchestrateur.rejeter(args.id, " ".join(args.motif))
    print("Dossier abandonné.")


def cmd_reponse(args) -> None:
    _, orchestrateur = construire(args.config)
    quand = datetime.fromisoformat(args.date) if args.date else None
    orchestrateur.indiquer_reponse(args.id, args.categorie, quand)
    print("Réponse enregistrée.")


def cmd_marquer_envoye(args) -> None:
    _, orchestrateur = construire(args.config)
    orchestrateur.marquer_envoye(args.id, args.preuve)
    print("Envoi enregistré.")


def cmd_liberer_envoi(args) -> None:
    _, orchestrateur = construire(args.config)
    orchestrateur.liberer_envoi(args.id, args.nature)
    print("Réservation d'envoi supprimée : le dossier pourra être renvoyé.")


def cmd_fermer(args) -> None:
    ctx, _ = construire(args.config)
    if not ctx.store.fermer_escalade(args.numero, " ".join(args.note)):
        sys.exit(f"Aucun élément ouvert n° {args.numero} dans rs file")
    print("Élément retiré de la file.")


def cmd_reprendre_enquetes(args) -> None:
    _, orchestrateur = construire(args.config)
    print(f"{orchestrateur.reprendre_enquetes()} dossier(s) renvoyé(s) à l'Enquête : rs cycle")


def cmd_statut(args) -> None:
    ctx, _ = construire(args.config)
    store, cfg = ctx.store, ctx.config
    compte = Counter(d.statut.value for d in store.lister())
    print("Dossiers : " + ", ".join(f"{k} {v}" for k, v in sorted(compte.items())))
    print(f"Envois cette semaine : {store.envois_semaine()}/{cfg.orchestrateur.candidatures_max_par_semaine}")
    print(f"Appels LLM cette semaine : {store.appels_llm_semaine()}/{cfg.orchestrateur.appels_api_max_par_semaine}")
    from .autonomie import envoi_automatique_debloque, taux_validation

    taux, n = taux_validation(store, cfg.autonomie.fenetre)
    mode = "envoi automatique (candidatures standard)" if envoi_automatique_debloque(store, cfg) else "validation humaine avant envoi"
    print(f"Mode : {mode} — {taux:.0%} validés tels quels sur {n}/{cfg.autonomie.fenetre}")
    for ligne in store.cx.execute("SELECT * FROM sources ORDER BY nom"):
        etat = "COUPÉE" if ligne["coupee"] else "ok"
        print(f"Source {ligne['nom']} : {etat}, {ligne['collectes']} collectes, dernière {ligne['derniere_collecte']}")
    print(f"Escalades ouvertes : {len(store.escalades_ouvertes())}")


def cmd_journal(args) -> None:
    ctx, _ = construire(args.config)
    for j in ctx.store.journal(args.id, limite=args.n):
        print(f"{j['horodatage']} {j['agent']:<13} {j['action']:<28} {j['dossier_id'] or '':<17} {json.dumps(j['details'], ensure_ascii=False)[:120]}")


def cmd_rejouer(args) -> None:
    ctx, _ = construire(args.config)
    dossier = ctx.store.get(args.id)
    if dossier is None:
        sys.exit(f"Dossier inconnu : {args.id}")
    for h in dossier.historique:
        print(f"\n== {h.horodatage:%Y-%m-%d %H:%M} {h.agent} : {h.action} (entrées {h.entrees_hash or '-'})")
        if h.entrees_hash and args.complet:
            print(json.dumps(ctx.store.entrees(h.entrees_hash), ensure_ascii=False, indent=2)[:4000])


def cmd_bilan(args) -> None:
    _, orchestrateur = construire(args.config)
    print(orchestrateur.apprentissage.executer())
    if args.appliquer:
        for ligne in orchestrateur.appliquer_propositions():
            print(f"Appliqué : {ligne}")
    else:
        restantes = orchestrateur.store.propositions()
        if restantes:
            print(f"{len(restantes)} proposition(s) en attente : rs bilan --appliquer")


def cmd_rgpd(args) -> None:
    ctx, _ = construire(args.config)
    if args.action == "export":
        sortie = Path(args.sortie or "export_rgpd.json")
        sortie.write_text(json.dumps(ctx.store.exporter(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Export : {sortie}")
    else:
        avant = ctx.horloge() - timedelta(days=30 * ctx.config.conservation_mois)
        n = ctx.store.purger(avant)
        for dossier in (ctx.config.dossier_donnees / "dossiers").glob("*"):
            if not ctx.store.existe(dossier.name):
                shutil.rmtree(dossier)
        print(f"{n} dossier(s) terminés depuis plus de {ctx.config.conservation_mois} mois supprimés.")


def cmd_detecter_ats(args) -> None:
    from .reseau import ErreurReseau

    trouve = detecter_ats(args.url)
    page = ""
    if trouve is None and not ats_sans_api(args.url):
        http = HttpStandard()
        try:
            trouve = detecter_dans_page(http, args.url)
            page = "" if trouve else http.get_texte(args.url)
        except ErreurReseau as e:
            raise SystemExit(f"Page inaccessible : {e}") from e
    if trouve is None:
        sans_api = ats_sans_api(args.url) or ats_sans_api(page)
        if sans_api:
            print(f"{sans_api} : pas d'API publique. Crée une alerte mail sur le site carrière, ou utilise son flux RSS s'il en a un.")
        else:
            print("ATS non reconnu. Essaie l'adresse de la page qui liste les offres, ou crée une alerte mail sur le site.")
        return
    ats, identifiant = trouve
    nom = args.nom or identifiant
    print("À ajouter dans config.yaml, sous sources.ats :")
    print(f'  - {{nom: "{nom}", ats: {ats}, identifiant: "{identifiant}"}}')


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rs", description="Système multi-agents de recherche de stage")
    parser.add_argument("--config", default="config.yaml")
    sous = parser.add_subparsers(dest="commande", required=True)

    sous.add_parser("init").set_defaults(func=cmd_init)

    p = sous.add_parser("profil")
    p.add_argument("--cv", required=True)
    p.add_argument("--cv-pdf", help="CV PDF à joindre (par défaut, --cv s'il est en PDF)")
    p.add_argument("--releves", nargs="*")
    p.add_argument("--preferences", default="preferences.yaml")
    p.set_defaults(func=cmd_profil)

    p = sous.add_parser("cycle")
    p.add_argument("--sans-collecte", action="store_true")
    p.add_argument("--sans-envoi", action="store_true")
    p.add_argument("--sans-suivi", action="store_true")
    p.set_defaults(func=cmd_cycle)

    sous.add_parser("file").set_defaults(func=cmd_file)
    sous.add_parser("statut").set_defaults(func=cmd_statut)
    sous.add_parser("reprendre-enquetes").set_defaults(func=cmd_reprendre_enquetes)

    p = sous.add_parser("fermer")
    p.add_argument("numero", type=int)
    p.add_argument("note", nargs="*")
    p.set_defaults(func=cmd_fermer)

    p = sous.add_parser("voir")
    p.add_argument("id")
    p.set_defaults(func=cmd_voir)

    p = sous.add_parser("valider")
    p.add_argument("id")
    p.add_argument("--modifie", action="store_true", help="relire les fichiers mail_vN.txt / lettre_vN.txt édités")
    p.set_defaults(func=cmd_valider)

    p = sous.add_parser("rejeter")
    p.add_argument("id")
    p.add_argument("motif", nargs="*")
    p.set_defaults(func=cmd_rejeter)

    p = sous.add_parser("reponse")
    p.add_argument("id")
    p.add_argument("categorie", choices=["refus", "entretien", "demande_info"])
    p.add_argument("--date")
    p.set_defaults(func=cmd_reponse)

    p = sous.add_parser("marquer-envoye")
    p.add_argument("id")
    p.add_argument("preuve")
    p.set_defaults(func=cmd_marquer_envoye)

    p = sous.add_parser("liberer-envoi")
    p.add_argument("id")
    p.add_argument("--nature", default="candidature")
    p.set_defaults(func=cmd_liberer_envoi)

    p = sous.add_parser("journal")
    p.add_argument("id", nargs="?")
    p.add_argument("-n", type=int, default=50)
    p.set_defaults(func=cmd_journal)

    p = sous.add_parser("rejouer")
    p.add_argument("id")
    p.add_argument("--complet", action="store_true")
    p.set_defaults(func=cmd_rejouer)

    p = sous.add_parser("bilan")
    p.add_argument("--appliquer", action="store_true")
    p.set_defaults(func=cmd_bilan)

    p = sous.add_parser("rgpd")
    p.add_argument("action", choices=["export", "purger"])
    p.add_argument("--sortie")
    p.set_defaults(func=cmd_rgpd)

    p = sous.add_parser("detecter-ats")
    p.add_argument("url")
    p.add_argument("--nom", default="")
    p.set_defaults(func=cmd_detecter_ats)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError, RuntimeError) as e:
        sys.exit(f"Erreur : {e}")


if __name__ == "__main__":
    main()
