"""Phase 9 : publication. Aucun accès réseau : API simulées avec respx, jetons dans un faux trousseau."""
import json
import threading
import urllib.request
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import respx

from app.config.schema import AppConfig, InstagramSection, TikTokSection, YouTubeSection
from app.core.errors import PublisherError, PublisherUnavailableError, TrumpNewsError
from app.core.interfaces import IPublisher, HealthStatus, PublishRequest, PublishResult
from app.database.models import FactCheck, Publication, Script, Story, Video, VideoReport
from app.database.session import create_db_engine, init_db, session_scope
from app.main import main
from app.providers.publishers.instagram import InstagramPublisher
from app.providers.publishers.tiktok import TikTokPublisher, chunk_plan
from app.providers.publishers.youtube import YouTubePublisher
from app.publishing.metadata import build_texts, make_title
from app.publishing.oauth import pkce_pair, wait_for_code
from app.publishing.service import PublishingService
from app.video.quality import QualityContext

REFS = [{"claim_id": "C1", "sources": [{"name": "CBS News", "url": "https://cbs.example/1", "quote": "q"},
                                       {"name": "CBS News", "url": "https://cbs.example/1", "quote": "q2"}]},
        {"claim_id": "C2", "sources": [{"name": "AP", "url": "https://ap.example/2", "quote": "q"}]}]


class FakeStore:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, name, *, required=False):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value


class FakePublisher(IPublisher):
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, []

    def health_check(self):
        return HealthStatus(True, "ok")

    def publish(self, req):
        self.calls.append(req)
        if self.error:
            raise self.error
        return self.result or PublishResult(self.name, "PUBLISHED", "X1", f"https://{self.name}.example/X1")

    def status(self, platform_video_id):
        return PublishResult(self.name, "PUBLISHED", "X2", "https://done.example")


def make_db(tmp_path, *, report="READY", script="APPROVED", verdict="SOLID", preview=False, ai_shots=0):
    factory = init_db(create_db_engine(tmp_path / "p.db"))
    video = tmp_path / "video_0001.mp4"
    video.write_bytes(b"0123456789" * 10)
    ctx = QualityContext(1080, 1920, 30, 10.0, ai_shots=ai_shots, preview=preview)
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        s.add(FactCheck(story_id=st.id, input_hash="h", verdict=verdict, data={}))
        sc = Script(story_id=st.id, style="neutral_news", tone="neutral", language="en", duration_target=30,
                    hook="According to CBS News, Trump visited <Kentucky>.", full_script="According to CBS News, Trump visited Kentucky.",
                    status=script, source_refs=REFS, claims={})
        s.add(sc)
        s.flush()
        v = Video(story_id=st.id, script_id=sc.id, video_path=str(video), duration=10.0, status=report)
        s.add(v)
        s.flush()
        s.add(VideoReport(video_id=v.id, score=1.0, status=report,
                          data={"context": asdict(ctx), "preview": preview, "credits": ["Photo: White House (public domain)"]}))
    return factory, video


def service(tmp_path, pubs, **kw):
    factory, _ = make_db(tmp_path, **kw)
    cfg = AppConfig.model_validate({"publishing": {"youtube": {"enabled": True}, "tiktok": {"enabled": True}}})
    return PublishingService(cfg, factory, lambda name: pubs[name]), factory


# ------------------------------------------------------------------ métadonnées
def test_metadata_is_built_from_script_sources_and_credits():
    title, desc, caption, tags = build_texts(hook="Hook <b>", full_script="Body text.", source_refs=REFS,
                                             credits=["Photo: X"], ai_content=True, hashtags=["#Trump", "#news"], language="en")
    assert title == "Hook b"
    assert "- CBS News: https://cbs.example/1" in desc and desc.count("cbs.example") == 1 and "- AP: https://ap.example/2" in desc
    assert "Image credits:" in desc and "AI-generated" in desc and desc.endswith("#Trump #news")
    assert caption.startswith("Hook b") and "Sources: CBS News, AP" in caption and caption.endswith("#Trump #news")
    assert "http" not in caption and tags == ["Trump", "news"]


def test_title_puts_news_first_and_keeps_attribution():
    hook = ("According to BBC US & Canada, Trump waived several Russia sanctions two days before they were due to take "
            "effect. More text.")
    title = make_title(hook)
    assert title.startswith("Trump waived several Russia sanctions") and title.endswith("(BBC US & Canada)")
    assert len(title) <= 100
    assert make_title("According to CBS, Trump visited Kentucky on Monday.") == "Trump visited Kentucky on Monday (CBS)"
    assert make_title("Trump visited Kentucky.") == "Trump visited Kentucky."


def test_image_credits_are_tidied():
    from app.utils.credits import tidy_artist

    assert tidy_artist("Unknown authorUnknown author") == "Unknown author"
    assert tidy_artist("http://events.vedomosti.ru/popup.shtml?x=1", "Vedomosti") == "Vedomosti"
    assert tidy_artist("http://events.vedomosti.ru/popup.shtml") == "Unknown author"
    assert tidy_artist("The White House") == "The White House" and tidy_artist("") == "Unknown author"
    _, desc, _, _ = build_texts(hook="h", full_script="b", source_refs=[], ai_content=False, hashtags=[], language="en",
                                credits=["Unknown authorUnknown author / Wikimedia Commons / Public domain",
                                         "http://x.example/p / Wikimedia Commons / Public domain"])
    assert desc.count("- Unknown author / Wikimedia Commons / Public domain") == 1 and "x.example" not in desc


def test_title_and_caption_limits():
    long = "word " * 60
    assert len(make_title(long)) <= 100 and make_title(long).endswith("…")
    _, desc, caption, _ = build_texts(hook="h", full_script="é" * 6000, source_refs=[], credits=[], ai_content=False,
                                      hashtags=["#a"], language="fr")
    assert len(desc.encode("utf-8")) <= 5000 and len(caption) <= 2200


def test_hashtags_config_accepts_words_without_hash():
    cfg = AppConfig.model_validate({"publishing": {"hashtags": ["Trump", "#news"], "instagram": {"user_id": 1784}}})
    assert cfg.publishing.hashtags == ["#Trump", "#news"] and cfg.publishing.instagram.user_id == "1784"
    with pytest.raises(Exception):
        AppConfig.model_validate({"publishing": {"hashtags": ["two words"]}})


# --------------------------------------------------------------------- service
def test_publish_records_each_platform_and_never_duplicates(tmp_path):
    pubs = {"youtube": FakePublisher("youtube"),
            "tiktok": FakePublisher("tiktok", error=PublisherUnavailableError("quota"))}
    svc, factory = service(tmp_path, pubs)
    plan, out = svc.publish(1)
    assert [o.status for o in out] == ["PUBLISHED", "FAILED"] and out[1].retryable
    req = pubs["youtube"].calls[0]
    assert req.title == "Trump visited Kentucky (CBS News)" and req.video_path.name == "video_0001.mp4"
    with session_scope(factory) as s:
        assert s.get(Video, 1).status == "PUBLISHED"
        rows = {r.platform: r.status for r in s.query(Publication)}
    assert rows == {"youtube": "PUBLISHED", "tiktok": "FAILED"}
    pubs["tiktok"].error = None
    plan, out = svc.publish(1)                     # YouTube déjà fait : seul TikTok est retenté
    assert plan.skipped == {"youtube": "déjà publiée"} and [o.platform for o in out] == ["tiktok"]
    assert len(pubs["youtube"].calls) == 1


@pytest.mark.parametrize("kw, expected", [
    ({"report": "NEEDS_REVIEW"}, "Contrôle qualité"),
    ({"script": "DRAFT"}, "non approuvé"),
    ({"script": "REJECTED"}, "refusé"),
    ({"verdict": "BLOCKED"}, "BLOCKED"),
    ({"preview": True}, "aperçu"),
])
def test_guards_block_publication(tmp_path, kw, expected):
    pubs = {"youtube": FakePublisher("youtube"), "tiktok": FakePublisher("tiktok")}
    svc, _ = service(tmp_path, pubs, **kw)
    with pytest.raises(TrumpNewsError, match=expected):
        svc.publish(1)
    assert not pubs["youtube"].calls and not pubs["tiktok"].calls


def test_approval_can_be_disabled_but_other_guards_remain(tmp_path):
    factory, _ = make_db(tmp_path, script="DRAFT")
    cfg = AppConfig.model_validate({"publishing": {"require_approval": False, "youtube": {"enabled": True}}})
    pub = FakePublisher("youtube")
    _, out = PublishingService(cfg, factory, lambda n: pub).publish(1)
    assert out[0].status == "PUBLISHED"


def test_no_platform_enabled(tmp_path):
    factory, _ = make_db(tmp_path)
    plan = PublishingService(AppConfig(), factory, lambda n: None).prepare(1)
    assert not plan.ok and "Aucune plateforme" in plan.problems[0]


def test_pending_publication_is_followed_up(tmp_path):
    pubs = {"youtube": FakePublisher("youtube"), "tiktok": FakePublisher("tiktok", PublishResult("tiktok", "PENDING", "pub1"))}
    svc, factory = service(tmp_path, pubs)
    svc.publish(1, ["tiktok"])
    _, out = svc.publish(1, ["tiktok"])            # en cours : pas de second envoi
    assert out == [] and len(pubs["tiktok"].calls) == 1
    done = svc.refresh_pending()
    assert done[0].status == "PUBLISHED" and svc.list()[0].url == "https://done.example"


# ----------------------------------------------------------------------- OAuth
def test_pkce_formats():
    import base64
    import hashlib
    v, c = pkce_pair()
    assert 43 <= len(v) <= 128 and c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    v, c = pkce_pair(hex_challenge=True)
    assert c == hashlib.sha256(v.encode()).hexdigest()


def _browser(code="CODE", tamper_state=False):
    """Simule le navigateur : suit la redirection vers le serveur local avec le code."""
    def opener(url):
        q = parse_qs(urlparse(url).query)
        redirect, state = q["redirect_uri"][0], q["state"][0]
        target = redirect + "?" + urlencode({"code": code, "state": "x" if tamper_state else state})
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return opener


def test_wait_for_code_local_server():
    build = lambda redirect, state: "https://auth.example/?" + urlencode({"redirect_uri": redirect, "state": state})  # noqa: E731
    code, redirect = wait_for_code(build, opener=_browser(), timeout_s=5)
    assert code == "CODE" and redirect.startswith("http://127.0.0.1:")
    with pytest.raises(PublisherError, match="state"):
        wait_for_code(build, opener=_browser(tamper_state=True), timeout_s=5)


# --------------------------------------------------------------------- YouTube
CID = "123-abc.apps.googleusercontent.com"


def _yt(store=None, **kw):
    store = store or FakeStore(YOUTUBE_CLIENT_ID=CID, YOUTUBE_CLIENT_SECRET="sec", YOUTUBE_REFRESH_TOKEN="r1")
    return YouTubePublisher(YouTubeSection(**kw), store, client=httpx.Client(), sleep=lambda s: None), store


def _req(tmp_path, ai=False):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"A" * 1000)
    return PublishRequest(p, "Title", "Desc", "Caption", ["Trump"], "en", ai, 30.0)


@respx.mock
def test_youtube_resumable_upload(tmp_path):
    pub, _ = _yt(privacy="unlisted")
    respx.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": "AT"})
    init = respx.post("https://www.googleapis.com/upload/youtube/v3/videos").respond(
        200, headers={"Location": "https://upload.example/session1"})
    put = respx.put("https://upload.example/session1").respond(201, json={"id": "abc123"})
    res = pub.publish(_req(tmp_path, ai=True))
    assert res.status == "PUBLISHED" and res.url == "https://www.youtube.com/shorts/abc123"
    body = json.loads(init.calls[0].request.content)
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
    assert body["snippet"]["categoryId"] == "25" and init.calls[0].request.headers["X-Upload-Content-Length"] == "1000"
    assert init.calls[0].request.url.params["uploadType"] == "resumable"
    assert put.calls[0].request.headers["Authorization"] == "Bearer AT"


@respx.mock
def test_youtube_resumes_after_server_error(tmp_path):
    pub, _ = _yt()
    respx.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": "AT"})
    respx.post("https://www.googleapis.com/upload/youtube/v3/videos").respond(200, headers={"Location": "https://upload.example/s"})
    calls = []

    def put(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        if request.headers.get("Content-Range") == "bytes */1000":
            return httpx.Response(308, headers={"Range": "bytes=0-399"})
        assert request.headers["Content-Range"] == "bytes 400-999/1000" and len(request.content) == 600
        return httpx.Response(200, json={"id": "v2"})

    respx.put("https://upload.example/s").mock(side_effect=put)
    assert pub.publish(_req(tmp_path)).platform_video_id == "v2"


@respx.mock
def test_youtube_errors(tmp_path):
    pub, _ = _yt()
    respx.post("https://oauth2.googleapis.com/token").respond(400, json={"error": "invalid_grant"})
    with pytest.raises(PublisherError, match="connect youtube"):
        pub.publish(_req(tmp_path))
    pub, _ = _yt(FakeStore(YOUTUBE_CLIENT_ID=CID, YOUTUBE_CLIENT_SECRET="sec"))
    with pytest.raises(PublisherError, match="non connecté"):
        pub.publish(_req(tmp_path))
    respx.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": "AT"})
    respx.post("https://www.googleapis.com/upload/youtube/v3/videos").respond(
        403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    pub, _ = _yt()
    with pytest.raises(PublisherUnavailableError, match="Quota"):
        pub.publish(_req(tmp_path))


@respx.mock
def test_youtube_connect_stores_refresh_token():
    pub, store = _yt(FakeStore(YOUTUBE_CLIENT_ID=CID, YOUTUBE_CLIENT_SECRET="sec"))
    token = respx.post("https://oauth2.googleapis.com/token").respond(200, json={"access_token": "AT", "refresh_token": "RT"})
    respx.get("https://www.googleapis.com/youtube/v3/channels").respond(200, json={"items": [{"snippet": {"title": "Ma chaîne"}}]})
    assert pub.connect(opener=_browser("C0DE"), timeout_s=5) == "Ma chaîne"
    form = parse_qs(token.calls[0].request.content.decode())
    assert form["code"] == ["C0DE"] and form["grant_type"] == ["authorization_code"] and form["code_verifier"]
    assert store.values["YOUTUBE_REFRESH_TOKEN"] == "RT"


def test_youtube_client_id_is_checked_before_opening_browser():
    opened = []
    pub, _ = _yt(FakeStore(YOUTUBE_CLIENT_ID=" " + CID + "\n", YOUTUBE_CLIENT_SECRET=" sec "))
    assert pub._client_ids() == (CID, "sec")
    pub, _ = _yt(FakeStore(YOUTUBE_CLIENT_ID="726216052065-gaih", YOUTUBE_CLIENT_SECRET="sec"))
    with pytest.raises(PublisherError, match="apps.googleusercontent.com"):
        pub.connect(opener=opened.append, timeout_s=5)
    assert opened == []


# ---------------------------------------------------------------------- TikTok
def test_tiktok_chunk_plan():
    assert chunk_plan(1000) == (1000, 1)
    assert chunk_plan(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size = 100 * 1024 * 1024 + 123
    chunk, n = chunk_plan(size)
    assert chunk == 10 * 1024 * 1024 and n == 10 and size - chunk * (n - 1) <= 128 * 1024 * 1024


def _tt(**kw):
    store = FakeStore(TIKTOK_CLIENT_KEY="ck", TIKTOK_CLIENT_SECRET="cs", TIKTOK_REFRESH_TOKEN="r1")
    pub = TikTokPublisher(TikTokSection(**kw), store, client=httpx.Client(), sleep=lambda s: None, poll_tries=3)
    return pub, store


API = "https://open.tiktokapis.com/v2"
CREATOR = {"data": {"creator_username": "me", "creator_nickname": "Me", "max_video_post_duration_sec": 600,
                    "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"], "comment_disabled": True},
           "error": {"code": "ok"}}


@respx.mock
def test_tiktok_direct_post(tmp_path):
    pub, store = _tt()
    respx.post(f"{API}/oauth/token/").respond(200, json={"access_token": "AT", "refresh_token": "r2"})
    respx.post(f"{API}/post/publish/creator_info/query/").respond(200, json=CREATOR)
    init = respx.post(f"{API}/post/publish/video/init/").respond(
        200, json={"data": {"publish_id": "p1", "upload_url": "https://up.tiktok.example/u"}, "error": {"code": "ok"}})
    up = respx.put("https://up.tiktok.example/u").respond(201)
    statuses = iter([{"status": "PROCESSING_UPLOAD"}, {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [777]}])
    respx.post(f"{API}/post/publish/status/fetch/").mock(
        side_effect=lambda r: httpx.Response(200, json={"data": next(statuses), "error": {"code": "ok"}}))
    res = pub.publish(_req(tmp_path, ai=True))
    assert res.status == "PUBLISHED" and res.url == "https://www.tiktok.com/@me/video/777"
    body = json.loads(init.calls[0].request.content)
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    assert body["post_info"]["privacy_level"] == "SELF_ONLY" and body["post_info"]["is_aigc"] is True
    assert body["post_info"]["disable_comment"] is True and body["post_info"]["title"] == "Caption"
    assert up.calls[0].request.headers["Content-Range"] == "bytes 0-999/1000"
    assert store.values["TIKTOK_REFRESH_TOKEN"] == "r2"


@respx.mock
def test_tiktok_refuses_disallowed_privacy_and_explains_audit(tmp_path):
    pub, _ = _tt(privacy="FOLLOWER_OF_CREATOR")
    respx.post(f"{API}/oauth/token/").respond(200, json={"access_token": "AT"})
    respx.post(f"{API}/post/publish/creator_info/query/").respond(200, json=CREATOR)
    with pytest.raises(PublisherError, match="non permise"):
        pub.publish(_req(tmp_path))
    pub, _ = _tt()
    respx.post(f"{API}/post/publish/video/init/").respond(
        403, json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}})
    with pytest.raises(PublisherError, match="audité"):
        pub.publish(_req(tmp_path))


@respx.mock
def test_tiktok_pending_when_processing_is_long(tmp_path):
    pub, _ = _tt()
    respx.post(f"{API}/oauth/token/").respond(200, json={"access_token": "AT"})
    respx.post(f"{API}/post/publish/creator_info/query/").respond(200, json=CREATOR)
    respx.post(f"{API}/post/publish/video/init/").respond(
        200, json={"data": {"publish_id": "p1", "upload_url": "https://up.tiktok.example/u"}, "error": {"code": "ok"}})
    respx.put("https://up.tiktok.example/u").respond(201)
    respx.post(f"{API}/post/publish/status/fetch/").respond(200, json={"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})
    res = pub.publish(_req(tmp_path))
    assert res.status == "PENDING" and res.platform_video_id == "p1"


# ------------------------------------------------------------------- Instagram
G = "https://graph.facebook.com/v25.0"


@respx.mock
def test_instagram_resumable_reel(tmp_path):
    store = FakeStore(INSTAGRAM_ACCESS_TOKEN="PT")
    pub = InstagramPublisher(InstagramSection(), "17841", store, client=httpx.Client(), sleep=lambda s: None, poll_tries=3)
    create = respx.post(f"{G}/17841/media").respond(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/C1"})
    up = respx.post("https://rupload.facebook.com/ig-api-upload/v25.0/C1").respond(200, json={"success": True})
    codes = iter(["IN_PROGRESS", "FINISHED"])
    respx.get(f"{G}/C1").mock(side_effect=lambda r: httpx.Response(200, json={"status_code": next(codes)}))
    pubcall = respx.post(f"{G}/17841/media_publish").respond(200, json={"id": "M9"})
    respx.get(f"{G}/M9").respond(200, json={"permalink": "https://www.instagram.com/reel/xyz/"})
    res = pub.publish(_req(tmp_path))
    assert res.status == "PUBLISHED" and res.url == "https://www.instagram.com/reel/xyz/"
    form = parse_qs(create.calls[0].request.content.decode())
    assert form["media_type"] == ["REELS"] and form["upload_type"] == ["resumable"] and form["caption"] == ["Caption"]
    h = up.calls[0].request.headers
    assert h["Authorization"] == "OAuth PT" and h["offset"] == "0" and h["file_size"] == "1000"
    assert parse_qs(pubcall.calls[0].request.content.decode())["creation_id"] == ["C1"]


@respx.mock
def test_instagram_connect_finds_account_and_expired_token(tmp_path):
    store = FakeStore()
    pub = InstagramPublisher(InstagramSection(), "", store, client=httpx.Client())
    respx.get(f"{G}/me").respond(200, json={"id": "P1", "name": "Page", "instagram_business_account": {"id": "1784", "username": "tn"}})
    assert pub.connect("PT") == ("1784", "tn") and store.values["INSTAGRAM_ACCESS_TOKEN"] == "PT"
    respx.post(f"{G}/1784/media").respond(400, json={"error": {"code": 190, "message": "expired"}})
    with pytest.raises(PublisherError, match="connect instagram"):
        pub.publish(_req(tmp_path))


@respx.mock
def test_instagram_pending_then_finished_on_refresh(tmp_path):
    store = FakeStore(INSTAGRAM_ACCESS_TOKEN="PT")
    pub = InstagramPublisher(InstagramSection(), "1784", store, client=httpx.Client(), sleep=lambda s: None, poll_tries=1)
    respx.post(f"{G}/1784/media").respond(200, json={"id": "C1"})
    respx.post("https://rupload.facebook.com/ig-api-upload/v25.0/C1").respond(200, json={"success": True})
    route = respx.get(f"{G}/C1").respond(200, json={"status_code": "IN_PROGRESS"})
    res = pub.publish(_req(tmp_path))
    assert res.status == "PENDING" and res.platform_video_id == "container:C1"
    route.respond(200, json={"status_code": "FINISHED"})
    respx.post(f"{G}/1784/media_publish").respond(200, json={"id": "M1"})
    respx.get(f"{G}/M1").respond(200, json={"permalink": "https://www.instagram.com/reel/a/"})
    assert pub.status("container:C1").status == "PUBLISHED"


# ------------------------------------------------------------------------- CLI
def test_cli_publish_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    make_db(tmp_path / "data")
    (tmp_path / "config.yaml").write_text("storage:\n  database: p.db\npublishing:\n  youtube:\n    enabled: true\n",
                                          encoding="utf-8")
    assert main(["publish", "1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Titre : Trump visited Kentucky (CBS News)" in out and "Publiable sur : youtube" in out
