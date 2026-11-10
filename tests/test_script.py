import json
from types import SimpleNamespace as NS

import pytest

from app.config.schema import AppConfig, ScriptSection
from app.core.errors import TrumpNewsError
from app.core.interfaces import ILLMProvider, LLMResponse
from app.database.models import FactCheck, Script, Story
from app.database.session import create_db_engine, init_db, session_scope
from app.factcheck.models import ClaimResult, Evidence, FactCheckResult
from app.script.models import ClaimInfo, ScriptBlockedError
from app.script.service import ScriptGenerator
from app.script.validate import count_words, target_words, validate

SC = ScriptSection()
TITLE = "Trump says US will not attack Iran before midterms"
CLAIMS = {
    "C1": ClaimInfo("C1", "Trump said the US will not attack Iran before the November midterm elections.", "statement", "LIKELY", "Trump", ["Reuters", "BBC News"]),
    "C2": ClaimInfo("C2", "A federal judge extended a temporary block on the media ban.", "fact", "CONFIRMED", None, ["Reuters", "BBC News", "NPR"]),
    "C3": ClaimInfo("C3", "Advisers are discussing a plan to strike Iran before the midterms, according to two officials.", "allegation", "REPORTED", None, ["The New York Times"]),
    "C4": ClaimInfo("C4", "Oil prices hit $105 on Tuesday.", "fact", "DISPUTED", None, ["Reuters", "CNN"]),
}


def seg(section, type_, text, *claims):
    return NS(section=section, type=type_, text=text, claims=list(claims))


def good():
    return [
        seg("hook", "attributed", "Trump says the US will not attack Iran before the November midterm elections.", "C1"),
        seg("context", "context", "Here is what we know so far."),
        seg("main", "fact", "A federal judge extended a temporary block on the media ban.", "C2"),
        seg("main", "attributed", "According to the New York Times, advisers are discussing a plan to strike Iran before the midterms.", "C3"),
        seg("details", "disputed", "Reports differ on oil prices, however: Reuters says they hit $105 on Tuesday.", "C4"),
        seg("conclusion", "context", "We will keep following this story."),
    ]


def check(segs, **kw):
    args = dict(story_title=TITLE, language="en", tone="neutral", duration_s=30, cfg=SC)
    args.update(kw)
    return validate(segs, CLAIMS, **args)


def codes(vs, hard_only=False):
    return {v.code for v in vs if v.hard or not hard_only}


def test_good_script_passes():
    assert check(good()) == []
    assert 60 <= count_words(" ".join(s.text for s in good())) <= 86


def test_status_wording_rules():
    s = good()
    s[2] = seg("main", "fact", "Advisers are discussing a plan to strike Iran before the midterms.", "C3")
    assert {"not_established", "attribution"} <= codes(check(s))                       # allégation énoncée comme un fait
    s = good()
    s[0] = seg("hook", "attributed", "The US will not attack Iran before the November midterm elections.", "C1")
    assert "attribution" in codes(check(s))                                             # déclaration sans « Trump »/« said »
    s = good()
    s[4] = seg("details", "attributed", "Reuters says oil prices hit $105 on Tuesday.", "C4")
    assert "disputed_type" in codes(check(s))
    s[4] = seg("details", "disputed", "Oil prices hit $105 on Tuesday, Reuters says.", "C4")
    assert "disputed_wording" in codes(check(s))                                        # divergence non présentée


def test_numbers_and_citations():
    s = good()
    s[2] = seg("main", "fact", "A federal judge extended a temporary block on the media ban for 90 days.", "C2")
    assert "number" in codes(check(s))
    s = good()
    s[2] = seg("main", "fact", "A federal judge extended a temporary block on the media ban.")
    assert "uncited" in codes(check(s))
    s[2] = seg("main", "fact", "A federal judge extended a temporary block on the media ban.", "C9")
    assert "invalid_claim" in codes(check(s))
    s = good()
    s[1] = seg("context", "context", "Oil is up 5 percent today.")
    assert "context" in codes(check(s))


def test_hook_rules():
    s = good()
    s[0] = seg("hook", "attributed", "Trump says the US will not attack Iran before the November midterm elections and more details are coming in this very minute.", "C1")
    assert "hook" in codes(check(s))
    s[0] = seg("hook", "attributed", "Trump says the US will NEVER attack Iran before midterms.", "C1")
    assert "hook" in codes(check(s))
    s[0] = seg("hook", "attributed", "You won't believe it: Trump says the US will not attack Iran.", "C1")
    assert "clickbait" in codes(check(s))


def test_opinion_rules():
    s = good()
    s[1] = seg("context", "opinion", "In our view this is a bold promise.", "C1")
    assert "opinion_not_allowed" in codes(check(s))                       # ton neutre : pas d'opinion maison
    ok = check(s, tone="anti_trump")
    assert "opinion_not_allowed" not in codes(ok) and "unmarked_opinion" not in codes(ok)
    s[1] = seg("context", "opinion", "Analysts say this is a bold promise.", "C1")
    assert "fabricated_attribution" in codes(check(s, tone="anti_trump"))  # tiers inventé
    assert "unmarked_opinion" in codes(check(s, tone="pro_trump"))         # pas de formule éditoriale
    many = good()[:2] + [seg("main", "opinion", "In our view it is bold.", "C1")] * 3 + good()[2:]
    assert "opinion_limit" in codes(check(many, tone="pro_trump", duration_s=45))


def test_length_duplicate_structure_entities():
    assert "length" in codes(check(good(), duration_s=90)) and "length" in codes(check(good(), duration_s=20))
    full = " ".join(s.text for s in good())
    assert "duplicate" in codes(check(good(), recent_scripts=[full]))
    s = good()[1:]
    assert "structure" in codes(check(s))
    s = good()
    s[3] = seg("main", "attributed", "According to the New York Times, advisers like Marco Rubio are discussing a plan to strike Iran.", "C3")
    soft = check(s)
    assert any(v.code == "entity" and not v.hard for v in soft)
    strict = check(s, cfg=ScriptSection(strict_entities=True))
    assert any(v.code == "entity" and v.hard for v in strict)


def test_french_markers():
    cl = {"C1": ClaimInfo("C1", "Le président a annoncé un plan de 18 millions de dollars.", "statement", "REPORTED", "Trump", ["Reuters"])}
    ok = validate([seg("hook", "attributed", "Selon Reuters, Trump annonce un plan de 18 millions de dollars.", "C1")], cl,
                  story_title="t", language="fr", tone="neutral", duration_s=20, cfg=ScriptSection(length_low=0.01))
    assert "attribution" not in codes(ok)
    bad = validate([seg("hook", "fact", "Un plan de 18 millions de dollars est lancé.", "C1")], cl,
                   story_title="t", language="fr", tone="neutral", duration_s=20, cfg=ScriptSection(length_low=0.01))
    assert {"not_established", "attribution"} <= codes(bad)


# ----------------------------------------------------------------- générateur
class SeqLLM(ILLMProvider):
    name, model = "fake", "fake"

    def __init__(self, *payloads, judge=None):
        self.payloads, self.calls, self.judge, self.judge_calls = list(payloads), [], judge, []

    def health_check(self): ...
    def list_models(self): return []

    def complete(self, messages, **kw):
        if "faithful" in json.dumps(kw.get("json_schema") or {}):          # passe de vérification de fidélité
            self.judge_calls.append(messages)
            ok = {"segments": [{"index": i, "faithful": True, "unsupported": ""} for i in range(30)]}
            j = self.judge[min(len(self.judge_calls) - 1, len(self.judge) - 1)] if self.judge else ok
            return LLMResponse(json.dumps(j), "fake")
        self.calls.append(messages)
        p = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return LLMResponse(json.dumps(p), "fake")


def payload(segs, visuals=None):
    return {"segments": [{"section": s.section, "type": s.type, "text": s.text, "claims": s.claims} for s in segs],
            "visual_suggestions": visuals or [{"segment": 0, "description": "map of the Gulf"}]}


def fc_result(verdict="CAUTION", claims=None):
    ev = lambda n: [Evidence("S1", n.lower(), n, f"https://{n.lower()}.com/a", "an exact quote from the source")]  # noqa: E731
    claims = claims or [
        ClaimResult(CLAIMS[k].text, CLAIMS[k].kind, CLAIMS[k].status, 0.7, CLAIMS[k].speaker, ev(CLAIMS[k].sources[0]))
        for k in ("C1", "C2", "C3", "C4")]
    return FactCheckResult(1, TITLE, verdict, 0.7, "m", claims, [])


@pytest.fixture()
def env(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "s.db"))
    with session_scope(factory) as s:
        st = Story(title=TITLE, status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", model="m", verdict="CAUTION", confidence=0.7, data=fc_result().to_dict()))
    cfg = AppConfig.model_validate({"editorial": {"duration_s": 30}})
    return cfg, factory


def test_generation_with_correction_loop(env):
    cfg, factory = env
    bad = good()
    bad[2] = seg("main", "attributed", "According to the New York Times, advisers are discussing a strike.", "C9")  # inexistante
    llm = SeqLLM(payload(bad), payload(good()))
    g = ScriptGenerator(cfg, llm, factory).generate(1)
    assert g.attempts == 2 and len(llm.calls) == 2
    feedback = llm.calls[1][-1].content
    assert "REJECTED" in feedback and "C9" in feedback
    assert g.hook.startswith("Trump says") and g.ending == "We will keep following this story."
    assert g.status == "NEEDS_REVIEW"                        # verdict CAUTION + affirmation DISPUTED
    assert {c["id"] for c in g.claims_used} == {"C1", "C2", "C3", "C4"}
    assert g.source_references[0]["sources"][0]["quote"] == "an exact quote from the source"
    assert g.visual_suggestions[0]["description"] == "map of the Gulf"
    with session_scope(factory) as s:
        row = s.get(Script, g.id)
        assert row.full_script == g.full_script and row.claims["segments"][0]["section"] == "hook"


def test_clean_script_on_safe_story_is_draft(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "d.db"))
    only = [ClaimResult(CLAIMS["C2"].text, "fact", "CONFIRMED", 0.9, None,
                        [Evidence("S1", "reuters", "Reuters", "https://r.com", "an exact quote from the source")])]
    with session_scope(factory) as s:
        st = Story(title="Judge extends block on media ban", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", model="m", verdict="SAFE", confidence=0.9, data=fc_result("SAFE", only).to_dict()))
    segs = [
        seg("hook", "fact", "A federal judge extended a temporary block on the media ban.", "C1"),
        seg("main", "fact", "A federal judge extended a temporary block on the media ban, and that block stays in place for now.", "C1"),
        seg("details", "context", "Here is what that means in practice."),
        seg("conclusion", "context", "We will keep following this story as it develops."),
    ]
    cfg = AppConfig.model_validate({"script": {"length_low": 0.3, "duplicate_similarity": 1.0}})
    g = ScriptGenerator(cfg, SeqLLM(payload(segs)), factory).generate(1, duration_s=20)
    assert g.status == "DRAFT" and g.attempts == 1 and g.warnings == []


def test_pruning_when_corrections_fail(env):
    cfg, factory = env
    bad = good()
    bad[3] = seg("main", "attributed", "According to the New York Times, advisers are discussing a strike.", "C9")  # jamais corrigé
    cfg = AppConfig.model_validate({"editorial": {"duration_s": 30}, "script": {"max_attempts": 2, "length_low": 0.3}})
    llm = SeqLLM(payload(bad))
    g = ScriptGenerator(cfg, llm, factory).generate(1)
    assert len(llm.calls) == 2 and g.status == "NEEDS_REVIEW"
    assert "advisers are discussing a strike" not in g.full_script            # segment fautif retiré
    assert any("supprimé" in w for w in g.warnings)


def test_invalid_hook_blocks(env):
    cfg, factory = env
    bad = good()
    bad[0] = seg("hook", "attributed", "Trump says the US will not attack Iran before the midterm elections.", "C9")  # affirmation inexistante
    cfg = AppConfig.model_validate({"script": {"max_attempts": 1}})
    with pytest.raises(ScriptBlockedError):
        ScriptGenerator(cfg, SeqLLM(payload(bad)), factory).generate(1)


def test_blocked_or_missing_factcheck(env, tmp_path):
    cfg, factory = env
    with pytest.raises(TrumpNewsError):
        ScriptGenerator(cfg, SeqLLM(), factory).generate(99)            # pas de fact-check
    with session_scope(factory) as s:
        st = Story(title="x", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", model="m", verdict="BLOCKED", confidence=0, data=fc_result("BLOCKED", []).to_dict()))
        sid = st.id
    with pytest.raises(ScriptBlockedError):
        ScriptGenerator(cfg, SeqLLM(), factory).generate(sid)


def test_recent_hooks_are_passed_and_invalid_params_rejected(env):
    cfg, factory = env
    gen = ScriptGenerator(cfg, SeqLLM(payload(good())), factory)
    first = gen.generate(1)
    gen.generate(1, style="dramatic", tone="balanced")
    prompt = gen.llm.calls[-1][1].content + gen.llm.calls[-1][0].content
    assert first.hook in prompt and "Dramatic" in prompt and "Balanced" in prompt
    with pytest.raises(TrumpNewsError):
        gen.generate(1, style="nope")


def test_target_words_by_language():
    assert target_words(30, "en", SC) == 75 and target_words(30, "fr", SC) == 81 and target_words(30, "es", SC) == 84


# ------------------------------------------------- nouveaux comportements
def test_number_formats_across_languages():
    from app.factcheck.grounding import numbers_grounded

    assert numbers_grounded("Un plan de 1,5 milliard de dollars", "A plan worth $1.5 billion")
    assert numbers_grounded("18 000 personnes", "18,000 people attended")
    assert not numbers_grounded("Un plan de 2,5 milliards", "A plan worth $1.5 billion")


def test_entity_check_skipped_across_languages():
    cl = {"C1": ClaimInfo("C1", "Trump said Russia will supply diesel.", "statement", "CONFIRMED", "Trump", ["Reuters"])}
    seg_fr = [seg("hook", "attributed", "Trump affirme que la Russie fournira du diesel.", "C1")]
    base = dict(story_title="t", tone="neutral", duration_s=20, cfg=ScriptSection(length_low=0.01, strict_entities=True))
    assert "entity" not in codes(validate(seg_fr, cl, language="fr", **base))
    seg_en = [seg("hook", "attributed", "Trump said Brazil will supply diesel.", "C1")]
    assert "entity" in codes(validate(seg_en, cl, language="en", **base))


def test_possessive_is_not_a_new_name():
    cl = {"C1": ClaimInfo("C1", "Trump said Russia will supply diesel.", "statement", "CONFIRMED", "Trump", ["Reuters"])}
    base = dict(story_title="t", tone="neutral", duration_s=20, cfg=ScriptSection(length_low=0.01, strict_entities=True))
    seg_en = [seg("hook", "attributed", "According to Reuters, Russia's diesel will follow Trump’s statement.", "C1")]
    assert "entity" not in codes(validate(seg_en, cl, language="en", **base))
    seg_en = [seg("hook", "attributed", "According to Reuters, Brazil's diesel will follow.", "C1")]
    assert "entity" in codes(validate(seg_en, cl, language="en", **base))


def test_duration_adapts_to_available_material(tmp_path):
    from app.script.validate import supported_duration

    assert [supported_duration(n, "en", SC) for n in (1, 2, 3, 4, 6)] == [20, 30, 45, 60, 90]
    factory = init_db(create_db_engine(tmp_path / "a.db"))
    only = [ClaimResult(CLAIMS["C2"].text, "fact", "CONFIRMED", 0.9, None,
                        [Evidence("S1", "reuters", "Reuters", "https://r.com", "an exact quote from the source")])]
    with session_scope(factory) as s:
        st = Story(title="Judge extends block on media ban", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", model="m", verdict="SAFE", confidence=0.9, data=fc_result("SAFE", only).to_dict()))
    segs = [
        seg("hook", "fact", "A federal judge extended a temporary block on the media ban.", "C1"),
        seg("main", "fact", "A federal judge extended a temporary block on the media ban, and that block stays in place for now.", "C1"),
        seg("conclusion", "context", "We will keep following this story as it develops."),
    ]
    cfg = AppConfig.model_validate({"script": {"duplicate_similarity": 1.0}})
    g = ScriptGenerator(cfg, SeqLLM(payload(segs)), factory).generate(1, duration_s=60)
    assert g.duration_target == 20 and any("ramenée de 60 s à 20 s" in w for w in g.warnings)
    cfg2 = AppConfig.model_validate({"script": {"adapt_duration": False, "max_attempts": 1}})
    with pytest.raises(ScriptBlockedError):                 # sans adaptation : 60 s impossibles, et on n'invente pas
        ScriptGenerator(cfg2, SeqLLM(payload([segs[0]] + [seg("main", "fact", "x y z.", "C9")] + segs[2:])), factory).generate(1, duration_s=60)


def test_faithfulness_judge_rejects_embellishment_and_loop_fixes_it(env):
    cfg, factory = env
    judge = [
        {"segments": [{"index": 0, "faithful": True, "unsupported": ""},
                      {"index": 3, "faithful": False, "unsupported": "will secure fuel supplies"}]},
        {"segments": [{"index": i, "faithful": True, "unsupported": ""} for i in range(6)]},
    ]
    llm = SeqLLM(payload(good()), judge=judge)
    g = ScriptGenerator(cfg, llm, factory).generate(1)
    assert g.attempts == 2 and len(llm.judge_calls) == 2
    feedback = llm.calls[1][-1].content
    assert "will secure fuel supplies" in feedback and "segment 4" in feedback


def test_unfaithful_segment_pruned_and_unfaithful_hook_blocks(env):
    cfg, factory = env
    always = [{"segments": [{"index": 3, "faithful": False, "unsupported": "added purpose"}]}]
    cfg2 = AppConfig.model_validate({"editorial": {"duration_s": 30}, "script": {"max_attempts": 2, "length_low": 0.3}})
    g = ScriptGenerator(cfg2, SeqLLM(payload(good()), judge=always), factory).generate(1)
    assert "advisers are discussing" not in g.full_script.lower() and g.status == "NEEDS_REVIEW"
    hook_bad = [{"segments": [{"index": 0, "faithful": False, "unsupported": "added purpose"}]}]
    with pytest.raises(ScriptBlockedError):
        ScriptGenerator(cfg2, SeqLLM(payload(good()), judge=hook_bad), factory).generate(1)


def test_judge_failure_does_not_crash(env):
    cfg, factory = env

    class BrokenJudge(SeqLLM):
        def complete(self, messages, **kw):
            if "faithful" in json.dumps(kw.get("json_schema") or {}):
                raise RuntimeError("LM Studio indisponible")
            return super().complete(messages, **kw)

    g = ScriptGenerator(cfg, BrokenJudge(payload(good())), factory).generate(1)
    assert g.status == "NEEDS_REVIEW" and any("fidélité" in w for w in g.warnings)


# --------------------------------------------- attribution automatique (source réelle)
def test_autofix_attribution_rules():
    from app.script.autofix import attribute, fix_attributions, source_names
    from app.script.models import Violation
    from app.script.service import SegmentOut

    assert source_names(["C3"], CLAIMS) == ["The New York Times"]
    assert source_names(["C1"], CLAIMS) == ["Reuters", "BBC News"]               # LIKELY : deux éditeurs
    assert source_names(["C4"], CLAIMS) == [] and source_names(["C2"], CLAIMS) == []   # DISPUTED / CONFIRMED : rien
    assert attribute("Trump visited Kentucky.", ["CBS News"], "en") == "According to CBS News, Trump visited Kentucky."
    assert attribute("The judge ruled.", ["Reuters"], "en") == "According to Reuters, the judge ruled."
    assert attribute("Le juge a tranché.", ["Le Monde", "AFP"], "fr") == "Selon Le Monde et AFP, le juge a tranché."
    assert attribute("Advisers met.", ["NYT"], "es").startswith("Según NYT, ")
    segs = [SegmentOut(section="hook", type="fact", text="Advisers are discussing a plan.", claims=["C3"]),
            SegmentOut(section="main", type="fact", text="Oil prices hit $105.", claims=["C4"])]
    v = [Violation("attribution", "x", 0), Violation("not_established", "x", 0),
         Violation("disputed_type", "x", 1)]
    fixed, notes = fix_attributions(segs, v, CLAIMS, "en")
    assert fixed[0].text == "According to The New York Times, advisers are discussing a plan." and fixed[0].type == "attributed"
    assert fixed[1] == segs[1] and len(notes) == 1 and "The New York Times" in notes[0]   # autre erreur : pas touché


def test_hook_without_attribution_is_fixed_without_extra_llm_call(env):
    cfg, factory = env
    bad = good()
    bad[0] = seg("hook", "fact", "Advisers are discussing a plan to strike Iran before the midterms.", "C3")  # cas réel P4
    llm = SeqLLM(payload(bad))
    g = ScriptGenerator(cfg, llm, factory).generate(1)
    assert len(llm.calls) == 1 and g.attempts == 1                            # aucune tentative LLM supplémentaire
    assert g.hook == "According to The New York Times, advisers are discussing a plan to strike Iran before the midterms."
    assert any("attribution ajoutée automatiquement" in w for w in g.warnings)
    assert len(llm.judge_calls) == 1                                          # la passe de fidélité a bien tourné
    off = AppConfig.model_validate({"editorial": {"duration_s": 30}, "script": {"auto_attribution": False, "max_attempts": 1}})
    with pytest.raises(ScriptBlockedError):
        ScriptGenerator(off, SeqLLM(payload(bad)), factory).generate(1)


def test_autofix_credits_every_combined_claim():
    """Cas réel : la conclusion combine C4 (NKU The Northerner) et C5 (WLWT) : les deux médias sont cités."""
    from app.script.autofix import source_names

    claims = {
        "C4": ClaimInfo("C4", "Trump rallied in Kentucky at the Truist Arena.", "fact", "REPORTED", None, ["NKU The Northerner Online"]),
        "C5": ClaimInfo("C5", "Trump visited Northern Kentucky.", "fact", "REPORTED", None, ["WLWT"]),
        "C6": ClaimInfo("C6", "Trump spoke for an hour.", "fact", "REPORTED", None, ["CBS News"]),
        "C7": ClaimInfo("C7", "The crowd was large.", "fact", "REPORTED", None, ["WLWT", "CBS News"]),
    }
    assert source_names(["C4", "C5"], claims) == ["NKU The Northerner Online", "WLWT"]
    assert source_names(["C5", "C7"], claims) == ["WLWT"]                          # WLWT couvre déjà C7
    assert source_names(["C4", "C5", "C6"], claims) == []                          # 3 médias : pas de correction automatique
    assert source_names(["C1", "C4"], CLAIMS | claims) == ["Reuters", "NKU The Northerner Online"]
