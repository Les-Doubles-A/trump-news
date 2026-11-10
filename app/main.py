"""Point d'entrée CLI (Phases 1-9). Interface graphique : run.bat ui."""
from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys
from pathlib import Path

from app.config.loader import load_config
from app.config.secrets import set_secret
from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.core.interfaces import ChatMessage, SynthOptions
from app.providers.tts.factory import build_tts_provider, default_voice
from app.providers.tts.piper import download_voice
from app.providers.visuals.factory import build_visual_provider
from app.visuals.models import VisualQuery
from app.visuals.sheet import contact_sheet
from app.providers.music.local import LocalMusicProvider
from app.audio.music_gen import MOODS, make_track
from app.video.service import list_videos
from app.video.templates import STYLE_TEMPLATE, TEMPLATES
from app.script.models import STYLES, TONES, ScriptBlockedError
from app.script.service import get_script, list_scripts
from app.database.session import latest_logs
from app.news.service import mark_story_used
from app.utils.timeutil import as_utc, utcnow

log = logging.getLogger("app")


def _ctx(config_path: str | None) -> AppContext:
    return AppContext.load(config_path)


def _bootstrap(config_path: str | None):
    ctx = _ctx(config_path)
    return ctx.cfg, ctx.factory, ctx.data_root


def cmd_init(args) -> int:
    cfg, _, root = _bootstrap(args.config)
    print(f"OK : configuration valide, dossiers et base SQLite prêts dans {root}")
    return 0


def cmd_config(args) -> int:
    cfg = load_config(args.config)
    print(cfg.model_dump_json(indent=2))
    return 0


def cmd_check_llm(args) -> int:
    status = _ctx(args.config).llm().health_check()
    print(("● CONNECTED  " if status.ok else "● NOT CONNECTED  ") + status.detail)
    for m in status.models:
        print(f"  - {m}")
    return 0 if status.ok else 1


def cmd_ask(args) -> int:
    resp = _ctx(args.config).llm().complete([ChatMessage("user", args.prompt)])
    print(resp.text)
    print(f"\n[{resp.model} · {resp.latency_s:.1f}s · tokens={resp.usage.total_tokens}]", file=sys.stderr)
    return 0


def cmd_logs(args) -> int:
    _, factory, _ = _bootstrap(args.config)
    for e in reversed(latest_logs(factory, args.n)):
        print(f"{e.ts:%H:%M:%S} {e.level:<7} {e.message}")
    return 0


def cmd_set_secret(args) -> int:
    # Un copier-coller emporte souvent des espaces ou un retour à la ligne : Google refuse alors l'identifiant.
    value = getpass.getpass(f"Valeur pour {args.name} (saisie masquée) : ").strip().strip('"').strip()
    if not value:
        print("ERREUR : aucune valeur saisie, rien n'a été enregistré.", file=sys.stderr)
        return 2
    set_secret(args.name, value)
    print(f"{args.name} enregistré dans le trousseau Windows ({len(value)} caractères).")
    return 0


def _service(args):
    return _ctx(args.config).news_service()


def _print_stories(rows, explain: bool = False) -> None:
    now = utcnow()
    if not rows:
        print("Aucun sujet. Lancez d'abord : run.bat fetch-news")
        return
    for r in rows:
        age_h = (now - as_utc(r.newest)).total_seconds() / 3600
        age = f"{age_h:.0f}h" if age_h < 48 else f"{age_h / 24:.0f}j"
        used = (" [UTILISÉ]" if r.used else "") + (f" [fact-check : {r.verdict}]" if r.verdict else "")
        print(f"#{r.id:<4} {r.score * 100:4.0f}%  {r.n_sources} src / {r.n_articles} art  texte {r.n_text}  {age:>4}  {r.category or '-':<13} {r.title}{used}")
        if explain:
            comp = "  ".join(f"{k}={v}" for k, v in r.breakdown.components.items())
            print(f"        {comp}\n        sources : {', '.join(r.sources)}")


def cmd_fetch_news(args) -> int:
    svc, providers = _service(args)
    if not providers:
        print("Aucun fournisseur d'actualités activé (config.yaml > news.providers).", file=sys.stderr)
        return 2
    rep = svc.run()
    for name, res in rep.provider_status.items():
        print(f"● OK     {name} : {res.count} articles" if res.ok else f"● ECHEC  {res.error}")
    print(
        f"\nArticles : {rep.fetched} récupérés · {rep.invalid} invalides · {rep.too_old} trop anciens · "
        f"{rep.irrelevant} hors sujet · {rep.duplicates} doublons · {rep.new_articles} nouveaux"
    )
    print("Regroupement : " + ("sémantique (embeddings LM Studio)" if rep.semantic else
          "par mots uniquement (aucun modèle d'embedding disponible dans LM Studio)"))
    print(f"Sujets : {rep.stories_created} créés, {rep.stories_updated} mis à jour  ({rep.duration_s:.1f}s)\n")
    _print_stories(svc.list_stories(limit=10))
    return 0 if any(r.ok for r in rep.provider_status.values()) else 1


def cmd_news(args) -> int:
    svc, _ = _service(args)
    _print_stories(svc.list_stories(limit=args.limit, include_used=args.used, hours=args.hours), explain=args.explain)
    return 0


def cmd_cluster_debug(args) -> int:
    svc, _ = _service(args)
    pairs = svc.similar_pairs(hours=args.hours, limit=args.limit)
    thr = svc.cfg.embeddings.threshold
    for a, b, lex, cos in pairs:
        c = f"cos={cos:.2f}" if cos is not None else "cos=n/a"
        merged = "FUSION" if (lex >= svc.cfg.similarity_threshold or (cos or 0) >= thr) else "      "
        print(f"{merged} {c} mots={lex:.2f}\n   - {a}\n   - {b}")
    print(f"\nSeuils actuels : cosinus >= {thr} ; mots >= {svc.cfg.similarity_threshold}")
    return 0


def cmd_fact_check(args) -> int:
    result = _ctx(args.config).fact_checker().check(args.story_id, force=args.force)
    levels = {lv: sum(1 for x in result.sources if x["level"] == lv) for lv in ("full", "summary", "title")}
    print(f"Sujet #{result.story_id} : {result.story_title}")
    print(f"Verdict : {result.verdict}  (confiance {result.confidence:.2f})  · modèle {result.model or 'auto'}"
          f"{'  · résultat en cache' if result.from_cache else ''}")
    print(f"Sources : {len(result.sources)} ({levels['full']} texte complet, {levels['summary']} résumé, {levels['title']} titre seul)")
    print(f"Affirmations : {result.llm_claims} proposée(s) par le LLM, {len(result.claims)} retenue(s)\n")
    if args.verbose:
        for x in result.sources:
            print(f"  {x['id']} {x['name']:<22} {x['level']:<8} {x.get('chars', '?'):>5} car.  {x['title'][:70]}")
        print()
    for c in result.claims:
        print(f"[{c.status} {c.confidence:.2f}] ({c.kind}) {c.claim}")
        print(f"    sources : {', '.join(sorted({e.source_name for e in c.supporting}))}")
        if c.contradicting:
            print(f"    CONTREDIT par : {', '.join(sorted({e.source_name for e in c.contradicting}))}")
        print(f"    « {c.supporting[0].quote} »  [{c.supporting[0].source_id}]")
        print(f"    → {c.phrasing}")
    if result.dropped:
        print(f"\n{len(result.dropped)} affirmation(s) écartée(s) :")
        for d in result.dropped:
            print(f"  - {d['reason']} : {d['claim'][:100]}")
    for w in result.warnings:
        print(f"! {w}")
    return 0 if result.verdict != "BLOCKED" else 1


def _print_script(sc: dict) -> None:
    print(f"Script #{sc['id']} · sujet #{sc['story_id']} · style {sc['style']} · ton {sc['tone']} · {sc['language']} · "
          f"cible {sc['duration']} s")
    print(f"Statut : {sc['status']}" + (f"  · {sc['words']} mots (~{sc['seconds']} s)" if sc.get("words") else ""))
    print()
    for seg in sc["segments"]:
        tag = f"[{seg['section'].upper()}/{seg['type']}]"
        print(f"{tag:<22} {seg['text']}   {','.join(seg['claims'])}")
    print("\nNarration complète :\n" + sc["full"])
    if sc.get("used"):
        print("\nAffirmations utilisées :")
        for c in sc["used"]:
            print(f"  {c['id']} [{c['status']}/{c['kind']}] {c['claim']}")
    for v in sc.get("visuals", []):
        print(f"  visuel (segment {v['segment'] + 1}) : {v['description']}")
    for w in sc.get("warnings", []):
        print(f"! {w}")


def cmd_script(args) -> int:
    try:
        g = _ctx(args.config).script_generator().generate(
            args.story_id, style=args.style, tone=args.tone, duration_s=args.duration, language=args.lang)
    except ScriptBlockedError as exc:
        print(f"SCRIPT BLOQUÉ : {exc}", file=sys.stderr)
        return 1
    _print_script({"id": g.id, "story_id": g.story_id, "style": g.style, "tone": g.tone, "language": g.language,
                   "duration": g.duration_target, "status": g.status, "words": g.word_count, "seconds": g.est_seconds,
                   "segments": g.segments, "full": g.full_script, "used": g.claims_used, "visuals": g.visual_suggestions,
                   "warnings": g.warnings})
    print(f"\n({g.attempts} tentative(s))")
    return 0


def cmd_scripts(args) -> int:
    _, factory, _ = _bootstrap(args.config)
    rows = list_scripts(factory, args.limit)
    if not rows:
        print("Aucun script. Lancez : run.bat script <id_sujet>")
    for r in rows:
        print(f"#{r.id:<4} sujet #{r.story_id:<4} {r.status:<13} {r.style:<13} {r.tone:<9} {r.duration_target}s  {r.hook}")
    return 0


def cmd_script_show(args) -> int:
    _, factory, _ = _bootstrap(args.config)
    r = get_script(factory, args.script_id)
    if r is None:
        print(f"Script #{args.script_id} introuvable.", file=sys.stderr)
        return 1
    data = r.claims or {}
    _print_script({"id": r.id, "story_id": r.story_id, "style": r.style, "tone": r.tone, "language": r.language,
                   "duration": r.duration_target, "status": r.status, "segments": data.get("segments", []),
                   "full": r.full_script, "used": data.get("claims", []), "visuals": data.get("visuals", []),
                   "warnings": data.get("warnings", [])})
    return 0


def _play(path) -> None:
    import os

    if hasattr(os, "startfile"):
        os.startfile(str(path))  # Windows : ouvre le lecteur par défaut
    else:
        print("(--play n'est disponible que sous Windows)")


def cmd_voices(args) -> int:
    cfg, _, _ = _bootstrap(args.config)
    name = args.provider or cfg.tts.provider
    prov = build_tts_provider(name, cfg)
    print(f"Voix {name}" + (" (internet requis)" if prov.requires_internet else " (locale)") + f" · langue : {args.lang or 'toutes'}")
    for v in prov.list_voices(args.lang):
        print(f"  {v.id:<40} {v.gender or '':<7} {v.name}")
    return 0


def cmd_tts_test(args) -> int:
    cfg, _, root = _bootstrap(args.config)
    name = args.provider or cfg.tts.provider
    lang = args.lang or cfg.app.language
    voice = args.voice or default_voice(name, cfg, lang)
    if not voice:
        print(f"Aucune voix {lang} configurée pour {name}. Voir : run.bat voices --provider {name} --lang {lang}", file=sys.stderr)
        return 2
    out = root / "audio" / f"test_{name}.wav"
    try:
        res = build_tts_provider(name, cfg).synthesize(args.text, out, SynthOptions(voice=voice, language=lang, speed=args.speed or cfg.tts.speed))
    except TrumpNewsError as exc:
        fb = cfg.tts.fallback
        if args.provider or not fb or fb == name or not default_voice(fb, cfg, lang):
            raise
        print(f"! {name} en échec : {exc}\n! Essai avec la voix de secours « {fb} »…", file=sys.stderr)
        name, voice = fb, default_voice(fb, cfg, lang)
        out = root / "audio" / f"test_{name}.wav"
        res = build_tts_provider(name, cfg).synthesize(args.text, out, SynthOptions(voice=voice, language=lang, speed=args.speed or cfg.tts.speed))
    print(f"OK : {out}  ({res.duration_s:.1f} s, voix {voice}, {res.chars} caractères" + (f", ~{res.cost_usd} $)" if res.cost_usd else ")"))
    if args.play:
        _play(out)
    return 0


def cmd_narrate(args) -> int:
    res = _ctx(args.config).narration().narrate(args.script_id, provider=args.provider, voice=args.voice,
                                                       speed=args.speed, force=args.force)
    print(f"Voix off #{res.id} · script #{res.script_id} · {res.provider}/{res.voice} · {res.language}"
          + ("  · depuis le cache" if res.from_cache else "") + ("  · VOIX DE SECOURS" if res.fallback_used else ""))
    print(f"Fichier : {res.audio_path}\nDurée : {res.duration_s:.1f} s" + (f"  · coût estimé ~{res.cost_usd} $" if res.cost_usd else ""))
    for seg in res.timeline:
        print(f"  {seg['start']:6.2f}-{seg['end']:6.2f}s  [{seg['section']}] {seg['text'][:70]}")
    for w in res.warnings:
        print(f"! {w}")
    if args.play:
        _play(res.audio_path)
    return 0


def cmd_download_voice(args) -> int:
    cfg, _, _ = _bootstrap(args.config)
    dest = cfg.tts.piper.models_dir
    dest = dest if dest.is_absolute() else Path.cwd() / dest
    files = download_voice(args.name, dest)
    print("Voix Piper téléchargée : " + ", ".join(f.name for f in files) + f"\nDossier : {dest}")
    return 0


def cmd_visuals(args) -> int:
    ctx = _ctx(args.config)
    root = ctx.data_root
    plan = ctx.visual_planner().plan(args.script_id, use_ai=True if args.ai else (False if args.no_ai else None))
    print(f"Plan visuel #{plan.id} · script #{plan.script_id} · {len(plan.items)} prise(s)"
          + ("  · CONTENU IA À SIGNALER" if plan.ai_disclosure else ""))
    for it in plan.items:
        a = it.asset
        print(f"  {it.start:6.1f}-{it.end:5.1f}s  seg {it.segment_index + 1}.{it.shot_index + 1} [{it.role:<7}] "
              f"{a['provider']:<9} {a['kind']:<12} {a.get('license') or '':<22} {Path(a['path']).name}")
    if plan.credits:
        print("\nCrédits (à ajouter à la description) :\n  " + "\n  ".join(plan.credits))
    for w in plan.warnings:
        print(f"! {w}")
    sheet = contact_sheet([i.__dict__ for i in plan.items], root / "images" / f"plan_{plan.id}_sheet.jpg")
    print(f"\nPlanche-contact : {sheet}")
    if args.play:
        _play(sheet)
    return 0


def cmd_visual_test(args) -> int:
    cfg, _, root = _bootstrap(args.config)
    prov = build_visual_provider(args.provider, cfg)
    st = prov.health_check()
    print(("● OK  " if st.ok else "● ! ") + st.detail)
    q = VisualQuery(args.kind, args.text, entities=[args.text] if args.kind == "photo" else [], keywords=args.text.split(),
                    language=cfg.app.language)
    asset = prov.get(q, root / "images", set())
    if asset is None:
        print("Aucun résultat pour cette requête.")
        return 1
    print(f"{asset.provider} · {asset.kind} · {asset.width}x{asset.height} · {asset.license}\n{asset.path}")
    if asset.attribution:
        print(f"Crédit : {asset.attribution}")
    if args.play:
        _play(asset.path)
    return 0


def _print_outcome(o) -> None:
    flag = {"READY": "READY", "NEEDS_REVIEW": "! NEEDS REVIEW"}.get(o.status, o.status)
    print(f"Vidéo #{o.video_id} · {o.template} · {o.duration:.1f} s · qualité {o.report.score:.2f} · {flag}")
    print(f"  Fichier : {o.path}")
    if o.thumbnail:
        print(f"  Vignette : {o.thumbnail}")
    if o.subtitles:
        print(f"  Sous-titres : {o.subtitles}")
    for c in o.report.checks:
        mark = "OK " if c.ok else ("XX " if c.critical else "-- ")
        print(f"  {mark}{c.name:<26} {c.detail}")
    if o.credits:
        print("\nCrédits (à reprendre dans la description) :\n  " + "\n  ".join(o.credits))
    for w in o.warnings:
        print(f"! {w}")
    if o.status != "READY":
        print("\nCette vidéo ne sera pas publiée automatiquement (XX = contrôle bloquant, -- = pénalité de score).")


def cmd_render(args) -> int:
    svc = _ctx(args.config).video_service()
    st = svc.renderer.health_check()
    if not st.ok:
        print(f"ERREUR : {st.detail}", file=sys.stderr)
        return 2
    music = True if args.music == "on" else (False if args.music == "off" else None)
    print("Montage en cours (de une à quelques minutes selon la durée et le PC)...")
    o = svc.render(args.script_id, template=args.template, music=music, preview=args.preview, keep_work=args.keep_work)
    print(f"Terminé en {o.elapsed_s:.0f} s.")
    _print_outcome(o)
    if args.play:
        _play(o.path)
    return 0


def cmd_videos(args) -> int:
    _, factory, _ = _bootstrap(args.config)
    rows = list_videos(factory, args.limit)
    if not rows:
        print("Aucune vidéo. Lancez : run.bat render <id_script>")
    for r in rows:
        score = f"{r.quality_score:.2f}" if r.quality_score is not None else "  - "
        dur = f"{r.duration:5.1f}s" if r.duration else "    - "
        print(f"#{r.id:<4} script #{r.script_id:<4} {r.status:<13} {r.template or '-':<14} {dur}  qualité {score}  "
              f"{Path(r.video_path).name if r.video_path else ''}")
    return 0


def cmd_video_check(args) -> int:
    _print_outcome(_ctx(args.config).video_service().check(args.video_id))
    return 0


def cmd_templates(args) -> int:
    cfg = load_config(args.config)
    print(f"Template actuel : {cfg.video.template}   (changer : run.bat set-option video.template <nom>)\n")
    for t in TEMPLATES.values():
        print(f"  {t.name:<14} {t.label}")
    print("\n« auto » choisit selon le style du script : " + ", ".join(f"{k} -> {v}" for k, v in STYLE_TEMPLATE.items()))
    return 0


def cmd_music(args) -> int:
    cfg = load_config(args.config)
    for label, folder, on in (("Musique", cfg.video.music_dir, cfg.video.music), ("Effets sonores", cfg.video.sfx_dir, cfg.video.sfx)):
        folder = folder if folder.is_absolute() else Path.cwd() / folder
        prov = LocalMusicProvider(folder)
        tracks = prov.list_tracks()
        print(f"{label} ({'activé' if on else 'désactivé'}) — dossier {folder}")
        for t in tracks:
            print(f"  OK {t.path.name:<32} licence {t.license}" + (f" · {t.credit}" if t.credit else ""))
        for name in prov.skipped:
            print(f"  XX {name:<32} ignoré : ajoutez {Path(name).stem}.txt contenant « licence: ... »")
        if not tracks and not prov.skipped:
            print("  (aucun fichier audio)")
    print("\nActiver : run.bat set-option video.music true   ·   run.bat set-option video.sfx true")
    return 0


def cmd_pipeline(args) -> int:
    from app.pipeline.runner import PipelineOptions, PipelineRunner

    def progress(i, n, msg):
        print(f"[{i}/{n}] {msg}")

    res = PipelineRunner(_ctx(args.config), progress=progress).run(args.story_id, PipelineOptions(
        duration_s=args.duration, template=args.template, preview=args.preview, force_factcheck=args.force))
    print()
    for st in res.steps:
        print(f"  {'OK ' if st.ok else 'XX '}{st.name:<10} {st.seconds:5.1f} s  {st.detail}")
    print(f"\n{res.status} : {res.message}")
    if res.video_path:
        print(f"Fichier : {res.video_path}")
    if res.status == "NEEDS_REVIEW" and res.script_id:
        print(f"Relire le script : run.bat script-show {res.script_id}  puis  run.bat approve {res.script_id} --verifie --note \"...\"")
    if args.publish and res.video_id:
        if res.status != "READY":
            print(f"Publication non lancée : la vidéo est {res.status}.")
        else:
            plan = _ctx(args.config).publishing().prepare(res.video_id)
            if plan.ok:
                return cmd_publish(argparse.Namespace(config=args.config, video_id=res.video_id, platform=None, dry_run=False))
            print("Publication non lancée :\n  " + "\n  ".join(plan.problems))
            print(f"Ensuite : run.bat publish {res.video_id}")
    return 0 if res.status in ("READY", "NEEDS_REVIEW") else 1


def cmd_approve(args) -> int:
    if not args.verifie:
        print("ERREUR : relisez d'abord le script et ses sources (run.bat script-show "
              f"{args.script_id}), puis confirmez avec --verifie.", file=sys.stderr)
        return 2
    out = _ctx(args.config).reviews().approve(args.script_id, sources_checked=True, note=args.note or "")
    _print_review(out)
    return 0


def cmd_reject(args) -> int:
    _print_review(_ctx(args.config).reviews().reject(args.script_id, note=args.note or ""))
    return 0


def cmd_connect(args) -> int:
    ctx = _ctx(args.config)
    pub = ctx.publisher(args.platform)
    if args.platform == "instagram":
        from app.database.session import set_setting
        from app.providers.publishers.factory import INSTAGRAM_USER_SETTING

        token = getpass.getpass("Jeton de Page Facebook (saisie masquée) : ").strip()
        if not token:
            print("ERREUR : aucun jeton saisi.", file=sys.stderr)
            return 2
        ig_id, username = pub.connect(token)
        set_setting(ctx.factory, INSTAGRAM_USER_SETTING, ig_id)
        print(f"Instagram connecté : @{username or '?'} (compte {ig_id}). Jeton rangé dans le trousseau Windows.")
    else:
        print("Le navigateur va s'ouvrir sur la page officielle de connexion. Acceptez, puis revenez ici (5 min maximum)...")
        name = pub.connect()
        print(f"{args.platform} connecté : « {name} ». Jeton rangé dans le trousseau Windows.")
    if not getattr(ctx.cfg.publishing, args.platform).enabled:
        print(f"Pour l'inclure dans les publications : run.bat set-option publishing.{args.platform}.enabled true")
    return 0


def cmd_publish_check(args) -> int:
    ctx = _ctx(args.config)
    code = 0
    for name in ("youtube", "tiktok", "instagram"):
        st = ctx.publisher(name).health_check()
        on = "activé" if getattr(ctx.cfg.publishing, name).enabled else "désactivé"
        print(f"{'● OK ' if st.ok else '● !  '} {name:<10} ({on})  {st.detail}")
        code = code or (0 if st.ok or on == "désactivé" else 1)
    return code


def _print_plan(plan) -> None:
    req = plan.request
    if req is not None:
        print(f"Titre : {req.title}\n\nDescription (YouTube) :\n{req.description}\n\nLégende (TikTok, Instagram) :\n{req.caption}\n")
    for p, why in plan.skipped.items():
        print(f"  -- {p} ignoré : {why}")
    for pb in plan.problems:
        print(f"  XX {pb}")


def cmd_publish(args) -> int:
    svc = _ctx(args.config).publishing()
    if args.dry_run:
        plan = svc.prepare(args.video_id, args.platform)
        _print_plan(plan)
        print(("Publiable sur : " + ", ".join(plan.platforms)) if plan.ok and plan.platforms else
              "Rien ne serait publié." if plan.ok else "Non publiable en l'état.")
        return 0 if plan.ok else 1
    print("Publication en cours (envoi puis traitement par chaque plateforme, de une à quelques minutes)...")
    plan, outcomes = svc.publish(args.video_id, args.platform)
    for p, why in plan.skipped.items():
        print(f"  -- {p:<10} ignoré : {why}")
    for o in outcomes:
        mark = {"PUBLISHED": "OK ", "PENDING": ".. "}.get(o.status, "XX ")
        print(f"  {mark}{o.platform:<10} {o.status:<10} {o.url or ''}  {o.detail}")
        if o.status == "FAILED" and o.retryable:
            print("               (erreur temporaire : relancez la même commande plus tard)")
    return 0 if all(o.status != "FAILED" for o in outcomes) else 1


def cmd_publications(args) -> int:
    svc = _ctx(args.config).publishing()
    if args.refresh:
        for o in svc.refresh_pending():
            print(f"  {o.platform:<10} {o.status:<10} {o.url or ''}  {o.detail}")
    rows = svc.list(args.limit)
    if not rows:
        print("Aucune publication. Voir : run.bat publish <id_vidéo> --dry-run")
    for r in rows:
        when = f"{r.published_at or r.created_at:%d/%m %H:%M}"
        print(f"#{r.id:<4} vidéo #{r.video_id:<4} {r.platform:<10} {r.status:<10} {when}  {r.url or r.error or ''}"[:220])
    return 0


def cmd_autopilot(args) -> int:
    def progress(i, n, msg):
        print(f"[{i}/{n}] {msg}")

    rep = _ctx(args.config).autopilot(progress=progress).run()
    print()
    for a in rep.attempts:
        mark = {"PUBLISHED": "OK ", "PENDING": ".. "}.get(a.status, "XX " if a.status == "FAILED" else "-- ")
        print(f"  {mark}sujet #{a.story_id:<4} {a.status:<10} {a.url or ''}  {a.message}"[:300])
        print(f"       {a.title}"[:200])
    print(f"\n{rep.message}")
    return 1 if any(a.status == "FAILED" for a in rep.attempts) else 0


RUN_FR = {"OK": "vidéo(s) publiée(s)", "NOTHING": "rien de publié", "FAILED": "échec", "PREFLIGHT": "services indisponibles",
          "LOCKED": "déjà en cours", "INTERRUPTED": "interrompu", "RUNNING": "en cours"}


def _print_run(r) -> None:
    when = r.started_at.astimezone() if r.started_at.tzinfo else r.started_at
    print(f"  #{r.id:<4} {when:%d/%m %H:%M}  {r.trigger:<9} {RUN_FR.get(r.status, r.status):<22} "
          f"{r.published} publiée(s)  {r.message}"[:240])


def cmd_schedule(args) -> int:
    svc = _ctx(args.config).scheduler()
    if args.action == "install":
        st = svc.install()
        print(f"Planification installée : le pilote se lancera chaque jour à {', '.join(st.times)} (heure du PC).")
        print("Conditions : PC allumé et session Windows ouverte ; un lancement manqué est rattrapé au réveil du PC.")
        print("Changer les heures : run.bat set-option scheduler.times 08:00,13:00,18:00   puis   run.bat schedule install")
    elif args.action == "remove":
        print("Planification supprimée." if svc.remove() else "Aucune planification installée.")
    elif args.action == "run-now":
        print("Lancement du pilote comme une tâche planifiée (vérification des services, puis génération et publication)...")
        _print_run(svc.run("manual", progress=lambda i, n, m: print(f"[{i}/{n}] {m}")))
    else:
        st = svc.status()
        if st.installed:
            print(f"Planification ACTIVE : chaque jour à {', '.join(st.times)}. Prochain lancement : {st.next_run:%d/%m à %H:%M}.")
        else:
            print("Planification INACTIVE. Activer : run.bat schedule install")
        rows = svc.history(args.limit)
        if rows:
            print("\nDerniers lancements :")
            for r in rows:
                _print_run(r)
    return 0


def cmd_scheduled_run(args) -> int:
    """Exécuté par la tâche Windows (pythonw.exe : aucune console). Sorties redirigées vers data/logs/scheduled/."""
    from datetime import datetime

    if sys.stdout is None or sys.stderr is None:
        logs = Path.cwd() / "data" / "logs" / "scheduled"
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / f"{datetime.now():%Y%m%d-%H%M}.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = out
    row = _ctx(args.config).scheduler().run("scheduled")
    _print_run(row)
    return 0 if row.status in ("OK", "NOTHING", "LOCKED") else 1


def _print_review(out) -> None:
    print(f"Script #{out.script_id} : {'APPROUVÉ' if out.decision == 'APPROVED' else 'REFUSÉ'}")
    for vid, status in out.video_statuses.items():
        print(f"  vidéo #{vid} recontrôlée : {status}")
    for w in out.warnings:
        print(f"! {w}")


def cmd_ui(args) -> int:
    try:
        from app.ui.app import run_ui
    except ImportError as exc:
        print(f"ERREUR : interface indisponible ({exc}). Installez-la : .venv\\Scripts\\python -m pip install -r requirements.txt",
              file=sys.stderr)
        return 2
    return run_ui(args.config)


def cmd_make_music(args) -> int:
    from app.utils.ffmpeg import require_ffmpeg

    cfg = load_config(args.config)
    folder = cfg.video.music_dir if cfg.video.music_dir.is_absolute() else Path.cwd() / cfg.video.music_dir
    print("Synthèse de la musique (quelques secondes)...")
    out = make_track(folder, require_ffmpeg(cfg.video.ffmpeg_path), args.mood, args.duration, args.seed)
    print(f"Piste créée : {out}  (licence CC0, générée localement)")
    print("Activer la musique : run.bat set-option video.music true")
    return 0


def cmd_reset_config(args) -> int:
    import shutil

    example, target = Path.cwd() / "config.example.yaml", Path.cwd() / "config.yaml"
    if not example.exists():
        print("ERREUR : config.example.yaml introuvable dans ce dossier.", file=sys.stderr)
        return 2
    if target.exists():
        shutil.copyfile(target, target.with_suffix(".yaml.bak"))
        print("Ancienne configuration sauvegardée : config.yaml.bak")
    shutil.copyfile(example, target)
    print("config.yaml recréé depuis config.example.yaml (liste complète des flux d'actualités).")
    print("Vos réglages personnels définis avec « set-option » (fichier .env) sont conservés.")
    return 0


def cmd_set_option(args) -> int:
    """Écrit TRUMPNEWS__SECTION__CLE=valeur dans .env (prioritaire sur config.yaml) après validation."""
    import os

    key = args.key.strip().lower()
    if not re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", key):
        print("ERREUR : clé attendue sous la forme section.cle (ex. llm.model).", file=sys.stderr)
        return 2
    name = "TRUMPNEWS__" + key.replace(".", "__").upper()
    try:  # validation avant écriture : on n'enregistre jamais une valeur invalide
        load_config(args.config, base_dir=Path.cwd(), environ={**os.environ, name: args.value})
    except TrumpNewsError as exc:
        print(f"Valeur refusée, .env inchangé :\n{exc}", file=sys.stderr)
        return 2
    env = Path.cwd() / ".env"
    lines = env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []
    new = f"{name}={args.value}"
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i] = new
            break
    else:
        lines.append(new)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK : {key} = {args.value}   (enregistré dans .env)")
    return 0


def cmd_mark_used(args) -> int:
    _, factory, _ = _bootstrap(args.config)
    ok = mark_story_used(factory, args.story_id)
    print("Sujet marqué comme utilisé." if ok else f"Sujet #{args.story_id} introuvable.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trump-news", description="Trump News — usine à vidéos courtes (Phases 1-9)")
    p.add_argument("--config", help="chemin d'un fichier config YAML")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="valide la config, crée les dossiers et la base").set_defaults(fn=cmd_init)
    sub.add_parser("config", help="affiche la configuration effective").set_defaults(fn=cmd_config)
    sub.add_parser("check-llm", help="teste LM Studio et liste les modèles").set_defaults(fn=cmd_check_llm)
    a = sub.add_parser("ask", help="envoie un prompt à LM Studio")
    a.add_argument("prompt")
    a.set_defaults(fn=cmd_ask)
    lg = sub.add_parser("logs", help="derniers logs enregistrés en base")
    lg.add_argument("-n", type=int, default=30)
    lg.set_defaults(fn=cmd_logs)
    s = sub.add_parser("set-secret", help="enregistre un secret dans le trousseau")
    s.add_argument("name")
    s.set_defaults(fn=cmd_set_secret)

    f = sub.add_parser("fetch-news", help="récupère les actualités, dédoublonne, regroupe et score")
    f.set_defaults(fn=cmd_fetch_news)
    n = sub.add_parser("news", help="liste les sujets classés par score")
    n.add_argument("-n", "--limit", type=int, default=20)
    n.add_argument("--hours", type=int, help="sujets créés depuis N heures")
    n.add_argument("--used", action="store_true", help="inclure les sujets déjà utilisés")
    n.add_argument("--explain", action="store_true", help="détail du score par composante")
    n.set_defaults(fn=cmd_news)
    cd = sub.add_parser("cluster-debug", help="paires de titres proches, pour régler les seuils de regroupement")
    cd.add_argument("--hours", type=int, default=24)
    cd.add_argument("--limit", type=int, default=30)
    cd.set_defaults(fn=cmd_cluster_debug)
    fcp = sub.add_parser("fact-check", help="vérifie les affirmations d'un sujet (sources + citations)")
    fcp.add_argument("story_id", type=int)
    fcp.add_argument("--force", action="store_true", help="ignore le résultat en cache")
    fcp.add_argument("--verbose", "-v", action="store_true", help="liste les sources et la taille du texte exploité")
    fcp.set_defaults(fn=cmd_fact_check)
    sp = sub.add_parser("script", help="génère le script vidéo d'un sujet déjà vérifié")
    sp.add_argument("story_id", type=int)
    sp.add_argument("--style", choices=STYLES)
    sp.add_argument("--tone", choices=TONES)
    sp.add_argument("--duration", type=int, choices=[20, 30, 45, 60, 90])
    sp.add_argument("--lang", choices=["en", "fr", "es"])
    sp.set_defaults(fn=cmd_script)
    sl = sub.add_parser("scripts", help="derniers scripts générés")
    sl.add_argument("-n", "--limit", type=int, default=20)
    sl.set_defaults(fn=cmd_scripts)
    ss = sub.add_parser("script-show", help="affiche un script enregistré")
    ss.add_argument("script_id", type=int)
    ss.set_defaults(fn=cmd_script_show)
    vp = sub.add_parser("voices", help="liste les voix d'un fournisseur TTS")
    vp.add_argument("--provider", choices=["edge", "piper", "elevenlabs"])
    vp.add_argument("--lang", choices=["en", "fr", "es"])
    vp.set_defaults(fn=cmd_voices)
    tt = sub.add_parser("tts-test", help="synthétise une phrase pour valider un fournisseur vocal")
    tt.add_argument("text")
    tt.add_argument("--provider", choices=["edge", "piper", "elevenlabs"])
    tt.add_argument("--lang", choices=["en", "fr", "es"])
    tt.add_argument("--voice")
    tt.add_argument("--speed", type=float)
    tt.add_argument("--play", action="store_true")
    tt.set_defaults(fn=cmd_tts_test)
    na = sub.add_parser("narrate", help="génère la voix off d'un script")
    na.add_argument("script_id", type=int)
    na.add_argument("--provider", choices=["edge", "piper", "elevenlabs"])
    na.add_argument("--voice")
    na.add_argument("--speed", type=float)
    na.add_argument("--force", action="store_true")
    na.add_argument("--play", action="store_true")
    na.set_defaults(fn=cmd_narrate)
    dv = sub.add_parser("download-voice", help="télécharge une voix Piper (ex. en_US-lessac-medium)")
    dv.add_argument("name")
    dv.set_defaults(fn=cmd_download_voice)
    vi = sub.add_parser("visuals", help="plan visuel d'un script (photos libres, cartes, illustrations)")
    vi.add_argument("script_id", type=int)
    vi.add_argument("--ai", action="store_true", help="autorise les illustrations IA même si désactivées")
    vi.add_argument("--no-ai", action="store_true", help="interdit les illustrations IA")
    vi.add_argument("--play", action="store_true")
    vi.set_defaults(fn=cmd_visuals)
    vt = sub.add_parser("visual-test", help="teste un fournisseur d'images")
    vt.add_argument("text")
    vt.add_argument("--provider", choices=["user", "wikimedia", "pexels", "ai", "cards"], default="wikimedia")
    vt.add_argument("--kind", default="photo", choices=["photo", "stock", "illustration", "headline", "claim", "number"])
    vt.add_argument("--play", action="store_true")
    vt.set_defaults(fn=cmd_visual_test)
    rd = sub.add_parser("render", help="monte la vidéo d'un script (voix off et plan visuel requis)")
    rd.add_argument("script_id", type=int)
    rd.add_argument("--template", choices=["auto", *TEMPLATES])
    rd.add_argument("--music", choices=["on", "off"], help="force la musique (sinon réglage video.music)")
    rd.add_argument("--preview", action="store_true", help="aperçu rapide en 540x960 (jamais publié)")
    rd.add_argument("--keep-work", action="store_true", help="conserve les fichiers intermédiaires")
    rd.add_argument("--play", action="store_true")
    rd.set_defaults(fn=cmd_render)
    vl = sub.add_parser("videos", help="dernières vidéos montées")
    vl.add_argument("-n", "--limit", type=int, default=20)
    vl.set_defaults(fn=cmd_videos)
    vc = sub.add_parser("video-check", help="refait le contrôle qualité d'une vidéo")
    vc.add_argument("video_id", type=int)
    vc.set_defaults(fn=cmd_video_check)
    sub.add_parser("templates", help="liste les templates de montage").set_defaults(fn=cmd_templates)
    sub.add_parser("music", help="liste la musique et les effets sonores utilisables (licence déclarée)").set_defaults(fn=cmd_music)
    pl = sub.add_parser("pipeline", help="chaîne complète pour un sujet : fact-check, script, voix, visuels, montage")
    pl.add_argument("story_id", type=int)
    pl.add_argument("--duration", type=int, choices=[20, 30, 45, 60, 90])
    pl.add_argument("--template", choices=["auto", *TEMPLATES])
    pl.add_argument("--preview", action="store_true", help="montage en aperçu 540x960")
    pl.add_argument("--force", action="store_true", help="refait le fact-check même s'il est en cache")
    pl.add_argument("--publish", action="store_true", help="publie ensuite si la vidéo est READY et publiable")
    pl.set_defaults(fn=cmd_pipeline)
    cn = sub.add_parser("connect", help="connecte un compte de publication (YouTube, TikTok, Instagram)")
    cn.add_argument("platform", choices=["youtube", "tiktok", "instagram"])
    cn.set_defaults(fn=cmd_connect)
    sub.add_parser("publish-check", help="état de connexion des comptes de publication").set_defaults(fn=cmd_publish_check)
    pb = sub.add_parser("publish", help="publie une vidéo READY sur les plateformes activées")
    pb.add_argument("video_id", type=int)
    pb.add_argument("--platform", action="append", choices=["youtube", "tiktok", "instagram"],
                    help="plateforme précise (répétable) ; sinon toutes celles activées")
    pb.add_argument("--dry-run", action="store_true", help="affiche titre, description et contrôles sans rien envoyer")
    pb.set_defaults(fn=cmd_publish)
    pu = sub.add_parser("publications", help="dernières publications")
    pu.add_argument("-n", "--limit", type=int, default=30)
    pu.add_argument("--refresh", action="store_true", help="suit les publications en cours de traitement")
    pu.set_defaults(fn=cmd_publications)
    sub.add_parser("autopilot", help="actualités, choix des sujets, montage et publication YouTube sans relecture "
                                     "(réglages : section autopilot)").set_defaults(fn=cmd_autopilot)
    sch = sub.add_parser("schedule", help="lancement automatique du pilote aux heures scheduler.times (Planificateur Windows)")
    sch.add_argument("action", nargs="?", default="status", choices=["status", "install", "remove", "run-now"])
    sch.add_argument("-n", "--limit", type=int, default=10, help="nombre de lancements affichés (status)")
    sch.set_defaults(fn=cmd_schedule)
    sub.add_parser("scheduled-run", help="(utilisé par la tâche Windows) un lancement planifié du pilote") \
        .set_defaults(fn=cmd_scheduled_run)
    ap = sub.add_parser("approve", help="approuve un script après relecture (rend ses vidéos publiables si tout le reste est OK)")
    ap.add_argument("script_id", type=int)
    ap.add_argument("--verifie", action="store_true", help="je confirme avoir vérifié les citations et les sources")
    ap.add_argument("--note", help="ce que vous avez vérifié (obligatoire si le fact-check est CAUTION)")
    ap.set_defaults(fn=cmd_approve)
    rj = sub.add_parser("reject", help="refuse un script (ses vidéos ne seront jamais publiées)")
    rj.add_argument("script_id", type=int)
    rj.add_argument("--note")
    rj.set_defaults(fn=cmd_reject)
    sub.add_parser("ui", help="ouvre l'interface graphique").set_defaults(fn=cmd_ui)
    mm = sub.add_parser("make-music", help="génère une musique de fond libre de droits (synthèse locale) dans assets/music")
    mm.add_argument("--mood", choices=list(MOODS), default="news")
    mm.add_argument("--duration", type=float, default=60.0)
    mm.add_argument("--seed", type=int, default=0, help="variante (autre valeur = autres timbres)")
    mm.set_defaults(fn=cmd_make_music)
    rc = sub.add_parser("reset-config", help="recrée config.yaml depuis config.example.yaml (sauvegarde l'ancien)")
    rc.set_defaults(fn=cmd_reset_config)
    so = sub.add_parser("set-option", help="change un réglage sans éditer de fichier (ex. llm.model qwen/qwen3-8b)")
    so.add_argument("key")
    so.add_argument("value")
    so.set_defaults(fn=cmd_set_option)
    mu = sub.add_parser("mark-used", help="marque un sujet comme déjà traité")
    mu.add_argument("story_id", type=int)
    mu.set_defaults(fn=cmd_mark_used)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except TrumpNewsError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
