import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import httpx
import pytest
import respx

from app.audio.narration import NarrationService
from app.audio.wavtools import concat_wavs, wav_duration
from app.config.schema import AppConfig
from app.core.errors import ProviderError, TTSError, TTSUnavailableError
from app.core.interfaces import HealthStatus, ITTSProvider, SynthOptions, SynthResult, Voice
from app.database.models import Script, Story
from app.database.session import create_db_engine, init_db, session_scope
from app.providers.tts.edge import EdgeTTSProvider
from app.providers.tts.elevenlabs import ElevenLabsProvider
from app.providers.tts.piper import PiperProvider, download_voice, voice_urls

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def make_wav(path: Path, seconds: float, rate: int = 22050, freq: int = 440, amp: float = 0.3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)))
    return path


def copy_converter(src: Path, dst: Path):
    """Remplace FFmpeg dans les tests unitaires : le « mp3 » reçu est en réalité un WAV."""
    shutil.copyfile(src, dst)


# ------------------------------------------------------------------ wav
def test_concat_wavs_with_pauses(tmp_path):
    a, b = make_wav(tmp_path / "a.wav", 1.0), make_wav(tmp_path / "b.wav", 0.5)
    total = concat_wavs([a, b], [0.25, 0.0], tmp_path / "o.wav")
    assert total == pytest.approx(1.75, abs=0.01) and wav_duration(tmp_path / "o.wav") == pytest.approx(1.75, abs=0.01)
    c = make_wav(tmp_path / "c.wav", 0.5, rate=16000)
    with pytest.raises(ProviderError):
        concat_wavs([a, c], [0, 0], tmp_path / "x.wav")
    with pytest.raises(ValueError):
        concat_wavs([a], [0, 0], tmp_path / "y.wav")


# ---------------------------------------------------------------- piper
class FakeRun:
    def __init__(self, rc=0, write=True):
        self.cmd, self.input, self.rc, self.write = None, None, rc, write

    def __call__(self, cmd, input=None, **kw):
        self.cmd, self.input = cmd, input
        if self.write:
            make_wav(Path(cmd[cmd.index("--output_file") + 1]), 1.2)
        return subprocess.CompletedProcess(cmd, self.rc, b"", b"model error")


def test_piper_synthesize_and_errors(tmp_path):
    models = tmp_path / "voices"
    models.mkdir()
    (models / "en_US-lessac-medium.onnx").write_bytes(b"x")
    exe = tmp_path / "piper.exe"
    exe.write_bytes(b"x")
    run = FakeRun()
    p = PiperProvider(str(exe), models, runner=run)
    res = p.synthesize("Hello world", tmp_path / "o.wav", SynthOptions("en_US-lessac-medium", "en", speed=1.25))
    assert res.duration_s == pytest.approx(1.2, abs=0.01) and run.input == b"Hello world"
    assert run.cmd[run.cmd.index("--length_scale") + 1] == "0.800"                      # vitesse x1.25
    with pytest.raises(TTSError, match="download-voice"):
        p.synthesize("x", tmp_path / "o2.wav", SynthOptions("fr_FR-siwis-medium", "fr"))
    with pytest.raises(TTSError, match="Piper a échoué"):
        PiperProvider(str(exe), models, runner=FakeRun(rc=1, write=False)).synthesize("x", tmp_path / "o3.wav", SynthOptions("en_US-lessac-medium"))
    with pytest.raises(TTSError, match="introuvable"):
        PiperProvider("piper-absent", models).synthesize("x", tmp_path / "o4.wav", SynthOptions("en_US-lessac-medium"))
    assert [v.id for v in p.list_voices("en")] == ["en_US-lessac-medium"] and p.list_voices("fr") == []
    assert p.health_check().ok and not PiperProvider("piper-absent", models).health_check().ok


@respx.mock
def test_piper_voice_download(tmp_path):
    urls = voice_urls("en_US-lessac-medium")
    assert urls[0] == "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
    for u in urls:
        respx.get(u).respond(200, content=b"data")
    files = download_voice("en_US-lessac-medium", tmp_path / "v")
    assert [f.name for f in files] == ["en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json"]
    respx.get(urls[0]).respond(404)
    with pytest.raises(TTSError):
        download_voice("en_US-lessac-medium", tmp_path / "v2")
    with pytest.raises(TTSError, match="invalide"):
        voice_urls("nope")


# ----------------------------------------------------------------- edge
class FakeCommunicate:
    last = None

    def __init__(self, text, voice, rate="+0%", pitch="+0Hz", fail=False):
        FakeCommunicate.last = (text, voice, rate, pitch)
        self.fail = fail

    async def stream(self):
        if self.fail:
            raise ConnectionError("réseau coupé")
        yield {"type": "audio", "data": make_bytes()}
        yield {"type": "WordBoundary", "offset": 5_000_000, "duration": 3_000_000, "text": "Hello"}
        yield {"type": "WordBoundary", "offset": 9_000_000, "duration": 4_000_000, "text": "world"}


def make_bytes():
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x01" * 24000)
    return buf.getvalue()


def test_edge_provider_words_and_rate(tmp_path):
    p = EdgeTTSProvider(communicate_factory=FakeCommunicate, converter=copy_converter)
    res = p.synthesize("Hello world", tmp_path / "e.wav", SynthOptions("en-US-AndrewNeural", "en", speed=1.15))
    assert FakeCommunicate.last[2] == "+15%" and res.duration_s == pytest.approx(1.0, abs=0.01)
    assert res.words == [{"text": "Hello", "start": 0.5, "end": 0.8}, {"text": "world", "start": 0.9, "end": 1.3}]
    p.synthesize("x", tmp_path / "e2.wav", SynthOptions("v", speed=0.9))
    assert FakeCommunicate.last[2] == "-10%"


def test_edge_failure_is_retryable(tmp_path):
    p = EdgeTTSProvider(communicate_factory=lambda *a, **k: FakeCommunicate(*a, fail=True, **k), converter=copy_converter)
    with pytest.raises(TTSUnavailableError):
        p.synthesize("x", tmp_path / "e.wav", SynthOptions("v"))


def test_edge_voices_filter():
    raw = [{"ShortName": "fr-FR-HenriNeural", "Locale": "fr-FR", "Gender": "Male", "FriendlyName": "Henri"},
           {"ShortName": "en-US-AndrewNeural", "Locale": "en-US", "Gender": "Male"}]
    p = EdgeTTSProvider(voices_loader=lambda: raw)
    assert [v.id for v in p.list_voices("fr")] == ["fr-FR-HenriNeural"] and len(p.list_voices()) == 2


@pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg requis")
def test_edge_real_mp3_conversion(tmp_path):
    mp3 = tmp_path / "t.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", str(mp3)], check=True)

    class Mp3Communicate(FakeCommunicate):
        async def stream(self):
            yield {"type": "audio", "data": mp3.read_bytes()}

    res = EdgeTTSProvider(communicate_factory=Mp3Communicate).synthesize("x", tmp_path / "o.wav", SynthOptions("v"))
    assert res.duration_s == pytest.approx(1.5, abs=0.15)


# ------------------------------------------------------------ elevenlabs
@respx.mock
def test_elevenlabs(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-secret-key-123")
    route = respx.post("https://api.elevenlabs.io/v1/text-to-speech/VOICE1").respond(200, content=make_bytes())
    p = ElevenLabsProvider("eleven_multilingual_v2", 0.5, 0.75, 0.0, cost_per_1k_chars=0.3, converter=copy_converter)
    res = p.synthesize("a" * 500, tmp_path / "el.wav", SynthOptions("VOICE1", "en", speed=1.5))
    req = route.calls[0].request
    assert req.headers["xi-api-key"] == "xi-secret-key-123" and res.cost_usd == pytest.approx(0.15)
    import json
    body = json.loads(req.content)
    assert body["voice_settings"]["speed"] == 1.2 and body["model_id"] == "eleven_multilingual_v2"   # vitesse bornée à 1.2
    for code, exc in ((401, TTSError), (429, TTSUnavailableError), (503, TTSUnavailableError), (422, TTSError)):
        respx.post("https://api.elevenlabs.io/v1/text-to-speech/VOICE1").respond(code, text="no")
        with pytest.raises(exc) as e:
            p.synthesize("x", tmp_path / "e.wav", SynthOptions("VOICE1"))
        assert type(e.value) is exc
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    from app.core.errors import SecretNotFoundError
    monkeypatch.setattr("app.config.secrets._keyring", lambda: None)
    with pytest.raises(SecretNotFoundError):
        p.synthesize("x", tmp_path / "e.wav", SynthOptions("VOICE1"))


# --------------------------------------------------------------- service
class FakeTTS(ITTSProvider):
    requires_internet = False

    def __init__(self, name="fake", fail_times=0, always_fail=False):
        self.name, self.fail_times, self.always_fail, self.calls, self.texts, self.speeds = name, fail_times, always_fail, 0, [], []

    def health_check(self): return HealthStatus(True, "ok")
    def list_voices(self, language=None): return [Voice("v1", "V1", "en")]

    def synthesize(self, text, out_path, options):
        self.calls += 1
        if self.always_fail:
            raise TTSError("service HS")
        if self.calls <= self.fail_times:
            raise TTSUnavailableError("réseau")
        self.texts.append(text)
        self.speeds.append(options.speed)
        make_wav(out_path, len(text) / 20.0)
        return SynthResult(out_path, wav_duration(out_path), [{"text": "w", "start": 0.0, "end": 0.2}], len(text), 0.01)


SEGS = [
    {"index": 0, "section": "hook", "type": "attributed", "text": "Trump says Zelensky will visit.", "claims": ["C1"]},
    {"index": 1, "section": "main", "type": "fact", "text": "A federal judge extended the block.", "claims": ["C2"]},
    {"index": 2, "section": "conclusion", "type": "context", "text": "We will keep following this.", "claims": []},
]


@pytest.fixture()
def env(tmp_path):
    factory = init_db(create_db_engine(tmp_path / "t.db"))
    with session_scope(factory) as s:
        st = Story(title="t", status="NEW")
        s.add(st)
        s.flush()
        s.add(Script(story_id=st.id, style="breaking_news", tone="neutral", language="en", duration_target=30,
                     full_script="x", claims={"segments": SEGS}))
    return factory, tmp_path


def service(env, providers, **cfg):
    factory, tmp = env
    base = {"tts": {"normalize_loudness": False, "provider": "edge", "fallback": "piper", **cfg}}
    return NarrationService(AppConfig.model_validate(base), factory, tmp / "data", sleep=lambda s: None,
                            provider_factory=lambda n: providers[n])


def test_narration_timeline_pauses_and_db(env):
    prov = FakeTTS("edge")
    res = service(env, {"edge": prov}).narrate(1)
    d = [len(s["text"]) / 20.0 for s in SEGS]
    t = res.timeline
    assert t[0]["start"] == 0 and t[0]["end"] == pytest.approx(d[0], abs=0.01)
    assert t[1]["start"] == pytest.approx(d[0] + 0.4, abs=0.01)                        # pause après le hook
    assert t[2]["start"] == pytest.approx(d[0] + 0.4 + d[1] + 0.25, abs=0.02)
    assert res.duration_s == pytest.approx(sum(d) + 0.4 + 0.25, abs=0.05) and res.audio_path.exists()
    assert t[0]["words"][0]["start"] == 0.0 and t[1]["words"][0]["start"] == t[1]["start"]
    assert res.cost_usd == pytest.approx(0.03) and prov.speeds[0] == pytest.approx(1.08)  # prosodie « breaking_news »


def test_cache_and_force(env):
    prov = FakeTTS("edge")
    svc = service(env, {"edge": prov})
    first = svc.narrate(1)
    second = svc.narrate(1)
    assert second.from_cache and second.id == first.id and prov.calls == 3
    third = svc.narrate(1, force=True)
    assert not third.from_cache and prov.calls == 6
    assert not svc.narrate(1, speed=1.1).from_cache                                   # réglage différent = nouvelle voix off


def test_retry_then_success_and_fallback(env):
    flaky = FakeTTS("edge", fail_times=2)
    res = service(env, {"edge": flaky}).narrate(1)
    assert not res.fallback_used and flaky.calls == 5                                 # 2 échecs réessayés puis 3 segments
    broken, backup = FakeTTS("edge", always_fail=True), FakeTTS("piper")
    res = service(env, {"edge": broken, "piper": backup}).narrate(1, force=True)
    assert res.fallback_used and res.provider == "piper" and "secours" in res.warnings[0]
    with pytest.raises(TTSError, match="Aucun fournisseur"):
        service(env, {"edge": FakeTTS(always_fail=True), "piper": FakeTTS(always_fail=True)}).narrate(1, force=True)


def test_pronunciations_and_missing_voice(env):
    prov = FakeTTS("edge")
    res = service(env, {"edge": prov}, pronunciations={"Zelensky": "Zelenski"}).narrate(1)
    assert "Zelenski" in prov.texts[0] and "Zelensky" in res.timeline[0]["text"]       # lu autrement, affiché tel quel
    with pytest.raises(TTSError, match="voices"):
        service(env, {"elevenlabs": FakeTTS("elevenlabs")}, provider="elevenlabs", fallback=None).narrate(1)


def test_unknown_script(env):
    from app.core.errors import TrumpNewsError

    with pytest.raises(TrumpNewsError):
        service(env, {"edge": FakeTTS()}).narrate(99)


@pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg requis")
def test_real_loudness_normalization(env):
    factory, tmp = env
    quiet = FakeTTS("edge")
    orig = quiet.synthesize
    quiet.synthesize = lambda text, out, o: (make_wav(out, 1.0, amp=0.02), SynthResult(out, 1.0, [], len(text)))[1]
    cfg = {"tts": {"normalize_loudness": True, "provider": "edge", "fallback": None}}
    res = NarrationService(AppConfig.model_validate(cfg), factory, tmp / "data", sleep=lambda s: None,
                           provider_factory=lambda n: quiet).narrate(1)
    out = subprocess.run(["ffmpeg", "-i", str(res.audio_path), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    lufs = float([l for l in out.stderr.splitlines() if "I:" in l and "LUFS" in l][-1].split("I:")[1].split("LUFS")[0])
    assert -19 < lufs < -13                                                              # cible -16 LUFS
    with wave.open(str(res.audio_path)) as w:
        assert w.getframerate() == 22050                                                 # pas de sur-échantillonnage à 192 kHz


def test_config_legacy_local_and_bounds():
    assert AppConfig.model_validate({"tts": {"provider": "local"}}).tts.provider == "piper"
    with pytest.raises(Exception):
        AppConfig.model_validate({"tts": {"speed": 3}})


def test_edge_403_is_explained_and_not_retried(tmp_path):
    class Handshake(Exception):
        status = 403

    class Denied(FakeCommunicate):
        async def stream(self):
            raise Handshake("403, message='Invalid response status'")
            yield  # pragma: no cover

    p = EdgeTTSProvider(communicate_factory=Denied, converter=copy_converter)
    with pytest.raises(TTSError) as e:
        p.synthesize("x", tmp_path / "e.wav", SynthOptions("v"))
    assert not isinstance(e.value, TTSUnavailableError) and "pip install -U edge-tts" in str(e.value) and "Piper" in str(e.value)


def test_narration_falls_back_immediately_on_403(env):
    class Denied(FakeTTS):
        def synthesize(self, text, out_path, options):
            self.calls += 1
            raise TTSError("403 refusé")

    edge, piper = Denied("edge"), FakeTTS("piper")
    res = service(env, {"edge": edge, "piper": piper}).narrate(1)
    assert res.fallback_used and edge.calls == 1          # un seul essai : une erreur 403 n'est pas réessayée
