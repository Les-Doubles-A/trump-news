"""Calculs de montage en fonctions pures (testables sans FFmpeg) : découpage des prises en images, transitions,
cadrage 9:16 (fond flouté), Ken Burns, graphe de filtres final (vidéo + sous-titres + audio)."""
from __future__ import annotations

from dataclasses import dataclass

from app.video.models import RenderJob, Shot

FILL_TOLERANCE = 0.12   # écart relatif de format accepté pour remplir l'écran sans fond flouté
# plage « TV » (16-235) imposée : un JPEG en plage pleine donnerait du yuvj420p, mal géré par certaines plateformes
TV_RANGE = "scale=out_range=tv,format=yuv420p,setparams=range=tv"


def shot_bounds(shots: list[Shot], duration: float) -> list[tuple[float, float]]:
    """Intervalle réellement affiché par chaque prise : de son début jusqu'au début de la suivante
    (les pauses entre segments restent sur l'image précédente), la première commence à 0, la dernière finit avec la voix."""
    out = []
    for i, s in enumerate(shots):
        a = 0.0 if i == 0 else s.start
        b = shots[i + 1].start if i + 1 < len(shots) else duration
        out.append((a, b))
    return out


@dataclass
class ClipSpec:
    index: int          # indice de la prise dans job.shots
    first: int          # première image (dans la vidéo finale) couverte par ce clip, transitions comprises
    frames: int         # nombre d'images du clip


@dataclass
class ClipPlan:
    clips: list[ClipSpec]
    transition_frames: int
    total_frames: int


def plan_clips(shots: list[Shot], duration: float, fps: int, transition_s: float) -> ClipPlan:
    """Découpe en images entières. Avec une transition de T images centrée sur chaque changement de prise,
    chaque clip déborde de T/2 de part et d'autre : la vidéo garde exactement la durée de la voix et chaque
    changement d'image tombe pile sur le début du segment correspondant."""
    total = max(1, round(duration * fps))
    starts: list[tuple[int, int]] = []          # (image de début, indice de prise)
    for i, s in enumerate(shots):
        f = 0 if not starts else round(s.start * fps)
        if f >= total - 1:                       # prise au-delà de la voix : ignorée
            break
        if starts and f <= starts[-1][0]:        # moins d'une image : la prise précédente est remplacée
            starts[-1] = (starts[-1][0], i)
            continue
        starts.append((f, i))
    if not starts:
        raise ValueError("aucune prise à monter")
    bounds = [f for f, _ in starts] + [total]
    min_len = min(b - a for a, b in zip(bounds, bounds[1:]))
    t = int(min(transition_s * fps, 0.4 * min_len)) // 2 * 2 if len(starts) > 1 else 0
    if t < 2:
        t = 0
    clips = []
    for k, (f, idx) in enumerate(starts):
        first = f - t // 2 if k > 0 else 0
        last = bounds[k + 1] + t // 2 if k + 1 < len(starts) else total
        clips.append(ClipSpec(idx, first, last - first))
    return ClipPlan(clips, t, total)


def fills_frame(width: int, height: int, out_w: int, out_h: int) -> bool:
    target = out_w / out_h
    return abs(width / max(1, height) - target) / target < FILL_TOLERANCE


KEN_BURNS = ("in", "out", "pan", "in_top")


def zoompan_expr(direction: str, zoom: float, frames: int) -> tuple[str, str, str]:
    """Expressions (z, x, y) de zoompan. Aucune virgule : pas d'échappement nécessaire dans le graphe."""
    d = max(1, frames - 1)
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if zoom <= 0:
        return "1", cx, cy
    if direction == "out":
        return f"{1 + zoom:.4f}-{zoom:.4f}*on/{d}", cx, cy
    if direction == "pan":
        return f"{1 + zoom:.4f}", f"(iw-iw/zoom)*on/{d}", cy
    if direction == "in_top":
        return f"1+{zoom:.4f}*on/{d}", cx, "(ih-ih/zoom)*0.3"
    return f"1+{zoom:.4f}*on/{d}", cx, cy


def shot_filter(shot: Shot, index: int, frames: int, job: RenderJob) -> str:
    """Graphe d'une prise : image fixe -> clip 9:16 animé de `frames` images ([v])."""
    w, h, t = job.width, job.height, job.template
    if shot.kind == "card" or fills_frame(shot.width, shot.height, w, h):
        base = f"[0:v]format=rgb24,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[base];"
    else:   # photo paysage/carrée : fond flouté et assombri + image entière centrée
        dim = f"{t.background_dim:.2f}"
        blur = f",gblur=sigma={t.blur_sigma:g}" if t.blur_sigma > 0 else ""
        base = (f"[0:v]format=rgb24,split=2[a][b];"
                f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}{blur},"
                f"colorchannelmixer=rr={dim}:gg={dim}:bb={dim}[bg];"
                f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base];")
    if shot.kind == "card":   # le texte d'une carte ne doit pas sortir du cadre : zoom avant léger, centré
        direction, zoom = "in", t.zoom * 0.4
    else:
        direction, zoom = KEN_BURNS[index % len(KEN_BURNS)], t.zoom
    up = 1 if job.preview else 2   # zoompan arrondit au pixel : travailler en 2x atténue le tremblement
    z, x, y = zoompan_expr(direction, zoom, frames)
    return (base + f"[base]scale={w * up}:{h * up},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={job.fps},{TV_RANGE}[v]")


def final_graph(job: RenderJob, plan: ClipPlan, *, voice_input: int, music_input: int | None,
                sfx_input: int | None, ass_name: str | None, fonts_dir: str | None) -> str:
    """Graphe final : clips -> transitions -> sous-titres -> barre de progression ; voix (+ musique en retrait
    sous la voix, + effets sur les transitions). Sorties : [vout] et [aout]."""
    fps, n, t = job.fps, len(plan.clips), plan.transition_frames
    parts = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps}[c{i}]" for i in range(n)]
    if n == 1:
        parts.append("[c0]null[vx]")
    elif t == 0:
        parts.append("".join(f"[c{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vx]")
    else:
        prev = "c0"
        for k in range(1, n):
            out = "vx" if k == n - 1 else f"x{k}"
            offset = plan.clips[k].first / fps
            parts.append(f"[{prev}][c{k}]xfade=transition={job.template.transition}:duration={t / fps:.4f}:"
                         f"offset={offset:.4f}[{out}]")
            prev = out
    v = "vx"
    if ass_name:
        fd = f":fontsdir={fonts_dir}" if fonts_dir else ""
        parts.append(f"[{v}]ass={ass_name}{fd}[vs]")
        v = "vs"
    if job.progress_bar:
        bar_h = max(4, round(10 * job.width / 1080))
        dur = plan.total_frames / fps
        parts.append(f"color=c=0x{job.template.progress_color}:s={job.width}x{bar_h}:r={fps}:d={dur:.4f}[bar]")
        parts.append(f"[{v}][bar]overlay=x='-w+W*t/{dur:.4f}':y=0:shortest=1[vb]")
        v = "vb"
    parts.append(f"[{v}]{TV_RANGE}[vout]")

    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    dur = plan.total_frames / fps
    mix = []
    if music_input is None and sfx_input is None:
        parts.append(f"[{voice_input}:a]{fmt}[aout]")
        return ";\n".join(parts)
    if music_input is not None:
        fade_out = max(0.0, dur - 1.5)
        parts.append(f"[{voice_input}:a]{fmt},asplit=2[vo][sc]")
        parts.append(f"[{music_input}:a]{fmt},atrim=0:{dur:.4f},asetpts=PTS-STARTPTS,volume={job.music_volume_db:g}dB,"
                     f"afade=t=in:d=1,afade=t=out:st={fade_out:.3f}:d=1.5[mus]")
        # ducking : la musique baisse automatiquement quand la voix parle
        parts.append("[mus][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck]")
        mix += ["[vo]", "[duck]"]
    else:
        parts.append(f"[{voice_input}:a]{fmt}[vo]")
        mix.append("[vo]")
    if sfx_input is not None and t > 0 and n > 1:
        hits = [plan.clips[k].first / fps for k in range(1, n)]
        labels = "".join(f"[s{k}]" for k in range(len(hits)))
        parts.append(f"[{sfx_input}:a]{fmt},volume={job.sfx_volume_db:g}dB,asplit={len(hits)}{labels}"
                     if len(hits) > 1 else f"[{sfx_input}:a]{fmt},volume={job.sfx_volume_db:g}dB[s0]")
        for k, at in enumerate(hits):
            parts.append(f"[s{k}]adelay={round(at * 1000)}:all=1[d{k}]")
            mix.append(f"[d{k}]")
    if len(mix) == 1:
        parts.append(f"{mix[0]}anull[mix]")
    else:
        parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[mix]")
    parts.append("[mix]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]")
    return ";\n".join(parts)
