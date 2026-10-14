import os
import json

import httpx
import pytest

from app.config import settings
from app.models import ModelProfile
from app.services.providers import ProviderError, discover_local, embed, generate
from app.security import encrypt


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_cloud_and_local_protocols_are_explicit(client, monkeypatch, provider):
    calls = []
    def respond(self, url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith(("/chat/completions", "/api/chat")):
            answer = json.dumps({"status":"answered","answer":"依据 [1]","citation_ids":["1"]})
            body = {"choices":[{"message":{"content":answer}}],"usage":{"prompt_tokens":12}} if provider == "openai" else {
                "message":{"content":answer},"prompt_eval_count":12,"eval_count":8}
        else:
            body = {"data":[{"index":1,"embedding":[0.,1.]},{"index":0,"embedding":[1.,0.]}]} if provider == "openai" else {
                "embeddings":[[1.,0.],[0.,1.]]}
        return httpx.Response(200,json=body,request=httpx.Request("POST",url))
    monkeypatch.setattr(httpx.Client,"post",respond)
    p=ModelProfile(provider=provider,base_url="http://configured/v1" if provider=="openai" else "http://configured",
        model="test-model",temperature=.1,dimension=2,secret=encrypt("test-key"))
    response=generate(p,"system","question",structured=True)
    assert response["citation_ids"]==["1"] and response["_usage"]["prompt_tokens"]==12
    assert embed(["one","two"],p)==[[1.,0.],[0.,1.]]
    assert len(calls)==2 and all(url.startswith(p.base_url) for url,_ in calls)
    if provider=="openai":
        assert calls[0][1]["headers"]["Authorization"]=="Bearer test-key"
        assert calls[0][1]["json"]["response_format"]=={"type":"json_object"}
    else:
        assert calls[0][1]["json"]["format"]=="json"
        assert "headers" not in calls[0][1]


def test_provider_failure_is_not_empty_knowledge(client, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("private-provider-information")
    monkeypatch.setattr(httpx.Client, "post", fail)
    with pytest.raises(ProviderError, match="模型服务不可用") as exc:
        generate(ModelProfile(provider="ollama",base_url="http://local",model="example",temperature=.1),"system","question")
    assert "private" not in str(exc.value)


def test_semantica_graph_provenance_and_snapshot(client, monkeypatch):
    from .conftest import login, post
    monkeypatch.setenv("SEMANTICA_ENABLED", "true")
    settings.cache_clear()
    login(client)
    kb = post(client,"/admin/knowledge-bases",{"name":"Semantica 集成"})
    post(client,f"/admin/knowledge-bases/{kb['id']}/knowledge",{"title":"极限","kind":"concept","content":"极限示例",
        "reviewed":True,"sources":[{"locator":"人工示例","note":"integration test"}]})
    task = post(client,f"/admin/knowledge-bases/{kb['id']}/publish")
    assert task["state"] == "succeeded", task
    assert (settings().storage_dir/"semantic"/kb["id"]/"versions.db").exists()
    assert (settings().storage_dir/"semantic"/kb["id"]/"provenance.db").exists()


@pytest.mark.skipif(not os.environ.get("RUN_OLLAMA_TESTS"), reason="requires explicit local Ollama integration run")
def test_real_local_embedding_discovery_and_generation(client):
    url = os.environ.get("OLLAMA_TEST_URL", "http://127.0.0.1:11434")
    found = discover_local(url)
    assert any(m["embedding"] for m in found)
    model = next(m for m in found if m["name"] == "bge-m3:567m")
    p = ModelProfile(provider="ollama",purpose="embedding",model=model["name"],base_url=url,dimension=model["dimension"],secret="")
    vectors = embed(["函数连续与可导的关系", "不定积分需要积分常数"],p)
    assert len(vectors) == 2 and len(vectors[0]) == model["dimension"]
