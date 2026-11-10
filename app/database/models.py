"""Modèles SQLAlchemy. Statuts stockés en texte pour rester lisibles dans SQLite."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VideoStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    RENDERING = "RENDERING"
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class PublicationStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class FactStatus(str, enum.Enum):
    CONFIRMED = "CONFIRMED"
    LIKELY = "LIKELY"
    REPORTED = "REPORTED"
    DISPUTED = "DISPUTED"
    UNKNOWN = "UNKNOWN"


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(50), default="media")  # agency | media | official | social
    reliability: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Story(Base):
    __tablename__ = "stories"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    topic: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    articles: Mapped[list["Article"]] = relationship(back_populates="story")


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(1000))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    source_name: Mapped[str | None] = mapped_column(String(200))
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(2000))
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id"))
    score: Mapped[float | None] = mapped_column(Float)
    fact_status: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    story: Mapped[Story | None] = relationship(back_populates="articles")


class Script(Base):
    __tablename__ = "scripts"
    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    style: Mapped[str] = mapped_column(String(50))
    tone: Mapped[str] = mapped_column(String(30))
    language: Mapped[str] = mapped_column(String(5))
    duration_target: Mapped[int] = mapped_column(Integer)
    hook: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    ending: Mapped[str | None] = mapped_column(Text)
    full_script: Mapped[str | None] = mapped_column(Text)
    claims: Mapped[list | None] = mapped_column(JSON)
    source_refs: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"))
    video_path: Mapped[str | None] = mapped_column(String(1000))
    template: Mapped[str | None] = mapped_column(String(50))
    duration: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default=VideoStatus.DRAFT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VideoReport(Base):
    """Rapport de montage et de contrôle qualité d'une vidéo (nouvelle table : pas de migration nécessaire)."""
    __tablename__ = "video_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    """Relecture humaine d'un script (nouvelle table : pas de migration nécessaire). Trace qui a décidé, quand et pourquoi."""
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), index=True)
    decision: Mapped[str] = mapped_column(String(20))            # APPROVED | REJECTED
    note: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(100))
    sources_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    factcheck_verdict: Mapped[str | None] = mapped_column(String(20))   # verdict au moment de la décision
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"))
    platform: Mapped[str] = mapped_column(String(30))
    platform_video_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default=PublicationStatus.PENDING.value)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(String(2000))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderRecord(Base):
    __tablename__ = "providers"
    __table_args__ = (UniqueConstraint("kind", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # llm | news | tts | visual | music | publisher
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict | None] = mapped_column(JSON)  # jamais de secrets ici
    status: Mapped[str | None] = mapped_column(String(50))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LogEntry(Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10))
    logger: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(50), index=True)
    extra: Mapped[dict | None] = mapped_column(JSON)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    run_at: Mapped[str] = mapped_column(String(10))  # HH:MM
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSON)


class FactCheck(Base):
    """Résultat d'une vérification de sujet. Nouvelle table : créée automatiquement sans migration."""

    __tablename__ = "fact_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))
    verdict: Mapped[str] = mapped_column(String(20))  # SAFE | CAUTION | BLOCKED
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Narration(Base):
    """Voix off générée pour un script. Nouvelle table : créée automatiquement sans migration."""

    __tablename__ = "narrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    voice: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(5))
    audio_path: Mapped[str] = mapped_column(String(1000))
    duration: Mapped[float] = mapped_column(Float)
    timeline: Mapped[list] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VisualAssetRow(Base):
    """Image mise en cache (avec licence et crédit). Nouvelle table : créée automatiquement."""

    __tablename__ = "visual_assets"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(String(1000))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    license: Mapped[str | None] = mapped_column(String(200))
    attribution: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VisualPlanRow(Base):
    __tablename__ = "visual_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"), index=True)
    narration_id: Mapped[int | None] = mapped_column(ForeignKey("narrations.id"))
    plan: Mapped[dict] = mapped_column(JSON)
    ai_disclosure: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledRun(Base):
    """Un lancement du pilote (planifié par Windows ou « run-now »). Nouvelle table : pas de migration."""
    __tablename__ = "scheduled_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))       # scheduled | manual
    status: Mapped[str] = mapped_column(String(20))        # RUNNING | OK | NOTHING | FAILED | PREFLIGHT | LOCKED | INTERRUPTED
    published: Mapped[int] = mapped_column(Integer, default=0)
    autopilot_run_id: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutopilotAttempt(Base):
    """Sujet essayé par le pilote automatique (nouvelle table : pas de migration). Un sujet essayé n'est jamais retenté."""
    __tablename__ = "autopilot_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    status: Mapped[str] = mapped_column(String(30))     # PUBLISHED | PENDING | SKIPPED | FAILED | CANCELLED
    video_id: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(2000))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
