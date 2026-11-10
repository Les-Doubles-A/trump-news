from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config.schema import NewsSection
from app.core.errors import NewsProviderError
from app.core.interfaces import INewsProvider, RawArticle
from app.database.models import Article, Story
from app.database.session import create_db_engine, init_db, session_scope
from app.news.cluster import cluster_items, ignore_tokens, similarity, title_tokens
from app.news.normalize import clean_text, compute_relevance, normalize
from app.news.scoring import ScoredArticle, score_story
from app.news.service import NewsIngestionService, mark_story_used
from app.news.sources import SourceResolver
from app.providers.news.gdelt import GDELTProvider
from app.providers.news.rss import GoogleNewsProvider, RSSProvider
from sqlalchemy import select

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CFG = NewsSection()

RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>T</title>
<item><title>Trump signs executive order on steel tariffs</title><link>https://www.bbc.co.uk/news/a1?utm_source=x</link>
<pubDate>Wed, 07 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;The &lt;b&gt;president&lt;/b&gt; signed it.&lt;/p&gt;</description>
<media:thumbnail url="https://img.example/1.jpg"/></item>
<item><title>No link here</title></item>
</channel></rss>"""

GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>Trump warns China over trade deal - Reuters</title><link>https://news.google.com/rss/articles/CBM1</link>
<pubDate>Wed, 07 Oct 2026 09:00:00 GMT</pubDate><description>&lt;a&gt;x&lt;/a&gt;</description>
<source url="https://www.reuters.com">Reuters</source></item></channel></rss>"""


def test_clean_text_and_relevance():
    assert clean_text("<p>Hello &amp; <b>bye</b></p>") == "Hello & bye"
    assert compute_relevance("Trump speaks", None, CFG) == 1.0
    assert compute_relevance("Markets rally", "Trump comments", CFG) == 0.6
    assert compute_relevance("White House statement", None, CFG) == 0.5
    assert compute_relevance("Cat videos", "nothing", CFG) == 0.0


def test_source_resolver_merges_same_publisher():
    r = SourceResolver(CFG)
    a = r.resolve("Reuters", "https://news.google.com/rss/articles/x")
    b = r.resolve("Reuters World", "https://www.reuters.com/world/a")
    assert a.key == b.key == "reuters" and a.reliability == 0.92
    assert r.resolve("BBC News", "https://www.bbc.co.uk/news/a").key == "bbc"
    assert r.resolve("AP", "https://apnews.com/x").key == "ap"
    assert r.resolve(None, "https://blog.unknownsite.com/p").reliability == 0.5
    assert r.resolve("Weird Paper", "https://news.google.com/x").key == "weirdpaper"


def test_normalize_drops_invalid_and_fills_dates():
    r = SourceResolver(CFG)
    ign = ignore_tokens(CFG)
    assert normalize(RawArticle("", "https://a.com/x"), r, CFG, NOW, ign) is None
    assert normalize(RawArticle("T", "ftp://a.com/x"), r, CFG, NOW, ign) is None
    n = normalize(RawArticle("Trump news", "https://www.a.com/x/?utm_source=z", summary="Trump news"), r, CFG, NOW, ign)
    assert n.published_at == NOW and n.summary is None


def test_similarity_and_clustering():
    ign = ignore_tokens(CFG)
    t = lambda s: title_tokens(s, ign)  # noqa: E731
    same = similarity(t("Trump signs executive order on steel tariffs"), t("President signs new executive order imposing steel tariffs"))
    assert same >= 0.5
    assert similarity(t("Trump says he will visit Japan next month"), t("Trump says tariffs on China will rise")) < 0.2
    titles = ["Trump signs executive order on steel tariffs", "Trump says he will visit Japan next month",
              "President signs new executive order imposing steel tariffs"]
    groups = cluster_items(titles, lambda a, b: similarity(t(a), t(b)), 0.5)
    assert sorted(len(g) for g in groups) == [1, 2]


def sa(title="Trump announces tariffs on steel", key="reuters", rel=0.9, age_h=1.0, summary=None, image=None):
    return ScoredArticle(title, summary, key, rel, NOW - timedelta(hours=age_h), image, 1.0)


def test_scoring_behaviour():
    base = score_story([sa()], CFG, NOW).total
    assert score_story([sa(age_h=24)], CFG, NOW).total < base                      # récence
    assert score_story([sa(), sa(key="bbc"), sa(key="ap")], CFG, NOW).total > base  # plusieurs sources
    assert score_story([sa(title="Trump reportedly may announce tariffs, sources say")], CFG, NOW).total < base  # incertitude
    assert score_story([sa(rel=0.2)], CFG, NOW).total < base                        # source faible
    same_src = score_story([sa(), sa(title="Trump announces steel tariffs again")], CFG, NOW)
    assert same_src.n_sources == 1 and same_src.components["-duplicates"] == 0.5   # même éditeur = 1 source
    assert 0 <= base <= 1


def test_scoring_weights_are_configurable():
    cfg = NewsSection.model_validate({"scoring": {"w_recency": 0.0}})
    assert score_story([sa(age_h=48)], cfg, NOW).total > score_story([sa(age_h=48)], CFG, NOW).total


@respx.mock
def test_rss_provider_parses_feed():
    respx.get("https://feed.example/rss").respond(200, text=RSS)
    items = RSSProvider("Feed", "https://feed.example/rss").fetch()
    assert len(items) == 1
    a = items[0]
    assert a.title.startswith("Trump signs") and a.published_at == datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    assert a.image_url == "https://img.example/1.jpg" and a.source == "Feed"


@respx.mock
def test_rss_errors_are_typed():
    respx.get("https://feed.example/500").respond(500)
    respx.get("https://feed.example/junk").respond(200, text="<html>pas un flux</html>")
    respx.get("https://feed.example/down").mock(side_effect=httpx.ConnectError("x"))
    for path in ("500", "junk", "down"):
        with pytest.raises(NewsProviderError):
            RSSProvider("F", f"https://feed.example/{path}").fetch()


@respx.mock
def test_google_news_strips_publisher_suffix():
    route = respx.get(url__startswith="https://news.google.com/rss/search").respond(200, text=GNEWS)
    a = GoogleNewsProvider("G", "Donald Trump").fetch()[0]
    assert a.title == "Trump warns China over trade deal" and a.source == "Reuters" and a.summary is None
    assert "q=Donald+Trump+when%3A1d" in str(route.calls[0].request.url)


@respx.mock
def test_gdelt_provider():
    respx.get("https://api.gdeltproject.org/api/v2/doc/doc").respond(200, json={"articles": [
        {"url": "https://x.com/a", "title": "Trump speaks", "seendate": "20261007T101500Z", "domain": "x.com"}, {"url": "", "title": "bad"}]})
    items = GDELTProvider("GD", "Trump").fetch()
    assert len(items) == 1 and items[0].published_at == datetime(2026, 10, 7, 10, 15, tzinfo=timezone.utc)
    respx.get("https://api.gdeltproject.org/api/v2/doc/doc").respond(200, text="Your query is too short")
    with pytest.raises(NewsProviderError):
        GDELTProvider("GD", "T").fetch()


class FakeProvider(INewsProvider):
    def __init__(self, name, items=None, error=None):
        self.name, self.items, self.error, self.calls = name, items or [], error, 0

    def fetch(self, limit=50):
        self.calls += 1
        if self.error:
            raise NewsProviderError(self.error)
        return list(self.items)


def raw(title, url, source, age_h=1.0, summary=None):
    return RawArticle(title, url, source, NOW - timedelta(hours=age_h), summary=summary)


@pytest.fixture()
def factory(tmp_path):
    return init_db(create_db_engine(tmp_path / "n.db"))


def make(factory, providers):
    return NewsIngestionService(CFG, providers, factory, now=lambda: NOW, sleep=lambda s: None)


def test_end_to_end_ingestion(factory):
    good = FakeProvider("A", [
        raw("Trump signs executive order on steel tariffs", "https://bbc.co.uk/1", "BBC News"),
        raw("Trump visits Japan next month", "https://bbc.co.uk/2", "BBC News"),
        raw("Cat show winners announced", "https://bbc.co.uk/3", "BBC News"),                 # hors sujet
        raw("Trump old story from last week", "https://bbc.co.uk/4", "BBC News", age_h=200),   # trop ancien
        raw("", "https://bbc.co.uk/5", "BBC News"),                                            # invalide
        raw("Trump signs executive order on steel tariffs", "https://bbc.co.uk/1?utm_source=z", "BBC News"),  # doublon exact
    ])
    bad = FakeProvider("B", error="HTTP 500")
    svc = make(factory, [good, bad])
    rep = svc.run()
    assert (rep.fetched, rep.invalid, rep.too_old, rep.irrelevant, rep.duplicates, rep.new_articles) == (6, 1, 1, 1, 1, 2)
    assert rep.stories_created == 2 and not rep.provider_status["B"].ok and rep.provider_status["A"].ok
    assert bad.calls == 3  # retries

    # 2e exécution : rien de nouveau
    rep2 = make(factory, [good]).run()
    assert rep2.new_articles == 0 and rep2.stories_created == 0

    # un autre éditeur couvre le même sujet -> même story, plus de sources
    other = FakeProvider("C", [raw("Trump signs new executive order imposing steel tariffs", "https://reuters.com/9", "Reuters")])
    before = {r.id: r for r in svc.list_stories()}
    rep3 = make(factory, [other]).run()
    assert rep3.stories_created == 0 and rep3.stories_updated == 1
    rows = svc.list_stories()
    steel = next(r for r in rows if "steel" in r.title.lower() or r.n_sources == 2)
    assert steel.n_sources == 2 and steel.score > before[steel.id].score
    assert rows[0].id == steel.id  # classé premier

    # sujet utilisé : masqué par défaut, mais les nouveaux articles s'y rattachent
    assert mark_story_used(factory, steel.id)
    assert steel.id not in [r.id for r in svc.list_stories()]
    assert steel.id in [r.id for r in svc.list_stories(include_used=True)]
    with session_scope(factory) as s:
        assert s.scalar(select(Story).where(Story.id == steel.id)).used_at is not None
        assert len(list(s.scalars(select(Article)))) == 3


def test_all_providers_failing_does_not_crash(factory):
    rep = make(factory, [FakeProvider("X", error="down")]).run()
    assert rep.fetched == 0 and not rep.provider_status["X"].ok


def test_config_validation():
    from app.config.loader import load_config
    from app.core.errors import ConfigError

    with pytest.raises(ConfigError):
        load_config(base_dir=__import__("pathlib").Path("."), environ={"TRUMPNEWS__NEWS__PROVIDERS": "[{type: rss, name: X}]"})
    assert len(NewsSection().providers) >= 5


class FakeEmbedder:
    """Vecteurs fixés à la main : les titres d'un même sujet pointent dans la même direction."""

    def __init__(self, mapping, fail=False):
        self.mapping, self.fail, self.calls = mapping, fail, 0

    def embed(self, texts, model=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("pas de modèle d'embedding")
        return [self.mapping.get(t, [0.0, 0.0, 1.0]) for t in texts]


ADV = [
    raw("Trump rebukes political advisers for taking other work during midterms", "https://wapo.com/1", "Washington Post"),
    raw("Trump threatens to fire political aides if their interests conflict", "https://reuters.com/2", "Reuters"),
    raw("Trump visits Japan next month", "https://bbc.co.uk/3", "BBC News"),
]
VEC = {ADV[0].title: [1.0, 0.1, 0.0], ADV[1].title: [0.95, 0.2, 0.0], ADV[2].title: [0.0, 1.0, 0.0]}


def test_semantic_clustering_merges_differently_worded_headlines(factory):
    svc = NewsIngestionService(CFG, [FakeProvider("P", ADV)], factory, now=lambda: NOW, sleep=lambda s: None,
                               embedder=FakeEmbedder(VEC))
    rep = svc.run()
    assert rep.semantic and rep.stories_created == 2
    top = svc.list_stories()
    assert sorted(r.n_sources for r in top) == [1, 2]


def test_lexical_fallback_when_embeddings_fail(factory):
    emb = FakeEmbedder(VEC, fail=True)
    svc = NewsIngestionService(CFG, [FakeProvider("P", ADV)], factory, now=lambda: NOW, sleep=lambda s: None, embedder=emb)
    rep = svc.run()
    assert not rep.semantic and rep.stories_created == 3  # pas de fusion sans embeddings, et pas de plantage


def test_later_article_joins_existing_story_semantically(factory):
    emb = FakeEmbedder(VEC)
    NewsIngestionService(CFG, [FakeProvider("P", ADV[:1])], factory, now=lambda: NOW, sleep=lambda s: None, embedder=emb).run()
    rep = NewsIngestionService(CFG, [FakeProvider("Q", ADV[1:2])], factory, now=lambda: NOW, sleep=lambda s: None, embedder=emb).run()
    assert rep.stories_created == 0 and rep.stories_updated == 1


def test_embeddings_can_be_disabled(factory):
    cfg = NewsSection.model_validate({"embeddings": {"enabled": False}})
    emb = FakeEmbedder(VEC)
    NewsIngestionService(cfg, [FakeProvider("P", ADV)], factory, now=lambda: NOW, sleep=lambda s: None, embedder=emb).run()
    assert emb.calls == 0
