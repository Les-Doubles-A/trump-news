import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.config.schema import LLMSection
from app.core.errors import ConfigError, LLMResponseError, LLMUnavailableError
from app.core.interfaces import ChatMessage
from app.providers.llm.factory import build_llm_provider
from app.providers.llm.lmstudio import LMStudioProvider

BASE = "http://localhost:1234/v1"
MODELS = {"data": [{"id": "text-embedding-nomic-embed"}, {"id": "qwen2.5-7b-instruct"}, {"id": "llama-3.1-8b"}]}


def chat_payload(text, model="qwen2.5-7b-instruct"):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture()
def llm():
    p = LMStudioProvider(retries=3, sleep=lambda s: None)
    yield p
    p.close()


@respx.mock
def test_list_models_filters_embeddings(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    assert llm.list_models() == ["qwen2.5-7b-instruct", "llama-3.1-8b"]


@respx.mock
def test_health_ok_and_down(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    st = llm.health_check()
    assert st.ok and "qwen2.5-7b-instruct" in st.models
    respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
    st = llm.health_check()
    assert not st.ok and "LM Studio" in st.detail


@respx.mock
def test_health_no_model_loaded(llm):
    respx.get(f"{BASE}/models").respond(200, json={"data": []})
    st = llm.health_check()
    assert not st.ok and "aucun modèle" in st.detail


@respx.mock
def test_complete_auto_model_and_usage(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=chat_payload("Bonjour"))
    r = llm.complete([ChatMessage("user", "salut")])
    assert r.text == "Bonjour" and r.usage.total_tokens == 15
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "qwen2.5-7b-instruct" and sent["stream"] is False


@respx.mock
def test_explicit_model_skips_listing():
    p = LMStudioProvider(model="llama-3.1-8b", sleep=lambda s: None)
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=chat_payload("x", "llama-3.1-8b"))
    p.complete([ChatMessage("user", "hi")])
    assert json.loads(route.calls[0].request.content)["model"] == "llama-3.1-8b"


@respx.mock
def test_retries_on_5xx_then_succeeds(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [httpx.Response(503, text="busy"), httpx.ConnectError("x"), httpx.Response(200, json=chat_payload("ok"))]
    assert llm.complete([ChatMessage("user", "hi")]).text == "ok"
    assert route.call_count == 3


@respx.mock
def test_unavailable_after_retries(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMUnavailableError):
        llm.complete([ChatMessage("user", "hi")])
    assert route.call_count == 3


@respx.mock
def test_4xx_not_retried(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/chat/completions").respond(400, json={"error": "bad"})
    with pytest.raises(LLMResponseError):
        llm.complete([ChatMessage("user", "hi")])
    assert route.call_count == 1


@respx.mock
def test_empty_or_malformed_response(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    respx.post(f"{BASE}/chat/completions").respond(200, json={"choices": []})
    with pytest.raises(LLMResponseError):
        llm.complete([ChatMessage("user", "hi")])


class Claim(BaseModel):
    claim: str
    status: str
    confidence: float


@respx.mock
def test_complete_json_with_schema_and_repair(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [
        httpx.Response(200, json=chat_payload("Désolé, voici: pas de json")),
        httpx.Response(200, json=chat_payload('```json\n{"claim":"x","status":"REPORTED","confidence":0.6}\n```')),
    ]
    out = llm.complete_json([ChatMessage("user", "go")], schema=Claim)
    assert out == Claim(claim="x", status="REPORTED", confidence=0.6)
    first = json.loads(route.calls[0].request.content)
    assert first["response_format"]["type"] == "json_schema"
    second = json.loads(route.calls[1].request.content)
    assert "JSON" in second["messages"][-1]["content"]


@respx.mock
def test_complete_json_gives_up(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    respx.post(f"{BASE}/chat/completions").respond(200, json=chat_payload("pas de json"))
    with pytest.raises(LLMResponseError):
        llm.complete_json([ChatMessage("user", "go")], repair_attempts=1)


def test_endpoint_normalisation():
    assert LMStudioProvider(endpoint="http://localhost:1234/v1/").base_url == "http://localhost:1234"


def test_factory():
    assert isinstance(build_llm_provider(LLMSection()), LMStudioProvider)
    with pytest.raises(ConfigError):
        build_llm_provider(LLMSection(provider="openai"))


def test_is_text_model():
    from app.providers.llm.lmstudio import is_text_model

    assert is_text_model("qwen/qwen3-8b") and is_text_model("qwen/qwen3-14b")
    assert not is_text_model("qwen/qwen2.5-vl-7b")
    assert not is_text_model("openwebui-title-generator-270m")


@respx.mock
def test_auto_skips_vision_and_tiny_models():
    p = LMStudioProvider(sleep=lambda s: None)
    respx.get(f"{BASE}/models").respond(200, json={"data": [{"id": "qwen/qwen2.5-vl-7b"}, {"id": "openwebui-title-generator-270m"}, {"id": "qwen/qwen3-8b"}]})
    assert p.resolve_model() == "qwen/qwen3-8b"


@respx.mock
def test_auto_falls_back_when_model_fails_to_load():
    p = LMStudioProvider(sleep=lambda s: None)
    respx.get(f"{BASE}/models").respond(200, json={"data": [{"id": "qwen/qwen3-14b"}, {"id": "qwen/qwen3-8b"}]})
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": 'Failed to load model "qwen3-14b".'}}),
        httpx.Response(200, json=chat_payload("ok", "qwen/qwen3-8b")),
    ]
    assert p.complete([ChatMessage("user", "hi")]).text == "ok"
    assert p.resolve_model() == "qwen/qwen3-8b"


@respx.mock
def test_fallback_after_model_was_resolved_in_advance():
    """Cas réel : le fact-check appelle resolve_model() (qui retient qwen3-14b) avant complete()."""
    p = LMStudioProvider(sleep=lambda s: None)
    respx.get(f"{BASE}/models").respond(200, json={"data": [{"id": "qwen/qwen3-14b"}, {"id": "qwen/qwen3-8b"}]})
    assert p.resolve_model() == "qwen/qwen3-14b"
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": 'Failed to load model "qwen3-14b".'}}),
        httpx.Response(200, json=chat_payload("ok", "qwen/qwen3-8b")),
    ]
    assert p.complete([ChatMessage("user", "hi")]).text == "ok"
    assert p.resolve_model() == "qwen/qwen3-8b"
    assert json.loads(route.calls[1].request.content)["model"] == "qwen/qwen3-8b"


@respx.mock
def test_explicit_model_load_error_is_explained():
    from app.core.errors import LLMModelLoadError

    p = LMStudioProvider(model="qwen/qwen3-14b", sleep=lambda s: None)
    respx.post(f"{BASE}/chat/completions").respond(400, json={"error": {"message": "Failed to load model"}})
    with pytest.raises(LLMModelLoadError) as e:
        p.complete([ChatMessage("user", "hi")])
    assert "VRAM" in str(e.value)


@respx.mock
def test_think_blocks_removed_from_text(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    respx.post(f"{BASE}/chat/completions").respond(200, json=chat_payload("<think>je réfléchis</think>\n\nBonjour"))
    assert llm.complete([ChatMessage("user", "hi")]).text == "Bonjour"


@respx.mock
def test_embed_auto_model_and_order(llm):
    respx.get(f"{BASE}/models").respond(200, json=MODELS)
    route = respx.post(f"{BASE}/embeddings").respond(200, json={"data": [
        {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})
    out = llm.embed(["a", "b"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]
    assert json.loads(route.calls[0].request.content)["model"] == "text-embedding-nomic-embed"


@respx.mock
def test_embed_without_embedding_model(llm):
    respx.get(f"{BASE}/models").respond(200, json={"data": [{"id": "qwen/qwen3-8b"}]})
    with pytest.raises(LLMUnavailableError):
        llm.embed(["a"])
