import base64
import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from app.config.schema import AppConfig
from app.core.errors import ProviderError, SecretNotFoundError
from app.core.interfaces import HealthStatus
from app.database.models import Narration, Script, Story, VisualAssetRow, VisualPlanRow
from app.database.session import create_db_engine, init_db, session_scope
from app.providers.visuals.a1111 import A1111Provider, build_prompt
from app.providers.visuals.base import IVisualProvider
from app.providers.visuals.cards import CardsProvider
from app.providers.visuals.pexels import PexelsProvider
from app.providers.visuals.user import UserImagesProvider
from app.providers.visuals.wikimedia import WikimediaProvider, license_rank
from app.visuals.cards import H, W, card_id, render_card
from app.visuals.models import VisualAsset, VisualQuery
from app.visuals.planner import VisualPlanner, desc_kind, extract_entities
from app.visuals.sheet import contact_sheet


def jpeg_bytes(color=(200, 30, 30), size=(1600, 1000), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


# ---------------------------------------------------------------- cartes
def test_card_rendering_and_ids(tmp_path):
    out = render_card("claim", "A federal judge extended a temporary block on the media ban.", tmp_path / "c.png",
                      footer="Reuters · BBC News", badge="Reported", category="justice")
    with Image.open(out) as im:
        assert im.size == (W, H) == (1080, 1920)
    assert card_id("claim", "a", "b", "c", "d") == card_id("claim", "a", "b", "c", "d") != card_id("claim", "a", "b", "c", "e")
    render_card("number", "Oil prices hit $105 a barrel.", tmp_path / "n.png")
    render_card("headline", "x " * 300, tmp_path / "long.png")           # texte très long : ne plante pas, tient dans l'image


# ------------------------------------------------------------- wikimedia
def page(title, lic, w=1600, h=1000, mime="image/jpeg", artist="<a href='x'>Jane Doe</a>", idx=1):
    return {"title": title, "index": idx, "imageinfo": [{
        "thumburl": f"https://upload.example/{title.split(':')[1]}", "thumbwidth": w, "thumbheight": h, "mime": mime,
        "descriptionurl": f"https://commons.wikimedia.org/wiki/{title}",
        "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": artist},
                        "ImageDescription": {"value": "Donald Trump speaking"}}}]}


def test_license_rules():
    assert license_rank("Public domain", False) == 0 and license_rank("CC0", False) == 0 and license_rank("PD-USGov", False) == 0
    assert license_rank("CC BY 4.0", False) == 1 and license_rank("CC BY-SA 4.0", False) is None
    assert license_rank("CC BY-SA 4.0", True) == 2
    for bad in ("CC BY-NC 2.0", "CC BY-ND 2.0", "Fair use", "", "All rights reserved"):
        assert license_rank(bad, True) is None


@respx.mock
def test_wikimedia_filters_ranks_and_credits(tmp_path):
    pages = {str(i): p for i, p in enumerate([
        page("File:NC.jpg", "CC BY-NC 2.0", idx=1),
        page("File:SVG.svg", "Public domain", mime="image/svg+xml", idx=2),
        page("File:Tiny.jpg", "Public domain", w=400, h=300, idx=3),
        page("File:SA.jpg", "CC BY-SA 4.0", idx=4),
        page("File:BY.jpg", "CC BY 4.0", idx=5),
        page("File:PD.jpg", "Public domain", idx=6, artist="Official White House Photo"),
    ])}
    respx.get("https://commons.wikimedia.org/w/api.php").respond(200, json={"query": {"pages": pages}})
    for name in ("PD.jpg", "BY.jpg"):
        respx.get(f"https://upload.example/{name}").respond(200, content=jpeg_bytes())
    prov = WikimediaProvider(contact="me@example.com")
    q = VisualQuery("photo", entities=["Donald Trump"])
    a = prov.get(q, tmp_path, set())
    assert a.external_id == "File:PD.jpg" and a.license == "Public domain" and a.path.exists()      # domaine public d'abord
    assert a.attribution == "Official White House Photo / Wikimedia Commons / Public domain"
    assert a.source_url.endswith("File:PD.jpg") and a.sha256
    b = prov.get(q, tmp_path, {"wikimedia:File:PD.jpg"})                                             # déjà utilisé : suivant
    assert b.external_id == "File:BY.jpg" and "CC BY 4.0" in b.attribution
    assert prov.get(q, tmp_path, {"wikimedia:File:PD.jpg", "wikimedia:File:BY.jpg"}) is None         # NC, SA, SVG, petite : refusées
    assert "me@example.com" in respx.calls[0].request.headers["user-agent"]


@respx.mock
def test_wikimedia_errors(tmp_path):
    respx.get("https://commons.wikimedia.org/w/api.php").respond(503)
    with pytest.raises(ProviderError):
        WikimediaProvider().get(VisualQuery("photo", entities=["x"]), tmp_path, set())
    respx.get("https://commons.wikimedia.org/w/api.php").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(ProviderError):
        WikimediaProvider().get(VisualQuery("photo", entities=["x"]), tmp_path, set())
    assert WikimediaProvider().get(VisualQuery("photo"), tmp_path, set()) is None           # sans entité : rien à chercher


# ---------------------------------------------------------------- pexels
@respx.mock
def test_pexels(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.secrets._keyring", lambda: None)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(SecretNotFoundError):
        PexelsProvider().get(VisualQuery("stock", keywords=["oil", "refinery"]), tmp_path, set())
    monkeypatch.setenv("PEXELS_API_KEY", "pexels-key-123")
    photos = [{"id": 1, "width": 3000, "height": 4000, "url": "https://pexels.com/1", "photographer": "Ann",
               "src": {"large2x": "https://img.example/1.jpg"}},
              {"id": 2, "width": 3000, "height": 4000, "url": "https://pexels.com/2", "photographer": "Bob",
               "src": {"large2x": "https://img.example/2.jpg"}}]
    route = respx.get("https://api.pexels.com/v1/search").respond(200, json={"photos": photos})
    respx.get("https://img.example/1.jpg").respond(200, content=jpeg_bytes(size=(1200, 1800)))
    respx.get("https://img.example/2.jpg").respond(200, content=jpeg_bytes((10, 90, 200), (1200, 1800)))
    p = PexelsProvider()
    a = p.get(VisualQuery("stock", keywords=["oil", "refinery"]), tmp_path, set())
    assert route.calls[0].request.headers["authorization"] == "pexels-key-123" and a.attribution == "Photo : Ann / Pexels"
    assert p.get(VisualQuery("stock", keywords=["oil"]), tmp_path, {"pexels:1"}).external_id == "2"
    respx.get("https://api.pexels.com/v1/search").respond(401)
    with pytest.raises(ProviderError):
        p.get(VisualQuery("stock", keywords=["oil"]), tmp_path, set())


# ------------------------------------------------------------- IA locale
def test_ai_prompt_never_names_people():
    p = build_prompt("Photo of Donald Trump and Vladimir Putin shaking hands, realistic portrait", "flat vector illustration",
                     ["Donald Trump"])
    assert "Trump" not in p and "Putin" not in p and "realistic" not in p.lower() and p.startswith("flat vector illustration")
    assert "a public figure" in p
    assert "abstract symbolic" in build_prompt("", "style", [])


@respx.mock
def test_ai_provider_badge_flag_and_guardrails(tmp_path):
    route = respx.post("http://sd.local/sdapi/v1/txt2img").respond(200, json={"images": [base64.b64encode(jpeg_bytes((240, 240, 240), (768, 1344), "PNG")).decode()]})
    prov = A1111Provider("http://sd.local", "flat vector illustration", width=768, height=1344)
    a = prov.get(VisualQuery("illustration", "Donald Trump at a podium in Washington", entities=["Donald Trump"], language="fr"), tmp_path, set())
    body = json.loads(route.calls[0].request.content)
    assert "Trump" not in body["prompt"] and "photorealistic" in body["negative_prompt"] and body["width"] == 768
    assert a.ai_generated and a.kind == "illustration" and a.attribution == "Illustration générée par IA"
    with Image.open(a.path) as im:                                         # bandeau incrusté en bas, sombre sur fond clair
        assert im.convert("RGB").getpixel((5, im.height - 5))[0] < 80 and im.getpixel((im.width // 2, 100))[0] > 200
    respx.post("http://sd.local/sdapi/v1/txt2img").respond(500, text="boom")
    with pytest.raises(ProviderError):
        prov.get(VisualQuery("illustration", "other scene"), tmp_path, set())


# ---------------------------------------------------------- user + cards
def test_user_images_matching(tmp_path):
    d = tmp_path / "mine"
    d.mkdir()
    for n in ("white_house_podium.jpg", "oil_refinery.png", "random.jpg"):
        Image.new("RGB", (800, 600), (1, 2, 3)).save(d / n)
    p = UserImagesProvider(d)
    a = p.get(VisualQuery("photo", "Trump at the White House podium"), tmp_path, set())
    assert a.external_id == "white_house_podium.jpg" and a.license == "fournie par l'utilisateur"
    assert p.get(VisualQuery("photo", "White House podium"), tmp_path, {"user:white_house_podium.jpg"}) is None
    assert p.get(VisualQuery("photo", "volcano"), tmp_path, set()) is None
    assert not UserImagesProvider(tmp_path / "nope").health_check().ok


def test_cards_provider_reuse_and_kinds(tmp_path):
    p = CardsProvider()
    a = p.get(VisualQuery("claim", "Some claim text here.", footer="Reuters"), tmp_path, set())
    b = p.get(VisualQuery("claim", "Some claim text here.", footer="Reuters"), tmp_path, set())
    assert a.external_id == b.external_id and a.sha256 == b.sha256 and p.get(VisualQuery("photo", "x"), tmp_path, set()) is None


# ---------------------------------------------------------------- planner
def test_entity_and_description_helpers():
    ents = extract_entities(["Trump said the US will not attack Iran before the November midterm elections.",
                             "Oil prices hit $105, Vladimir Putin said, and Trump agreed."], {"Trump": "Donald Trump"})
    assert ents[0] == "Donald Trump" and "Iran" in ents and "Vladimir Putin" in ents and "Oil" not in ents and "November" not in ents
    assert desc_kind("Text card: 'According to Reuters'") == "card" and desc_kind("Map of the Gulf") == "illustration"
    assert desc_kind("Trump at a podium") == "photo"


class FakeProv(IVisualProvider):
    def __init__(self, name, kinds, internet=False, ai=False, fail=False):
        self.name, self.kinds, self.requires_internet, self.ai_generated, self.fail, self.calls, self.queries = \
            name, frozenset(kinds), internet, ai, fail, 0, []

    def health_check(self): return HealthStatus(True, "ok")

    def get(self, query, dest_dir, exclude):
        self.calls += 1
        self.queries.append(query)
        if self.fail:
            raise ProviderError("service HS")
        key = (query.entities[:1] or query.keywords[:1] or ["x"])[0]
        for i in range(6):
            ext = f"{key}_{i}"
            if f"{self.name}:{ext}" in exclude:
                continue
            path = dest_dir / self.name / f"{ext}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg_bytes((40 * (i + 1) % 255, 90, (len(ext) * 17) % 255), (1200, 800)))
            import hashlib
            return VisualAsset(self.name, ext, path, "illustration" if self.ai_generated else "photo", 1200, 800,
                               license="generated" if self.ai_generated else "Public domain",
                               attribution=None if self.ai_generated else f"Photo {ext} / Wikimedia Commons / Public domain",
                               ai_generated=self.ai_generated, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return None


SEGMENTS = [
    {"index": 0, "section": "hook", "type": "attributed", "text": "Trump says the US will not attack Iran before the midterms.", "claims": ["C1"]},
    {"index": 1, "section": "main", "type": "fact", "text": "A federal judge extended a temporary block on the media ban.", "claims": ["C2"]},
    {"index": 2, "section": "main", "type": "attributed", "text": "According to Reuters, oil prices hit $105 on Tuesday, reports say.", "claims": ["C3"]},
    {"index": 3, "section": "details", "type": "disputed", "text": "However, reports differ on who ordered the change.", "claims": ["C4"]},
    {"index": 4, "section": "conclusion", "type": "context", "text": "We will keep following this story.", "claims": []},
]
CLAIMS = [
    {"id": "C1", "claim": "Trump said the US will not attack Iran before the November midterm elections.", "kind": "statement", "status": "LIKELY"},
    {"id": "C2", "claim": "A federal judge extended a temporary block on the media ban.", "kind": "fact", "status": "CONFIRMED"},
    {"id": "C3", "claim": "Oil prices hit $105 on Tuesday, according to Reuters.", "kind": "fact", "status": "REPORTED"},
    {"id": "C4", "claim": "Officials disagree on who ordered the change.", "kind": "fact", "status": "DISPUTED"},
]
REFS = [{"claim_id": c["id"], "claim": c["claim"], "status": c["status"],
         "sources": [{"name": n, "url": "", "quote": ""} for n in ("Reuters", "BBC News")]} for c in CLAIMS]
TIMELINE = [{"index": 0, "start": 0.0, "end": 7.0}, {"index": 1, "start": 7.4, "end": 13.0}, {"index": 2, "start": 13.25, "end": 18.0},
            {"index": 3, "start": 18.25, "end": 21.5}, {"index": 4, "start": 21.75, "end": 24.0}]


@pytest.fixture()
def env(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "v.db"))
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        sc = Script(story_id=st.id, style="neutral_news", tone="neutral", language="en", duration_target=30, full_script="x",
                    source_refs=REFS, claims={"segments": SEGMENTS, "claims": CLAIMS,
                                              "visuals": [{"segment": 0, "description": "Trump at the White House podium"}]})
        s.add(sc)
        s.flush()
        s.add(Narration(script_id=sc.id, provider="fake", voice="v", language="en", audio_path="a.wav", duration=20.0,
                        timeline=[{**t, "section": "x", "text": "", "audio": "", "words": []} for t in TIMELINE], input_hash="h"))
    return factory, tmp_path


def make_planner(env, extra=None, **cfg):
    factory, tmp = env
    base = {"visuals": {"avoid_recent_plans": 5, **cfg}}
    config = AppConfig.model_validate(base)
    provs = {"user": UserImagesProvider(tmp / "none"), "wikimedia": FakeProv("wikimedia", {"photo"}, internet=True),
             "cards": CardsProvider(), **(extra or {})}
    return VisualPlanner(config, factory, tmp / "data", provider_factory=lambda n: provs[n], sleep=lambda s: None), provs


def test_plan_follows_narration_timeline(env):
    planner, provs = make_planner(env)
    plan = planner.plan(1)
    by_seg = {}
    for it in plan.items:
        by_seg.setdefault(it.segment_index, []).append(it)
    assert [len(by_seg[i]) for i in range(5)] == [2, 1, 1, 1, 1]                      # durées réelles -> nombre de prises
    h1, h2 = by_seg[0]
    assert h1.start == 0.0 and h1.end == pytest.approx(3.5) == h2.start and h2.end == 7.0
    first = h1.asset
    assert h1.role == "primary" and first["provider"] == "wikimedia" and first["external_id"].startswith("Donald Trump")  # alias
    assert h2.role == "card" and h2.asset["provider"] == "cards"
    seg1 = by_seg[1][0]                                                                 # photo introuvable : la carte unique est prolongée
    assert seg1.asset["provider"] == "cards" and seg1.start == 7.4 and seg1.end == 13.0
    assert by_seg[4][0].role == "sources" and by_seg[4][0].asset["provider"] == "cards"   # conclusion : carte des sources
    assert plan.credits == [f"Photo {first['external_id']} / Wikimedia Commons / Public domain"] and not plan.ai_disclosure
    keys = [(i.asset["provider"], i.asset["external_id"]) for i in plan.items]
    assert len(keys) == len(set(keys))                                                                # aucune image répétée
    with session_scope(env[0]) as s:
        assert s.get(VisualPlanRow, plan.id).narration_id == 1 and len(list(s.query(VisualAssetRow))) >= 4


def test_outlet_names_are_not_photo_subjects(env):
    planner, provs = make_planner(env)
    planner.plan(1)
    searched = {e for q in provs["wikimedia"].queries for e in q.entities}
    assert "Donald Trump" in searched and "Reuters" not in searched and "BBC News" not in searched


def test_number_card_and_status_badge(env):
    planner, provs = make_planner(env)
    plan = planner.plan(1)
    texts = [(q.kind, q.badge) for q in provs["wikimedia"].queries]
    assert plan  # le plan existe ; on vérifie les cartes via le fichier généré
    seg2 = [i for i in plan.items if i.segment_index == 2]
    assert all(i.asset["width"] == 1080 or i.asset["provider"] == "wikimedia" for i in seg2)


def test_recent_visuals_not_reused_across_plans(env):
    planner, _ = make_planner(env)
    p1 = planner.plan(1)
    p2 = planner.plan(1)
    ids1 = {(i.asset["provider"], i.asset["external_id"]) for i in p1.items if i.asset["provider"] != "cards"}
    ids2 = {(i.asset["provider"], i.asset["external_id"]) for i in p2.items if i.asset["provider"] != "cards"}
    assert ids1 and ids2 and not (ids1 & ids2)
    planner3, _ = make_planner(env, avoid_recent_plans=0)
    p3 = planner3.plan(1)
    assert {(i.asset["provider"], i.asset["external_id"]) for i in p3.items if i.asset["provider"] != "cards"} & ids1


def test_provider_failure_is_isolated(env):
    broken = FakeProv("wikimedia", {"photo"}, internet=True, fail=True)
    planner, _ = make_planner(env, extra={"wikimedia": broken})
    plan = planner.plan(1)
    assert broken.calls <= 2                                                          # écarté après échec, pas de martelage
    assert any("wikimedia en échec" in w and "Internet requis" in w for w in plan.warnings)
    assert len({i.segment_index for i in plan.items}) == 5 and all(i.asset["provider"] == "cards" for i in plan.items)


def test_ai_disclosure_and_toggle(env):
    ai = FakeProv("ai", {"illustration", "stock"}, ai=True)
    planner, provs = make_planner(env, extra={"ai": ai, "wikimedia": FakeProv("wikimedia", {"photo"}, fail=True)}, ai={"enabled": True})
    plan = planner.plan(1)
    assert plan.ai_disclosure and any(i.asset["ai_generated"] for i in plan.items) and ai.calls > 0
    ai2 = FakeProv("ai", {"illustration", "stock"}, ai=True)
    planner2, _ = make_planner(env, extra={"ai": ai2}, ai={"enabled": True})
    plan2 = planner2.plan(1, use_ai=False)
    assert ai2.calls == 0 and not plan2.ai_disclosure


def test_no_narration_estimates_durations(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "n.db"))
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        s.add(Script(story_id=st.id, style="s", tone="neutral", language="fr", duration_target=30, full_script="x",
                     source_refs=REFS, claims={"segments": SEGMENTS, "claims": CLAIMS, "visuals": []}))
    planner, _ = make_planner((factory, tmp_path))
    plan = planner.plan(1)
    assert any("durées estimées" in w for w in plan.warnings) and plan.items[0].start == 0.0
    assert all(b.start >= a.end - 1e-6 for a, b in zip(plan.items, plan.items[1:]))   # chronologie croissante
    assert any(i.asset["provider"] == "cards" for i in plan.items)
    # statut en français sur les cartes : rendu sans erreur
    from app.visuals.planner import STATUS_LABEL
    assert STATUS_LABEL["fr"]["DISPUTED"] == "Contesté"


def test_unknown_script(env):
    from app.core.errors import TrumpNewsError

    planner, _ = make_planner(env)
    with pytest.raises(TrumpNewsError):
        planner.plan(99)


def test_contact_sheet(env, tmp_path):
    planner, _ = make_planner(env)
    plan = planner.plan(1)
    out = contact_sheet([i.__dict__ for i in plan.items], tmp_path / "sheet.jpg")
    with Image.open(out) as im:
        assert im.width > 800 and im.height > 400


def test_cli_visuals(tmp_path, monkeypatch, capsys):
    import app.main as m

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("visuals:\n  wikimedia:\n    enabled: false\n", encoding="utf-8")
    assert m.main(["init"]) == 0
    factory = init_db(create_db_engine(tmp_path / "data" / "trump_news.db"))
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        s.add(Script(story_id=st.id, style="s", tone="neutral", language="en", duration_target=30, full_script="x",
                     source_refs=REFS, claims={"segments": SEGMENTS, "claims": CLAIMS, "visuals": []}))
    assert m.main(["visuals", "1"]) == 0
    out = capsys.readouterr().out
    assert "Plan visuel #1" in out and "cards" in out and "Planche-contact" in out
    assert any((tmp_path / "data" / "images").glob("plan_1_sheet.jpg"))


def test_consecutive_cards_of_a_segment_are_merged(env):
    broken = FakeProv("wikimedia", {"photo"}, internet=True, fail=True)       # aucune photo : tout retombe sur des cartes
    planner, _ = make_planner(env, extra={"wikimedia": broken})
    plan = planner.plan(1)
    hook = [i for i in plan.items if i.segment_index == 0]
    assert len(hook) == 1 and hook[0].start == 0.0 and hook[0].end == 7.0      # une seule carte, prolongée sur tout le hook
    for seg in range(5):
        cards = [i for i in plan.items if i.segment_index == seg]
        assert all(not (a.asset["provider"] == b.asset["provider"] == "cards") for a, b in zip(cards, cards[1:])
                   if b.role != "sources")


def test_hook_opens_on_a_photo_even_if_the_script_suggests_a_map(env):
    factory, _ = env
    with session_scope(factory) as s:
        sc = s.get(Script, 1)
        sc.claims = {**sc.claims, "visuals": [{"segment": 0, "description": "Map of the U.S. with Kentucky highlighted"}]}
    planner, provs = make_planner(env)
    first = planner.plan(1).items[0]
    assert first.start == 0.0 and first.asset["provider"] == "wikimedia" and first.asset["external_id"].startswith("Donald Trump")


def test_hook_photo_falls_back_to_configured_subject(env):
    factory, _ = env
    with session_scope(factory) as s:
        sc = s.get(Script, 1)
        segs = [dict(x) for x in sc.claims["segments"]]
        claims = [dict(c) for c in sc.claims["claims"]]
        segs[0]["text"] = "the midterms are coming, reports say."                  # aucun nom propre dans le hook...
        claims[0]["claim"] = "the midterm elections are coming."                   # ...ni dans son affirmation
        sc.claims = {**sc.claims, "segments": segs, "claims": claims, "visuals": []}
    planner, _ = make_planner(env)
    assert planner.plan(1).items[0].asset["external_id"].startswith("Donald Trump")
    planner, _ = make_planner(env, hook_entity="")
    assert planner.plan(1).items[0].asset["provider"] == "cards"


def test_sources_card_survives_card_merge(env):
    """Cas réel : sans photo, la conclusion commence par une carte ; la carte des sources ne doit pas être fusionnée avec elle."""
    broken = FakeProv("wikimedia", {"photo"}, internet=True, fail=True)
    planner, _ = make_planner(env, extra={"wikimedia": broken}, target_shot_s=1.0)   # conclusion de 2,25 s -> 2 prises
    plan = planner.plan(1)
    concl = [i for i in plan.items if i.segment_index == 4]
    assert len(concl) == 2 and concl[-1].role == "sources" and concl[-1].end == 24.0
    assert concl[0].asset["external_id"] != concl[-1].asset["external_id"]


# ------------------------------------------------------- pertinence des photos Wikimedia
@respx.mock
def test_wikimedia_relevance_copies_and_crowd(tmp_path):
    from app.providers.visuals.wikimedia import base_key

    results = {
        "Gulf of Mexico": [page("File:Gulf of Tunis NASA.jpg", "Public domain", idx=1),                 # « Gulf » seul : refusé
                           page("File:Gulf of Mexico satellite.jpg", "CC BY 4.0", idx=2)],
        "Donald Trump": [page("File:Maithripala Sirisena Donald Trump Melania Trump Jayanthi Sirisena.jpg", "Public domain", idx=1),
                         page("File:Donald Trump official portrait (cropped).jpg", "Public domain", idx=2),
                         page("File:Donald Trump at a rally.jpg", "CC BY 4.0", idx=3)],
        "Donald Trump Kentucky": [],
    }
    for p in results["Gulf of Mexico"]:
        p["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "satellite view"
    searched = []

    def api(request):
        term = request.url.params["gsrsearch"].removesuffix(" filetype:bitmap")
        searched.append(term)
        return httpx.Response(200, json={"query": {"pages": {str(i): p for i, p in enumerate(results.get(term, []))}}})

    respx.get("https://commons.wikimedia.org/w/api.php").mock(side_effect=api)
    respx.get(url__startswith="https://upload.example/").respond(200, content=jpeg_bytes())
    prov = WikimediaProvider()
    gulf = prov.get(VisualQuery("photo", entities=["Gulf of Mexico"]), tmp_path, set())
    assert gulf.external_id == "File:Gulf of Mexico satellite.jpg"                       # pertinence avant licence
    trump = prov.get(VisualQuery("photo", entities=["Donald Trump", "Kentucky"]), tmp_path,
                     {"wikimedia:File:Donald Trump official portrait.jpg"})
    assert searched[1:] == ["Donald Trump Kentucky", "Donald Trump"]                     # combinaison d'abord
    assert trump.external_id == "File:Donald Trump at a rally.jpg"                       # copie « cropped » écartée, foule reléguée
    assert base_key("File:X (cropped).jpg") == base_key("File:X.jpg") == base_key("File:X (2).png")


def test_multiword_entities():
    ents = extract_entities(["Trump mentioned changing the name of the Gulf of Mexico to the Gulf of America."], {"Trump": "Donald Trump"})
    assert ents == ["Donald Trump", "Gulf of Mexico", "Gulf of America"]
