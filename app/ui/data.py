"""Lectures en base pour l'interface (sans Qt : testables seules). Rapides : exécutées dans le fil de l'interface."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.core.context import AppContext
from app.database.models import Script, Story, Video
from app.database.session import session_scope
from app.utils.timeutil import utcnow


@dataclass
class Counts:
    stories_24h: int
    to_review: int
    videos_ready: int
    videos_review: int


def counts(ctx: AppContext) -> Counts:
    since = utcnow() - timedelta(hours=24)
    with session_scope(ctx.factory) as s:
        def n(q):
            return s.scalar(q) or 0
        return Counts(
            n(select(func.count(Story.id)).where(Story.created_at >= since)),
            n(select(func.count(Script.id)).where(Script.status == "NEEDS_REVIEW")),
            n(select(func.count(Video.id)).where(Video.status == "READY")),
            n(select(func.count(Video.id)).where(Video.status == "NEEDS_REVIEW")),
        )


def scripts(ctx: AppContext, limit: int = 200) -> list[Script]:
    with session_scope(ctx.factory) as s:
        return list(s.scalars(select(Script).order_by(Script.id.desc()).limit(limit)))


def videos(ctx: AppContext, limit: int = 200) -> list[Video]:
    with session_scope(ctx.factory) as s:
        return list(s.scalars(select(Video).order_by(Video.id.desc()).limit(limit)))
