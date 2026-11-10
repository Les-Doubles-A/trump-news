from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.config.schema import AppConfig
from app.core.interfaces import IPublisher
from app.core.registry import Registry
from app.database.session import get_setting

INSTAGRAM_USER_SETTING = "publishing.instagram.user_id"   # trouvé par « connect instagram » (pas un secret)

PUBLISHERS: Registry[IPublisher] = Registry("publication")


def _youtube(cfg: AppConfig, factory):
    from app.providers.publishers.youtube import YouTubePublisher
    return YouTubePublisher(cfg.publishing.youtube)


def _tiktok(cfg: AppConfig, factory):
    from app.providers.publishers.tiktok import TikTokPublisher
    return TikTokPublisher(cfg.publishing.tiktok)


def _instagram(cfg: AppConfig, factory):
    from app.providers.publishers.instagram import InstagramPublisher
    user = cfg.publishing.instagram.user_id or (get_setting(factory, INSTAGRAM_USER_SETTING) if factory else "") or ""
    return InstagramPublisher(cfg.publishing.instagram, user)


PUBLISHERS.register("youtube", _youtube)
PUBLISHERS.register("tiktok", _tiktok)
PUBLISHERS.register("instagram", _instagram)


def build_publisher(name: str, cfg: AppConfig, factory: sessionmaker[Session] | None = None) -> IPublisher:
    return PUBLISHERS.create(name, cfg=cfg, factory=factory)
