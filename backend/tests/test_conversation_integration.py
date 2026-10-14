import json

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application, Knowledge
from app.schemas import ChatInput
from app.services import learning, streaming
from .conftest import post
from .test_streaming import cloud_profile, events


@pytest.mark.parametrize("stream", [False, True])
def test_followup_retrieval_and_generation_share_context_without_recursive_storage(
        client, platform, monkeypatch, stream):
    cloud_profile(platform)
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    prompts, queries, query_hints = [], [], []
    original_retrieve = learning.retrieve

    def capture_retrieve(db, app, releases, question, *args, **kwargs):
        queries.append(question)
        query_hints.append(kwargs)
        return original_retrieve(db, app, releases, question, *args, **kwargs)

    def generate(profile, system, prompt, **kwargs):
        prompts.append(json.loads(prompt))
        assert "历史引用编号不得沿用" in system
        assert "不是新的事实证据" in system
        return {"status": "answered", "answer": "第一步辨认复合函数，第二步应用链式法则 [1]。",
                "citation_ids": ["1"]}

    async def stream_generate(profile, system, prompt):
        encoded = json.dumps(generate(profile, system, prompt), ensure_ascii=False)
        for start in range(0, len(encoded), 9):
            yield encoded[start:start + 9]

    monkeypatch.setattr(learning, "retrieve", capture_retrieve)
    monkeypatch.setattr(learning, "generate", generate)
    monkeypatch.setattr(streaming, "stream_generate", stream_generate)

    def send(body):
        url = f"/api/apps/{app_id}/chat" + ("/stream" if stream else "")
        response = client.post(url, json=body)
        if stream:
            frames = events(response)
            assert frames[-1][0] == "done", frames
            return frames[-1][1]
        assert response.status_code == 200, response.text
        return response.json()

    first = send({"message": "请解释导数知识中的链式法则"})
    second = send({"message": "为什么第二步成立？", "conversation_id": first["conversation_id"]})
    assert prompts[0]["conversation_context"]["recent_turns"] == []
    assert prompts[1]["question"] == "为什么第二步成立？"
    assert queries[0] == "请解释导数知识中的链式法则"
    assert queries[1] == "为什么第二步成立？"
    assert "请解释导数知识中的链式法则" in query_hints[1]["topic_hint"]
    previous = prompts[1]["conversation_context"]["recent_turns"][0]
    assert previous["question"] == queries[0]
    assert "第二步应用链式法则 [历史引用 1]" in previous["answer"]
    assert prompts[1]["evidence"][0]["content"] == bundle["node"]["content"]
    metadata = second["retrieval"]["conversation_context"]
    assert metadata["history_count"] == 1 and metadata["followup_applied"]
    assert all(isinstance(value, (int, bool)) for value in metadata.values())
    assert "链式法则" not in json.dumps(second["retrieval"], ensure_ascii=False)
    stored = client.get(f"/api/apps/{app_id}/conversations/{first['conversation_id']}").json()
    assert [message["question"] for message in stored] == [queries[0], "为什么第二步成立？"]
    assert stored[0]["result"] == first and stored[1]["result"] == second


def test_prepare_rejects_foreign_conversation_before_retrieval(client, platform, monkeypatch):
    first_app, other_app = [bundle["app"]["id"] for bundle in platform["bundles"]]
    first = post(client, f"/apps/{first_app}/chat", {"message": "导数知识"})
    monkeypatch.setattr(learning, "retrieve", lambda *a, **kw: pytest.fail("Unauthorized retrieval"))
    response = client.post(f"/api/apps/{other_app}/chat", json={
        "message": "继续", "conversation_id": first["conversation_id"]})
    assert response.status_code == 404
    post(client, "/auth/register", {"username": "other-context-user", "password": "student-test-password"})
    response = client.post(f"/api/apps/{first_app}/chat", json={
        "message": "继续", "conversation_id": first["conversation_id"]})
    assert response.status_code == 404


@pytest.mark.parametrize("change", ["app_version", "withdrawal"])
def test_prepare_and_prompt_drop_invalid_historical_answers(client, platform, change):
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    first = post(client, f"/apps/{app_id}/chat", {"message": "解释导数知识"})
    user_id = cloud_profile(platform)
    with Session(engine()) as db:
        if change == "app_version":
            db.get(Application, app_id).version += 1
        else:
            db.get(Knowledge, bundle["node"]["id"]).withdrawn = True
        db.commit()
        data = ChatInput(message="继续解释", conversation_id=first["conversation_id"])
        app, _, _, evidence, _, result = learning.prepare_chat(db, app_id, user_id, data)
        _, prompt = learning.generation_prompt(app, data, evidence)
    dialogue = json.loads(prompt)["conversation_context"]
    assert first["answer"] not in json.dumps(dialogue, ensure_ascii=False)
    assert all(not turn["answer"] for turn in dialogue["recent_turns"])
    assert result["retrieval"]["conversation_context"]["excluded_assistant_turns"] == 1
    assert not result["retrieval"]["conversation_context"]["followup_applied"]
