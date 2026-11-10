from __future__ import annotations

from app.config.schema import NewsProviderSpec, NewsSection
from app.core.interfaces import INewsProvider
from app.core.registry import Registry
from app.providers.news.gdelt import GDELTProvider
from app.providers.news.rss import GoogleNewsProvider, RSSProvider

news_registry: Registry[INewsProvider] = Registry("actualités")
news_registry.register("rss", lambda spec: RSSProvider(spec.name, spec.url))
news_registry.register("google_news", lambda spec: GoogleNewsProvider(spec.name, spec.query, spec.language, spec.country))
news_registry.register("gdelt", lambda spec: GDELTProvider(spec.name, spec.query, spec.language))


def build_news_providers(cfg: NewsSection) -> list[INewsProvider]:
    specs: list[NewsProviderSpec] = [p for p in cfg.providers if p.enabled]
    return [news_registry.create(s.type, spec=s) for s in specs]
