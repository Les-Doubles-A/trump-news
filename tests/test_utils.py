import pytest

from app.core.retry import retry_call
from app.utils.json_tools import extract_json
from app.utils.logging_setup import redact, register_secret


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Voici:\n```json\n{"a": [1,2]}\n```\nfin') == {"a": [1, 2]}
    assert extract_json('<think>hmm {x}</think>{"ok": true}') == {"ok": True}
    assert extract_json('texte [1, 2] suite') == [1, 2]
    with pytest.raises(ValueError):
        extract_json("rien ici")


def test_redact():
    register_secret("supersecretvalue123")
    assert "supersecretvalue123" not in redact("key is supersecretvalue123")
    assert "abc12345" not in redact("api_key=abc12345 foo")
    assert "tokenvalue99" not in redact("Authorization: Bearer tokenvalue99")
    assert "sk-abcdefghijklmnop" not in redact("using sk-abcdefghijklmnop now")


def test_retry_backoff_and_giveup():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "ok"

    assert retry_call(flaky, attempts=3, retry_on=(ConnectionError,), sleep=sleeps.append) == "ok"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 1.2

    def always():
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        retry_call(always, attempts=2, retry_on=(ConnectionError,), sleep=lambda s: None)


def test_non_retryable_not_retried():
    n = []

    def f():
        n.append(1)
        raise ValueError("no")

    with pytest.raises(ValueError):
        retry_call(f, attempts=5, retry_on=(ConnectionError,), sleep=lambda s: None)
    assert len(n) == 1
