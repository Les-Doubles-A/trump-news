import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config.schema import AppConfig, FactCheckSection
from app.core.interfaces import ILLMProvider, LLMResponse
from app.database.models import Article, Story
from app.database.session import create_db_engine, init_db, session_scope
from app.factcheck.assess import assess, independent_origins, verdict_for
from app.factcheck.fetcher import ArticleTextFetcher
from app.factcheck.grounding import normalize, numbers_grounded, quote_in_text
from app.factcheck.models import ClaimResult, FactCheckResult, SourceDoc
from app.factcheck.service import LLMFactChecker
from app.prompts import render_prompt
from app.utils.json_tools import inline_refs

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
FC = FactCheckSection()


# ---------------------------------------------------------------- grounding
def test_quote_in_text_exact_fuzzy_and_fabricated():
    text = "Trump told reporters on Tuesday that the U.S. will not attack Iran before the November midterm elections."
    assert quote_in_text("the U.S. will not attack Iran before the November midterm elections", text)
    assert quote_in_text("The US will not attack Iran before the November midterm election", text)  # petite variation
    assert not quote_in_text("the U.S. will launch strikes on Iran next week", text)               # inventée
    assert not quote_in_text("will not attack", text)                                               # trop courte
    assert normalize("“Hello”—world’s") == normalize('"hello" world\'s')


def test_numbers_grounded():
    assert numbers_grounded("Trump announces $18 million relief", "He announced $18 million in relief")
    assert not numbers_grounded("Oil hits $150", "Oil prices hit $105 on Tuesday")
    assert numbers_grounded("About 1,200 people", "1200 people attended")


# ------------------------------------------------------------------ assess
def doc(i, key, rel=0.8, level="summary"):
    return SourceDoc(f"S{i}", i, key, key, f"https://{key}.com/{i}", "t", "d", "x", level, rel)


LONG = "The administration said it will impose new tariffs on imported steel starting next month according to officials"


def test_assess_status_rules():
    s3 = [(doc(1, "reuters", 0.92), "q one"), (doc(2, "bbc", 0.88), "q two"), (doc(3, "npr", 0.85), "q three")]
    assert assess("fact", s3, [], FC)[0] == "CONFIRMED"
    assert assess("fact", s3[:2], [], FC)[0] == "LIKELY"
    assert assess("fact", s3[:1], [], FC)[0] == "REPORTED"
    assert assess("fact", [], [], FC)[0] == "UNKNOWN"
    assert assess("fact", s3, [(doc(4, "foxnews"), "x")], FC)[0] == "DISPUTED"
    assert assess("allegation", s3, [], FC)[0] == "REPORTED"   # une allégation n'est jamais confirmée
    assert assess("opinion", s3, [], FC)[0] == "REPORTED"
    low = [(doc(i, k, 0.4), "q") for i, k in enumerate(["a", "b", "c"], 1)]
    assert assess("fact", low, [], FC)[0] == "LIKELY"          # 3 sources peu fiables : pas CONFIRMED


def test_syndicated_wire_counts_once():
    sup = [(doc(1, "bbc"), LONG), (doc(2, "nbcnews"), LONG), (doc(3, "cbsnews"), LONG)]
    assert independent_origins("fact", sup) == 1
    assert independent_origins("statement", sup) == 3   # citer la même déclaration de Trump = plusieurs médias
    status, _, notes = assess("fact", sup, [], FC)
    assert status == "REPORTED" and "dépêche" in notes


def test_verdicts():
    c = lambda st, kind="fact", conf=0.8: ClaimResult("a b c d", kind, st, conf)  # noqa: E731
    assert verdict_for([])[0] == "BLOCKED"
    assert verdict_for([c("CONFIRMED")])[0] == "SAFE"
    assert verdict_for([c("REPORTED")])[0] == "CAUTION"
    assert verdict_for([c("DISPUTED"), c("CONFIRMED")])[0] == "CAUTION"
    assert verdict_for([c("LIKELY", "opinion")])[0] == "CAUTION"


# ---------------------------------------------------------- prompts / schema
def test_prompt_renders_and_is_strict():
    msgs = render_prompt("fact_check", story_title="T", max_claims=5, no_think=True,
                         sources=[{"id": "S1", "name": "BBC", "title": "H", "published": "d", "text": "body", "level": "title"}])
    assert msgs[0].role == "system" and "between 4 and 5 distinct claims" in msgs[0].content
    assert "[S1] BBC" in msgs[1].content and "TITLE ONLY" in msgs[1].content and msgs[1].content.endswith("/no_think")
    from app.core.errors import ConfigError
    with pytest.raises(ConfigError):
        render_prompt("fact_check", story_title="T")  # variable manquante : erreur claire


def test_inline_refs_removes_defs():
    from app.factcheck.service import Extraction

    schema = inline_refs(Extraction.model_json_schema())
    assert "$defs" not in schema and "$ref" not in json.dumps(schema)


# ------------------------------------------------------------------ fetcher
ARTICLE = "<html><body><article><h1>Title</h1>" + "".join(
    f"<p>Paragraph {i}: the president announced a policy and officials explained its details to reporters at length.</p>" for i in range(12)
) + "</article></body></html>"


@respx.mock
def test_fetcher_respects_robots_and_extracts():
    respx.get("https://ok.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /private/")
    respx.get("https://ok.example/news/a").respond(200, text=ARTICLE, headers={"content-type": "text/html"})
    f = ArticleTextFetcher()
    text = f.fetch("https://ok.example/news/a")
    assert text and "president announced" in text
    assert f.fetch("https://ok.example/private/x") is None           # interdit par robots.txt
    assert f.fetch("https://news.google.com/rss/articles/abc") is None  # redirection Google : ignorée


@respx.mock
def test_fetcher_declines_when_unsure():
    respx.get("https://down.example/robots.txt").respond(503)
    assert ArticleTextFetcher().fetch("https://down.example/a") is None  # robots indisponible : on s'abstient
    respx.get("https://wall.example/robots.txt").respond(404)
    respx.get("https://wall.example/a").respond(403)
    assert ArticleTextFetcher().fetch("https://wall.example/a") is None  # refus : pas de contournement
    respx.get("https://thin.example/robots.txt").respond(404)
    respx.get("https://thin.example/a").respond(200, text="<html><body><p>Subscribe to read.</p></body></html>")
    assert ArticleTextFetcher().fetch("https://thin.example/a") is None


# ------------------------------------------------------------------ service
class FakeLLM(ILLMProvider):
    name, model = "fake", "fake-model"

    def __init__(self, payload):
        self.payload, self.calls, self.last_messages = payload, 0, None

    def health_check(self): ...
    def list_models(self): return ["fake-model"]

    def complete(self, messages, **kw):
        self.calls += 1
        self.last_messages = messages
        return LLMResponse(json.dumps(self.payload), "fake-model")


@pytest.fixture()
def env(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "f.db"))
    with session_scope(factory) as s:
        st = Story(title="Trump says US will not attack Iran before midterms", status="NEW")
        s.add(st)
        s.flush()
        rows = [
            ("Trump says US will not attack Iran before midterms", "https://bbc.co.uk/1", "BBC News", "Trump told reporters on Tuesday the U.S. will not attack Iran before the November midterm elections."),
            ("No new attack on Iran before midterms, Trump says", "https://reuters.com/2", "Reuters", "President Trump said the United States will not attack Iran before the midterm elections in November, the White House said."),
            ("Trump says no Iran strike before midterms", "https://npr.org/3", "NPR", "In remarks at the White House Trump said no strike on Iran would happen before November's midterms."),
            ("Trump Is Considering a Plan to Strike Iran Before the Midterms", "https://nytimes.com/4", "The New York Times", "Advisers are discussing a plan to strike Iran before the midterm elections, according to two officials."),
        ]
        for i, (t, u, src, summ) in enumerate(rows):
            s.add(Article(url=u, title=t, source_name=src, summary=summ, hash=f"h{i}", story_id=st.id,
                          published_at=NOW - timedelta(hours=i)))
        sid = st.id
    cfg = AppConfig.model_validate({"factcheck": {"fetch_full_text": False}})
    return cfg, factory, sid


def payload(*claims):
    return {"claims": list(claims)}


GOOD = {
    "claim": "Trump said the US will not attack Iran before the November midterm elections.",
    "kind": "statement", "speaker": "Trump",
    "supporting": [
        {"source": "S1", "quote": "the United States will not attack Iran before the midterm elections in November"},  # Reuters
        {"source": "S2", "quote": "the U.S. will not attack Iran before the November midterm elections"},               # BBC
        {"source": "S3", "quote": "no strike on Iran would happen before November's midterms"},
    ],
    "contradicting": [{"source": "S4", "quote": "discussing a plan to strike Iran before the midterm elections"}],
}


def srcs(result):
    return {d["id"]: d["key"] for d in result.sources}


def test_fact_check_end_to_end(env):
    cfg, factory, sid = env
    llm = FakeLLM(payload(GOOD))
    res = LLMFactChecker(cfg, llm, factory).check(sid)
    assert len(res.claims) == 1 and not res.dropped
    c = res.claims[0]
    assert srcs(res) == {"S1": "reuters", "S2": "bbc", "S3": "npr", "S4": "nytimes"}  # classées par fiabilité
    assert c.kind == "statement" and c.status == "DISPUTED"          # contredit par le NYT
    assert len(c.supporting) == 3 and c.contradicting[0].source_key == "nytimes"
    assert res.verdict == "CAUTION" and "Présenter les deux versions" in c.phrasing
    sent = llm.last_messages[1].content
    assert "[S1]" in sent and "/no_think" in sent


def test_hallucinated_quote_and_number_are_rejected(env):
    cfg, factory, sid = env
    fake_quote = {"claim": "Trump announced a ground invasion of Iran next week.", "kind": "fact",
                  "supporting": [{"source": "S1", "quote": "Trump announced a ground invasion of Iran next week"}]}
    bad_number = {"claim": "Trump said 300 troops will deploy to the region.", "kind": "statement",
                  "supporting": [{"source": "S1", "quote": "President Trump said the United States will not attack Iran"}]}
    bad_source = {"claim": "Trump said the US will not attack Iran before the midterm elections.", "kind": "statement",
                  "supporting": [{"source": "S9", "quote": "the U.S. will not attack Iran before the November midterm elections"}]}
    res = LLMFactChecker(cfg, FakeLLM(payload(fake_quote, bad_number, bad_source)), factory).check(sid)
    assert res.claims == [] and res.verdict == "BLOCKED" and len(res.dropped) == 3
    joined = " ".join(res.warnings)
    assert "Citation introuvable" in joined and "Chiffre" in joined and "inexistante" in joined


def test_duplicates_merge_and_confirmed_status(env):
    cfg, factory, sid = env
    a = {**GOOD, "contradicting": [], "supporting": GOOD["supporting"][:2]}
    b = {**GOOD, "contradicting": [], "supporting": GOOD["supporting"][2:]}
    res = LLMFactChecker(cfg, FakeLLM(payload(a, b)), factory).check(sid)
    assert len(res.claims) == 1 and len(res.claims[0].supporting) == 3
    assert res.claims[0].status == "CONFIRMED" and res.verdict == "SAFE"


def test_cache_and_force(env):
    cfg, factory, sid = env
    llm = FakeLLM(payload(GOOD))
    fcx = LLMFactChecker(cfg, llm, factory)
    first = fcx.check(sid)
    second = fcx.check(sid)
    assert llm.calls == 1 and second.from_cache and second.claims[0].claim == first.claims[0].claim
    fcx.check(sid, force=True)
    assert llm.calls == 2
    from app.factcheck.service import latest_verdict
    assert latest_verdict(factory, sid)[0] == "CAUTION"
    with session_scope(factory) as s:
        from sqlalchemy import select
        assert {a.fact_status for a in s.scalars(select(Article))} == {"CAUTION"}


def test_unknown_story(env):
    from app.core.errors import TrumpNewsError

    cfg, factory, _ = env
    with pytest.raises(TrumpNewsError):
        LLMFactChecker(cfg, FakeLLM(payload()), factory).check(999)


def test_result_roundtrip():
    r = FactCheckResult(1, "t", "SAFE", 0.8, "m", [ClaimResult("a b c d", "fact", "CONFIRMED", 0.9)], [{"id": "S1"}])
    assert FactCheckResult.from_dict(r.to_dict(), from_cache=True).claims[0].status == "CONFIRMED"


def test_question_headline_is_not_a_claim(env):
    cfg, factory, sid = env
    q = {"claim": "Trump is visiting the Syracuse area this week.", "kind": "fact",
         "supporting": [{"source": "S2", "quote": "Why is Trump in Upstate New York?"}]}
    res = LLMFactChecker(cfg, FakeLLM(payload(q)), factory).check(sid)
    assert res.claims == [] and res.verdict == "BLOCKED"
    assert any("question" in w for w in res.warnings)


def test_single_title_only_source_is_blocked(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "t.db"))
    with session_scope(factory) as s:
        st = Story(title="Trump heads to Texas as midterm woes deepen", status="NEW")
        s.add(st)
        s.flush()
        s.add(Article(url="https://news.google.com/rss/articles/x", title="Trump heads to Texas as Republican midterm woes deepen",
                      source_name="The Guardian", hash="g1", story_id=st.id, published_at=NOW))
        sid = st.id
    cfg = AppConfig.model_validate({"factcheck": {"fetch_full_text": False}})
    c = {"claim": "Trump is heading to Texas as Republican midterm concerns deepen.", "kind": "fact",
         "supporting": [{"source": "S1", "quote": "Trump heads to Texas as Republican midterm woes deepen"}]}
    res = LLMFactChecker(cfg, FakeLLM(payload(c)), factory).check(sid)
    assert len(res.claims) == 1 and res.claims[0].status == "REPORTED"
    assert res.verdict == "BLOCKED" and any("titre" in w for w in res.warnings)


def test_result_records_actual_model(env):
    cfg, factory, sid = env
    llm = FakeLLM(payload(GOOD))
    llm.last_model = "qwen2.5-7b-instruct"
    assert LLMFactChecker(cfg, llm, factory).check(sid).model == "qwen2.5-7b-instruct"


def test_result_reports_llm_claim_count_and_source_sizes(env):
    cfg, factory, sid = env
    short = {"claim": "Too short", "kind": "fact", "supporting": []}
    res = LLMFactChecker(cfg, FakeLLM(payload(GOOD, short)), factory).check(sid)
    assert res.llm_claims == 2 and len(res.claims) == 1
    assert all(x["chars"] > 0 for x in res.sources)


# ------------------------------------------------- la citation doit soutenir l'affirmation
def test_unsupported_terms_rules():
    from app.factcheck.grounding import shares_content, unsupported_terms

    ig = {"trump", "donald", "president"}
    google = "I don't think I could have done this in my first term. Google wouldn't have taken my call in the first."
    assert unsupported_terms("Trump mentioned changing the name of the Gulf of Mexico to the Gulf of America.", google, ig) \
        == ["Gulf", "Mexico", "America"]                                      # cas réel : citation hors sujet
    assert unsupported_terms("Trump visited Ohio on Monday.", "Trump visits Kentucky for 13th rally", ig) == ["Ohio"]
    assert unsupported_terms("Trump also referenced changing the name of Lake Ontario to Lake America.",
                             "I also changed the name of a lake in Canada to the Lake America, referring to Lake Ontario.", ig) == []
    assert unsupported_terms("The U.S. imposed tariffs on Canada and Mexico.",
                             "the United States imposed tariffs on Canadian and Mexican goods", ig) == []   # équivalents, familles
    assert unsupported_terms("Trump said the US will not attack Iran.", "no strike on Iran would happen", ig) == []  # 1 nom sur 2
    assert shares_content("Trump claimed things are unbelievable.", "I can do stuff that is unbelievable.", ig)
    assert not shares_content("Trump praised the economy.", "We had a great crowd tonight.", ig)


def test_off_topic_quote_is_rejected(env):
    cfg, factory, sid = env
    off = {"claim": "Trump said NATO allies will pay for a new base in Poland.", "kind": "statement", "speaker": "Trump",
           "supporting": [{"source": "S2", "quote": "the U.S. will not attack Iran before the November midterm elections"}],  # BBC
           "contradicting": []}
    res = LLMFactChecker(cfg, FakeLLM(payload(GOOD, off)), factory).check(sid)
    assert [c.claim for c in res.claims] == [GOOD["claim"]]
    assert res.dropped[0]["claim"] == off["claim"] and "sur le sujet" in res.dropped[0]["reason"]
    assert any("hors sujet" in w and "NATO" in w for w in res.warnings)
    cfg2 = AppConfig.model_validate({"factcheck": {"fetch_full_text": False, "require_quote_support": False}})
    res2 = LLMFactChecker(cfg2, FakeLLM(payload(GOOD, off)), factory).check(sid, force=True)
    assert len(res2.claims) == 2                                             # règle désactivable
