import pytest

from app.config.loader import load_config
from app.core.errors import ConfigError


def test_defaults_without_file(tmp_path):
    cfg = load_config(base_dir=tmp_path, environ={})
    assert cfg.llm.endpoint == "http://localhost:1234"
    assert cfg.llm.model == "auto"
    assert cfg.video.resolution == "1080x1920"
    assert cfg.scheduler.timezone == "Europe/Paris"


def test_yaml_and_env_override(tmp_path):
    (tmp_path / "config.yaml").write_text("llm:\n  endpoint: http://10.0.0.5:1234\n  retries: 2\n", encoding="utf-8")
    cfg = load_config(base_dir=tmp_path, environ={"TRUMPNEWS__LLM__RETRIES": "5", "TRUMPNEWS__SCHEDULER__ENABLED": "false"})
    assert cfg.llm.endpoint == "http://10.0.0.5:1234"
    assert cfg.llm.retries == 5
    assert cfg.scheduler.enabled is False


def test_invalid_values_give_readable_error(tmp_path):
    (tmp_path / "config.yaml").write_text("llm:\n  endpoint: localhost\nscheduler:\n  times: ['25:99']\n", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(base_dir=tmp_path, environ={})
    assert "llm.endpoint" in str(e.value) and "scheduler.times" in str(e.value)


def test_unknown_key_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("llm:\n  endpont: x\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(base_dir=tmp_path, environ={})


def test_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml", base_dir=tmp_path, environ={})


def test_example_config_is_valid():
    from pathlib import Path

    cfg = load_config(Path("config.example.yaml"), base_dir=Path.cwd(), environ={})
    assert cfg.editorial.mode == "semi_auto"


def test_storage_dirs(tmp_path):
    cfg = load_config(base_dir=tmp_path, environ={})
    root = cfg.storage.ensure_dirs(tmp_path)
    for sub in ("articles", "scripts", "audio", "images", "videos", "thumbnails", "subtitles", "logs"):
        assert (root / sub).is_dir()
