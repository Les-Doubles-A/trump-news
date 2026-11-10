"""État des services externes (tableau de bord de l'interface, et plus tard du scheduler). Ne lève jamais d'exception."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.core.context import AppContext
from app.utils.sysmem import memory_status

MIN_COMMIT_GB = 7.0      # un modèle 8b + son contexte réserve ~6 Go


@dataclass
class ServiceStatus:
    key: str
    label: str
    ok: bool
    detail: str
    warning: bool = False      # fonctionne, mais à surveiller


def _safe(key: str, label: str, fn: Callable[[], ServiceStatus]) -> ServiceStatus:
    try:
        return fn()
    except Exception as exc:   # un test d'état ne doit jamais faire tomber l'interface
        return ServiceStatus(key, label, False, f"erreur : {exc}"[:200])


def check_llm(ctx: AppContext) -> ServiceStatus:
    def run():
        st = ctx.llm().health_check()
        model = ctx.cfg.llm.model
        detail = st.detail + ("" if model == "auto" else f" · modèle {model}")
        if st.ok and model != "auto" and st.models and model not in st.models:
            return ServiceStatus("llm", "LM Studio", True, f"modèle {model} introuvable dans LM Studio", warning=True)
        return ServiceStatus("llm", "LM Studio", st.ok, detail)
    return _safe("llm", "LM Studio", run)


def check_ffmpeg(ctx: AppContext) -> ServiceStatus:
    def run():
        from app.providers.video.ffmpeg_renderer import FFmpegRenderer
        st = FFmpegRenderer(ctx.cfg.video.ffmpeg_path).health_check()
        return ServiceStatus("ffmpeg", "FFmpeg", st.ok, st.detail)
    return _safe("ffmpeg", "FFmpeg", run)


def check_tts(ctx: AppContext) -> ServiceStatus:
    def run():
        from app.providers.tts.factory import build_tts_provider
        name = ctx.cfg.tts.provider
        st = build_tts_provider(name, ctx.cfg).health_check()
        return ServiceStatus("tts", f"Voix ({name})", st.ok, st.detail)
    return _safe("tts", "Voix", run)


def check_wikimedia(ctx: AppContext) -> ServiceStatus:
    def run():
        w = ctx.cfg.visuals.wikimedia
        if not w.enabled:
            return ServiceStatus("wikimedia", "Wikimedia", True, "désactivé (cartes uniquement)", warning=True)
        if not w.contact:
            return ServiceStatus("wikimedia", "Wikimedia", False, "contact manquant : Wikimedia refuse les requêtes (403). "
                                 "run.bat set-option visuals.wikimedia.contact votre@email")
        from app.providers.visuals.wikimedia import WikimediaProvider
        st = WikimediaProvider(w.contact).health_check()
        return ServiceStatus("wikimedia", "Wikimedia", st.ok, st.detail)
    return _safe("wikimedia", "Wikimedia", run)


def check_memory(status_fn=memory_status) -> ServiceStatus:
    def run():
        m = status_fn()
        if m is None:
            return ServiceStatus("memory", "Mémoire", True, "non mesurée sur ce système")
        detail = f"{m.commit_free_gb:.1f} Go réservables sur {m.commit_limit_gb:.1f} Go"
        if m.commit_free_gb < MIN_COMMIT_GB:
            hint = ("aucun fichier de pagination : activez-le (sysdm.cpl > Avancé > Performances > Mémoire virtuelle)"
                    if not m.has_pagefile else "fermez des programmes (WSL/Docker, navigateur)")
            return ServiceStatus("memory", "Mémoire", True, f"{detail} : si le modèle LM Studio n'est pas déjà chargé, "
                                 f"il risque de ne pas pouvoir l'être ; {hint}", warning=True)
        return ServiceStatus("memory", "Mémoire", True, detail)
    return _safe("memory", "Mémoire", run)


def check_all(ctx: AppContext) -> list[ServiceStatus]:
    return [check_llm(ctx), check_ffmpeg(ctx), check_tts(ctx), check_wikimedia(ctx), check_memory()]
