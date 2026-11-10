# Trump News — Phases 1-8a : socle, actualités, fact-check, script, voix, visuels, montage, interface

Dépôt GitHub : **https://github.com/Les-Doubles-A/trump-news**

Usine automatisée de vidéos courtes d'actualité. Cette phase fournit le socle ; les phases suivantes ajoutent news, fact-check, script, voix, visuels, montage, UI, publication, scheduler, analytics.

## Installation (Windows)
1. Installer **Python 3.11+** (cocher « Add to PATH ») : https://www.python.org/downloads/
2. Installer **FFmpeg** (voix off et montage) : `winget install Gyan.FFmpeg`, puis rouvrir le terminal.
3. Installer **LM Studio** : https://lmstudio.ai — télécharger un modèle de chat (ex. un modèle « Instruct » 7-8B), puis onglet **Developer** → *Start Server* (port 1234).
4. Dans le dossier du projet : `install.bat`
5. Éditer `config.yaml` (endpoint, modèle, langue...) et `.env` si besoin.

## Commandes (Phase 1)
```
run.bat init          valide la config, crée /data/* et la base SQLite
run.bat config        affiche la configuration effective
run.bat check-llm     teste LM Studio et liste les modèles
run.bat ask "Bonjour" envoie un prompt au modèle
run.bat logs -n 30    derniers logs enregistrés en base
run.bat set-secret OPENAI_API_KEY   stocke un secret dans le trousseau Windows
```
Tests : `pip install -r requirements-dev.txt` puis `python -m pytest -q`.

## Configuration
- `config.yaml` (copié depuis `config.example.yaml`, ignoré par Git).
- Surcharge par variable d'environnement : `TRUMPNEWS__LLM__ENDPOINT=http://localhost:1234`.
- Secrets : variables d'environnement / `.env` (ignoré par Git) ou Windows Credential Manager. Jamais dans le code ni dans les logs (masquage automatique).

## Architecture
- `app/core` : interfaces (`ILLMProvider`), registre de plugins, erreurs, retry avec backoff.
- `app/providers/llm` : `LMStudioProvider` (API compatible OpenAI, modèle `auto`, JSON strict + auto-correction, retries, timeouts).
- `app/config` : schéma Pydantic validé, chargeur YAML + env, secrets.
- `app/database` : modèles SQLAlchemy (10 tables), WAL + clés étrangères, journal des logs en base.
- `app/utils` : extraction JSON de sorties LLM, hash de déduplication, logs JSON masquant les secrets.
- `prompts/` : prompts séparés du code (remplis à partir de la Phase 3).

Le LLM reste interchangeable : seule la fabrique `providers/llm/factory.py` connaît LM Studio. OpenAI/Anthropic/Ollama s'ajoutent comme fournisseurs enregistrés dans le registre.

## Limites connues de cette phase
- Les migrations Alembic ne sont pas encore en place : le schéma est créé par `create_all` (version stockée dans `settings.schema_version`). Alembic sera ajouté avant le premier changement de schéma.
- Seul LM Studio est implémenté côté LLM.

## Phase 2 : ingestion des actualités
```
run.bat fetch-news            collecte, normalise, dédoublonne, regroupe en sujets, score
run.bat news                  sujets classés par score (-n 20, --hours 24, --used)
run.bat news --explain        détail du score par composante et sources de chaque sujet
run.bat mark-used 12          marque un sujet comme déjà traité (ne sera plus proposé)
```
Pipeline : fournisseurs (RSS, Google News RSS, GDELT) → normalisation (HTML nettoyé, URL sans paramètres de suivi, date UTC, éditeur canonique) → filtre de pertinence (mot-clé « trump » par défaut) → dédoublonnage (hash + similarité de titres) → regroupement en **sujets** → score.

- **Un éditeur = une source**, même s'il apparaît via plusieurs flux (Reuters via Google News + Reuters RSS comptent pour 1).
- **Score** (0-100 %) : importance, récence (demi-vie 12 h), nombre de sources indépendantes, pertinence, potentiel vidéo, moins pénalités (doublons de même source, formulations incertaines, source peu fiable). Tous les poids et listes de termes sont dans `config.yaml > news.scoring`.
- **Fiabilité des éditeurs** : valeurs par défaut modifiables (`news.source_reliability`). Elles servent au classement, pas à décider de la vérité d'une information : c'est le rôle du fact-check (Phase 3).
- Un fournisseur en panne est réessayé 3 fois (backoff) puis ignoré, sans bloquer les autres.
- Les sujets déjà utilisés ne sont plus proposés, et les nouveaux articles sur le même événement s'y rattachent.

### Regroupement par le sens (embeddings)
Les titres d'un même événement partagent souvent peu de mots (« rebukes advisers » / « threatens to fire aides »). Pour les regrouper, installez dans LM Studio un modèle d'embedding (onglet Discover : `nomic-embed-text-v1.5`). `fetch-news` l'utilise automatiquement et affiche « Regroupement : sémantique ». Sans modèle, le regroupement se fait par mots communs (moins précis).
Réglage des seuils : `run.bat cluster-debug` liste les paires de titres proches avec leur similarité et indique celles qui seraient fusionnées ; ajustez `news.embeddings.threshold` dans `config.yaml`.

Limites connues : les liens Google News sont des redirections Google (non résolues), donc le texte de ces articles n'est pas disponible avant la Phase 3 ; les flux par défaut n'ont pas pu être testés en direct pendant le développement (adaptez les URL dans `config.yaml` si l'un d'eux a changé : `fetch-news` indique lequel échoue).

## Phase 3 : fact-checking
```
run.bat fact-check 2          vérifie le sujet n°2 (voir run.bat news)
run.bat fact-check 2 --force  ignore le résultat en cache
```
**Prérequis LM Studio** : longueur de contexte **8192 minimum** (le dossier de sources fait ~2 500 tokens, la réponse jusqu'à 4 096). Modèle conseillé : `qwen/qwen3-8b` ou `qwen/qwen3-14b`.

**Principe : le LLM propose, le code vérifie.**
1. Sélection d'une source par éditeur (texte complet > résumé > titre), jusqu'à 6. Le texte complet est téléchargé quand c'est permis (robots.txt respecté, aucun contournement de paywall ; sinon, repli sur le résumé du flux). Les liens Google News ne sont pas téléchargeables : ces sources restent « titre seul ».
2. Le LLM (prompt `prompts/fact_check.txt`, modifiable) extrait les affirmations en JSON strict, avec pour chacune une citation mot pour mot de chaque source.
3. Vérifications automatiques : la source citée existe ; **la citation figure réellement dans le texte de la source** ; chaque chiffre de l'affirmation figure dans la source. Sinon la source est retirée, et l'affirmation écartée si plus aucune ne tient.
4. Statut décidé par des règles, pas par le LLM :
   - `CONFIRMED` : relayé par ≥ 3 éditeurs indépendants de fiabilité moyenne ≥ 0,7 ; `LIKELY` : ≥ 2 ; `REPORTED` : 1 seule source ; `DISPUTED` : contredit par une source ; `UNKNOWN` : aucune source vérifiable.
   - Une allégation (« sources say… ») ou une opinion ne dépasse jamais `REPORTED`.
   - Une même dépêche reprise mot pour mot par plusieurs médias compte pour une seule origine.
5. Verdict du sujet : `SAFE` (au moins une affirmation CONFIRMED/LIKELY, sans litige parmi les 3 premières), `CAUTION`, ou `BLOCKED` (rien de vérifiable).
Chaque affirmation porte une consigne de formulation pour le script (« Selon… », « Les sources divergent… »). Le résultat est stocké (table `fact_checks`) avec les sources et citations ; les relances sont servies depuis le cache tant que les articles et le prompt ne changent pas.

**À savoir** : « CONFIRMED » signifie *recoupé par plusieurs éditeurs indépendants*, pas vérifié contre un document primaire. La vérification des citations prouve que la source dit bien ce qui est cité, pas que la citation entraîne à elle seule l'affirmation : le dossier (affirmation, citation, sources) est conservé pour relecture, et le mode SEMI AUTO permettra de le valider.

## Phase 4 : génération du script
```
run.bat script 7                                   style/ton/durée/langue = config.yaml (editorial.*, app.language)
run.bat script 7 --style explainer --tone balanced --duration 60 --lang fr
run.bat scripts                                    derniers scripts      run.bat script-show 5   affiche un script
```
Prérequis : `run.bat fact-check <sujet>` exécuté avant (un sujet BLOQUÉ par le fact-check ne produit pas de script).
Styles : `breaking_news, neutral_news, fast_news, dramatic, explainer, timeline, fact_check`. Tons : `neutral, balanced, pro_trump, anti_trump`. Durées : 20, 30, 45, 60, 90 s. Langues : `en, fr, es`.

**Principe : chaque phrase est rattachée à des affirmations vérifiées, et un programme contrôle le script avant de l'accepter.**
Le LLM (prompt `prompts/script_generation.txt`, modifiable) écrit des segments `hook → context → main → details → impact → conclusion`, chacun avec son type et les affirmations utilisées (C1, C2…). Le contrôle automatique rejette le script si :
- une phrase ne cite aucune affirmation, ou cite une affirmation inexistante ;
- un **chiffre** n'apparaît dans aucune affirmation citée (noms propres inconnus : avertissement, ou rejet avec `script.strict_entities: true`) ;
- une affirmation `LIKELY`/`REPORTED`, une déclaration ou une allégation est énoncée comme un fait (attribution obligatoire : « selon… », « Trump a déclaré… ») ;
- une affirmation `DISPUTED` est présentée sans la divergence des sources ;
- une opinion apparaît en ton neutre/équilibré, ou une opinion éditoriale (tons pro/anti, 2 maximum) n'est pas introduite par une formule explicite (« à notre avis… ») ou est attribuée à des « analystes/critiques » inventés ;
- le hook est trop long, racoleur, en majuscules ; la durée est hors cible ; le script est quasi identique à un script récent.
En cas de rejet, le LLM reçoit la liste exacte des erreurs et corrige (3 essais). Si des segments restent fautifs, ils sont **supprimés** (jamais corrigés à la main par l'application) et le script passe en `NEEDS_REVIEW` ; si le hook lui-même est invalide, la génération est refusée. Les hooks récents sont transmis au LLM pour varier les accroches.
Le script est enregistré avec ses segments, les affirmations utilisées, les citations des sources (`source_references`) et des suggestions de visuels pour la Phase 6.

Statut `DRAFT` : sujet SAFE, aucune correction forcée, aucun avertissement. Tout le reste → `NEEDS_REVIEW` (validation en mode SEMI AUTO). Le contenu est de la narration IA : pensez à activer l'indication « contenu généré par IA » exigée par les plateformes lors de la publication.

### Phase 4.1 : fidélité du script et durée adaptée
- **Vérification de fidélité (2e passe LLM, `script.llm_verify`)** : après le contrôle de forme, un second appel demande au LLM, segment par segment, si le texte ajoute une information absente des affirmations citées (cause, but, conséquence, réaction, adjectif de jugement…). Un segment infidèle est renvoyé en correction avec la phrase fautive citée ; s'il persiste, il est supprimé ; si c'est le hook, le script est refusé. Si cette passe est indisponible, un avertissement est ajouté et le script passe en `NEEDS_REVIEW`.
- **Durée adaptée à la matière (`script.adapt_duration`)** : environ 30 mots par affirmation vérifiée. Avec 1 affirmation, un script de 45 s est ramené à 20 s (avertissement affiché) plutôt que de broder.
- Les nombres sont comparés indépendamment du format (`1,5` / `1.5` / `18 000`). Le contrôle des noms propres n'a lieu que si le script est dans la langue des affirmations (anglais) ; en français/espagnol il est ignoré.
- `run.bat fact-check <id> --verbose` liste les sources, leur niveau (texte complet / résumé / titre) et la taille de texte exploitée, ainsi que le nombre d'affirmations proposées par le LLM.

## Phase 5 : voix off (TTS)
```
run.bat voices --provider edge --lang fr          liste les voix
run.bat tts-test "Hello world" --play              teste le fournisseur configuré (--provider, --lang, --voice)
run.bat narrate 3 --play                           voix off du script n°3 (--provider --voice --speed --force)
run.bat download-voice en_US-lessac-medium         télécharge une voix Piper (hors ligne)
```
**Fournisseurs** (`tts.provider`, repli automatique `tts.fallback`) :
| | Edge TTS (défaut) | Piper | ElevenLabs |
|---|---|---|---|
| Coût | gratuit | gratuit | payant (API officielle) |
| Internet | oui, service **non officiel** de Microsoft | non | oui |
| Qualité | voix neuronales naturelles | correcte | très élevée |
| Mise en place | rien (`edge-tts` est installé) | `pip install piper-tts` puis `download-voice` | variable `ELEVENLABS_API_KEY` + `voices` dans la config |
La clé ElevenLabs se met dans `.env` ou dans le trousseau Windows (`run.bat set-secret ELEVENLABS_API_KEY`), jamais dans `config.yaml`. Edge TTS et ElevenLabs renvoient du MP3 : **FFmpeg est requis** (`winget install Gyan.FFmpeg`, puis rouvrir le terminal). Piper seul n'en a pas besoin, sauf pour la normalisation du volume.

**Fonctionnement** : une voix off par **segment** du script (durées réelles → synchronisation des visuels et sous-titres), pauses configurables (0,4 s après le hook, 0,25 s ailleurs), concaténation, normalisation à -16 LUFS. La **chronologie** (début/fin de chaque segment, et de chaque mot avec Edge TTS) est enregistrée avec le fichier dans la table `narrations`. Les réglages influent sur le résultat : vitesse (`tts.speed`), « ton » par style de vidéo (`tts.style_prosody` : breaking_news plus rapide, dramatic plus lent), prononciations (`tts.pronunciations: {Zelensky: "Zelenski"}`), stabilité/modèle ElevenLabs. En cas d'échec : 3 essais avec attente croissante, puis voix de secours. Un résultat identique (même script, voix, réglages) est servi depuis le cache.

Hors ligne : seuls Piper (+ LM Studio) fonctionnent sans Internet ; `voices` et `tts-test` indiquent si le fournisseur nécessite Internet. XTTS n'est pas implémenté (l'interface `ITTSProvider` permet de l'ajouter comme plugin).

### Dépannage Edge TTS : erreur 403 « Invalid response status »
Edge TTS est un service Microsoft non officiel qui change régulièrement. En cas de 403 : (1) `pip install -U edge-tts` dans le `.venv` ; (2) vérifier que l'heure Windows est exacte (synchroniser) ; (3) désactiver VPN/proxy/pare-feu pour tester ; (4) réessayer plus tard. Si le problème persiste, utiliser **Piper** (`pip install piper-tts`, `run.bat download-voice en_US-lessac-medium`, `tts.provider: piper`) ou ElevenLabs. `narrate` bascule déjà seul sur la voix de secours, et `tts-test` aussi quand aucun `--provider` n'est imposé.

## Phase 6 : visuels
```
run.bat visuals 3 --play                    plan visuel du script n°3 + planche-contact (--ai / --no-ai)
run.bat visual-test "Donald Trump" --provider wikimedia --play     teste un fournisseur d'images
```
Un plan = pour chaque segment du script, une à trois **prises** (selon la durée réelle de la voix off, ~3,5 s par prise ; sans voix off, durées estimées), enregistré en base avec les fichiers, licences et crédits. Les fournisseurs sont essayés dans l'ordre de `visuals.order` :

| Fournisseur | Contenu | Licence / règles |
|---|---|---|
| `user` | vos images dans `assets/user_images` (mots du nom de fichier = mots-clés) | vos droits |
| `wikimedia` | photos de **Wikimedia Commons** (personnes, lieux) | domaine public, CC0, CC BY seulement (NC/ND/fair use refusées ; CC BY-SA optionnel) ; crédit généré automatiquement |
| `pexels` | photos génériques (désactivé par défaut ; clé gratuite `PEXELS_API_KEY`) | Pexels License |
| `ai` | illustrations **stylisées** par Stable Diffusion local (Automatic1111/Forge avec `--api` ; désactivé par défaut) | voir ci-dessous |
| `cards` | cartes 1080x1920 générées (titre, affirmation avec étiquette de statut « Rapporté / Contesté… », chiffre clé, **carte des sources** en conclusion) | aucun droit à gérer ; repli garanti |

- **Pas de fausse photo.** Le fournisseur IA impose un style non photoréaliste, retire tout nom de personne du prompt, ajoute un prompt négatif (visages, photos, logos, texte) et **incruste un bandeau « illustration générée par IA »** que rien ne désactive. Le plan porte l'indicateur `ai_disclosure` à reprendre lors de la publication.
- **Photo d'ouverture** : la première image de la vidéo est toujours une photo du sujet principal (nom cité dans le hook, sinon `visuals.hook_entity`, « Donald Trump » par défaut), la carte titre vient juste après.
- **Pas de répétition** : une même image n'est pas utilisée deux fois dans un plan, ni dans les 5 derniers plans (`visuals.avoid_recent_plans`). Les noms de médias (Reuters, BBC…) ne sont jamais cherchés comme sujets de photo ; les noms de personnes passent par `visuals.entity_aliases` (Trump → Donald Trump).
- **Crédits** : la liste des attributions (Wikimedia, Pexels) est produite avec le plan pour la description de la vidéo.
- Un fournisseur en panne est écarté pour le reste du plan (avertissement, « Internet requis » indiqué) et les cartes prennent le relais : le plan est toujours complet, même hors ligne.
- Wikimedia demande un contact dans le User-Agent : renseignez `visuals.wikimedia.contact` (e-mail ou URL).
Limites : les photos libres de Trump sont surtout des photos officielles ou anciennes ; il n'y a pas de photos d'agence (droits). Le cadrage 9:16 des photos paysage (fond flouté) est fait au montage (Phase 7).

## Phase 7 : montage vidéo (FFmpeg)
```
run.bat render 3 --preview --play           aperçu rapide 540x960 du script n°3 (jamais publié)
run.bat render 3 --play                     vidéo finale 1080x1920, 30 i/s (--template X, --music on|off, --keep-work)
run.bat videos                              dernières vidéos (statut, durée, score qualité)
run.bat video-check 1                       refait le contrôle qualité de la vidéo n°1
run.bat templates                           liste les templates de montage
run.bat music                               musique et effets sonores utilisables (licence déclarée)
```
Pré-requis : `run.bat narrate 3` puis `run.bat visuals 3`. Si la voix off est régénérée après le plan visuel, le montage refuse et demande de relancer `visuals` (sinon les images seraient décalées).

- **Cadrage** : photos paysage sur fond flouté et assombri, image entière au centre ; images verticales et cartes en plein écran ; léger zoom ou panoramique (Ken Burns) sur chaque prise ; transitions selon le template. Chaque changement d'image tombe au début du segment de voix correspondant et la vidéo a exactement la durée de la voix.
- **Sous-titres mot par mot** (fichier ASS dans `data/subtitles`) : blocs de 2 à 4 mots, mot en cours en jaune, placés vers 64 % de la hauteur (hors des zones masquées par TikTok, Shorts et Reels). Le texte est celui du script, même si une prononciation a été remplacée. Avec Edge, les temps des mots sont exacts ; avec Piper ou ElevenLabs, ils sont **estimés** (signalé). Pendant une carte qui affiche déjà le texte lu, les sous-titres sont masqués (`video.subtitles_on_cards`).
- **Mentions à l'écran** : crédit de chaque photo Wikimedia/Pexels pendant son affichage, « illustration générée par IA » pendant toute image IA, carte des sources en conclusion.
- **Templates** : `breaking_news`, `news_fast`, `minimal`, `documentary`, `political` ; `auto` (défaut) choisit selon le style du script. `run.bat set-option video.template minimal`.
- **Musique (désactivée par défaut)** : uniquement vos fichiers dans `assets/music`, chacun accompagné d'un `.txt` du même nom déclarant la licence (voir `assets/music/LISEZMOI.txt`). Volume bas, baisse automatique quand la voix parle, fondu en fin. Rien n'est téléchargé. `run.bat make-music --mood news` (ou `calm`, `--seed 2` pour une variante) synthétise localement une piste libre de droits (CC0) dans ce dossier. Même principe pour l'effet sonore des transitions (`assets/sfx`, `video.sfx`).
- **Contrôle qualité** après chaque montage : fichier lisible, 1080x1920, 30 i/s, H.264, durée = voix, audio présent, pas d'écran noir, sous-titres complets, carte des sources, crédits, mention IA, fact-check non bloqué, script validé (bloquants) ; volume -16 LUFS, crêtes, silences, sensationnalisme du hook, horodatage des mots (score). Statut **READY** si aucun contrôle bloquant n'échoue et score ≥ `quality.min_score` ; sinon **NEEDS_REVIEW** : la vidéo ne sera jamais publiée automatiquement. Un script en NEEDS_REVIEW donne donc une vidéo NEEDS_REVIEW.
- Fichiers : `data/videos/video_0001.mp4` + rapport `video_0001.json`, vignette dans `data/thumbnails`. Les prises intermédiaires sont gardées 7 jours dans `data/videos/cache` (re-montage plus rapide).
- Ordre de grandeur mesuré : 45 s de vidéo en ~45 s de calcul sur un PC récent (~20 s si les images n'ont pas changé, ~7 s en aperçu).

Réglages utiles :
```
run.bat set-option video.template breaking_news
run.bat set-option video.music true
run.bat set-option video.max_words_per_caption 2
run.bat set-option video.font_file assets/fonts/MaPolice.ttf
run.bat set-option video.preset fast
```

## Phase 8a : interface graphique et relecture
```
run.bat ui                                       ouvre l'interface
run.bat pipeline 48 --duration 30                chaîne complète pour un sujet (fact-check → script → voix → visuels → montage)
run.bat approve 2 --verifie --note "citations relues"   approuve un script après relecture
run.bat reject 2 --note "raison"                 refuse un script (ses vidéos ne seront jamais publiées)
```
- **Écrans** : Tableau de bord (état de LM Studio, FFmpeg, voix, Wikimedia et de la mémoire réservable ; compteurs ; dernières tâches), Actualités (sujets, détail du score, fact-check, pipeline), Scripts (chaque phrase avec son affirmation, la citation exacte et la source ; relecture), Vidéos (lecteur, contrôle qualité, crédits, nouveau contrôle, remontage), Journal (logs en direct). Le thème suit le mode clair/sombre de Windows.
- **Tâches de fond** : une seule tâche lourde à la fois (LM Studio ne tient qu'un modèle), la fenêtre reste utilisable ; « Annuler » arrête la chaîne entre deux étapes.
- **Relecture** : approuver exige la case « J'ai vérifié les citations et les sources », plus une note quand le fact-check est CAUTION. Un sujet BLOCKED ne peut pas être approuvé. La décision est tracée (table `reviews` : qui, quand, note) et les vidéos du script sont recontrôlées : une vidéo NEEDS_REVIEW devient READY si le seul contrôle en échec était « script validé ». L'approbation ne lève jamais un contrôle technique.
- Mémoire : si Windows ne peut plus réserver assez de mémoire (pas de fichier de pagination, WSL/Docker ouverts), le tableau de bord le signale avant que LM Studio n'échoue.
- À venir en 8b : écran Réglages (sans YAML) et assistant de premier lancement.

### Choisir un sujet à vérifier
`run.bat news` affiche « texte N » : le nombre d'articles du sujet dont on a un résumé ou le texte (les autres sont des titres seuls, issus de Google News). **Choisissez en priorité un sujet avec « texte 2 » ou plus** : le fact-check y trouve plus d'affirmations et le script a de la matière. Les flux directs (NYT, Washington Post, PBS, CNBC, Al Jazeera, NBC, CBS, ABC, The Hill) ont été ajoutés pour cela ; un flux qui change d'adresse est signalé « ECHEC » par `fetch-news` et peut être retiré de `config.yaml`.

### Réglages sans éditer de fichier
```
run.bat reset-config                           recrée config.yaml depuis config.example.yaml (ancien -> config.yaml.bak)
run.bat set-option llm.model qwen/qwen3-8b     change un réglage (écrit dans .env, prioritaire sur config.yaml)
run.bat set-option visuals.wikimedia.contact moi@example.com
run.bat config                                 affiche la configuration effective
```
`set-option` valide la valeur avant de l'écrire (une valeur invalide est refusée et `.env` reste intact). Format de la clé : `section.cle` ou `section.sous_section.cle`.

## Phase 9 : publication (YouTube Shorts, TikTok, Instagram Reels)
```
run.bat connect youtube                       relie la chaîne YouTube (le navigateur s'ouvre sur la page Google)
run.bat connect tiktok                        relie le compte TikTok
run.bat connect instagram                     relie le compte Instagram (jeton de Page Facebook demandé, saisie masquée)
run.bat publish-check                         état de connexion des trois comptes
run.bat publish 4 --dry-run                   titre, description, légende et contrôles, sans rien envoyer
run.bat publish 4                             publie la vidéo n°4 sur les plateformes activées
run.bat publish 4 --platform youtube          une seule plateforme (option répétable)
run.bat publications --refresh                historique, et suivi des publications encore en traitement
run.bat pipeline 48 --publish                 chaîne complète puis publication si la vidéo est publiable
```
- **Uniquement les API officielles** (YouTube Data v3, TikTok Content Posting, Instagram Graph). Aucun mot de passe ne passe par l'application ; les jetons sont rangés dans le trousseau Windows.
- **Garde-fous** : vidéo READY (pas un aperçu), fact-check non BLOCKED, script non refusé et **approuvé** (`run.bat approve S --verifie`), sauf si `publishing.require_approval` vaut false. Jamais deux fois la même vidéo sur la même plateforme ; un échec sur une plateforme n'empêche pas les autres et chaque tentative est enregistrée (table `publications`).
- **Métadonnées** construites sans LLM : titre = hook du script ; description YouTube = narration + sources avec liens + crédits images + mention IA éventuelle + hashtags ; légende TikTok/Instagram = titre + noms des sources + hashtags. Images IA présentes : signalé à YouTube (`containsSyntheticMedia`) et TikTok (`is_aigc`).
- **Visibilité par défaut** : YouTube `private`, TikTok `SELF_ONLY`. Passer en public : `run.bat set-option publishing.youtube.privacy public`.

### Préparer chaque plateforme (une seule fois)
**YouTube**
1. https://console.cloud.google.com : créer un projet, activer « YouTube Data API v3 ».
2. « Écran de consentement OAuth » : type Externe, ajouter votre adresse Google comme utilisateur test.
3. « Identifiants » > « Créer » > « ID client OAuth » > type **Application de bureau**.
4. Puis :
```
run.bat set-secret YOUTUBE_CLIENT_ID
run.bat set-secret YOUTUBE_CLIENT_SECRET
run.bat connect youtube
run.bat set-option publishing.youtube.enabled true
```
Limites Google : tant que le projet est en mode « Test », la connexion expire au bout de 7 jours (refaire `connect youtube`) ; le quota par défaut ne permet que quelques envois par jour ; les vidéos d'un projet non audité par Google restent privées.

**TikTok**
1. https://developers.tiktok.com : créer une application, ajouter **Login Kit** (plateforme Desktop) et **Content Posting API** avec **Direct Post** activé, autorisations `user.info.basic` et `video.publish`.
2. Adresse de retour (Redirect URI) : `http://localhost:3455/callback/`
3. Puis :
```
run.bat set-secret TIKTOK_CLIENT_KEY
run.bat set-secret TIKTOK_CLIENT_SECRET
run.bat connect tiktok
run.bat set-option publishing.tiktok.enabled true
```
Limite TikTok : tant que l'application n'est pas auditée par TikTok, elle ne publie que sur un compte **privé** et en `SELF_ONLY`. L'audit se demande sur le portail développeur.

**Instagram**
1. Compte Instagram **professionnel** (Business ou Créateur) relié à une **Page Facebook**.
2. https://developers.facebook.com : application de type Business avec **Facebook Login for Business**, permissions `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`.
3. Obtenir un **jeton de Page** (Graph API Explorer : jeton utilisateur avec ces permissions, puis `me/accounts` donne le jeton de la Page ; un jeton de Page tiré d'un jeton utilisateur longue durée n'expire pas).
4. Puis :
```
run.bat connect instagram
run.bat set-option publishing.instagram.enabled true
```
La vidéo est envoyée directement depuis le PC (envoi « resumable »), aucun hébergement web n'est nécessaire. Limite Meta : 100 publications par 24 h.

## Pilote automatique : tout générer et publier sur YouTube
Bouton **« Tout générer et publier »** du Tableau de bord (`run.bat ui`), ou :
```
run.bat autopilot
```
Récupère les actualités, prend les meilleurs sujets récents jamais montés, lance la chaîne complète et publie sur YouTube **sans relecture humaine**. Garde-fous (plus stricts qu'en publication manuelle) :
- seule une vidéo qui passe **tous les contrôles bloquants** est publiée ;
- un script NEEDS_REVIEW est approuvé par le pilote seulement si tous ses avertissements sont **mineurs** (longueur du texte, attribution à la source ajoutée par le code, durée réduite) et qu'il ne cite aucune affirmation DISPUTED ; l'approbation est tracée (relecteur « pilote automatique », sources non vérifiées par un humain). Tout autre avertissement (segment supprimé, conclusion manquante, nom propre non sourcé...) fait écarter le sujet, qui reste à relire dans l'écran Scripts. Désactiver : `run.bat set-option autopilot.auto_approve_minor false` ;
- fact-check SAFE ou CAUTION (`autopilot.allow_caution false` : SAFE seulement), jamais BLOCKED ;
- jamais deux fois le même sujet ; plafond par lancement et sur 24 h glissantes ;
- erreur technique (LM Studio, FFmpeg) : arrêt, le sujet sera réessayé ; quota YouTube dépassé : arrêt, la vidéo déjà montée est renvoyée au lancement suivant.

Réglages (tous facultatifs) :
```
run.bat set-option autopilot.videos_per_run 3        vidéos publiées au plus par lancement (1 à 5)
run.bat set-option autopilot.max_per_day 3           publications YouTube au plus sur 24 h
run.bat set-option autopilot.max_attempts 6          sujets essayés au plus par lancement
run.bat set-option autopilot.hours 24                ancienneté maximale des sujets
run.bat set-option autopilot.allow_caution false     SAFE seulement
run.bat set-option autopilot.youtube_privacy public  public, unlisted ou private (publication manuelle inchangée)
```
## Phase 10 : planification (FULL AUTO)
Le pilote se lance tout seul chaque jour aux heures `scheduler.times` (par défaut 08:00, 13:00, 18:00, heure du PC), par le Planificateur de tâches Windows, même si l'interface est fermée :
```
run.bat schedule install                       crée la tâche Windows (aucun droit administrateur)
run.bat schedule                               état, prochain lancement, derniers lancements
run.bat schedule run-now                       lance tout de suite, exactement comme la tâche (publie !)
run.bat schedule remove                        supprime la tâche
run.bat set-option scheduler.times 08:00,13:00,18:00    puis à nouveau : run.bat schedule install
```
Même chose dans l'interface : encadré « Planification » du Tableau de bord (Activer / Désactiver).
- Conditions : PC allumé et session Windows ouverte. Un lancement manqué (PC éteint, en veille) est rattrapé au démarrage suivant ; jamais deux lancements à la fois (verrou partagé avec le bouton de l'interface).
- Avant chaque lancement : LM Studio, FFmpeg et la voix sont vérifiés ; si LM Studio ne répond pas, son serveur est démarré (`lms server start`, désactivable : `scheduler.start_lmstudio false`). Le modèle doit pouvoir être chargé à la demande par LM Studio.
- Un lancement coupé (plantage, PC éteint) est noté « interrompu » ; le suivant reprend les vidéos déjà montées sans les remonter.
- Le pilote écarte un sujet trop proche d'une vidéo publiée dans les 48 h (`autopilot.neighbor_hours`, `autopilot.neighbor_similarity`).
- Journal de chaque lancement : `run.bat schedule`, table `scheduled_runs`, fichiers `data/logs/scheduled/`.

Google verrouille en privé les vidéos envoyées par un projet non audité : pour que « public » soit respecté, demandez l'audit « YouTube API Services » du projet Google Cloud.

<!-- installeur-release -->
## Installation

Téléchargez **`trump-news-Setup-0.8.0.exe`** depuis la [dernière release](https://github.com/Les-Doubles-A/trump-news/releases/latest), puis lancez-le.

Prérequis : **Python 3.11+** (cocher « Add to PATH »), **FFmpeg** et **LM Studio** (voir plus haut). À la fin, l'installeur lance `install.bat` qui crée l'environnement Python. Le raccourci « Trump News » du menu Démarrer ouvre l'interface (`run.bat ui`).

L'installeur n'est pas signé : si Windows SmartScreen l'affiche, cliquez sur « Informations complémentaires » puis « Exécuter quand même ».

### Recréer l'installeur

Compiler l'application (`(sources Python)`), puis lancer [Inno Setup 6](https://jrsoftware.org/isinfo.php) : `iscc installer\trump-news.iss`. Le setup est créé dans `installer\Output\`.
