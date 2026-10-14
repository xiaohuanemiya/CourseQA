"""Exercise real provider admission wrappers using local wire-protocol transports."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import AdmissionLease, Application, ChatRun, ModelProfile, User
from app.schemas import AgentOptions, ChatInput
from app.services import admission, chat_runs, learning, providers, streaming
from app.services.retrieval import Evidence
from . import test_agent_evaluation

platform = test_agent_evaluation.platform


def setup(platform, monkeypatch, *, enabled=True):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_concurrency", 1)
    monkeypatch.setattr(cfg, "admission_poll_seconds", .01)
    with Session(engine()) as db:
        app_id = platform["bundles"][0]["app"]["id"]
        app = db.get(Application, app_id)
        app.published = {**app.published, "agent": AgentOptions(enabled=enabled, skills=[],
                            max_subagents=2, max_parallel_subagents=2).model_dump()}
        profile = db.get(ModelProfile, platform["generation"]["id"])
        profile.provider, profile.base_url = "openai", "http://model.invalid"
        user_id = db.scalar(select(User.id))
        db.commit()
        return app_id, user_id, profile.id


def evidence(db, app, releases, query, use_graph=True, **kwargs):
    release, snapshot = releases[0]
    return Evidence([{**snapshot["nodes"][0], "release_id": release.id, "path": [], "score": 1}], {})


def leases(scope=None):
    with Session(engine()) as db:
        query = select(AdmissionLease).where(AdmissionLease.owner_token.is_not(None))
        if scope:
            query = query.where(AdmissionLease.scope == scope)
        return db.scalars(query).all()


def answer():
    return json.dumps({"status": "answered", "answer": "本例设 x 为实数 [1]。", "citation_ids": ["1"]})


def frame(delta=None, finish=None):
    return ("data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]}) + "\n\n").encode()


class WireBody(httpx.AsyncByteStream):
    def __init__(self, frames, counter, gate=None):
        self.frames, self.counter, self.gate, self.closed = frames, counter, gate, False
        counter["active"] += 1
        counter["peak"] = max(counter["peak"], counter["active"])

    async def __aiter__(self):
        if self.gate is not None:
            await self.gate.wait()
        for value in self.frames:
            await asyncio.sleep(.005)
            yield value

    async def aclose(self):
        if not self.closed:
            self.counter["active"] -= 1
            self.closed = True


def wire(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


def test_two_chats_and_parallel_children_share_actual_http_limit_without_parent_deadlock(platform, monkeypatch):
    app_id, user_id, _ = setup(platform, monkeypatch)
    monkeypatch.setattr(learning, "retrieve", evidence)
    counter, requests = {"active": 0, "peak": 0}, []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        initial = json.loads(payload["messages"][1]["content"])
        if initial["task"] != initial["original_question"]:
            content = json.dumps({"report": "条件核查完成 [1]。"})
            frames = [frame({"content": content}), frame(finish="stop")]
        elif not any(message["role"] == "tool" for message in payload["messages"]):
            calls = [{"index": index, "id": f"child-{index}", "type": "function",
                      "function": {"name": "spawn_agent", "arguments": json.dumps({
                          "name": f"核查 {index}", "task": f"检查独立条件 {index}"})}}
                     for index in range(2)]
            frames = [frame({"tool_calls": calls}), frame(finish="tool_calls")]
        else:
            frames = [frame({"content": answer()}), frame(finish="stop")]
        return httpx.Response(200, stream=WireBody([*frames, b"data: [DONE]\n\n"], counter))

    wire(monkeypatch, handler)

    async def check():
        async def chat(index):
            return [event async for event in streaming.chat_events(app_id, user_id,
                ChatInput(message=f"核查导数 {index}", request_id=str(uuid.uuid4())))]

        return await asyncio.wait_for(asyncio.gather(chat(1), chat(2)), 6)

    outputs = asyncio.run(check())
    assert counter == {"active": 0, "peak": 1} and len(requests) == 8
    assert any(kind == "status" and value.get("state") == "queued" for output in outputs for kind, value in output)
    for output in outputs:
        final = next(value for kind, value in output if kind == "done")
        assert final["agent"]["subagents"] == 2 and final["agent"]["model_calls"] == 4
        assert not [value for kind, value in output if kind == "error"]
    assert not leases() and providers.model_limits.get() is False


def test_cancel_queued_chat_emits_sse_status_without_another_http_and_frees_user_slot(platform, monkeypatch):
    app_id, user_id, _ = setup(platform, monkeypatch)
    monkeypatch.setattr(learning, "retrieve", evidence)
    counter, requests = {"active": 0, "peak": 0}, []

    async def check():
        started, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def handler(request):
            requests.append(json.loads(request.content))
            started.set()
            return httpx.Response(200, stream=WireBody([
                frame({"content": answer()}), frame(finish="stop"), b"data: [DONE]\n\n"], counter, release))

        wire(monkeypatch, handler)

        async def chat(request_id, second=False):
            output = []
            source = streaming.chat_events(app_id, user_id, ChatInput(message="解释导数", request_id=request_id))
            async for event in streaming.event_stream(source):
                output.append(event)
                if second and event.startswith("event: status\n") and '"state": "queued"' in event:
                    waiting.set()
            return output

        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        first = asyncio.create_task(chat(first_id))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(chat(second_id, True))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert len(requests) == 1
            with Session(engine()) as db:
                assert chat_runs.cancel_run(db, app_id, user_id, second_id)["state"] == "cancelled"
            second_output = await asyncio.wait_for(second, 3)
            assert any('"error_code": "cancelled"' in event for event in second_output)
            assert len(requests) == 1 and len(leases(f"user:{user_id}")) == 1
        finally:
            release.set()
        first_output = await asyncio.wait_for(first, 3)
        assert any(event.startswith("event: done\n") for event in first_output)

    asyncio.run(check())
    assert not leases() and counter == {"active": 0, "peak": 1}


def test_chat_context_activates_real_sync_embedding_and_generation_wrappers_across_threads(platform, monkeypatch):
    app_id, user_id, profile_id = setup(platform, monkeypatch, enabled=False)
    with Session(engine()) as db:
        app = db.get(Application, app_id)
        embedding_id = app.published["embedding_profile_id"]
        embedding = db.get(ModelProfile, embedding_id)
        embedding.provider, embedding.base_url, embedding.dimension = "openai", "http://model.invalid", 2
        db.commit()
    requests = []

    def retrieve(db, app, releases, question, use_graph=True, **kwargs):
        vector = providers.embed([question], db.get(ModelProfile, embedding_id))
        assert vector == [[1.0, 0.0]]
        return evidence(db, app, releases, question)

    def handler(request):
        requests.append(request.url.path)
        assert providers.model_limits.get() is True
        key = embedding_id if request.url.path == "/embeddings" else profile_id
        assert len(leases(f"model:{key}")) == 1
        if request.url.path == "/embeddings":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": answer()}, "finish_reason": "stop"}]})

    original = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(learning, "retrieve", retrieve)

    async def check():
        return await streaming.chat_result(app_id, user_id,
            ChatInput(message="解释导数", request_id=str(uuid.uuid4())))

    result = asyncio.run(check())
    assert result["status"] == "answered" and requests == ["/embeddings", "/chat/completions"]
    assert not leases() and providers.model_limits.get() is False
    with Session(engine()) as db:
        assert db.scalar(select(ChatRun)).state == "succeeded"


@pytest.mark.parametrize("outcome", ["success", "failure", "close"])
def test_provider_stream_holds_slot_until_http_close_and_releases_after_all_outcomes(platform, monkeypatch, outcome):
    _, _, profile_id = setup(platform, monkeypatch)
    profile = SimpleNamespace(id=profile_id, provider="openai", base_url="http://model.invalid",
                              model="test", secret="", temperature=.1)
    counter, bodies = {"active": 0, "peak": 0}, []

    def handler(request):
        assert len(leases(f"model:{profile_id}")) == 1
        body = WireBody([frame({"content": answer()}), frame(finish="stop"), b"data: [DONE]\n\n"], counter)
        bodies.append(body)
        return httpx.Response(500 if outcome == "failure" else 200, stream=body)

    wire(monkeypatch, handler)

    async def check():
        token = providers.model_limits.set(True)
        source = providers.stream_tool_turn(profile, [{"role": "user", "content": "q"}], [])
        try:
            if outcome == "failure":
                with pytest.raises(providers.ProviderError):
                    await anext(source)
            elif outcome == "close":
                assert (await anext(source))[0] == "delta"
                assert len(leases(f"model:{profile_id}")) == 1 and not bodies[0].closed
            else:
                assert [item async for item in source][-1][0] == "done"
        finally:
            await source.aclose()
            providers.model_limits.reset(token)

    asyncio.run(check())
    assert bodies[0].closed and counter["active"] == 0 and not leases()


def test_duplicate_observer_disconnect_never_cancels_original_execution(platform, monkeypatch):
    app_id, user_id, _ = setup(platform, monkeypatch)
    monkeypatch.setattr(learning, "retrieve", evidence)
    counter, requests = {"active": 0, "peak": 0}, []

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        data = ChatInput(message="解释导数", request_id=str(uuid.uuid4()))

        def handler(request):
            requests.append(request)
            entered.set()
            return httpx.Response(200, stream=WireBody([
                frame({"content": answer()}), frame(finish="stop"), b"data: [DONE]\n\n"], counter, release))

        wire(monkeypatch, handler)

        async def consume():
            return [item async for item in streaming.chat_events(app_id, user_id, data)]

        original = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        observer = streaming.chat_events(app_id, user_id, data)
        try:
            kind, value = await anext(observer)
            assert kind == "status" and value["state"] == "running"
            await observer.aclose()
            with Session(engine()) as db:
                assert chat_runs.get_run(db, app_id, user_id, data.request_id)["state"] == "running"
            assert not original.done() and len(requests) == 1 and counter["active"] == 1
        finally:
            release.set()
        output = await asyncio.wait_for(original, 3)
        assert any(kind == "done" for kind, _ in output)
        with Session(engine()) as db:
            assert chat_runs.get_run(db, app_id, user_id, data.request_id)["state"] == "succeeded"

    asyncio.run(check())
    assert not leases() and counter["active"] == 0


def test_cancel_sync_model_queue_does_not_send_http_when_slot_later_becomes_free(platform, monkeypatch):
    app_id, user_id, profile_id = setup(platform, monkeypatch, enabled=False)
    monkeypatch.setattr(learning, "retrieve", evidence)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": answer()}, "finish_reason": "stop"}]})

    original_client = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))

    async def check():
        waiting = asyncio.Event()
        data = ChatInput(message="解释导数", request_id=str(uuid.uuid4()))

        async def consume():
            result = []
            async for event in streaming.chat_events(app_id, user_id, data, stream=False):
                result.append(event)
                if event[0] == "status" and event[1].get("state") == "queued":
                    waiting.set()
            return result

        async with admission.model_slot(profile_id):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(waiting.wait(), 2)
            with Session(engine()) as db:
                chat_runs.cancel_run(db, app_id, user_id, data.request_id)
            # Allow the durable cancellation heartbeat to cancel the async holder.
            await asyncio.sleep(1.2)
            assert not requests
        output = await asyncio.wait_for(task, 3)
        assert any(kind == "error" and value.get("error_code") == "cancelled" for kind, value in output)
        assert not requests

    asyncio.run(check())
    assert not leases()
