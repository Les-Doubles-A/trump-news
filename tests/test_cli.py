from pathlib import Path

from app.main import main


def test_init_and_check_llm_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRUMPNEWS__LLM__ENDPOINT", "http://127.0.0.1:9")  # port fermé
    monkeypatch.setenv("TRUMPNEWS__LLM__RETRIES", "1")
    assert main(["init"]) == 0
    assert (tmp_path / "data" / "trump_news.db").exists()
    assert (tmp_path / "data" / "videos").is_dir()
    assert main(["check-llm"]) == 1
    assert "NOT CONNECTED" in capsys.readouterr().out


def test_invalid_config_returns_error_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("llm:\n  endpoint: nope\n", encoding="utf-8")
    assert main(["init"]) == 2
    assert "llm.endpoint" in capsys.readouterr().err


def test_fact_check_command(tmp_path, monkeypatch, capsys):
    import json
    from datetime import datetime, timezone

    import app.main as m
    from app.core.interfaces import ILLMProvider, LLMResponse
    from app.database.models import Article, Story
    from app.database.session import create_db_engine, init_db, session_scope

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("factcheck:\n  fetch_full_text: false\n", encoding="utf-8")
    assert m.main(["init"]) == 0
    factory = init_db(create_db_engine(tmp_path / "data" / "trump_news.db"))
    with session_scope(factory) as s:
        st = Story(title="Judge extends block on Trump media ban", status="NEW")
        s.add(st)
        s.flush()
        s.add(Article(url="https://bbc.co.uk/1", title="Judge extends block", source_name="BBC News", hash="x", story_id=st.id,
                      summary="A federal judge on Monday extended a temporary block on the administration's media ban.",
                      published_at=datetime.now(timezone.utc)))

    class Fake(ILLMProvider):
        name, model = "fake", "fake"
        def health_check(self): ...
        def list_models(self): return []
        def complete(self, messages, **kw):
            return LLMResponse(json.dumps({"claims": [{
                "claim": "A federal judge extended a temporary block on the media ban.", "kind": "fact",
                "supporting": [{"source": "S1", "quote": "judge on Monday extended a temporary block on the administration's media ban"}]}]}), "fake")

    monkeypatch.setattr("app.providers.llm.factory.build_llm_provider", lambda cfg: Fake())
    assert m.main(["fact-check", "1"]) == 0
    out = capsys.readouterr().out
    assert "Verdict : CAUTION" in out and "[REPORTED" in out and "Selon <média>" in out
    assert m.main(["fact-check", "1"]) == 0
    assert "résultat en cache" in capsys.readouterr().out
    assert m.main(["fact-check", "42"]) == 2


def test_voice_commands(tmp_path, monkeypatch, capsys):
    import app.main as m
    from tests.test_tts import FakeTTS

    monkeypatch.chdir(tmp_path)
    fake = FakeTTS("edge")
    monkeypatch.setattr(m, "build_tts_provider", lambda name, cfg: fake)
    assert m.main(["voices", "--lang", "en"]) == 0
    assert "v1" in capsys.readouterr().out
    assert m.main(["tts-test", "Hello there", "--provider", "edge"]) == 0
    out = capsys.readouterr().out
    assert "OK :" in out and (tmp_path / "data" / "audio" / "test_edge.wav").exists()
    (tmp_path / "config.yaml").write_text("tts:\n  edge:\n    voices: {}\n", encoding="utf-8")
    assert m.main(["tts-test", "Hi", "--provider", "edge"]) == 2   # aucune voix configurée : message clair, pas de plantage


def test_tts_test_uses_fallback_when_primary_fails(tmp_path, monkeypatch, capsys):
    import app.main as m
    from app.core.errors import TTSError
    from tests.test_tts import FakeTTS

    monkeypatch.chdir(tmp_path)

    class Down(FakeTTS):
        def synthesize(self, *a, **k):
            raise TTSError("403 refusé")

    monkeypatch.setattr(m, "build_tts_provider", lambda name, cfg: Down("edge") if name == "edge" else FakeTTS("piper"))
    assert m.main(["tts-test", "Hello"]) == 0
    cap = capsys.readouterr()
    assert "voix de secours" in cap.err and "OK :" in cap.out and (tmp_path / "data" / "audio" / "test_piper.wav").exists()
    assert m.main(["tts-test", "Hello", "--provider", "edge"]) == 2          # provider explicite : pas de repli silencieux


def test_reset_config_and_set_option(tmp_path, monkeypatch, capsys):
    import shutil

    import app.main as m

    monkeypatch.chdir(tmp_path)
    shutil.copyfile(Path(__file__).resolve().parent.parent / "config.example.yaml", tmp_path / "config.example.yaml")
    (tmp_path / "config.yaml").write_text("news:\n  providers: []\n", encoding="utf-8")
    assert m.main(["reset-config"]) == 0
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8").startswith("news:")
    assert "NPR Politics" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert m.main(["set-option", "llm.model", "qwen/qwen3-8b"]) == 0
    assert m.main(["set-option", "visuals.wikimedia.contact", "moi@example.com"]) == 0
    assert m.main(["set-option", "llm.model", "qwen/qwen3-14b"]) == 0               # remplace la ligne existante
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env.count("TRUMPNEWS__LLM__MODEL=") == 1 and "qwen3-14b" in env and "MOI@" not in env
    before = env
    assert m.main(["set-option", "llm.retries", "abc"]) == 2                          # valeur invalide : refusée, .env intact
    assert m.main(["set-option", "modele", "x"]) == 2
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before
    capsys.readouterr()
    monkeypatch.delenv("TRUMPNEWS__LLM__MODEL", raising=False)
    from app.config.loader import load_config
    cfg = load_config(base_dir=tmp_path)                                              # .env pris en compte par le chargeur
    assert cfg.llm.model == "qwen/qwen3-14b" and cfg.visuals.wikimedia.contact == "moi@example.com" and len(cfg.news.providers) >= 15


def test_config_with_bom_is_readable(tmp_path):
    from app.config.loader import load_config

    (tmp_path / "config.yaml").write_bytes("\ufeffllm:\n  model: qwen/qwen3-8b  # é\n".encode("utf-8"))
    assert load_config(base_dir=tmp_path, environ={}).llm.model == "qwen/qwen3-8b"
