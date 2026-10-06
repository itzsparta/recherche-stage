# Plan : remplissage automatique des sites carrière

Statut : proposition, rien n'est implémenté.

## Objectif

Aujourd'hui, une candidature sans adresse mail finit à l'étape `depot` : l'interface
affiche « Chercher sa page carrières » et l'utilisateur remplit tout à la main.

Cible : un bouton « Déposer pour moi » sur la carte qui fait le travail suivant.

1. Il ouvre le site carrière dans un navigateur visible.
2. Il se connecte, ou crée le compte s'il n'existe pas.
3. Il remplit le formulaire avec le profil.
4. Il joint le CV et la lettre.
5. Il s'arrête devant le bouton « Envoyer ».

L'utilisateur relit la page et clique lui-même. Le bot détecte alors la page de
confirmation et classe la candidature comme envoyée.

## Pas de LLM dans le remplissage

C'est juste pour le remplisseur : toutes les valeurs existent déjà.

| Champ du formulaire | Valeur | Origine |
|---|---|---|
| Prénom, nom, mail, téléphone, ville | `ProfilStructure.identite` | CV |
| École, diplôme, dates | `formations` | CV |
| CV | `profil.cv_pdf` ou `documents.piece_jointe_cv` | fichier |
| Lettre de motivation (fichier ou zone de texte) | `documents.lettre` / `texte_lettre` | Rédaction, validée par l'utilisateur |
| Questions ouvertes connues (« Pourquoi nous ? ») | `documents.reponses_formulaire` | Rédaction, validée |
| Dates de stage, durée | `GrilleCriteres` / config | config |
| Consentement RGPD de l'entreprise | case à cocher | l'utilisateur l'a accepté une fois pour toutes, par site |

La reconnaissance des champs reprend la méthode de `excel.py` :
- elle lit l'attribut `autocomplete` (`given-name`, `email`, `tel`…), le `name`, l'`id` et le libellé `<label>` ;
- elle compare avec un dictionnaire de synonymes français et anglais ;
- un adaptateur par ATS donne les sélecteurs exacts quand le formulaire est connu.

Le LLM reste là où il est déjà : la Rédaction produit la lettre et les réponses,
l'Enquête trouve l'adresse de la page carrière. Le remplisseur, lui, est
déterministe, testable et gratuit.

Règle « rien d'inventé » : un champ obligatoire sans valeur dans le profil n'est
jamais deviné. Le bot s'arrête dessus, le surligne et passe la main.

## Comptes

Les sites qui exigent un compte sont Workday, SuccessFactors, Taleo et iCIMS,
ainsi que certains Teamtailor et sites maison. Pour Workday, il faut **un compte
par entreprise**.

- **Mail** : l'adresse de l'utilisateur (`identite.email`).
- **Mot de passe** : un mot de passe fort tiré au hasard, un par site, rangé dans le
  Gestionnaire d'identification Windows avec `keyring`. Jamais dans `config.yaml`,
  la base SQLite ou les journaux.
- **Registre des comptes** : une table `comptes_sites` (site, entreprise, adresse de
  connexion, date de création, sans mot de passe). Une commande `rs comptes` et un
  panneau dans l'interface les listent, avec le lien de suppression de chaque site.
  Pour le RGPD, l'utilisateur sait ainsi où il a laissé ses données.
- **Mail de vérification** : le bot lit la boîte par IMAP. Il ne garde qu'un mail
  arrivé après la création, venant du domaine du site, et il ouvre ce lien-là. Sans
  IMAP (Wi-Fi qui bloque le port 993), l'interface demande de cliquer le lien à la
  main, puis le bot reprend.
- **Création au cas par cas** : elle n'est faite qu'après confirmation sur la carte
  (« Créer un compte sur careers.xxx.com avec remi.d… ? »). Un réglage
  `depot.creer_comptes: demander | toujours | jamais` change ce comportement.

## Limites assumées (CGU, sécurité)

- **CAPTCHA** (fréquent à l'inscription Workday) : le bot ne le résout ni ne le
  contourne. Le navigateur est visible, l'utilisateur coche la case, le bot reprend.
- **Pas d'outils anti-détection.** Aucun plugin « stealth », aucune empreinte
  falsifiée, aucune rotation d'IP. Le bot avance à rythme humain et dépose une seule
  candidature à la fois.
- **Navigateur de l'utilisateur.** Playwright pilote Edge, déjà installé sous Windows
  (`channel="msedge"`), avec un profil dédié qui garde les sessions. Rien à
  télécharger.
- **Clic final humain.** Il garde la validation avant envoi, et c'est ce qui rend
  l'outil défendable vis-à-vis des CGU. L'outil devient un remplissage automatique
  amélioré : c'est l'utilisateur qui dépose. Plusieurs ATS (Workday notamment)
  interdisent dans leurs conditions l'accès « automatisé ». Un dépôt entièrement
  automatique les enfreindrait, et ce mode n'est donc pas proposé.
- **Sites bloqués.** LinkedIn, Indeed et les autres sites dont les CGU interdisent
  tout automate sont exclus d'office, comme pour la collecte.
- **Anti-doublon.** `store.reserver_envoi` est appelé *avant* d'ouvrir le
  formulaire. Un dossier déjà déposé n'est jamais rouvert.

## Architecture

```
src/recherche_stage/depot/
  __init__.py
  navigateur.py     # contexte Playwright persistant (Edge, visible), captures d'écran
  champs.py         # reconnaissance des champs : autocomplete/name/label -> clé du profil
  valeurs.py        # clé du profil -> valeur (Identite, Formation, documents, grille)
  comptes.py        # keyring + table comptes_sites + mail de vérification (IMAP)
  deposeur.py       # déroulé : ouvrir -> compte -> remplir -> joindre -> pause -> confirmation
  adaptateurs/
    base.py         # interface : reconnaitre(page), connexion(), remplir(), confirmation(page)
    greenhouse.py   # sans compte
    lever.py        # sans compte
    smartrecruiters.py
    workable.py
    ashby.py
    workday.py      # compte par tenant, formulaire en plusieurs étapes
    successfactors.py
    generique.py    # repli par libellés : sites maison, formulaires de contact
```

Les points de branchement dans le code existant :

- **`FicheEntreprise.page_carriere`** (nouveau champ) : l'Enquête le remplit déjà de
  fait. Elle cherche le site, il suffit de garder l'URL renvoyée par l'outil.
  `sources/ats.detecter_ats` / `detecter_dans_page` choisit ensuite l'adaptateur.
- **Choix du canal** (`Envoi.choisir_canal`) : il passe `canal="formulaire"` avec
  l'URL de la page carrière plutôt que celle de la fiche de l'annuaire.
- **`Orchestrateur.deposer(dossier_id)`** fait les étapes suivantes :
  - il réserve l'envoi et lance le `Deposeur` dans un fil à part, comme `rediger` ;
  - il relaie l'avancement dans le journal de l'interface ;
  - à la confirmation, il appelle `marquer_envoye(preuve=capture + URL)`, puis met
    l'Excel à jour.
- **Interface** : la carte « dépôt » gagne les boutons « Déposer pour moi » et
  « Reprendre » (après un CAPTCHA ou un champ manquant), en plus de « C'est déposé ».
  L'état du dépôt s'affiche : *connexion*, *création du compte*, *mail de
  vérification*, *à toi de jouer : …*, *prêt, clique Envoyer*.
- **Dépendances** : `playwright` et `keyring`, en option
  (`pip install -e .[depot]`), pour que le reste fonctionne sans elles.

## Tests

- `champs.py` et `valeurs.py` : tests unitaires purs, avec des tableaux libellé →
  clé attendue, en français et en anglais.
- Chaque adaptateur : une page HTML de test écrite à la main, qui reproduit la
  structure du formulaire de l'ATS (pas une copie du site). Elle est servie en local
  et pilotée par Playwright sans écran, avec le Chromium de l'environnement de CI.
  On vérifie :
  - les champs remplis ;
  - les fichiers joints ;
  - l'arrêt sur un champ obligatoire inconnu ;
  - l'arrêt sur un CAPTCHA factice ;
  - aucun clic sur « Envoyer » ;
  - la détection de la page de confirmation.
- `comptes.py` : `keyring` remplacé par un faux en mémoire. On vérifie qu'aucun mot
  de passe n'apparaît dans la base, les journaux ou les exceptions.
- Essai réel chez l'utilisateur, ATS par ATS, en commençant par un dossier peu
  important.

## Étapes proposées

1. **Repérage (court).** Pour les dossiers à l'étape `depot`, trouver la page
   carrière et l'ATS, puis compter par ATS. Le résultat fixe l'ordre des
   adaptateurs. Pour l'annuaire, beaucoup de PME n'ont pas de site carrière : il
   reste alors le formulaire de contact (adaptateur générique) ou le mail.
2. **Remplissage assisté sans compte.** Navigateur, champs, valeurs, adaptateur
   générique, Greenhouse, Lever, SmartRecruiters, Workable et Ashby. Bouton dans
   l'interface, pause avant envoi, détection de confirmation.
3. **Comptes.** `keyring`, registre, vérification par IMAP ou par clic, passage de
   main sur CAPTCHA. Adaptateurs Workday puis SuccessFactors.
4. **Finitions.**
   - `rs comptes` ;
   - capture jointe comme preuve ;
   - chiffres d'autonomie par ATS : taux de dépôts sans intervention ;
   - documentation.

## Décisions à prendre

- **Clic final** : humain (recommandé, voir CGU ci-dessus) ou automatique quand
  l'autonomie l'autorise.
- **Création de compte** : confirmation à chaque site (recommandé au début) ou
  automatique.
- **Mail de vérification** : lecture IMAP (il faut un réseau qui laisse passer le
  port 993) ou clic manuel.
