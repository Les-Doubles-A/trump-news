"""Contexte applicatif partagé par le CLI et l'interface : configuration, base, dossiers et fabriques des services.

Une seule façon de construire chaque service : l'interface (Phase 8) et le scheduler (Phase 10) obtiennent
exactement les mêmes objets que les commandes `run.bat`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config.loader import load_config
from app.config.schema import AppConfig
from app.core.interfaces import ILLMProvider
from app.database.session import DBLogHandler, create_db_engine, init_db
from app.utils.logging_setup import setup_logging


@dataclass
class AppContext:
    cfg: AppConfig
    factory: sessionmaker[Session]
    data_root: Path
    base_dir: Path
    config_path: str | None = None
    _llm: ILLMProvider | None = field(default=None, repr=False)

    @classmethod
    def load(cls, config_path: str | None = None, base_dir: Path | None = None, *, with_logging: bool = True,
             extra_log_handlers: tuple[logging.Handler, ...] = ()) -> "AppContext":
        base = Path(base_dir or Path.cwd())
        cfg = load_config(config_path, base_dir=base)
        data_root = cfg.storage.ensure_dirs(base)
        factory = init_db(create_db_engine(cfg.storage.database_path(base)))
        if with_logging:
            setup_logging(cfg.app.log_level, data_root / "logs", DBLogHandler(factory), extra=extra_log_handlers)
        return cls(cfg, factory, data_root, base, config_path)

    def reload_config(self) -> AppConfig:
        """Relit config.yaml + .env (après un changement de réglage) ; les services créés ensuite en tiennent compte."""
        self.cfg = load_config(self.config_path, base_dir=self.base_dir)
        self._llm = None
        return self.cfg

    # ------------------------------------------------------------ fabriques
    def llm(self) -> ILLMProvider:
        if self._llm is None:
            from app.providers.llm.factory import build_llm_provider
            self._llm = build_llm_provider(self.cfg.llm)
        return self._llm

    def news_service(self):
        from app.news.service import NewsIngestionService
        from app.providers.news.factory import build_news_providers

        providers = build_news_providers(self.cfg.news)
        embedder = self.llm() if self.cfg.news.embeddings.enabled else None
        return NewsIngestionService(self.cfg.news, providers, self.factory, embedder=embedder), providers

    def fact_checker(self):
        from app.factcheck.fetcher import ArticleTextFetcher
        from app.factcheck.service import LLMFactChecker

        return LLMFactChecker(self.cfg, self.llm(), self.factory, ArticleTextFetcher())

    def script_generator(self):
        from app.script.service import ScriptGenerator

        return ScriptGenerator(self.cfg, self.llm(), self.factory)

    def narration(self):
        from app.audio.narration import NarrationService

        return NarrationService(self.cfg, self.factory, self.data_root)

    def visual_planner(self):
        from app.visuals.planner import VisualPlanner

        return VisualPlanner(self.cfg, self.factory, self.data_root)

    def video_service(self):
        from app.video.service import VideoService

        return VideoService(self.cfg, self.factory, self.data_root, base_dir=self.base_dir)

    def reviews(self):
        from app.review.service import ReviewService

        return ReviewService(self.factory, video_service_factory=self.video_service)

    def reviews_as(self, reviewer: str):
        """Relecture au nom d'un automate (pilote automatique), tracée comme telle dans la table reviews."""
        from app.review.service import ReviewService

        return ReviewService(self.factory, video_service_factory=self.video_service, reviewer=reviewer)

    def publisher(self, name: str):
        from app.providers.publishers.factory import build_publisher

        return build_publisher(name, self.cfg, self.factory)

    def publishing(self):
        from app.publishing.service import PublishingService

        return PublishingService(self.cfg, self.factory, self.publisher)

    def scheduler(self):
        from app.scheduler.service import SchedulerService

        return SchedulerService(self)

    def autopilot(self, *, progress=None, token=None):
        from app.autopilot.service import Autopilot

        return Autopilot(self, progress=progress, token=token)
