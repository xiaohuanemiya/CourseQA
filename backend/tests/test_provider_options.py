import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import ModelProfile
from app.services import providers
from .conftest import post


@pytest.mark.parametrize("provider, base_url", [
    ("openai", "https://api.deepseek.com"),
    ("openai", "https://example.com/v1"),
    ("ollama", "http://local-model"),
])
def test_application_answer_has_no_platform_budget_and_long_history_is_preserved(
        client, platform, monkeypatch, provider, base_url):
    with Session(engine()) as db:
        profile = db.get(ModelProfile, platform["generation"]["id"])
        profile.provider, profile.base_url = provider, base_url
        db.commit()
    answer = "导数定义与推导 [1]。\n" * 4000 + "完整回答末尾 [1]。"
    content = json.dumps({"status": "answered", "answer": answer, "citation_ids": ["1"]})
    captured = []

    def handler(request):
        assert str(request.url).startswith(base_url)
        captured.append(json.loads(request.content))
        if provider == "ollama":
            data = {"done_reason": "stop", "message": {"content": content}, "eval_count": 4001}
        else:
            data = {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
                    "usage": {"completion_tokens": 4001}}
        return httpx.Response(200, json=data)

    original = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    app_id = platform["bundles"][0]["app"]["id"]
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["answer"] == answer and result["status"] == "answered"
    assert len(captured) == 1
    payload = captured[0]
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(payload)
    if provider == "ollama":
        assert payload["options"]["num_predict"] == -1
    history = client.get(f"/api/apps/{app_id}/conversations/{result['conversation_id']}").json()
    assert history[0]["result"]["answer"] == answer
    assert history[0]["result"]["citations"] == result["citations"]


@pytest.mark.parametrize("host, expects_thinking", [("api.deepseek.com", True), ("example.com", False)])
def test_split_budget_and_deepseek_option_only_on_official_endpoint(monkeypatch, host, expects_thinking):
    captured = []
    def handler(request):
        import json
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"groups":[]}'}}]})
    original = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(providers, "decrypt", lambda _: "test")
    profile = SimpleNamespace(provider="openai", base_url=f"https://{host}", secret="test", model="flash", temperature=0.1)
    providers.generate(profile, "system", "prompt", structured=True, max_tokens=6000, thinking=False)
    assert captured[0]["max_tokens"] == 6000
    assert ("thinking" in captured[0]) == expects_thinking
    if expects_thinking:
        assert captured[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_truncated_structured_output_rejected_even_when_json_is_valid(monkeypatch, provider):
    original = httpx.Client
    result = ({"choices": [{"finish_reason": "length", "message": {"content": '{"groups":[]}'}}]}
              if provider == "openai" else {"done_reason": "length", "message": {"content": '{"groups":[]}'}})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=result))
    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(providers, "decrypt", lambda _: "test")
    profile = SimpleNamespace(provider=provider, base_url="https://example.com", secret="test", model="flash", temperature=0.1)
    with pytest.raises(providers.ProviderError, match="自身输出或上下文上限"):
        providers.generate(profile, "system", "prompt", structured=True)


def test_vision_blocks_are_sent_in_user_message_with_original_detail(monkeypatch):
    import json
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"reviews":[]}'}}]})
    original = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(providers, "decrypt", lambda _: "test")
    profile = SimpleNamespace(provider="openai", base_url="https://api.deepseek.com", secret="test", model="deepseek-flash", temperature=.1)
    content = [{"type": "text", "text": "核对公式"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST", "detail": "original"}}]
    providers.generate(profile, "system", content, structured=True, thinking=True, reasoning_effort="low", max_tokens=12000)
    assert captured[0]["messages"][1] == {"role": "user", "content": content}
    assert captured[0]["thinking"] == {"type": "enabled"} and captured[0]["reasoning_effort"] == "low"
    assert captured[0]["response_format"] == {"type": "json_object"}
