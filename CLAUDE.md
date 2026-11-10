# Trump News — contexte pour Claude Code

Application desktop **Windows** (Python 3.12) qui produit chaque jour des vidéos courtes verticales d'actualité sur Donald Trump :
actualités → fact-check → script → voix → visuels → montage → publication. Voir `HANDOFF.md` pour l'historique détaillé.

## Règles de travail
- Répondre à l'utilisateur **en français**. Messages d'erreur, logs et CLI en français ; contenu vidéo par défaut en anglais.
- Du **vrai code fonctionnel** : pas de `pass`/`TODO` sur les fonctionnalités principales. Chaque phase : fichiers → code → tests `pytest` → correction → commandes à lancer.
- Ne pas enchaîner les phases sans que la précédente ait été testée par l'utilisateur sur son PC. Proposer l'architecture et attendre validation avant toute grosse génération.
- Tout est un **plugin/provider** derrière une interface (`app/core/interfaces.py`) : LLM, news, TTS, visuels, (à venir) renderer, musique, publishers.
- L'utilisateur n'est pas à l'aise avec l'édition de YAML : donner des **commandes complètes, une par ligne**, et privilégier `run.bat set-option clé valeur`.

## Principes éditoriaux NON NÉGOCIABLES
- Ne jamais inventer un fait. Chaque information du script doit être rattachée à une affirmation vérifiée, elle-même rattachée à une citation retrouvée **mot pour mot** dans une source.
- Le LLM propose, **le code vérifie** (citations, chiffres, statuts, attribution, fidélité). Les statuts CONFIRMED/LIKELY/REPORTED/DISPUTED/UNKNOWN sont calculés par des règles, jamais par le LLM.
- Faits, déclarations, allégations et opinions restent séparés ; une opinion n'est jamais présentée comme un fait ; pas d'attribution inventée (« les analystes disent… »).
- Pas de fausse photo réaliste : l'IA image est stylisée, sans nom de personne, avec bandeau « généré par IA » indélébile.
- Respect des API/CGU des plateformes ; aucun contournement d'anti-bot, de paywall ou de robots.txt ; aucune manipulation de vues/likes.
- Secrets jamais dans le code ni dans les logs (`.env`, trousseau Windows via `keyring`).

## Structure
```
app/core/        context.py (AppContext : config + base + fabriques des services, partagé CLI/UI), health.py (état des services), interfaces (ILLMProvider, INewsProvider, ITTSProvider, IEmbedder, IFactChecker, IVideoRenderer, IMusicProvider, IPublisher), registre, retry, erreurs
app/config/      schéma Pydantic (schema.py), chargeur YAML+.env (TRUMPNEWS__SECTION__CLE), secrets
app/database/    SQLAlchemy 2 + SQLite (create_all, pas encore d'Alembic : nouvelles TABLES seulement, jamais de nouvelle colonne sans migration)
app/providers/   llm/ (LM Studio), news/ (rss, google_news, gdelt), tts/ (edge, piper, elevenlabs), visuals/ (user, wikimedia, pexels, a1111, cards), video/ (ffmpeg), music/ (local, licence déclarée)
app/news/        normalisation, similarité (mots + embeddings), scoring configurable, ingestion
app/factcheck/   extraction par LLM + vérification déterministe (grounding.py, assess.py), fetcher robots.txt
app/script/      génération par segments rattachés aux affirmations, validateur déterministe, passe de fidélité LLM
app/audio/       voix off par segment, chronologie, normalisation -16 LUFS (ffmpeg)
app/visuals/     plan visuel, cartes 1080x1920 (Pillow), planche-contact
app/pipeline/    runner.py : chaîne fact-check → script → voix → visuels → montage (progression, annulation entre étapes)
app/review/      relecture humaine (approve/reject, table reviews, recontrôle des vidéos)
app/autopilot/   service.py : bouton « Tout générer et publier » / run.bat autopilot (choix des sujets, pipeline, publication YouTube sans relecture, table autopilot_attempts)
app/scheduler/   Phase 10 : windows_task.py (schtasks /XML, pythonw, sans admin), lock.py (verrou fichier partagé avec le pilote), service.py (préflight, scheduled-run, table scheduled_runs)
app/publishing/  garde-fous + métadonnées sans LLM + table publications (service.py, metadata.py), OAuth local (oauth.py)
app/providers/publishers/  youtube (Data v3 resumable), tiktok (Content Posting, Direct Post), instagram (Graph, resumable rupload)
app/ui/          PySide6 : main_window, jobs (JobRunner, 1 tâche lourde à la fois), pages/, dialogs, html_views, theme
app/video/       montage : composer (BDD -> RenderJob), alignment (temps des mots), subtitles (ASS), motion (calculs purs, graphes FFmpeg), quality, templates, service
prompts/         prompts Jinja2 modifiables (fact_check, script_generation, script_verify)
tests/           pytest (273 tests, aucun accès réseau ; tests/ui avec pytest-qt hors écran : respx + faux fournisseurs ; test_video_render.py lance le vrai FFmpeg, sauté s'il est absent)
```
Commandes : `run.bat <commande>` (voir `README.md`). Tests : `python -m pytest -q`.

## État
Phases 1 à 7 terminées et validées sur le PC de l'utilisateur, ainsi que les corrections pré-Phase 8 (citation sur le sujet, attribution automatique à la vraie source, pertinence Wikimedia). **Phase 8a (interface PySide6 : socle, Tableau de bord, Actualités, Scripts + relecture, Vidéos, Journal) codée et testée, en attente de validation par l'utilisateur.** Décisions : 8a puis 8b (Réglages + assistant de premier lancement), thème qui suit Windows, approbation = case « vérifié » toujours + note si CAUTION. **Phase 9 (publication YouTube/TikTok/Instagram par API officielles, `connect`, `publish`, `publications`, `pipeline --publish`) codée et testée sans réseau, en attente de validation sur le PC (comptes développeur à créer par l'utilisateur).** Publication YouTube validée sur le PC le 19/10/2026. **Pilote automatique** (bouton Tableau de bord + `run.bat autopilot`, YouTube seul, choix utilisateur : SAFE et CAUTION, jusqu'à 3 vidéos par clic, sans confirmation) codé et testé, en attente de validation. Pilote validé sur le PC le 19/10/2026 (2 vidéos publiées en public). **Phase 10 codée et testée (271+ tests), en attente de validation sur le PC** : Planificateur de tâches Windows (`schtasks`, sans admin) qui lance `run.bat autopilot`, FULL AUTO (publie), heures par défaut 08:00/13:00/18:00 ; verrou anti-lancement simultané, préflight (LM Studio via `lms server start`, FFmpeg, voix, YouTube), table `scheduled_runs`, exclusion des sujets voisins d'une vidéo publiée < 48 h, encadré Planification sur le Tableau de bord, commandes `schedule install|remove|status|run-now`. Ensuite 8b, 11 analytics.
La chaîne `fetch-news → fact-check N → script N → narrate S → visuals S → render S` fonctionne de bout en bout (ou `run.bat pipeline N`, ou l'interface `run.bat ui`).

## Pièges connus (déjà rencontrés sur le PC réel)
- Google News ne fournit que des **titres** : préférer les flux RSS directs (résumés) ; `run.bat news` affiche « texte N ».
- LM Studio : contexte **8192 minimum** ; `qwen2.5-7b` extrait trop peu d'affirmations, viser `qwen/qwen3-8b` ou 14b ; un modèle d'embedding (nomic) active le regroupement sémantique.
- Edge TTS (service Microsoft non officiel) peut renvoyer 403 : `pip install -U edge-tts`, horloge Windows exacte, sinon Piper.
- `loudnorm` de FFmpeg sur-échantillonne à 192 kHz si `-ar` n'est pas imposé (déjà corrigé).
- FFmpeg 9 n'a plus `-filter_complex_script` : utiliser `filter_script_args()` (`-/filter_complex` dès la v7).
- Images JPEG en plage pleine -> `yuvj420p` en sortie : la plage TV est imposée (`TV_RANGE` dans motion.py).
- Filtre `ass` : FFmpeg est lancé depuis le dossier de travail avec des noms relatifs (les « C:\ » cassent l'échappement).
- Pour tester avec le venv : `.venv\Scripts\python -m pytest -q` (pytest/respx dans requirements-dev.txt).
- Tests UI hors écran : `QT_QPA_PLATFORM=offscreen` (tests/ui/conftest.py) ; pour une capture lisible ajouter `QT_QPA_FONTDIR=C:/Windows/Fonts`.
- Ne jamais toucher un widget depuis un fil de fond : passer par `JobRunner` (résultats rendus par signaux Qt).
- Une liste `news.providers` écrite à la main dans `config.yaml` remplace la liste par défaut : utiliser `run.bat reset-config`.
- Fichiers de config lus en `utf-8-sig` (BOM toléré). `.env` écrase `config.yaml` (`set-option`).
