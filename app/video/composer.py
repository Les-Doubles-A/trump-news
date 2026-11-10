"""Base de données -> RenderJob : script, dernière voix off, dernier plan visuel, police, musique.
Refuse clairement les situations incohérentes (pas de voix, plan calé sur une ancienne voix, image disparue)."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audio.wavtools import wav_duration
from app.config.schema import AppConfig
from app.core.errors import TrumpNewsError
from app.core.interfaces import MusicTrack
from app.database.models import FactCheck, Narration, Script, VisualPlanRow
from app.database.session import session_scope
from app.providers.music.local import LocalMusicProvider
from app.video.alignment import caption_words
from app.video.models import RenderJob, Shot
from app.video.quality import QualityContext
from app.video.templates import get_template

CREDITED_PROVIDERS = ("wikimedia", "pexels")
WINDOWS_FONTS = {"Arial Black": "ariblk.ttf", "Arial": "arialbd.ttf", "Segoe UI Black": "seguibl.ttf",
                 "Segoe UI": "segoeuib.ttf", "Impact": "impact.ttf", "Verdana": "verdanab.ttf"}
FALLBACK_FONTS = ["Arial Black", "Segoe UI Black", "Arial"]
LINUX_FONT = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans")


@dataclass
class ComposedJob:
    job: RenderJob
    context: QualityContext
    story_id: int
    script_id: int
    narration_id: int
    plan_id: int
    template: str
    credits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_font(font_file: str | None, family: str, base_dir: Path, windows_dir: Path | None = None) -> tuple[Path | None, str | None]:
    """(fichier de police, nom de famille pour l'ASS). Police personnalisée > police du template > repli Windows > DejaVu."""
    if font_file:
        p = Path(font_file)
        p = p if p.is_absolute() else base_dir / p
        if not p.exists():
            raise TrumpNewsError(f"Police introuvable : {p} (réglage video.font_file).")
        from PIL import ImageFont
        try:
            return p, ImageFont.truetype(str(p), 20).getname()[0]
        except OSError as exc:
            raise TrumpNewsError(f"Police illisible : {p} ({exc}).") from exc
    fonts = windows_dir or Path("C:/Windows/Fonts")
    for fam in [family, *FALLBACK_FONTS]:
        f = WINDOWS_FONTS.get(fam)
        if f and (fonts / f).exists():
            return fonts / f, fam
    if sys.platform != "win32" and Path(LINUX_FONT[0]).exists():
        return Path(LINUX_FONT[0]), LINUX_FONT[1]
    return None, None


def compose(cfg: AppConfig, factory: sessionmaker[Session], script_id: int, data_root: Path, *, base_dir: Path | None = None,
            template: str | None = None, music: bool | None = None, preview: bool = False) -> ComposedJob:
    base = base_dir or Path.cwd()
    v = cfg.video
    warnings: list[str] = []
    with session_scope(factory) as s:
        sc = s.get(Script, script_id)
        if sc is None:
            raise TrumpNewsError(f"Script #{script_id} introuvable. Voir : run.bat scripts")
        nar = s.scalar(select(Narration).where(Narration.script_id == script_id).order_by(Narration.id.desc()))
        if nar is None:
            raise TrumpNewsError(f"Le script #{script_id} n'a pas de voix off. Lancez d'abord : run.bat narrate {script_id}")
        plan = s.scalar(select(VisualPlanRow).where(VisualPlanRow.script_id == script_id).order_by(VisualPlanRow.id.desc()))
        if plan is None:
            raise TrumpNewsError(f"Le script #{script_id} n'a pas de plan visuel. Lancez d'abord : run.bat visuals {script_id}")
        if plan.narration_id != nar.id:
            raise TrumpNewsError(f"Le plan visuel #{plan.id} a été calé sur une autre voix off que la dernière (#{nar.id}). "
                                 f"Relancez : run.bat visuals {script_id}")
        fc = s.scalar(select(FactCheck).where(FactCheck.story_id == sc.story_id).order_by(FactCheck.id.desc()))
        story_id, style, status, language = sc.story_id, sc.style, sc.status, sc.language
        segments = (sc.claims or {}).get("segments", [])
        narration = {"id": nar.id, "path": nar.audio_path, "duration": nar.duration, "timeline": nar.timeline}
        plan_data, plan_id = plan.plan, plan.id
        verdict = fc.verdict if fc else None

    voice = Path(narration["path"])
    voice = voice if voice.is_absolute() else base / voice
    if not voice.exists():
        raise TrumpNewsError(f"Fichier de voix off introuvable : {voice}. Relancez : run.bat narrate {script_id} --force")
    duration = wav_duration(voice) if voice.suffix.lower() == ".wav" else float(narration["duration"])

    shots, missing = [], []
    for it in sorted(plan_data.get("items", []), key=lambda i: i["start"]):
        a = it["asset"]
        p = Path(a["path"])
        p = p if p.is_absolute() else base / p
        if not p.exists():
            missing.append(p.name)
            continue
        credit = a.get("attribution") if a.get("provider") in CREDITED_PROVIDERS and a.get("attribution") else None
        shots.append(Shot(p, float(it["start"]), float(it["end"]), a.get("kind", "photo"), it.get("role", "primary"),
                          int(a.get("width") or 0), int(a.get("height") or 0), a.get("provider", ""), credit,
                          bool(a.get("ai_generated")),
                          hide_captions=(not v.subtitles_on_cards and a.get("kind") == "card" and it.get("role") != "sources")))
    if missing:
        raise TrumpNewsError(f"Image(s) du plan visuel #{plan_id} introuvable(s) : {', '.join(missing[:5])}. "
                             f"Relancez : run.bat visuals {script_id}")
    if not shots:
        raise TrumpNewsError(f"Le plan visuel #{plan_id} ne contient aucune image. Relancez : run.bat visuals {script_id}")

    words, estimated = caption_words(narration["timeline"])
    if estimated:
        warnings.append("Temps des mots estimés (le moteur vocal n'en fournit pas) : synchronisation approximative des sous-titres.")

    tpl_name = template or v.template
    tpl = get_template(tpl_name, style)
    if v.max_words_per_caption:
        tpl = tpl.model_copy(update={"max_words": v.max_words_per_caption})
    font_file, family = resolve_font(v.font_file, tpl.font, base)
    if family is None:
        warnings.append("Aucune police connue trouvée : police par défaut de libass.")

    w, h = (int(x) for x in v.resolution.split("x"))
    if preview:
        w, h = w // 4 * 2, h // 4 * 2      # moitié, dimensions paires (H.264)

    credits = list(plan_data.get("credits", []))
    track = sfx = None
    if music if music is not None else v.music:
        track = _pick(LocalMusicProvider(_abs(v.music_dir, base)), script_id, v.music_track, "Musique", warnings)
        if track and track.credit:
            credits.append(f"Music: {track.credit} ({track.license})")
    if v.sfx:
        sfx = _pick(LocalMusicProvider(_abs(v.sfx_dir, base)), 0, None, "Effet sonore", warnings)
        if sfx and sfx.credit:
            credits.append(f"Sound effect: {sfx.credit} ({sfx.license})")

    hook = next((sg["text"] for sg in segments if sg.get("section") == "hook"), segments[0]["text"] if segments else "")
    job = RenderJob(
        width=w, height=h, fps=v.fps, duration=duration, voice_path=voice, shots=shots, words=words, template=tpl,
        work_dir=data_root / "videos" / "work", subtitles_path=data_root / "subtitles" / "video.ass", language=language,
        subtitles=v.subtitles, transitions=v.transitions,
        progress_bar=tpl.progress_bar if v.progress_bar is None else v.progress_bar, music=track,
        music_volume_db=v.music_volume_db if v.music_volume_db is not None else tpl.music_volume_db, sfx=sfx,
        sfx_volume_db=v.sfx_volume_db, crf=28 if preview else v.crf, preset="ultrafast" if preview else v.preset,
        preview=preview, font_file=font_file, font_family=family, keep_work=v.keep_work, timeout_s=v.render_timeout_s,
    )
    ctx = QualityContext(
        width=w, height=h, fps=v.fps, expected_duration=round(duration, 3), language=language, hook=hook,
        subtitles_enabled=v.subtitles, shots_needing_credit=sum(1 for sh in shots if sh.credit),
        ai_shots=sum(1 for sh in shots if sh.ai_generated), has_sources_card=any(sh.role == "sources" for sh in shots),
        factcheck_verdict=verdict, script_status=status, preview=preview,
    )
    return ComposedJob(job, ctx, story_id, script_id, narration["id"], plan_id, tpl.name, credits, warnings)


def _abs(p: Path, base: Path) -> Path:
    return p if p.is_absolute() else base / p


def _pick(prov: LocalMusicProvider, seed: int, name: str | None, label: str, warnings: list[str]) -> MusicTrack | None:
    track = prov.pick(seed, name)
    for skipped in prov.skipped:
        warnings.append(f"{label} ignorée, licence non déclarée : {skipped} (ajoutez {Path(skipped).stem}.txt avec « licence: ... »).")
    if track is None:
        what = f"« {name} » " if name else ""
        warnings.append(f"{label} activée mais aucune piste {what}utilisable dans {prov.folder} : vidéo montée sans.")
    return track
